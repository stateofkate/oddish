"""The Harbor-side helpers take their database touchpoints as arguments.

``live_tail`` and ``quota_control`` run on the worker and inside the sandbox
monitor; the worker passes the ``*_db`` defaults, the monitor its own.
"""

from __future__ import annotations

import ast
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from harbor.trial.hooks import TrialEvent

from oddish.workers import console as console_module
from oddish.workers.harbor import live_tail, live_tail_db, quota_control, quota_db
from oddish.workers.queue import shared, trial_handler

_FORBIDDEN = ("oddish.db", "sqlalchemy", "asyncpg", "oddish.workers.queue")


def _imports(module) -> list[str]:
    names = []
    for node in ast.walk(ast.parse(Path(module.__file__).read_text())):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


@pytest.mark.parametrize("module", [live_tail, quota_control, console_module])
def test_sandbox_side_modules_import_no_database_and_no_queue(module):
    offenders = [n for n in _imports(module) if n.startswith(_FORBIDDEN)]
    assert offenders == [], offenders
    assert not hasattr(module, "get_session")


def test_the_worker_console_is_shared_not_owned_by_the_queue():
    assert shared.console is console_module.console is live_tail.console


def test_the_database_quota_seams_are_the_two_db_functions():
    seams = quota_db.DATABASE_SEAMS
    assert isinstance(seams, quota_control.QuotaSeams)
    assert seams.refresh_requested is quota_db.refresh_pause_requested
    assert seams.record_state is quota_db.record_pause_state


def test_the_tailer_needs_an_explicit_persist():
    with pytest.raises(TypeError):
        live_tail.LiveTailer(  # type: ignore[call-arg]
            trial_id="t", environment=None, attempt=1, log_path="/x", fold=None
        )
    with pytest.raises(TypeError):
        live_tail.start(  # type: ignore[call-arg]
            trial_id="t", environment=None, attempt=1, agent="claude-code", model=None
        )


@pytest.mark.asyncio
async def test_the_worker_hook_starts_the_tail_with_the_database_persist(monkeypatch):
    """The worker path's default: the trial handler hands the tailer the
    database persist; nothing is looked up from a module global."""
    started = []

    class _Trial:
        attempts, agent, model = 2, "claude-code", "m"
        org_id, billed_user_id = "org-1", "user-1"

        def __getattr__(self, name):  # every other column the handler stamps
            return None

        def __setattr__(self, name, value):
            pass

    trial = _Trial()

    @asynccontextmanager
    async def trial_session(*args, **kwargs):
        yield SimpleNamespace(), trial

    monkeypatch.setattr(trial_handler, "_trial_session", trial_session)
    monkeypatch.setattr(
        trial_handler.live_tail, "start", lambda **kw: started.append(kw)
    )
    monkeypatch.setattr(trial_handler.console, "print", lambda *a, **k: None)

    await trial_handler.handle_harbor_event(
        SimpleNamespace(
            event=TrialEvent.AGENT_START,
            environment=object(),
            environment_provider=None,
            environment_external_id=None,
            result=None,
        ),
        trial_id="trial-1",
    )

    (call,) = started
    assert call["persist"] is live_tail_db.persist_tick_to_database
    assert (call["attempt"], call["org_id"]) == (2, "org-1")


@pytest.mark.asyncio
async def test_agent_clock_uses_lifecycle_boundaries_and_ignores_replays(monkeypatch):
    from datetime import datetime, timedelta, timezone

    trial = SimpleNamespace(
        attempts=1, agent="codex", model="m", org_id="org", billed_user_id=None,
        superseded_by_trial_id=None, error_message=None, status=trial_handler.TrialStatus.RUNNING,
        max_attempts=1, phase_timing={"environment_setup": {"duration_sec": 45}},
    )

    @asynccontextmanager
    async def trial_session(*args, **kwargs):
        yield SimpleNamespace(), trial

    monkeypatch.setattr(trial_handler, "_trial_session", trial_session)
    start = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)

    async def emit(event, seconds):
        await trial_handler.handle_harbor_event(SimpleNamespace(
            event=event, timestamp=start + timedelta(seconds=seconds),
            environment=None, environment_provider=None, environment_external_id=None,
            result=None,
        ), trial_id="trial-1")

    await emit(TrialEvent.AGENT_START, 0)
    await emit(TrialEvent.AGENT_START, 3)
    assert trial.phase_timing["agent_execution"] == {"started_at": start.isoformat()}
    await emit(TrialEvent.AGENT_END, 65)
    await emit(TrialEvent.VERIFICATION_START, 70)
    assert trial.phase_timing["agent_execution"] == {
        "started_at": start.isoformat(),
        "finished_at": (start + timedelta(seconds=65)).isoformat(),
        "duration_sec": 65,
    }
    assert trial.phase_timing["environment_setup"] == {"duration_sec": 45}


