"""The monitor path is the default route for plain agent trials.

Covers the handler's routing decision and how the launcher finds the control
plane. The launch itself is covered by the boundary proofs on real Postgres.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from oddish.config import Settings, settings
from oddish.db import WorkerJobKind
from oddish.workers.jobs import handlers as handlers_module
from oddish.workers.jobs.handlers import TrialJobHandler
from oddish.workers.jobs.registry import JobOutcome
from oddish.workers.launcher import launch as monitor_launch
from oddish.workers.queue.worker_job_single_job import ClaimedWorkerJob


def _claim() -> ClaimedWorkerJob:
    return ClaimedWorkerJob(
        id="wj-1",
        kind=WorkerJobKind.TRIAL,
        queue_key="openai/gpt-5",
        subject_table="trials",
        subject_id="trial-abc",
        payload={"trial_id": "trial-abc"},
        attempts=1,
        max_attempts=6,
        org_id=None,
        parent_job_id=None,
        worker_id="w-1",
        queue_slot=0,
        modal_function_call_id=None,
    )


def _session_returning(trial):
    class _Session:
        async def get(self, _model, _id):
            return trial

    @asynccontextmanager
    async def _get_session():
        yield _Session()

    return _get_session


@pytest.fixture(autouse=True)
def _default_environment_has_a_starter(monkeypatch):
    monkeypatch.setattr(settings, "monitor_sandbox_starter", None)
    monkeypatch.setattr(settings, "harbor_environment", "modal")
    # The sandbox must be able to reach a control plane for the monitor to drive.
    monkeypatch.setattr(settings, "monitor_api_url", "https://api.test")


@pytest.fixture(autouse=True)
def _cpu_task(monkeypatch, tmp_path):
    """Every trial here reads a CPU-only task unless a test rewrites it."""
    from oddish.db import storage
    from oddish.workers.queue import trial_steps

    (tmp_path / "task.toml").write_text('version = "1.0"\n[environment]\ncpus = 2\n')

    async def _resolve(**_):
        return tmp_path, None, None

    async def _context(_session, _trial, **_):
        return SimpleNamespace(
            task_id="t", task_s3_key=None, task_path=None, trial_harbor_config={}
        )

    monkeypatch.setattr(storage, "resolve_task_directory", _resolve)
    monkeypatch.setattr(trial_steps, "load_trial_context", _context)


def _stub(monkeypatch, module, name, result):
    calls = []

    async def _fn(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(module, name, _fn)
    return calls


def test_monitor_path_is_on_by_default():
    assert Settings.model_fields["monitor_execution_enabled"].default is True
    # No forced provider: a trial runs on the one it asked for.
    assert Settings.model_fields["monitor_sandbox_starter"].default is None


@pytest.mark.asyncio
async def test_agent_trial_is_handed_to_the_monitor(monkeypatch):
    monkeypatch.setattr(settings, "monitor_execution_enabled", True)
    trial = SimpleNamespace(environment=None, kind="agent", harbor_config={})
    monkeypatch.setattr(handlers_module, "get_session", _session_returning(trial))
    launched = _stub(
        monkeypatch, monitor_launch, "launch_monitor_attempt", JobOutcome.handed_off()
    )
    drove = _stub(monkeypatch, handlers_module, "run_trial_job", None)

    outcome = await TrialJobHandler().run(_claim())

    assert outcome.handoff is not None
    assert len(launched) == 1
    assert drove == []  # the worker did not drive the trial


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trial",
    [
        SimpleNamespace(environment=None, kind="analysis", harbor_config={}),
        SimpleNamespace(environment=None, kind="qa", harbor_config=None),
    ],
    ids=["analysis", "qa"],
)
async def test_other_trial_kinds_stay_on_the_worker(monkeypatch, trial):
    assert await _can_drive(monkeypatch, trial) is False


@pytest.mark.asyncio
async def test_missing_trial_is_not_sent_to_the_monitor(monkeypatch):
    assert await _can_drive(monkeypatch, None) is False


@pytest.mark.asyncio
async def test_unset_kind_counts_as_agent(monkeypatch):
    assert await _can_drive(
        monkeypatch, SimpleNamespace(environment=None, kind=None, harbor_config=None)
    )


async def _can_drive(monkeypatch, trial) -> bool:
    monkeypatch.setattr(handlers_module, "get_session", _session_returning(trial))
    return await handlers_module._monitor_can_drive("trial-abc")


@pytest.mark.asyncio
async def test_launch_that_settled_here_reports_the_settled_outcome(monkeypatch):
    """A failed launch settles the attempt itself and returns no handoff."""
    monkeypatch.setattr(settings, "monitor_execution_enabled", True)
    trial = SimpleNamespace(environment=None, kind="agent", harbor_config={})
    monkeypatch.setattr(handlers_module, "get_session", _session_returning(trial))
    _stub(monkeypatch, monitor_launch, "launch_monitor_attempt", None)
    settled = JobOutcome.fail("sandbox refused", retryable=True)
    seen = _stub(monkeypatch, TrialJobHandler, "outcome_for_settled_trial", settled)

    outcome = await TrialJobHandler().run(_claim())

    assert outcome is settled
    assert len(seen) == 1


@pytest.mark.parametrize(
    ("explicit", "public", "app", "expected"),
    [
        (
            "https://mine.example",
            "https://pub.example",
            "oddish",
            "https://mine.example",
        ),
        (None, "https://pub.example", "oddish", "https://pub.example"),
        (
            None,
            None,
            "oddish-pr-1760",
            "https://abundant-ai-preview--oddish-pr-1760-api.modal.run",
        ),
        (
            None,
            None,
            "oddish-staging",
            "https://abundant-ai-staging--oddish-staging-api.modal.run",
        ),
        (None, None, "oddish", "https://abundant-ai--api.modal.run"),
        (None, None, "someone-elses-app", ""),
        (None, None, None, ""),
    ],
)
def test_control_plane_url_resolution(monkeypatch, explicit, public, app, expected):
    monkeypatch.setattr(settings, "monitor_api_url", explicit)
    for name, value in (
        ("ODDISH_PUBLIC_API_BASE_URL", public),
        ("MODAL_APP_NAME", app),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert (monitor_launch.control_plane_url() or "") == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url", ["", None, RuntimeError("no app")], ids=["empty", "none", "raises"]
)
async def test_no_control_plane_url_keeps_the_trial_on_the_worker(monkeypatch, url):
    def _url():
        if isinstance(url, Exception):
            raise url
        return url

    monkeypatch.setattr(monitor_launch, "control_plane_url", _url)
    assert (
        await _can_drive(
            monkeypatch, SimpleNamespace(environment=None, kind="agent", harbor_config={})
        )
        is False
    )


def _claim_attempt(n: int) -> ClaimedWorkerJob:
    from dataclasses import replace

    return replace(_claim(), attempts=n)


def _settled_agent_trial():
    from oddish.db import TrialStatus

    return SimpleNamespace(
        environment=None,
        kind="agent",
        harbor_config={},
        status=TrialStatus.SUCCESS,
        error_message=None,
    )


def _route_lines(monkeypatch) -> list[str]:
    from oddish.workers.console import console

    lines: list[str] = []
    monkeypatch.setattr(
        console, "print", lambda *a, **k: lines.append(" ".join(map(str, a)))
    )
    return lines


@pytest.mark.asyncio
async def test_sampled_out_trial_runs_on_the_worker_and_says_why(monkeypatch):
    monkeypatch.setattr(settings, "monitor_execution_enabled", True)
    monkeypatch.setattr(settings, "monitor_execution_sample_percent", 0)
    monkeypatch.setattr(
        handlers_module, "get_session", _session_returning(_settled_agent_trial())
    )
    launched = _stub(
        monkeypatch, monitor_launch, "launch_monitor_attempt", JobOutcome.handed_off()
    )
    drove = _stub(monkeypatch, handlers_module, "run_trial_job", None)
    lines = _route_lines(monkeypatch)

    await TrialJobHandler().run(_claim())

    assert launched == [] and len(drove) == 1
    assert any("route=worker reason=percent=0" in line for line in lines)


@pytest.mark.asyncio
async def test_sampled_trial_falls_back_to_the_worker_on_its_third_attempt(monkeypatch):
    from oddish.workers.launcher.route import bucket_for

    percent = bucket_for("trial-abc") + 1  # the fixture id is inside the sample
    monkeypatch.setattr(settings, "monitor_execution_enabled", True)
    monkeypatch.setattr(settings, "monitor_execution_sample_percent", percent)
    monkeypatch.setattr(settings, "monitor_execution_fallback_after_attempts", 2)
    monkeypatch.setattr(
        handlers_module, "get_session", _session_returning(_settled_agent_trial())
    )
    launched = _stub(
        monkeypatch, monitor_launch, "launch_monitor_attempt", JobOutcome.handed_off()
    )
    drove = _stub(monkeypatch, handlers_module, "run_trial_job", None)
    lines = _route_lines(monkeypatch)

    await TrialJobHandler().run(_claim_attempt(2))
    await TrialJobHandler().run(_claim_attempt(3))

    assert len(launched) == 1 and len(drove) == 1
    assert any(
        "attempt=2 route=monitor reason=sampled bucket=" in line for line in lines
    )
    assert any(
        "attempt=3 route=worker reason=fallback attempt=3>2" in line for line in lines
    )


@pytest.mark.asyncio
async def test_at_100_percent_every_attempt_stays_on_the_monitor(monkeypatch):
    monkeypatch.setattr(settings, "monitor_execution_enabled", True)
    monkeypatch.setattr(settings, "monitor_execution_sample_percent", 100)
    monkeypatch.setattr(
        handlers_module, "get_session", _session_returning(_settled_agent_trial())
    )
    launched = _stub(
        monkeypatch, monitor_launch, "launch_monitor_attempt", JobOutcome.handed_off()
    )
    _stub(monkeypatch, handlers_module, "run_trial_job", None)
    lines = _route_lines(monkeypatch)

    await TrialJobHandler().run(_claim_attempt(6))

    assert len(launched) == 1
    assert any("attempt=6 route=monitor reason=percent=100" in line for line in lines)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("environment", "gpus", "on_monitor"),
    [("modal", 1, False), ("daytona", 1, False), ("modal", 0, True)],
)
async def test_gpu_trial_runs_on_the_worker(
    monkeypatch, tmp_path, environment, gpus, on_monitor
):
    """A GPU trial never gets a monitor sandbox or a warm-up, on any provider;
    a CPU trial still does."""
    from oddish.workers.launcher.providers.modal import ModalStarter

    (tmp_path / "task.toml").write_text(
        f'version = "1.0"\n[environment]\ncpus = 2\nmemory_mb = 4096\ngpus = {gpus}\n'
    )
    monkeypatch.setattr(settings, "monitor_execution_enabled", True)
    monkeypatch.setattr(settings, "monitor_execution_sample_percent", 100)
    trial = _settled_agent_trial()
    trial.environment = environment
    monkeypatch.setattr(handlers_module, "get_session", _session_returning(trial))
    launched = _stub(
        monkeypatch, monitor_launch, "launch_monitor_attempt", JobOutcome.handed_off()
    )
    warmed = _stub(monkeypatch, ModalStarter, "build_warm_image", "im-x")
    drove = _stub(monkeypatch, handlers_module, "run_trial_job", None)
    lines = _route_lines(monkeypatch)

    await TrialJobHandler().run(_claim())

    assert warmed == []
    assert (len(launched), len(drove)) == ((1, 0) if on_monitor else (0, 1))
    gpu_line = any("route=worker reason=gpu" in line for line in lines)
    if environment == "daytona":
        assert any("reason=disabled-environment" in line for line in lines)
    else:
        assert gpu_line is not on_monitor


@pytest.mark.asyncio
async def test_the_route_line_names_where_a_non_agent_trial_goes(monkeypatch):
    """Staging 24 Sep: an audit trial logged route=monitor, then ran on the
    worker. The line reports the final decision and its reason."""
    monkeypatch.setattr(settings, "monitor_execution_enabled", True)
    monkeypatch.setattr(settings, "monitor_execution_sample_percent", 100)
    trial = _settled_agent_trial()
    trial.kind = "audit"
    monkeypatch.setattr(handlers_module, "get_session", _session_returning(trial))
    launched = _stub(
        monkeypatch, monitor_launch, "launch_monitor_attempt", JobOutcome.handed_off()
    )
    drove = _stub(monkeypatch, handlers_module, "run_trial_job", None)
    lines = _route_lines(monkeypatch)

    await TrialJobHandler().run(_claim())

    assert (len(launched), len(drove)) == (0, 1)
    assert any("route=worker reason=kind=audit" in line for line in lines), lines
