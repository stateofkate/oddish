"""Archil retries move to Modal after two failed durable attempts."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from oddish.db import TrialModel, TrialStatus
from oddish.workers.harbor.outcome import HarborOutcome
from oddish.workers.queue import trial_handler, trial_steps


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "environment,default_environment,status,attempts,max_attempts,expected",
    [
        ("archil", "daytona", TrialStatus.PENDING, 0, 6, "archil"),
        ("archil", "daytona", TrialStatus.RETRYING, 1, 6, "archil"),
        ("archil", "daytona", TrialStatus.RETRYING, 2, 6, "modal"),
        ("archil", "daytona", TrialStatus.RETRYING, 3, 6, "modal"),
        ("modal", "daytona", TrialStatus.RETRYING, 3, 6, "modal"),
        ("daytona", "archil", TrialStatus.RETRYING, 2, 6, "daytona"),
        ("numinous", "archil", TrialStatus.RETRYING, 2, 6, "numinous"),
        ("ec2", "archil", TrialStatus.RETRYING, 2, 6, "ec2"),
        (None, "archil", TrialStatus.RETRYING, 2, 6, "modal"),
        (None, "daytona", TrialStatus.RETRYING, 2, 6, None),
        ("archil", "daytona", TrialStatus.FAILED, 2, 6, "archil"),
        ("archil", "daytona", TrialStatus.SUCCESS, 2, 6, "archil"),
        ("archil", "daytona", TrialStatus.RETRYING, 2, 2, "archil"),
    ],
)
async def test_retry_pickup_persists_and_executes_the_fallback(
    monkeypatch,
    environment,
    default_environment,
    status,
    attempts,
    max_attempts,
    expected,
):
    trial = TrialModel(
        id="task-1-0",
        task_id="task-1",
        agent="nop",
        environment=environment,
        status=status,
        attempts=attempts,
        max_attempts=max_attempts,
        harbor_config={"environment": {"type": "archil"}},
    )
    session = AsyncMock()
    session.get.return_value = None
    committed_environments = []

    @asynccontextmanager
    async def trial_session(trial_id, *, with_for_update):
        assert trial_id == trial.id
        assert with_for_update is True
        yield session, trial
        committed_environments.append(trial.environment)

    monkeypatch.setattr(trial_steps, "_trial_session", trial_session)
    monkeypatch.setattr(trial_steps, "refresh_task_browse_summaries", AsyncMock())
    monkeypatch.setattr(
        trial_handler.settings, "harbor_environment", default_environment
    )

    prepared = await trial_handler.prepare_trial_run(
        trial_id=trial.id,
        worker_id="worker-1",
        queue_slot=None,
        modal_function_call_id=None,
    )

    assert prepared.trial_environment == expected
    assert committed_environments == [expected]
    assert prepared.trial_attempt == attempts + 1
    assert trial.max_attempts == max_attempts
    assert trial.harbor_config == {"environment": {"type": "archil"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["outcome", "exception", "worker_loss"])
async def test_failed_attempts_then_modal_success(monkeypatch, failure):
    trial = TrialModel(
        id="task-1-0",
        task_id="task-1",
        agent="nop",
        kind="agent",
        environment="archil",
        status=TrialStatus.PENDING,
        attempts=0,
        max_attempts=3,
    )
    session = AsyncMock()
    session.get.return_value = None

    @asynccontextmanager
    async def trial_session(_trial_id, **_kwargs):
        yield session, trial

    monkeypatch.setattr(trial_steps, "_trial_session", trial_session)
    monkeypatch.setattr(trial_handler, "_trial_session", trial_session)
    monkeypatch.setattr(trial_steps, "refresh_task_browse_summaries", AsyncMock())
    monkeypatch.setattr(trial_handler, "refresh_task_browse_summaries", AsyncMock())

    for attempt, provider in [(1, "archil"), (2, "archil"), (3, "modal")]:
        prepared = await trial_handler.prepare_trial_run(
            trial_id=trial.id,
            worker_id=None,
            queue_slot=None,
            modal_function_call_id=None,
        )
        assert prepared.trial_environment == provider
        assert trial.environment == provider
        assert prepared.trial_attempt == attempt
        success = attempt == 3
        if failure == "worker_loss" and not success:
            # Reconciliation marks the lost attempt RETRYING without an outcome.
            trial.status = TrialStatus.RETRYING
            continue
        outcome = (
            HarborOutcome(
                reward=1.0 if success else None,
                error=None if success else "ConnectionResetError: provider failed",
                exception_type=None if success else "ConnectionResetError",
                exit_code=0 if success else -1,
                duration_sec=1.0,
                job_result_path=None,
                job_dir=None,
            )
            if success or failure == "outcome"
            else None
        )
        terminal, run_hooks, event = await trial_handler._store_trial_results(
            trial_id=trial.id,
            outcome=outcome,
            trial_s3_key=None,
            execution_error="provider failed" if outcome is None else None,
            trial_attempt=attempt,
        )
        assert terminal is success
        assert run_hooks is success
        assert trial.status == (
            TrialStatus.SUCCESS if success else TrialStatus.RETRYING
        )
        if success:
            assert event["environment"] == "modal"
            assert event["attempts"] == 3
        else:
            assert event is None
    assert trial.max_attempts == 3
