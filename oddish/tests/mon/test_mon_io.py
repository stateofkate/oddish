"""oddish.mon's edges: secrets, the API client, task fetch, artifacts, status."""

from __future__ import annotations

import functools
import io
import json
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from oddish.mon import artifacts, env, status, task_fetch
from oddish.mon.client import (
    AttemptGone,
    ControlPlaneRefused,
    ControlPlaneUnavailable,
    MonitorClient,
    MonitorRejected,
)
from oddish.mon.contract import AttemptSpec

SPEC = {
    "trial_id": "trial-1",
    "worker_job_id": "job-1",
    "worker_job_attempt": 2,
    "trial_attempt": 1,
    "trial_kind": "agent",
    "task_id": "task-1",
    "task_name": "hello",
    "agent": "oracle",
    "model": None,
    "harbor_config": None,
    "org_id": "org-1",
    "billed_user_id": None,
}


def _env(**extra):
    return {
        env.API_URL_VAR: "https://api.example",
        env.TOKEN_VAR: "job-1.2.secret-token",
        **extra,
    }


def test_secrets_are_read_once_and_removed_from_the_environment():
    environ = _env(**{env.AGENT_ENV_VAR: json.dumps({"ANTHROPIC_API_KEY": "sk-x"})})
    loaded = env.load(environ)
    assert loaded.token == "job-1.2.secret-token"
    assert loaded.agent_env == {"ANTHROPIC_API_KEY": "sk-x"}
    assert env.TOKEN_VAR not in environ and env.AGENT_ENV_VAR not in environ
    assert "secret-token" not in repr(loaded) and "sk-x" not in repr(loaded)


@pytest.mark.parametrize(
    "url",
    ["http://api.example", "https://user:pw@api.example", "https://a.example?x=1"],
)
def test_api_url_must_be_bare_https(url):
    with pytest.raises(env.MonitorEnvError):
        env.load(_env(**{env.API_URL_VAR: url}))


def test_plain_http_is_allowed_only_for_a_local_control_plane():
    assert env.load(_env(**{env.API_URL_VAR: "http://localhost:8000"})).api_url


def _client(handler):
    return MonitorClient(env.load(_env()), transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_client_sends_the_token_only_in_the_header():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=SPEC)

    client = _client(handler)
    spec = await client.attempt()
    assert spec == AttemptSpec.model_validate(SPEC)
    (request,) = seen
    assert request.headers["authorization"] == "Bearer job-1.2.secret-token"
    assert "secret-token" not in str(request.url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "error"),
    [(410, AttemptGone), (401, MonitorRejected), (503, ControlPlaneUnavailable)],
)
async def test_client_maps_status_codes(status_code, error):
    # As our API answers: FastAPI errors are JSON with a ``detail``.
    client = _client(lambda request: httpx.Response(status_code, json={"detail": "nope"}))
    with pytest.raises(error):
        await client.heartbeat()
    # Only the attempt's end is recorded; an outage stays unknown.
    recorded = status.read_status(status.STATUS_PATH).get("phase")
    assert recorded == ("stopped" if status_code in (401, 410) else None)


@pytest.mark.asyncio
async def test_transport_errors_are_retryable_and_never_leak_the_token():
    def handler(request):
        raise httpx.ConnectError("boom job-1.2.secret-token")

    with pytest.raises(ControlPlaneUnavailable) as caught:
        await _client(handler).control()
    assert "secret-token" not in str(caught.value)


