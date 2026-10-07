"""oddish.mon's loops, hook and seams: what happens beside the driver."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from harbor.trial.hooks import TrialEvent

from oddish.mon import env, hooks, loops, seams
from oddish.mon.__main__ import main
from oddish.mon.client import AttemptGone, ControlPlaneUnavailable, MonitorRejected
from oddish.mon.contract import AttemptSpec
from oddish.workers.harbor import live_tail, quota_control

SPEC = AttemptSpec(
    trial_id="trial-1",
    worker_job_id="job-1",
    worker_job_attempt=2,
    trial_attempt=1,
    trial_kind="agent",
    task_id="task-1",
    task_name="hello",
    agent="oracle",
    model=None,
    harbor_config=None,
    org_id="org-1",
    billed_user_id=None,
)


class _Client:
    def __init__(self, **answers):
        self.answers = answers
        self.calls: list[tuple[str, object]] = []

    async def _answer(self, name, payload=None):
        self.calls.append((name, payload))
        answer = self.answers.get(name)
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def heartbeat(self, **kwargs):
        return await self._answer("heartbeat", kwargs)

    async def control(self):
        return await self._answer("control")

    async def lifecycle(self, summary):
        return await self._answer("lifecycle", summary)

    async def events(self, **kwargs):
        return await self._answer("events", kwargs)


@pytest.mark.asyncio
async def test_heartbeat_keeps_going_through_an_outage_and_reports_the_gap():
    client = _Client(
        heartbeat=[ControlPlaneUnavailable("HTTP 503"), None, AttemptGone("retried")]
    )
    with pytest.raises(loops.StopTrial):
        await loops.heartbeat_loop(client, interval=0)
    sent = [payload for _, payload in client.calls]
    assert sent[0]["pending_failure_count"] == 0
    # The beat after the outage carries the missed count, as the worker's does.
    assert sent[1] == {"pending_failure_count": 1, "pending_last_error": "HTTP 503"}
    assert sent[2]["pending_failure_count"] == 0


@pytest.mark.asyncio
async def test_control_loop_stops_on_revoked_approval_and_tracks_quota_pause():
    state = seams.ControlState()
    client = _Client(
        control=[
            {"authorized": True, "pause_requested": True},
            ControlPlaneUnavailable("timeout"),
            {"authorized": False, "detail": "not approved"},
        ]
    )
    with pytest.raises(loops.StopTrial, match="not approved"):
        await loops.control_loop(client, state, interval=0, trial_id="trial-1")
    assert state.pause_requested is True


@pytest.mark.asyncio
async def test_a_revoked_approval_is_recorded_before_the_stop_propagates():
    """Seen in review: the stop was recorded only after the live tail's final
    flush, so a kill in that window read as a failed start."""
    from oddish.mon import status

    client = _Client(control={"authorized": False, "detail": "not approved"})
    try:
        await loops.control_loop(
            client, seams.ControlState(), interval=0, trial_id="trial-1"
        )
    except loops.StopTrial:
        recorded = status.read_status(status.STATUS_PATH)
    assert (recorded["trial_id"], recorded["phase"]) == ("trial-1", "stopped")


@pytest.mark.asyncio
async def test_hook_reports_each_event_and_survives_a_control_plane_blip(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(hooks, "write_status", lambda **kwargs: None)
    started, stopped = [], []
    monkeypatch.setattr(live_tail, "start", lambda **kwargs: started.append(kwargs))
    monkeypatch.setattr(live_tail, "request_stop", stopped.append)
    client = _Client(lifecycle=[ControlPlaneUnavailable("blip"), None, None])

    async def persist(**kwargs):
        raise AssertionError("not reached: live_tail.start is stubbed")

    on_event = hooks.make_hook_callback(client, SPEC, persist=persist)

    environment = object()
    await on_event(
        SimpleNamespace(
            event=TrialEvent.AGENT_START, environment=environment, result=None
        )
    )
    await on_event(SimpleNamespace(event=TrialEvent.END, environment=None, result=None))

    # The event lost to the blip is sent again, before the newer one.
    assert [p["event"] for _, p in client.calls] == [
        "agent-start",
        "agent-start",
        "end",
    ]
    assert started[0]["environment"] is environment
    assert started[0]["attempt"] == SPEC.trial_attempt
    # The tail persists through the callback the runner built, not a global.
    assert started[0]["persist"] is persist
    assert stopped == ["trial-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [AttemptGone("gone"), MonitorRejected("refused")])
async def test_hook_lets_a_dead_attempt_stop_the_trial(monkeypatch, verdict):
    monkeypatch.setattr(hooks, "write_status", lambda **kwargs: None)
    reporter = hooks.LifecycleReporter(_Client(lifecycle=verdict))
    on_event = hooks.make_hook_callback(
        reporter._client, SPEC, reporter, persist=_no_persist
    )
    with pytest.raises(type(verdict)):
        await on_event(
            SimpleNamespace(event=TrialEvent.START, environment=None, result=None)
        )
    # The runner watches this beside the loops: the driver is stopped even if
    # Harbor swallows the hook's exception.
    with pytest.raises(loops.StopTrial):
        await asyncio.wait_for(reporter.stopped(), timeout=1)


async def _no_persist(**kwargs):
    raise AssertionError("no tick expected")


@pytest.mark.asyncio
async def test_seams_send_the_tails_tick_to_the_events_route():
    client = _Client(
        events={
            "flushed": 2,
            "checkpoint_written": True,
            "trial_finished": False,
            "quota_checked": True,
            "pause_requested": None,
        }
    )
    state = seams.ControlState()
    state.pause_requested = True
    driver = seams.build(client, state)
    result = await driver.persist(
        trial_id="trial-1",
        attempt=1,
        events=[{"seq": 1, "kind": "text", "payload": {}}],
        checkpoint_values={"cost_usd": 0.1},
        org_id="org-1",
        billed_user_id=None,
    )
    assert result == live_tail.PersistResult(flushed=2, checkpoint_written=True)
    (_, sent) = client.calls[0]
    assert sent["trial_attempt"] == 1 and sent["checkpoint"] == {"cost_usd": 0.1}
    assert isinstance(driver.quota, quota_control.QuotaSeams)
    assert await driver.quota.refresh_requested("org-1", None) is True
    # Nothing was installed anywhere: a second build is independent.
    assert seams.build(client, seams.ControlState()).persist is not driver.persist


@pytest.mark.asyncio
async def test_a_large_tick_is_posted_in_batches_the_events_route_accepts():
    """Seen in review: POST /events takes at most 1000 events, and a tick with
    more was refused on every later try."""
    from oddish.mon.contract import MAX_EVENTS_PER_POST

    def reply(flushed, **extra):
        return {
            "flushed": flushed,
            "checkpoint_written": False,
            "trial_finished": False,
            "quota_checked": True,
            "pause_requested": None,
            **extra,
        }

    events = [{"seq": n, "kind": "text", "payload": {}} for n in range(1, 2501)]
    tick = {
        "trial_id": "trial-1",
        "attempt": 1,
        "events": events,
        "checkpoint_values": {"cost_usd": 0.1},
        "org_id": "org-1",
        "billed_user_id": None,
    }
    client = _Client(
        events=[reply(1000), reply(1000), reply(500, checkpoint_written=True)]
    )
    persist = seams.build(client, seams.ControlState()).persist
    result = await persist(**tick)
    assert result == live_tail.PersistResult(flushed=2500, checkpoint_written=True)
    sent = [payload for _, payload in client.calls]
    assert [len(p["events"]) for p in sent] == [1000, 1000, 500]
    assert [p["events"][0]["seq"] for p in sent] == [1, 1001, 2001]
    assert [p["checkpoint"] for p in sent] == [None, None, {"cost_usd": 0.1}]
    assert MAX_EVENTS_PER_POST == 1000

    # The second batch fails: the first counts, the rest stays pending.
    client = _Client(events=[reply(1000), ControlPlaneUnavailable("HTTP 503")])
    persist = seams.build(client, seams.ControlState()).persist
    result = await persist(**tick)
    assert result == live_tail.PersistResult(flushed=1000)
    assert len(client.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [AttemptGone("gone"), MonitorRejected("refused")])
async def test_a_verdict_on_the_persist_tick_ends_the_attempt(verdict):
    """Seen in review: the client wrote "stopped" on a 410/401 from POST
    /events, but the tailer swallowed the exception and the driver kept
    running. The tick now records the verdict for the runner's watcher."""
    client = _Client(events=verdict)
    state = seams.ControlState()
    persist = seams.build(client, state).persist
    tick = {"trial_id": "trial-1", "attempt": 1, "events": [], "checkpoint_values": {"cost_usd": 0.1}}  # fmt: skip

    with pytest.raises(type(verdict)):
        await persist(**tick)
    assert state.ended.is_set() and state.verdict is verdict
    with pytest.raises(loops.StopTrial, match=str(verdict)):
        await asyncio.wait_for(loops.state_stopped(state), timeout=1)


