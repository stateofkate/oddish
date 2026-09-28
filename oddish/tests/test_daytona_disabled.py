"""Daytona cannot launch through new requests, queued work, or direct runners."""

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from harbor.models.environment_type import EnvironmentType
from oddish.config import settings, DisabledEnvironmentError
from oddish.db import TrialStatus
from oddish.schemas import (
    QAEvalCreateRequest,
    QARunRequest,
    TaskSubmission,
    TaskSweepSubmission,
    TrialSpec,
)
from pydantic import ValidationError


@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        (TrialSpec, {"agent": "nop", "environment": "daytona"}),
        (
            TaskSweepSubmission,
            {"task_id": "t", "configs": [{"agent": "nop"}], "environment": "daytona"},
        ),
        (
            TaskSweepSubmission,
            {"task_id": "t", "configs": [{"agent": "nop", "environment": "daytona"}]},
        ),
        (
            TaskSweepSubmission,
            {
                "task_id": "t",
                "configs": [{"agent": "nop"}],
                "harbor": {"environment": {"type": "daytona"}},
            },
        ),
        (
            TaskSubmission,
            {"task_id": "t", "trials": [{"agent": "nop", "environment": "daytona"}]},
        ),
        (QARunRequest, {"environment": "daytona"}),
        (
            QAEvalCreateRequest,
            {
                "name": "test",
                "agent": "nop",
                "source_trial_ids": ["t"],
                "environment": "daytona",
            },
        ),
    ],
)
def test_submissions_reject_daytona(schema, payload):
    with pytest.raises(ValidationError, match="Daytona is disabled"):
        schema.model_validate(payload)


@pytest.mark.parametrize("environment", [None, "modal"])
def test_qa_keeps_supported_environment_choices(environment):
    assert QARunRequest(environment=environment).environment == environment


def test_inherited_daytona_is_rejected_without_substitution():
    from oddish.core.sweeps import build_trial_specs_from_sweep

    submission = TaskSweepSubmission(task_id="t", configs=[{"agent": "nop"}])
    with pytest.raises(HTTPException, match="Daytona is disabled") as exc:
        build_trial_specs_from_sweep(
            submission, default_environment=EnvironmentType.DAYTONA
        )
    assert exc.value.status_code == 400
    assert submission.environment is None


@pytest.mark.asyncio
async def test_retry_rejects_saved_daytona_before_mutation(monkeypatch):
    from oddish.core.endpoints import trials

    trial = SimpleNamespace(id="t", environment="daytona")
    monkeypatch.setattr(trials, "get_trial_for_org_core", AsyncMock(return_value=trial))
    session = SimpleNamespace(
        get=AsyncMock(return_value=trial), execute=AsyncMock(), add=Mock()
    )
    with pytest.raises(HTTPException, match="Daytona is disabled"):
        await trials.retry_trial_core(session, trial_id="t")
    session.execute.assert_not_awaited()
    session.add.assert_not_called()
    assert trial.environment == "daytona"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [TrialStatus.PENDING, TrialStatus.RETRYING])
@pytest.mark.parametrize("environment", ["daytona", None])
async def test_queued_daytona_fails_permanently_before_task_or_credentials(
    monkeypatch, status, environment
):
    from oddish.workers.queue import trial_handler

    monkeypatch.setattr(settings, "harbor_environment", "daytona")
    trial = SimpleNamespace(status=status, idempotency_key=None, agent="nop")
    prepared = trial_handler.PreparedTrialRun(
        task_path="/unused",
        task_s3_key=None,
        task_id="t",
        trial_agent="nop",
        trial_model=None,
        trial_environment=environment,
        trial_harbor_config=None,
    )

    @asynccontextmanager
    async def trial_session(*args, **kwargs):
        yield SimpleNamespace(), trial

    monkeypatch.setattr(trial_handler, "_trial_session", trial_session)
    monkeypatch.setattr(
        trial_handler, "prepare_trial_run", AsyncMock(return_value=prepared)
    )
    funding = AsyncMock()
    task = AsyncMock()
    settlement = AsyncMock()
    monkeypatch.setattr(trial_handler, "claim_funding_key", funding)
    monkeypatch.setattr(trial_handler, "_prepare_trial_task", task)
    monkeypatch.setattr(trial_handler, "settle_trial_attempt", settlement)
    await trial_handler.run_trial_job("t", "nop")
    funding.assert_not_awaited()
    task.assert_not_awaited()
    settlement.assert_awaited_once()
    execution = settlement.call_args.kwargs["execution"]
    assert execution.retryable is False
    assert execution.outcome is None
    assert "Daytona is disabled" in execution.execution_error
    assert prepared.trial_environment == environment