def _tar(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_task_is_fetched_from_an_archive_or_from_loose_files(tmp_path):
    archive = _tar({"task.toml": b"v=1", "environment/Dockerfile": b"FROM x"})
    bodies = {
        "https://s3.test/archive": archive,
        "https://s3.test/a": b"v=1",
        "https://s3.test/b": b"FROM x",
    }
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=bodies[str(request.url)])
    )
    from_archive = await task_fetch.fetch_task(
        AttemptSpec(**SPEC, task_archive_url="https://s3.test/archive"),
        tmp_path / "one",
        transport=transport,
    )
    from_files = await task_fetch.fetch_task(
        AttemptSpec(
            **SPEC,
            task_files={
                "task.toml": "https://s3.test/a",
                "environment/Dockerfile": "https://s3.test/b",
            },
        ),
        tmp_path / "two",
        transport=transport,
    )
    for root in (from_archive, from_files):
        assert (root / "task.toml").read_bytes() == b"v=1"
        assert (root / "environment/Dockerfile").read_bytes() == b"FROM x"


@pytest.mark.asyncio
async def test_task_fetch_refuses_nothing_to_fetch_and_path_escapes(tmp_path):
    with pytest.raises(task_fetch.TaskFetchError):
        await task_fetch.fetch_task(AttemptSpec(**SPEC), tmp_path / "none")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x"))
    with pytest.raises(task_fetch.TaskFetchError):
        await task_fetch.fetch_task(
            AttemptSpec(**SPEC, task_files={"../escape": "https://s3.test/a"}),
            tmp_path / "bad",
            transport=transport,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("skill_ids", [["skill-1"], None])
async def test_selected_skills_come_from_the_control_plane(
    monkeypatch, tmp_path, skill_ids
):
    """Seen in review: the driver read the selected skills from Postgres, which
    the sandbox cannot reach, and ran without them."""
    import tempfile

    from oddish.mon import builder, runner, task_image
    from oddish.workers.harbor import runner as driver

    spec = {**SPEC, "harbor_config": {"skill_ids": skill_ids} if skill_ids else None}
    skill_md = "---\nname: alpha\ndescription: d\n---\nbody"
    asked = []

    def api(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path.rsplit("/", 1)[-1])
        if asked[-1] == "attempt":
            return httpx.Response(200, json=spec)
        files = [
            {"relative_path": "SKILL.md", "content": skill_md},
            {"relative_path": "scripts/run.sh", "content": "echo hi"},
        ]
        return httpx.Response(200, json={"skills": [{"name": "alpha", "files": files}]})

    class _Handed(Exception):
        pass

    handed = {}

    def drive(**kwargs):
        handed.update(kwargs)
        raise _Handed  # stop before the loops and the report

    async def fetch(spec, dest):
        return dest

    async def reuse(task, config):
        return config

    loaded = env.load(_env())
    monkeypatch.setattr(env, "load", lambda: loaded)
    monkeypatch.setattr(
        runner,
        "MonitorClient",
        functools.partial(MonitorClient, transport=httpx.MockTransport(api)),
    )
    monkeypatch.setattr(runner, "write_status", lambda **kwargs: None)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(task_fetch, "fetch_task", fetch)
    monkeypatch.setattr(builder, "env_for", lambda task: {})
    monkeypatch.setattr(task_image, "reuse", reuse)
    monkeypatch.setattr(driver, "run_harbor_trial_async", drive)

    with pytest.raises(_Handed):
        await runner.run()

    if not skill_ids:
        assert asked == ["attempt"]
        assert handed["staged_skills_root"] is None
        return
    assert asked == ["attempt", "skills"]
    root = Path(handed["staged_skills_root"])
    assert root.parent == handed["jobs_dir"].parent  # the attempt's workdir
    assert (root / "alpha" / "SKILL.md").read_text() == skill_md
    assert (root / "alpha" / "scripts" / "run.sh").read_text() == "echo hi"


@dataclass
class _Outcome:
    reward: float | None
    exit_code: int
    job_dir: Path | None
    job_result_path: Path | None = None


@pytest.mark.asyncio
async def test_artifacts_are_uploaded_before_the_result_is_reported(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "result.json").write_text("{}")
    calls = []

    def api(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(
                200,
                json={
                    "upload_url": "https://s3.test/put",
                    "upload_headers": {"Content-Type": "application/gzip"},
                    "archive_key": "k",
                },
            )
        body = json.loads(request.content)
        assert body == {
            "outcome": {"reward": 1.0, "exit_code": 0},
            "archive_uploaded": True,
            "execution_error": None,
            "retryable": True,
            "monitor_stats": None,
        }
        return httpx.Response(200, json={"accepted": True, "duplicate": False})

    def s3(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        with tarfile.open(fileobj=io.BytesIO(request.content), mode="r:gz") as tar:
            assert "./result.json" in tar.getnames()
        return httpx.Response(200)

    accepted = await artifacts.upload_and_report(
        _client(api), _Outcome(1.0, 0, job_dir), transport=httpx.MockTransport(s3)
    )
    assert accepted == {"accepted": True, "duplicate": False}
    assert [c[0] for c in calls] == ["POST", "PUT", "POST"]


@pytest.mark.asyncio
async def test_a_failure_before_harbor_starts_still_uploads_the_monitor_log(tmp_path):
    log_dir = tmp_path / "log-only"
    log_dir.mkdir()
    (log_dir / "oddish-mon.log").write_text("checked in\n")
    seen = {}

    def api(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(
                200, json={"upload_url": "https://s3.test/put", "archive_key": "k"}
            )
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"accepted": True, "duplicate": False})

    def s3(request: httpx.Request) -> httpx.Response:
        with tarfile.open(fileobj=io.BytesIO(request.content), mode="r:gz") as tar:
            seen["names"] = tar.getnames()
        return httpx.Response(200)

    await artifacts.upload_and_report(
        _client(api),
        None,  # the driver raised: no outcome, no job folder
        execution_error="RuntimeError: compose failed",
        fallback_dir=log_dir,
        transport=httpx.MockTransport(s3),
    )
    assert "./oddish-mon.log" in seen["names"]
    assert seen["body"]["archive_uploaded"] is True
    assert seen["body"]["outcome"] is None


@pytest.mark.asyncio
async def test_a_storage_outage_is_retried_but_a_refused_upload_is_not(
    monkeypatch, tmp_path
):
    """Seen in review: one 503 from storage lost a finished trial's result."""
    from oddish.mon import runner

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "result.json").write_text("{}")
    calls = []

    def api(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path.rsplit("/", 1)[-1])
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(
                200, json={"upload_url": "https://s3.test/put", "archive_key": "k"}
            )
        return httpx.Response(200, json={"accepted": True, "duplicate": False})

    storage = [httpx.ReadTimeout("slow"), httpx.Response(503), httpx.Response(200)]

    def s3(request: httpx.Request) -> httpx.Response:
        calls.append("put")
        answer = storage.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(
        artifacts,
        "upload_and_report",
        functools.partial(
            artifacts.upload_and_report, transport=httpx.MockTransport(s3)
        ),
    )
    outcome = _Outcome(1.0, 0, job_dir)
    await runner._deliver_result(_client(api), outcome, None, delay=0)
    # Every try asks for a fresh upload target: a presigned URL can expire.
    assert calls == ["artifacts", "put"] * 3 + ["result"]

    calls.clear()
    storage.append(httpx.Response(400))
    # Refused for good: not retried, but the verdict is still reported, without
    # artifacts, instead of being dropped as a failed start.
    await runner._deliver_result(_client(api), outcome, None, delay=0)
    assert calls == ["artifacts", "put", "result"]


def test_status_reports_whether_the_monitor_process_is_alive(tmp_path):
    path = tmp_path / "status.json"
    assert status.read_status(path) == {"alive": False, "reason": "no status"}

    status.write_status(trial_id="trial-1", phase="agent_running", path=path)
    live = status.read_status(path)
    assert live["alive"] is True and live["pid"] == os.getpid()
    assert live["phase"] == "agent_running"

    data = json.loads(path.read_text())
    data["pid"] = 2**22 + 12345  # a pid that is not running
    path.write_text(json.dumps(data))
    assert status.read_status(path)["alive"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        # Our API speaking: these end the attempt.
        (httpx.Response(410, json={"detail": "A newer attempt owns this job"}), AttemptGone),
        (httpx.Response(401, json={"detail": "Unknown monitor credential"}), MonitorRejected),
        # Our API refusing anything else: not retried, not a verdict.
        (httpx.Response(422, json={"detail": "bad result"}), ControlPlaneRefused),
        # The platform's edge speaking during a deploy: an outage, not a verdict.
        (httpx.Response(404, text="modal-http: app for invoked web endpoint is stopped"), ControlPlaneUnavailable),
        (httpx.Response(410, text="<html>gone</html>"), ControlPlaneUnavailable),
        (httpx.Response(401, text="unauthorized"), ControlPlaneUnavailable),
        (httpx.Response(429, json={"error": "slow down"}), ControlPlaneUnavailable),
        (httpx.Response(502, text="bad gateway"), ControlPlaneUnavailable),
        (httpx.Response(200, text="<html>maintenance</html>"), ControlPlaneUnavailable),
    ],
)
async def test_only_our_own_api_can_end_an_attempt(response, expected):
    """Seen in the preview: a redeploy answered 404 for two minutes and the
    monitor shut a healthy trial down."""
    client = _client(lambda request: response)
    with pytest.raises(expected):
        await client.heartbeat()
    await client.aclose()