@pytest.mark.asyncio
@pytest.mark.parametrize("reward", [0.0, 1.0])
async def test_verification_clock_is_retained_before_result_upload(monkeypatch, reward):
    from datetime import datetime, timedelta, timezone

    trial = SimpleNamespace(
        attempts=1, superseded_by_trial_id=None, error_message=None,
        status=trial_handler.TrialStatus.RUNNING, max_attempts=1,
        phase_timing={"agent_execution": {"duration_sec": 1.1}}, environment="modal",
    )

    @asynccontextmanager
    async def trial_session(*args, **kwargs):
        yield SimpleNamespace(), trial

    monkeypatch.setattr(trial_handler, "_trial_session", trial_session)
    start = datetime(2026, 10, 7, 20, 53, 14, tzinfo=timezone.utc)

    async def emit(event, seconds, result=None):
        await trial_handler.handle_harbor_event(SimpleNamespace(
            event=event, timestamp=start + timedelta(seconds=seconds),
            environment=None, environment_provider=None, environment_external_id=None,
            result=result,
        ), trial_id="trial-1")

    await emit(TrialEvent.VERIFICATION_START, 0)
    await emit(TrialEvent.VERIFICATION_START, 2)
    assert trial.phase_timing["verifier"] == {"started_at": start.isoformat()}
    result = SimpleNamespace(
        verifier_result=SimpleNamespace(rewards={"reward": reward}), exception_info=None,
    )
    await emit(TrialEvent.END, 2933, result)
    assert trial.phase_timing["verifier"] == {
        "started_at": start.isoformat(),
        "finished_at": (start + timedelta(seconds=2933)).isoformat(),
        "duration_sec": 2933,
    }
    assert trial.phase_timing["agent_execution"] == {"duration_sec": 1.1}
    assert trial.status == trial_handler.TrialStatus.SUCCESS
    assert trial.reward == reward
    assert trial.harbor_stage == "completed"
    await emit(TrialEvent.END, 2940, result)
    assert trial.phase_timing["verifier"]["duration_sec"] == 2933


@pytest.mark.asyncio
@pytest.mark.parametrize("verifier", [None, {
    "started_at": "2026-10-07T20:53:14+00:00",
    "finished_at": "2026-10-07T21:00:00+00:00",
    "duration_sec": 406,
}])
async def test_verification_end_preserves_recorded_or_absent_timing(monkeypatch, verifier):
    from datetime import datetime, timezone

    trial = SimpleNamespace(
        attempts=1, superseded_by_trial_id=None, error_message=None,
        status=trial_handler.TrialStatus.RUNNING, max_attempts=1,
        phase_timing={"verifier": verifier} if verifier else {}, environment="modal",
    )

    @asynccontextmanager
    async def trial_session(*args, **kwargs):
        yield SimpleNamespace(), trial

    monkeypatch.setattr(trial_handler, "_trial_session", trial_session)
    await trial_handler.handle_harbor_event(SimpleNamespace(
        event=TrialEvent.END, timestamp=datetime(2026, 10, 7, 21, 42, 7, tzinfo=timezone.utc),
        environment=None, environment_provider=None, environment_external_id=None,
        result=None,
    ), trial_id="trial-1")
    assert trial.phase_timing == ({"verifier": verifier} if verifier else {})