@pytest.mark.asyncio
async def test_a_refused_events_batch_is_dropped_not_retried(caplog):
    """Seen in review: a non-auth 4xx on POST /events was left pending and
    posted again every tick, blocking the live tail for good. It is dropped
    with an ERROR line, as a refused lifecycle event is."""
    import logging

    from oddish.mon.client import ControlPlaneRefused

    events = [{"seq": n, "kind": "text", "payload": {}} for n in range(1, 4)]
    tick = {"trial_id": "trial-1", "attempt": 1, "events": events, "checkpoint_values": None}  # fmt: skip
    client = _Client(events=ControlPlaneRefused("POST /events: HTTP 422"))
    state = seams.ControlState()
    persist = seams.build(client, state).persist

    with caplog.at_level(logging.ERROR, logger=seams.__name__):
        result = await persist(**tick)

    assert result == live_tail.PersistResult(flushed=3)  # the tailer drops them
    assert not state.ended.is_set()  # a refusal is not a verdict
    (record,) = [r for r in caplog.records if "dropped" in r.message]
    assert record.levelno == logging.ERROR and "HTTP 422" in record.message

    # A later tick posts again: the tail is not blocked.
    client.answers["events"] = {"flushed": 1, "checkpoint_written": False, "trial_finished": False, "quota_checked": True, "pause_requested": None}  # fmt: skip
    assert (await persist(**{**tick, "events": events[:1]})).flushed == 1
    assert len(client.calls) == 2