def _stage_monitor(
    monkeypatch, tmp_path, *, driver_error=None, refuse=None, hook_events=()
):
    """Set up ``runner.run`` against a fake API that answers ``refuse`` =
    (route, status) with our API's error, and everything else with success.
    The driver first fires ``hook_events`` through the hook callback, carrying
    on past a hook that raises as Harbor may, then raises ``driver_error``, or
    runs until cancelled when it is None. Returns the phases the runner writes and the calls the API receives."""
    import asyncio
    import tempfile

    from oddish.mon import builder, runner, task_image
    from oddish.workers.harbor import runner as driver

    spec = {**SPEC, "harbor_config": {"skill_ids": ["skill-1"]}}
    calls, phases = [], []

    def api(request: httpx.Request) -> httpx.Response:
        route = request.url.path.rsplit("/", 1)[-1]
        calls.append((route, json.loads(request.content) if request.content else None))
        if refuse is not None and route == refuse[0]:
            return httpx.Response(refuse[1], json={"detail": "attempt over"})
        if route == "attempt":
            return httpx.Response(200, json=spec)
        if route == "skills":
            return httpx.Response(200, json={"skills": []})
        return httpx.Response(200, json={"accepted": True, "duplicate": False})

    async def drive(**kwargs):
        from types import SimpleNamespace

        for event in hook_events:
            try:
                await kwargs["hook_callback"](
                    SimpleNamespace(event=event, environment=None, result=None)
                )
            except Exception:  # noqa: BLE001, S110 - Harbor may swallow a hook error
                pass
        if driver_error is None:
            await asyncio.Event().wait()
        raise driver_error

    async def fetch(spec, dest):
        return dest

    async def reuse(task, config):
        return config

    async def no_tail(trial_id):
        return None

    loaded = env.load(_env())
    monkeypatch.setattr(env, "load", lambda *args: loaded)
    monkeypatch.setattr(
        runner,
        "MonitorClient",
        functools.partial(MonitorClient, transport=httpx.MockTransport(api)),
    )
    monkeypatch.setattr(runner, "write_status", lambda **kw: phases.append(kw["phase"]))
    monkeypatch.setattr(runner, "_finish_live_tail", no_tail)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("ODDISH_MON_LOG_PATH", str(tmp_path / "no-log"))
    monkeypatch.setattr(task_fetch, "fetch_task", fetch)
    monkeypatch.setattr(builder, "env_for", lambda task: {})
    monkeypatch.setattr(task_image, "reuse", reuse)
    monkeypatch.setattr(driver, "run_harbor_trial_async", drive)
    return phases, calls


