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
    trial = SimpleNamespace(kind="agent", harbor_config={})
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
        SimpleNamespace(kind="agent", harbor_config={"mode": "probe"}),
        SimpleNamespace(kind="analysis", harbor_config={}),
        SimpleNamespace(kind="qa", harbor_config=None),
    ],
    ids=["probe", "analysis", "qa"],
)
async def test_other_trial_kinds_stay_on_the_worker(monkeypatch, trial):
    assert await _can_drive(monkeypatch, trial) is False


@pytest.mark.asyncio
async def test_missing_trial_is_not_sent_to_the_monitor(monkeypatch):
    assert await _can_drive(monkeypatch, None) is False


@pytest.mark.asyncio
async def test_unset_kind_counts_as_agent(monkeypatch):
    assert await _can_drive(monkeypatch, SimpleNamespace(kind=None, harbor_config=None))


async def _can_drive(monkeypatch, trial) -> bool:
    monkeypatch.setattr(handlers_module, "get_session", _session_returning(trial))
    return await handlers_module._monitor_can_drive("trial-abc")


@pytest.mark.asyncio
async def test_launch_that_settled_here_reports_the_settled_outcome(monkeypatch):
    """A failed launch settles the attempt itself and returns no handoff."""
    monkeypatch.setattr(settings, "monitor_execution_enabled", True)
    trial = SimpleNamespace(kind="agent", harbor_config={})
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
        ("https://mine.example", "https://pub.example", "oddish", "https://mine.example"),
        (None, "https://pub.example", "oddish", "https://pub.example"),
        (None, None, "oddish-pr-1760", "https://abundant-ai-preview--oddish-pr-1760-api.modal.run"),
        (None, None, "oddish-staging", "https://abundant-ai-staging--oddish-staging-api.modal.run"),
        (None, None, "oddish", "https://abundant-ai--api.modal.run"),
        (None, None, "someone-elses-app", ""),
        (None, None, None, ""),
    ],
)
def test_control_plane_url_resolution(monkeypatch, explicit, public, app, expected):
    monkeypatch.setattr(settings, "monitor_api_url", explicit)
    for name, value in (("ODDISH_PUBLIC_API_BASE_URL", public), ("MODAL_APP_NAME", app)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert (monitor_launch.control_plane_url() or "") == expected



@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["", None, RuntimeError("no app")], ids=["empty", "none", "raises"])
async def test_no_control_plane_url_keeps_the_trial_on_the_worker(monkeypatch, url):
    def _url():
        if isinstance(url, Exception):
            raise url
        return url

    monkeypatch.setattr(monitor_launch, "control_plane_url", _url)
    assert await _can_drive(monkeypatch, SimpleNamespace(kind="agent", harbor_config={})) is False