def test_status_command_exit_code_says_whether_the_monitor_is_alive(
    monkeypatch, capsys
):
    from oddish.mon import __main__ as cli

    monkeypatch.setattr(cli, "read_status", lambda: {"alive": True, "phase": "x"})
    assert main(["status"]) == 0
    monkeypatch.setattr(cli, "read_status", lambda: {"alive": False})
    assert main(["status"]) == 1
    assert '"alive": false' in capsys.readouterr().out
    assert main(["nonsense"]) == 2


def test_run_unpacks_the_model_credentials_before_settings_is_built(tmp_path):
    # Settings reads os.environ once, when the runner's imports first load
    # oddish.config. A fresh process, since this one has built it already.
    probe = (
        "import asyncio\n"
        "from oddish.mon.__main__ import main\n"
        "asyncio.run = lambda coro: coro.close()  # stop before the driver\n"
        "main(['run'])\n"
        "from oddish.config import settings\n"
        "print('key=' + str(settings.anthropic_api_key))\n"
    )
    environ = {
        k: v
        for k, v in os.environ.items()
        if k != "ANTHROPIC_API_KEY" and not k.startswith("ODDISH_MON_")
    }
    environ.update(
        {
            env.API_URL_VAR: "https://api.example",
            env.TOKEN_VAR: "job-1.2.secret",
            env.AGENT_ENV_VAR: json.dumps({"ANTHROPIC_API_KEY": "sk-synthetic"}),
            env.DRIVER_ENV_VAR: json.dumps({"ANTHROPIC_API_KEY": "sk-synthetic"}),
            "ODDISH_MON_LOG_PATH": str(tmp_path / "oddish-mon.log"),
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        env=environ,
        cwd=tmp_path,  # no .env for Settings to read
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    keys = [line for line in result.stdout.splitlines() if line.startswith("key=")]
    assert keys == ["key=sk-synthetic"]


@pytest.mark.asyncio
async def test_loops_count_what_they_delivered():
    """The counts behind the monitor's summary log line and its posted result."""
    from oddish.mon import loops
    from oddish.mon.client import ControlPlaneUnavailable
    from oddish.mon.seams import ControlState

    class _Client:
        def __init__(self):
            self.beats = 0

        async def heartbeat(self, **_kwargs):
            self.beats += 1
            if self.beats == 2:
                raise ControlPlaneUnavailable("pooler down")

        async def control(self):
            return {"authorized": True}

    stats, client = loops.LoopStats(), _Client()
    beat = asyncio.create_task(loops.heartbeat_loop(client, interval=0.01, stats=stats))
    ctrl = asyncio.create_task(
        loops.control_loop(client, ControlState(), interval=0.01, stats=stats)
    )
    await asyncio.sleep(0.08)
    for task in (beat, ctrl):
        task.cancel()
    await asyncio.gather(beat, ctrl, return_exceptions=True)

    assert stats.heartbeats_failed == 1
    assert stats.heartbeats_sent >= 2 and stats.control_checks >= 2
    assert stats.summary().startswith("heartbeats_sent=")
    assert set(stats.as_dict()) == {
        "heartbeats_sent",
        "heartbeats_failed",
        "control_checks",
        "control_failed",
    }


@pytest.mark.asyncio
async def test_stage_events_lost_to_an_outage_are_resent_in_order():
    """Seen in the preview: an API outage left a finished trial showing an old stage."""
    from oddish.mon.client import ControlPlaneUnavailable

    class _Client:
        def __init__(self):
            self.down, self.delivered = True, []

        async def lifecycle(self, summary):
            if self.down:
                raise ControlPlaneUnavailable("api down")
            self.delivered.append(summary["event"])

    client = _Client()
    reporter = hooks.LifecycleReporter(client)
    for name in ("agent-end", "verification-start"):
        with pytest.raises(ControlPlaneUnavailable):
            await reporter.report({"event": name})
    assert reporter.pending == 2 and client.delivered == []

    client.down = False
    await reporter.report({"event": "end"})  # the next event flushes the backlog first
    assert client.delivered == ["agent-end", "verification-start", "end"]
    assert reporter.pending == 0


@pytest.mark.asyncio
async def test_the_loops_outlive_anything_but_a_verdict_from_our_api():
    from oddish.mon.client import MonitorRejected
    from oddish.mon.seams import ControlState

    class _Client:
        def __init__(self, errors):
            self.errors, self.calls = list(errors), 0

        async def heartbeat(self, **_kwargs):
            return await self._next()

        async def control(self):
            await self._next()
            return {"authorized": True}

        async def _next(self):
            self.calls += 1
            if self.errors:
                raise self.errors.pop(0)

    # An outage, a surprise and a bug in between do not stop the trial.
    stats, client = (
        loops.LoopStats(),
        _Client([ControlPlaneUnavailable("404"), RuntimeError("surprise")]),
    )
    beat = asyncio.create_task(loops.heartbeat_loop(client, interval=0.01, stats=stats))
    await asyncio.sleep(0.06)
    assert (
        not beat.done() and stats.heartbeats_failed == 2 and stats.heartbeats_sent >= 1
    )
    beat.cancel()
    await asyncio.gather(beat, return_exceptions=True)

    # A verdict from our API does.
    for verdict in (AttemptGone("newer attempt"), MonitorRejected("bad token")):
        with pytest.raises(loops.StopTrial):
            await loops.heartbeat_loop(_Client([verdict]), interval=0.01)
        with pytest.raises(loops.StopTrial):
            await loops.control_loop(_Client([verdict]), ControlState(), interval=0.01)


# --- the monitor's log travels with the trial's files -------------------------


def _log_env(monkeypatch, tmp_path):
    log = tmp_path / "oddish-mon.log"
    log.write_text("checked in\n")
    monkeypatch.setenv("ODDISH_MON_LOG_PATH", str(log))


def test_the_monitor_log_is_placed_with_the_trials_files(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from oddish.mon import runner

    _log_env(monkeypatch, tmp_path)
    job_dir = tmp_path / "job"
    trial_dir = job_dir / "task-x__AbC"
    trial_dir.mkdir(parents=True)
    (trial_dir / "result.json").write_text("{}")

    runner._attach_monitor_log(SimpleNamespace(job_dir=job_dir))

    # The files API serves the trial folder: that is where the log must be.
    assert (trial_dir / "oddish-mon.log").read_text() == "checked in\n"
    assert not (job_dir / "oddish-mon.log").exists()


def test_without_a_trial_folder_the_log_stays_at_the_job_root(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from oddish.mon import runner

    _log_env(monkeypatch, tmp_path)
    job_dir = tmp_path / "job"
    job_dir.mkdir()

    runner._attach_monitor_log(SimpleNamespace(job_dir=job_dir))

    assert (job_dir / "oddish-mon.log").is_file()


def test_a_log_copy_that_fails_is_a_warning_not_a_failed_start(
    monkeypatch, tmp_path, caplog
):
    """Seen in review: with no job folder the fallback copy had no error
    handling, so a full disk after the driver finished raised before
    POST /result and turned a finished trial into a failed start."""
    import logging
    import shutil
    from types import SimpleNamespace

    from oddish.mon import runner

    _log_env(monkeypatch, tmp_path)

    def _no_space(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shutil, "copyfile", _no_space)
    with caplog.at_level(logging.WARNING, logger=runner.__name__):
        assert runner._attach_monitor_log(None) is None  # no job folder
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        assert runner._attach_monitor_log(SimpleNamespace(job_dir=job_dir)) is None
    warnings = [
        r for r in caplog.records if "could not attach the monitor log" in r.message
    ]
    assert len(warnings) == 2 and all(r.levelno == logging.WARNING for r in warnings)
    assert "No space left" in warnings[0].message


@pytest.mark.asyncio
async def test_the_tail_is_flushed_and_stopped_before_the_result_is_reported(
    monkeypatch,
):
    from oddish.mon import runner
    from oddish.workers.harbor import live_tail

    stopped = []

    async def _shutdown(trial_id, timeout_sec=15.0):
        stopped.append(trial_id)
        return 1

    monkeypatch.setattr(live_tail, "shutdown", _shutdown)
    await runner._finish_live_tail("trial-1")
    assert stopped == ["trial-1"]

    async def _boom(trial_id, timeout_sec=15.0):
        raise RuntimeError("tick lost")

    monkeypatch.setattr(live_tail, "shutdown", _boom)
    await runner._finish_live_tail("trial-1")  # logged, never raised


@pytest.mark.asyncio
async def test_hook_writes_setup_progress_and_does_not_report_it_as_a_stage(
    monkeypatch,
):
    from datetime import datetime, timezone

    from harbor.models.environment_subphase import EnvironmentSubphase, SubphaseStatus

    monkeypatch.setattr(live_tail, "_allocated_seqs", {})
    monkeypatch.setattr(
        hooks,
        "write_status",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("not a stage")),
    )
    client = _Client()
    persisted = []

    async def persist(**kwargs):
        persisted.append(kwargs)
        return live_tail.PersistResult(flushed=len(kwargs["events"]))

    on_event = hooks.make_hook_callback(client, SPEC, persist=persist)
    await on_event(
        SimpleNamespace(
            event=TrialEvent.ENVIRONMENT_SUBPHASE,
            subphase=EnvironmentSubphase(
                phase="kubernetes.cluster_up",
                status=SubphaseStatus.STARTED,
                provider="ec2",
                started_at=datetime(2026, 10, 5, tzinfo=timezone.utc),
            ),
        )
    )

    assert client.calls == []
    assert [e["kind"] for call in persisted for e in call["events"]] == [
        "env_subphase"
    ]
    assert persisted[0]["attempt"] == SPEC.trial_attempt
