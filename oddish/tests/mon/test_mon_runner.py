"""``runner.run`` in its three phases: prepare_task, drive_with_watchers and
report_until_accepted, and the exit contract that wraps them (status file plus
EXIT_ACCEPTED / EXIT_STOPPED; anything else raises for ``__main__``)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from oddish.mon import env, hooks, loops, runner
from oddish.mon.client import AttemptGone, MonitorRejected
from oddish.mon.contract import AttemptSpec
from oddish.mon.exit_codes import EXIT_ACCEPTED, EXIT_STOPPED
from oddish.workers.harbor import runner as harbor_runner


def _spec(**overrides) -> AttemptSpec:
    fields = dict(
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
    fields.update(overrides)
    return AttemptSpec(**fields)


MONITOR_ENV = env.MonitorEnv(
    api_url="https://api.test", token="job-1.2.secret", agent_env={}, registry_auth=[]
)


class _Client:
    def __init__(self, *, attempt=None, skills=()):
        self._attempt = attempt
        self._skills = list(skills)
        self.closed = False
        self.results: list[dict] = []

    async def attempt(self):
        if isinstance(self._attempt, Exception):
            raise self._attempt
        return self._attempt

    async def skills(self):
        return self._skills

    async def aclose(self):
        self.closed = True


async def _forever(*args, **kwargs):
    await asyncio.Event().wait()


def _status_log(monkeypatch) -> list[tuple[str, str]]:
    phases: list[tuple[str, str]] = []
    monkeypatch.setattr(
        runner,
        "write_status",
        lambda *, trial_id, phase: phases.append((trial_id, phase)),
    )
    return phases


def _driver_seams() -> runner.seams.DriverSeams:
    """A stand-in for what ``seams.build`` returns: the callbacks the driver
    gets are only passed through here, never called."""

    async def persist(**kwargs):
        return None

    return runner.seams.DriverSeams(persist=persist, quota=SimpleNamespace())


def _task(tmp_path: Path, spec=None) -> runner.PreparedTask:
    return runner.PreparedTask(
        spec=spec or _spec(),
        state=runner.seams.ControlState(),
        driver_seams=_driver_seams(),
        reporter=hooks.LifecycleReporter(_Client()),
        workdir=tmp_path,
        task_path=tmp_path / "task",
        harbor_config={},
        staged_skills_root=None,
    )


# --- prepare_task -----------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_task_fetches_the_task_and_stages_the_selected_skills(
    monkeypatch, tmp_path
):
    phases = _status_log(monkeypatch)
    built: list = []
    staged: list = []
    driver_seams = _driver_seams()
    monkeypatch.setattr(
        runner.tempfile, "mkdtemp", lambda prefix, dir=None: str(tmp_path)
    )

    def build(client, state):
        built.append((client, state))
        return driver_seams

    monkeypatch.setattr(runner.seams, "build", build)

    async def fetch_task(spec, task_dir):
        return Path(task_dir) / "task"

    async def reuse(task_path, harbor_config):
        return {**(harbor_config or {}), "reused": True}

    monkeypatch.setattr(runner.task_fetch, "fetch_task", fetch_task)
    monkeypatch.setattr(runner.registry_login, "login", lambda auth: ["ghcr.io"])
    monkeypatch.setattr(runner.builder, "env_for", lambda task_path: {})
    monkeypatch.setattr(runner.task_image, "reuse", reuse)
    monkeypatch.setattr(
        runner.resources, "with_reservation_semantics", lambda c: {**c, "pinned": True}
    )
    monkeypatch.setattr(
        runner, "materialize_skills", lambda skills, root: staged.append((skills, root))
    )
    client = _Client(skills=["bundle"])
    spec = _spec(harbor_config={"skill_ids": ["s-1"]})

    task = await runner.prepare_task(client, spec, MONITOR_ENV)

    assert phases == [("trial-1", "starting")]
    assert built == [(client, task.state)]
    assert task.driver_seams is driver_seams
    assert task.spec is spec
    assert task.task_path == tmp_path / "task"
    assert task.harbor_config == {"skill_ids": ["s-1"], "reused": True, "pinned": True}
    assert task.staged_skills_root == tmp_path / "agent_skills"
    assert staged == [(["bundle"], tmp_path / "agent_skills")]
    assert isinstance(task.reporter, hooks.LifecycleReporter)


@pytest.mark.asyncio
async def test_prepare_task_stages_no_skills_without_an_org(monkeypatch, tmp_path):
    _status_log(monkeypatch)
    monkeypatch.setattr(
        runner.tempfile, "mkdtemp", lambda prefix, dir=None: str(tmp_path)
    )
    monkeypatch.setattr(runner.seams, "build", lambda c, s: _driver_seams())

    async def fetch_task(spec, task_dir):
        return Path(task_dir) / "task"

    async def reuse(task_path, harbor_config):
        return dict(harbor_config or {})

    monkeypatch.setattr(runner.task_fetch, "fetch_task", fetch_task)
    monkeypatch.setattr(runner.registry_login, "login", lambda auth: [])
    monkeypatch.setattr(runner.builder, "env_for", lambda task_path: {})
    monkeypatch.setattr(runner.task_image, "reuse", reuse)
    monkeypatch.setattr(runner.resources, "with_reservation_semantics", lambda c: c)

    def _no_staging(*a):
        raise AssertionError("no skills should be staged")

    monkeypatch.setattr(runner, "materialize_skills", _no_staging)
    spec = _spec(org_id=None, harbor_config={"skill_ids": ["s-1"]})

    task = await runner.prepare_task(_Client(), spec, MONITOR_ENV)

    assert task.staged_skills_root is None


# --- drive_with_watchers ----------------------------------------------------


def _quiet_watchers(monkeypatch):
    monkeypatch.setattr(runner.loops, "heartbeat_loop", _forever)
    monkeypatch.setattr(runner.loops, "control_loop", _forever)
    flushed: list[str] = []

    async def finish(trial_id):
        flushed.append(trial_id)

    monkeypatch.setattr(runner, "_finish_live_tail", finish)
    return flushed


@pytest.mark.asyncio
async def test_the_drivers_outcome_is_the_result_and_the_tail_is_flushed(
    monkeypatch, tmp_path
):
    flushed = _quiet_watchers(monkeypatch)
    outcome = SimpleNamespace(reward=1.0)
    seen: dict = {}

    async def drive(**kwargs):
        seen.update(kwargs)
        return outcome

    monkeypatch.setattr(harbor_runner, "run_harbor_trial_async", drive)
    task = _task(tmp_path)

    result = await runner.drive_with_watchers(_Client(), task, MONITOR_ENV)

    assert result.outcome is outcome
    assert result.execution_error is None and result.retryable is True
    assert isinstance(result.stats, loops.LoopStats)
    assert flushed == ["trial-1"]
    assert seen["task_path"] == task.task_path
    assert seen["jobs_dir"] == tmp_path / "jobs"
    assert seen["trial_id"] == "trial-1" and seen["worker_job_id"] == "job-1"
    assert seen["extra_agent_env"] is None  # an empty agent env is not passed
    assert seen["quota_seams"] is task.driver_seams.quota


@pytest.mark.asyncio
async def test_a_driver_crash_is_a_result_with_its_retryability(monkeypatch, tmp_path):
    _quiet_watchers(monkeypatch)

    async def drive(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(harbor_runner, "run_harbor_trial_async", drive)
    monkeypatch.setattr(harbor_runner, "is_retryable_execution_error", lambda e: False)

    result = await runner.drive_with_watchers(_Client(), _task(tmp_path), MONITOR_ENV)

    assert result.outcome is None
    assert result.execution_error == "RuntimeError: boom"
    assert result.retryable is False


@pytest.mark.asyncio
async def test_a_watcher_ending_first_cancels_the_driver_and_stops_the_trial(
    monkeypatch, tmp_path
):
    flushed = _quiet_watchers(monkeypatch)
    cancelled: list[bool] = []

    async def drive(**kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def control_loop(client, state, *, stats, trial_id):
        await asyncio.sleep(0.01)
        raise loops.StopTrial("approval revoked")

    monkeypatch.setattr(harbor_runner, "run_harbor_trial_async", drive)
    monkeypatch.setattr(runner.loops, "control_loop", control_loop)

    with pytest.raises(loops.StopTrial, match="approval revoked"):
        await runner.drive_with_watchers(_Client(), _task(tmp_path), MONITOR_ENV)

    assert cancelled == [True]
    assert flushed == ["trial-1"]  # the tail is flushed on this exit too


@pytest.mark.asyncio
async def test_a_control_plane_verdict_from_the_driver_propagates(
    monkeypatch, tmp_path
):
    _quiet_watchers(monkeypatch)

    async def drive(**kwargs):
        raise AttemptGone("410")

    monkeypatch.setattr(harbor_runner, "run_harbor_trial_async", drive)

    with pytest.raises(AttemptGone):
        await runner.drive_with_watchers(_Client(), _task(tmp_path), MONITOR_ENV)


# --- report_until_accepted --------------------------------------------------


@pytest.mark.asyncio
async def test_report_heartbeats_until_accepted_then_records_done(
    monkeypatch, tmp_path
):
    phases = _status_log(monkeypatch)
    beats: list[str] = []

    async def heartbeat_loop(client, *, stats):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            beats.append("cancelled")
            raise

    reported: list[dict] = []

    async def upload_and_report(client, outcome, **kwargs):
        await asyncio.sleep(0)  # let the beat start, as a real upload would
        reported.append({"outcome": outcome, **kwargs})
        return {}

    monkeypatch.setattr(runner.loops, "heartbeat_loop", heartbeat_loop)
    monkeypatch.setattr(runner.artifacts, "upload_and_report", upload_and_report)
    monkeypatch.setattr(runner, "_attach_monitor_log", lambda outcome: None)
    from oddish.mon import memstat

    monkeypatch.setattr(memstat, "snapshot", lambda: "mem[test]")
    outcome = SimpleNamespace(reward=0.5, error=None)
    result = runner.DriveResult(
        outcome=outcome, execution_error=None, retryable=True, stats=loops.LoopStats()
    )

    await runner.report_until_accepted(_Client(), _task(tmp_path), result)

    assert phases == [("trial-1", "reporting"), ("trial-1", "done")]
    assert beats == ["cancelled"]
    assert len(reported) == 1
    assert reported[0]["outcome"] is outcome
    assert reported[0]["retryable"] is True
    assert reported[0]["execution_error"] is None


@pytest.mark.asyncio
async def test_a_refused_result_leaves_the_status_at_reporting(monkeypatch, tmp_path):
    phases = _status_log(monkeypatch)
    monkeypatch.setattr(runner.loops, "heartbeat_loop", _forever)
    monkeypatch.setattr(runner, "_attach_monitor_log", lambda outcome: None)
    from oddish.mon import memstat

    monkeypatch.setattr(memstat, "snapshot", lambda: "mem[test]")

    async def refused(client, outcome, **kwargs):
        raise MonitorRejected("newer attempt owns the row")

    monkeypatch.setattr(runner.artifacts, "upload_and_report", refused)
    result = runner.DriveResult(
        outcome=None, execution_error="x", retryable=True, stats=loops.LoopStats()
    )

    with pytest.raises(MonitorRejected):
        await runner.report_until_accepted(_Client(), _task(tmp_path), result)

    assert phases == [("trial-1", "reporting")]


# --- run: the exit contract -------------------------------------------------


def _wire_run(monkeypatch, client, *, prepare=None, drive=None, report=None):
    monkeypatch.setattr(runner.env, "load", lambda: MONITOR_ENV)
    monkeypatch.setattr(runner, "MonitorClient", lambda monitor_env: client)

    async def _prepare(c, spec, monitor_env):
        if prepare is not None:
            raise prepare
        return SimpleNamespace(spec=spec)

    async def _drive(c, task, monitor_env):
        if drive is not None:
            raise drive
        return "result"

    async def _report(c, task, result):
        if report is not None:
            raise report

    monkeypatch.setattr(runner, "prepare_task", _prepare)
    monkeypatch.setattr(runner, "drive_with_watchers", _drive)
    monkeypatch.setattr(runner, "report_until_accepted", _report)


@pytest.mark.asyncio
async def test_run_returns_accepted_and_closes_the_client(monkeypatch):
    phases = _status_log(monkeypatch)
    client = _Client(attempt=_spec())
    _wire_run(monkeypatch, client)

    assert await runner.run() == EXIT_ACCEPTED
    assert client.closed is True
    assert phases == []  # the phases themselves wrote starting/reporting/done


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "verdict"),
    [
        ("attempt", AttemptGone("410 at check-in")),
        ("prepare", MonitorRejected("401 fetching the task")),
        ("drive", loops.StopTrial("approval revoked")),
        ("report", AttemptGone("410 posting the result")),
    ],
)
async def test_run_maps_every_control_plane_verdict_to_stopped(
    monkeypatch, phase, verdict
):
    phases = _status_log(monkeypatch)
    client = _Client(attempt=verdict if phase == "attempt" else _spec())
    _wire_run(
        monkeypatch,
        client,
        prepare=verdict if phase == "prepare" else None,
        drive=verdict if phase == "drive" else None,
        report=verdict if phase == "report" else None,
    )

    assert await runner.run() == EXIT_STOPPED
    # Before check-in the trial is unknown; after it the status names it.
    assert phases == [("" if phase == "attempt" else "trial-1", "stopped")]
    assert client.closed is True


@pytest.mark.asyncio
async def test_run_lets_anything_else_raise_for_the_entrypoint(monkeypatch):
    phases = _status_log(monkeypatch)
    client = _Client(attempt=_spec())
    _wire_run(monkeypatch, client, drive=OSError("disk full"))

    with pytest.raises(OSError, match="disk full"):
        await runner.run()

    assert phases == []  # no terminal phase: the entrypoint reports a failed start
    assert client.closed is True