@pytest.mark.asyncio
async def test_a_dead_attempt_is_not_retried_by_the_report_loop():
    from oddish.mon import runner

    posted = []

    def api(request: httpx.Request) -> httpx.Response:
        posted.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(410, json={"detail": "A newer attempt owns this job"})

    with pytest.raises(AttemptGone):
        await runner._deliver_result(_client(api), None, "boom", delay=0)
    assert posted == ["result"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("refuse", "driver_error"),
    [
        (("attempt", 410), None),  # check-in
        (("skills", 410), None),  # task setup
        (("heartbeat", 410), None),  # driving
        (("heartbeat", 401), None),
        (("control", 410), None),
        (("control", 401), None),
        (("result", 410), RuntimeError("boom")),  # reporting
        (("result", 401), RuntimeError("boom")),
    ],
    ids=lambda value: "-".join(map(str, value)) if isinstance(value, tuple) else "",
)
async def test_the_control_plane_ending_the_attempt_exits_stopped_in_every_phase(
    monkeypatch, tmp_path, refuse, driver_error
):
    """Seen in review: a 410 on POST /result escaped the report loop, the
    monitor exited 1 and the entrypoint posted a failed start."""
    from oddish.mon import runner
    from oddish.mon.exit_codes import EXIT_STOPPED

    phases, calls = _stage_monitor(
        monkeypatch, tmp_path, driver_error=driver_error, refuse=refuse
    )
    assert await runner.run() == EXIT_STOPPED  # the entrypoint posts no failed start for it
    assert phases[-1] == "stopped"
    assert [route for route, _ in calls].count("result") == (refuse[0] == "result")


