"""Provider account caps must settle trial and worker-job state consistently."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from oddish.db import TrialStatus, WorkerJobKind
from oddish.workers.harbor.runner import HarborOutcome
from oddish.workers.jobs import handlers
from oddish.workers.jobs.handlers import TrialJobHandler
from oddish.workers.queue import trial_handler
from oddish.workers.queue.provider_failures import classify_provider_failure
from oddish.workers.queue.worker_job_single_job import ClaimedWorkerJob


USAGE_CAP = (
    "API Error: 400 You have reached your specified API usage limits. "
    "You will regain access on 2099-09-01 at 00:00 UTC.\n"
)


def _outcome(
    error: str, exception_type: str | None = None, http_status: int | None = None
) -> HarborOutcome:
    return HarborOutcome(
        reward=None,
        error=error,
        exit_code=1,
        duration_sec=1.0,
        job_result_path=None,
        job_dir=None,
        exception_type=exception_type,
        http_status=http_status,
    )


@pytest.mark.parametrize(
    ("error", "exception_type", "terminal"),
    [
        (USAGE_CAP, None, True),
        ("PermissionDeniedError: Error code: 403 - forbidden", None, True),
        ("RateLimitError: Error code: 429 - slow down", None, False),
        ("TimeoutError: provider timed out", None, False),
        (
            "BadRequestError: Error code: 400 - Your credit balance is too low",
            None,
            False,
        ),
    ],
)
def test_provider_failure_retry_policy(error, exception_type, terminal):
    assert (
        trial_handler._is_non_retryable_outcome(
            SimpleNamespace(harbor_config={}),
            _outcome(error, exception_type),
        )
        is terminal
    )


@pytest.mark.parametrize(
    ("original_error", "exception_type", "http_status", "failure_class", "terminal"),
    [
        (
            USAGE_CAP + ("provider diagnostic detail " * 40),
            None,
            400,
            "provider_usage_limit",
            True,
        ),
        ("Request rejected", None, 403, "permission_denied", True),
        ("Request rejected", "PermissionDeniedError", None, "permission_denied", True),
        ("Request rejected", None, 429, "rate_limit", False),
        ("Request rejected", None, 503, "provider_unavailable", False),
        ("PermissionDeniedError: forbidden", None, None, "permission_denied", True),
        (
            USAGE_CAP.replace("2099-09-01", "2000-09-01"),
            None,
            400,
            "provider_usage_limit",
            False,
        ),
        (
            "You have reached your specified API usage limits.",
            None,
            400,
            "provider_usage_limit",
            False,
        ),
        (
            USAGE_CAP + "Agent execution timed out",
            None,
            400,
            "provider_usage_limit",
            True,
        ),
    ],
    ids=[
        "future-cap",
        "http-403",
        "typed-permission",
        "http-429",
        "http-503",
        "text-permission",
        "expired-cap",
        "undated-cap",
        "future-cap-timeout",
    ],
)
@pytest.mark.parametrize("kind", ["agent", "qa", "audit"])
@pytest.mark.parametrize("reward", [None, 0.0, 1.0])
@pytest.mark.parametrize(
    "artifact_error", [None, "Failed to upload trial results to S3"]
)
@pytest.mark.asyncio
async def test_provider_failure_settlement_and_worker_agree(
    monkeypatch,
    capsys,
    original_error,
    exception_type,
    http_status,
    failure_class,
    terminal,
    kind,
    reward,
    artifact_error,
):
    # This suite drives the in-process worker settlement path. Monitor routing
    # has a separate execution/settlement contract and is now enabled by default.
    from oddish.config import settings

    monkeypatch.setattr(settings, "monitor_execution_enabled", False)
    harbor_outcome = replace(
        _outcome(original_error, exception_type, http_status), reward=reward
    )
    trial = SimpleNamespace(
        id="trial-usage-cap",
        task_id="task-usage-cap",
        kind=kind,
        agent="claude-code",
        model="claude-sonnet-4-6",
        harbor_config={"retry": {"exclude_exceptions": []}},
        status=TrialStatus.RUNNING,
        attempts=1,
        max_attempts=3,
        error_message=None,
        harbor_stage="agent",
        reward=None,
        harbor_result_path=None,
        trial_s3_key=None,
        input_tokens=None,
        cache_tokens=None,
        cache_write_tokens=None,
        output_tokens=None,
        cost_usd=None,
        phase_timing=None,
        result=None,
        has_trajectory=False,
        current_worker_id="worker-1",
        current_queue_slot=0,
        heartbeat_at=None,
        finished_at=None,
        superseded_by_trial_id=None,
        deleted_at=None,
    )

    @asynccontextmanager
    async def fake_trial_session(
        _trial_id: str, *, allow_missing: bool = False, with_for_update: bool = False
    ):
        yield object(), trial

    monkeypatch.setattr(trial_handler, "_trial_session", fake_trial_session)
    monkeypatch.setattr(
        trial_handler, "refresh_task_browse_summaries", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        trial_handler, "_log_trial_metering_integrity", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        trial_handler, "_verifier_ran_from_job_result", lambda _path: True
    )

    settled_terminal, completed, event = await trial_handler._store_trial_results(
        trial_id=trial.id,
        outcome=harbor_outcome,
        trial_s3_key=None,
        execution_error=None,
        trial_attempt=1,
        artifact_upload_error=artifact_error,
    )

    analysis_artifact_error = artifact_error if kind in ("qa", "audit") else None
    expected_reward = None if terminal or analysis_artifact_error else reward
    success = expected_reward is not None
    assert trial.reward == expected_reward
    assert (settled_terminal, completed) == (terminal or success, terminal or success)
    assert trial.status == (
        TrialStatus.SUCCESS
        if success
        else TrialStatus.FAILED
        if terminal
        else TrialStatus.RETRYING
    )
    assert (trial.finished_at is not None) is (terminal or success)
    assert trial.attempts == 1
    if terminal:
        assert f"FAILED ({exception_type or failure_class}; non-retryable)" in (
            capsys.readouterr().out
        )
    expected_error = (
        classify_provider_failure(original_error).error_summary
        if failure_class == "provider_usage_limit"
        else original_error
    )
    if analysis_artifact_error:
        expected_error = (
            f"{expected_error}\n{artifact_error}" if terminal else artifact_error
        )
    elif artifact_error:
        expected_error += (
            f"\nTrial finished but its files were not saved: {artifact_error}"
        )
    assert trial.error_message == expected_error

    @asynccontextmanager
    async def fake_handler_session():
        yield SimpleNamespace(get=AsyncMock(return_value=trial))

    monkeypatch.setattr(handlers, "get_session", fake_handler_session)
    monkeypatch.setattr(
        handlers, "run_trial_job", AsyncMock(return_value=harbor_outcome)
    )
    monkeypatch.setattr(handlers, "decrypt_credentials", lambda _value: None)
    job = ClaimedWorkerJob(
        id="worker-job-usage-cap",
        kind=WorkerJobKind.TRIAL,
        queue_key="anthropic/claude-sonnet-4-6",
        subject_table="trials",
        subject_id=trial.id,
        payload={},
        attempts=1,
        max_attempts=3,
        org_id=None,
        parent_job_id=None,
        worker_id="worker-1",
        queue_slot=0,
    )

    worker_outcome = await TrialJobHandler().run(job)

    if success:
        assert worker_outcome.failure is None
        return
    assert worker_outcome.failure is not None
    assert worker_outcome.failure.retryable is (not terminal)
    # Retriable analysis artifact errors replace the provider message; the
    # worker still sees the structured status, but no longer sees cap text.
    expected_worker_class = (
        "provider_error"
        if analysis_artifact_error
        and not terminal
        and failure_class == "provider_usage_limit"
        else failure_class
    )
    assert (
        worker_outcome.failure.provider_failure.failure_class == expected_worker_class
    )
    assert worker_outcome.failure.provider_failure.provider_status_code == http_status