@pytest.mark.asyncio
async def test_direct_harbor_launch_stops_before_patching_or_job_creation(monkeypatch):
    from oddish.workers.harbor import runner

    patch = Mock()
    monkeypatch.setattr(runner, "apply_harbor_patches", patch)
    with pytest.raises(DisabledEnvironmentError):
        await runner.run_harbor_trial_async(
            Path("/unused"),
            "nop",
            Path("/unused/jobs"),
            environment=EnvironmentType.DAYTONA,
        )
    patch.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        {"environment": "daytona"},
        {"environment_config": {"type": "daytona"}},
    ],
)
def test_ephemeral_child_rejects_old_daytona_payloads(payload):
    from oddish.workers.harbor._entry import _build_job_config

    with pytest.raises(DisabledEnvironmentError):
        _build_job_config(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("image", ["image:latest", "snapshot:old-daytona-image"])
async def test_monitor_cannot_start_or_warm_daytona(monkeypatch, image):
    from oddish.workers.launcher.providers import daytona

    client = Mock(side_effect=AssertionError("must not contact Daytona"))
    monkeypatch.setattr(daytona, "_client", client)
    starter = daytona.DaytonaStarter()
    with pytest.raises(DisabledEnvironmentError):
        await starter.start(name="test", image=image, env={})
    with pytest.raises(DisabledEnvironmentError):
        await starter.build_warm_image(image=image, request=None)
    client.assert_not_called()


def test_cli_never_coerces_daytona_to_modal():
    from importlib import import_module

    run = import_module("oddish.cli.run")
    with pytest.raises(DisabledEnvironmentError):
        run._normalize_hosted_environment(EnvironmentType.DAYTONA, is_modal_api=True)


def test_daytona_lookup_remains_available_for_cleanup():
    from oddish.runtime.registry import get_backend
    from oddish.runtime.routing import allowed_cloud_environments

    assert get_backend("daytona").name == "daytona"
    assert EnvironmentType.DAYTONA not in allowed_cloud_environments()


@pytest.mark.asyncio
async def test_forced_daytona_monitor_fails_permanently_before_image_resolution(
    monkeypatch,
):
    from oddish.workers.launcher import launch
    from oddish.workers.queue.trial_handler import PreparedTrialRun

    monkeypatch.setattr(settings, "monitor_sandbox_starter", "daytona")
    prepared = PreparedTrialRun(
        task_path="/unused",
        task_s3_key=None,
        task_id="t",
        trial_agent="nop",
        trial_model=None,
        trial_environment="modal",
        trial_harbor_config=None,
    )
    monkeypatch.setattr(launch, "control_plane_url", lambda: "https://api.test")
    monkeypatch.setattr(launch, "prepare_trial_run", AsyncMock(return_value=prepared))
    monkeypatch.setattr(launch, "_still_owns", AsyncMock(return_value=True))
    monkeypatch.setattr(launch.monitor_token, "revoke_monitor_credential", AsyncMock())
    settlement = AsyncMock()
    resolve = AsyncMock()
    monkeypatch.setattr(launch, "settle_trial_attempt", settlement)
    monkeypatch.setattr(launch, "_resolve_image", resolve)
    job = SimpleNamespace(
        id="j",
        subject_id="t",
        worker_id="w",
        attempts=1,
        queue_slot=0,
        modal_function_call_id=None,
        reroute_from_environment=None,
    )
    assert await launch.launch_monitor_attempt(job) is None
    resolve.assert_not_awaited()
    settlement.assert_awaited_once()
    execution = settlement.call_args.kwargs["execution"]
    assert execution.retryable is False
    assert "Daytona is disabled" in execution.execution_error