@pytest.mark.asyncio
@pytest.mark.parametrize("retryable", [True, False])
async def test_the_posted_retryable_follows_the_workers_rule(
    monkeypatch, tmp_path, retryable
):
    """Seen in review: every result was posted retryable, so a quota pause the
    worker fails for good would have been retried from the monitor."""
    from oddish.mon import runner
    from oddish.mon.exit_codes import EXIT_ACCEPTED
    from oddish.workers.harbor.quota_control import QuotaPauseControlError

    error = RuntimeError("compose failed") if retryable else QuotaPauseControlError("paused")
    _, calls = _stage_monitor(monkeypatch, tmp_path, driver_error=error)
    assert await runner.run() == EXIT_ACCEPTED
    (body,) = [body for route, body in calls if route == "result"]
    assert body["retryable"] is retryable
    assert body["execution_error"].startswith(type(error).__name__)


def test_an_outage_on_the_report_is_retried_then_fails_and_a_verdict_stops(
    monkeypatch, tmp_path, caplog
):
    """The report path's three answers, through the real ``python -m oddish.mon
    run``: an outage is retried and logged, then exits EXIT_FAILED (reported by
    the entrypoint); our API's 401 records "stopped" and exits EXIT_STOPPED."""
    import logging

    from oddish.mon import __main__ as cli
    from oddish.mon import runner
    from oddish.mon.exit_codes import EXIT_FAILED, EXIT_STOPPED

    monkeypatch.setattr(cli, "configure_logging", lambda: None)
    monkeypatch.setattr(runner, "REPORT_RETRY_SECONDS", 0)
    monkeypatch.setattr(runner, "REPORT_ATTEMPTS", 3)
    caplog.set_level(logging.WARNING)

    phases, calls = _stage_monitor(
        monkeypatch, tmp_path, driver_error=RuntimeError("boom"), refuse=("result", 503)
    )
    assert cli.main(["run"]) == EXIT_FAILED
    assert [route for route, _ in calls].count("result") == 3
    assert phases[-1] == "reporting"  # never done, never stopped
    assert status.read_status(status.STATUS_PATH).get("phase") is None
    outages = [r for r in caplog.records if "unavailable: POST /result" in r.getMessage()]
    assert [r.levelno for r in outages] == [logging.WARNING] * 3
    assert outages[-1].getMessage().endswith(
        'HTTP 503 (failure 3 in a row): {"detail":"attempt over"}'
    )
    (failed,) = [r for r in caplog.records if r.getMessage() == "monitor run failed"]
    assert failed.levelno == logging.ERROR and failed.exc_info[0] is ControlPlaneUnavailable

    phases, _ = _stage_monitor(
        monkeypatch, tmp_path, driver_error=RuntimeError("boom"), refuse=("result", 401)
    )
    assert cli.main(["run"]) == EXIT_STOPPED
    assert status.read_status(status.STATUS_PATH)["phase"] == "stopped"


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [410, 401])
async def test_a_verdict_on_a_lifecycle_post_stops_the_driver_and_stays_recorded(
    monkeypatch, tmp_path, verdict
):
    """Seen in review: a 401 on the lifecycle route was swallowed as a blip,
    Harbor kept driving and the next hook overwrote the "stopped" status."""
    import asyncio

    from harbor.trial.hooks import TrialEvent

    from oddish.mon import runner
    from oddish.mon.exit_codes import EXIT_STOPPED

    _stage_monitor(
        monkeypatch,
        tmp_path,
        refuse=("lifecycle", verdict),
        hook_events=(TrialEvent.START, TrialEvent.ENVIRONMENT_START),
    )
    # Without the stop the driver runs forever: fail instead of hanging.
    assert await asyncio.wait_for(runner.run(), timeout=10) == EXIT_STOPPED
    # The second hook still ran and wrote its stage; the verdict stays.
    assert status.read_status(status.STATUS_PATH)["phase"] == "stopped"


@pytest.mark.asyncio
async def test_a_refused_lifecycle_event_is_dropped_and_the_result_still_posted(
    caplog,
):
    """Seen in review: one lifecycle event refused with a 4xx aborted the
    whole report before POST /result."""
    import logging

    from oddish.mon import hooks, runner

    calls = []

    def api(request: httpx.Request) -> httpx.Response:
        route = request.url.path.rsplit("/", 1)[-1]
        calls.append(route)
        if route == "lifecycle" and calls.count("lifecycle") == 1:
            return httpx.Response(422, json={"detail": "bad summary"})
        return httpx.Response(200, json={"accepted": True, "duplicate": False})

    client = _client(api)
    reporter = hooks.LifecycleReporter(client)
    reporter._pending.extend([{"event": "agent-end"}, {"event": "end"}])
    await runner._deliver_result(
        client, None, "boom", reporter=reporter, delay=0
    )
    assert calls == ["lifecycle", "lifecycle", "result"]
    assert reporter.pending == 0
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("POST /lifecycle: HTTP 422: {\"detail\":\"bad summary\"}" in m for m in errors)
    assert any(m.startswith("lifecycle event agent-end dropped") for m in errors)


def test_a_recorded_end_is_not_replaced_by_a_later_stage(tmp_path):
    path = tmp_path / "status.json"
    status.write_status(trial_id="trial-1", phase="agent-start", path=path)
    status.write_status(trial_id="trial-1", phase="stopped", path=path)
    status.write_status(trial_id="trial-1", phase="end", path=path)
    assert status.read_status(path)["phase"] == "stopped"
    status.write_status(trial_id="trial-1", phase="done", path=path)
    assert status.read_status(path)["phase"] == "done"  # terminal over terminal


@pytest.mark.asyncio
async def test_a_refused_artifact_grant_still_reports_the_verdict(tmp_path):
    """A 4xx on POST /artifacts is the same fact as a refused upload: no files,
    but the result is posted (archive_uploaded=False), never a failed start."""
    from oddish.mon import runner

    calls = []

    def api(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path.rsplit("/", 1)[-1])
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(403, json={"detail": "no"})
        return httpx.Response(200, json={"accepted": True})

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "result.json").write_text("{}")
    outcome = _Outcome(1.0, 0, job_dir)
    await runner._deliver_result(_client(api), outcome, None, delay=0)
    assert calls == ["artifacts", "result"]
