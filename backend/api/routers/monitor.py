"""Control-plane calls made by the in-sandbox trial monitor (oddish-mon).

Authenticated by the monitor's own attempt credential, never by a user API key.
Each route calls the function the long-running worker calls today.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from oddish.config import settings
from oddish.core.quota_pause import quota_pause_requested
from oddish.db import TrialModel, get_read_session
from oddish.db.storage import StorageClient, get_storage_client
from oddish.mon.contract import (
    MAX_EVENTS_PER_POST,
    ArtifactUploadResponse,
    AttemptSpec,
    ResultPayload,
)
from oddish.schemas import SkillFile
from oddish.worker.probe_staging import load_org_skills
from oddish.workers.harbor import live_tail_db
from oddish.workers.launcher import attempt as monitor_attempt
from oddish.workers.launcher import heartbeat as monitor_heartbeat
from oddish.workers.launcher import hook_events as monitor_lifecycle
from oddish.workers.launcher import result as monitor_result
from oddish.workers.launcher import token as monitor_token
from oddish.workers.launcher.token import MonitorAttempt
from oddish.workers.queue.worker_job_single_job import SandboxCapacityLeaseLostError
from org_access import require_execution_org
from pydantic import BaseModel, Field

router = APIRouter(prefix="/monitor", tags=["Trial monitor"])
logger = logging.getLogger(__name__)


async def authorize_monitor(request: Request) -> MonitorAttempt:
    credential = request.headers.get("authorization", "").removeprefix("Bearer ")
    try:
        return await monitor_token.authorize_monitor(credential)
    except monitor_token.MonitorUnauthorized as exc:
        raise HTTPException(401, str(exc)) from exc
    except monitor_token.MonitorGone as exc:
        raise HTTPException(410, str(exc)) from exc


Attempt = Annotated[MonitorAttempt, Depends(authorize_monitor)]


class HeartbeatRequest(BaseModel):
    pending_failure_count: int = Field(default=0, ge=0)
    pending_last_error: str | None = None


class HeartbeatResponse(BaseModel):
    ok: bool = True


class LiveEvent(BaseModel):
    seq: int = Field(ge=1)
    kind: str = Field(max_length=64)
    payload: dict


class UsageCheckpoint(BaseModel):
    input_tokens: int | None = None
    cache_tokens: int | None = None
    cache_write_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class EventsRequest(BaseModel):
    trial_attempt: int = Field(ge=0)
    events: list[LiveEvent] = Field(
        default_factory=list, max_length=MAX_EVENTS_PER_POST
    )
    checkpoint: UsageCheckpoint | None = None


class EventsResponse(BaseModel):
    flushed: int
    checkpoint_written: bool
    trial_finished: bool
    quota_checked: bool
    pause_requested: bool | None


class ResultResponse(BaseModel):
    accepted: bool = True
    duplicate: bool = False


class LifecycleRequest(BaseModel):
    # monitor_lifecycle.summarize_hook_event output.
    summary: dict


class ControlResponse(BaseModel):
    authorized: bool
    detail: str | None = None
    pause_requested: bool = False


class MonitorSkill(BaseModel):
    name: str
    files: list[SkillFile]


class SkillsResponse(BaseModel):
    skills: list[MonitorSkill]


@router.post("/heartbeat")
async def heartbeat(attempt: Attempt, body: HeartbeatRequest) -> HeartbeatResponse:
    try:
        still_owned = await monitor_heartbeat.record_monitor_heartbeat(
            attempt,
            pending_failure_count=body.pending_failure_count,
            pending_last_error=body.pending_last_error,
        )
    except SandboxCapacityLeaseLostError as exc:
        raise HTTPException(410, f"Capacity lease lost: {exc}") from exc
    if not still_owned:
        raise HTTPException(410, "This attempt no longer owns the job")
    return HeartbeatResponse()


@router.get("/control")
async def control(attempt: Attempt) -> ControlResponse:
    """Approval and quota-pause state, polled on the worker's 15 s cadence."""
    try:
        await require_execution_org(attempt.org_id)
    except HTTPException as exc:
        return ControlResponse(authorized=False, detail=str(exc.detail))
    pause_requested = False
    if attempt.trial_id is not None and attempt.org_id is not None:
        async with get_read_session() as session:
            trial = await session.get(TrialModel, attempt.trial_id)
            pause_requested = await quota_pause_requested(
                session,
                org_id=attempt.org_id,
                billed_user_id=trial.billed_user_id if trial else None,
            )
    return ControlResponse(authorized=True, pause_requested=pause_requested)


@router.post("/events")
async def events(attempt: Attempt, body: EventsRequest) -> EventsResponse:
    """One live-tail persist tick: transcript rows, usage checkpoint, quotas.

    Runs the same function the worker's tailer runs. The trial, org and billed
    user come from the credential and the trial row, never from the request.
    """
    if attempt.trial_id is None:
        raise HTTPException(409, "This attempt does not drive a trial")
    async with get_read_session() as session:
        trial = await session.get(TrialModel, attempt.trial_id)
    result = await live_tail_db.persist_tick_to_database(
        trial_id=attempt.trial_id,
        attempt=body.trial_attempt,
        events=[event.model_dump() for event in body.events],
        checkpoint_values=(
            body.checkpoint.model_dump() if body.checkpoint is not None else None
        ),
        org_id=attempt.org_id,
        billed_user_id=trial.billed_user_id if trial else None,
    )
    return EventsResponse(
        flushed=result.flushed,
        checkpoint_written=result.checkpoint_written,
        trial_finished=result.trial_finished,
        quota_checked=result.quota_checked,
        pause_requested=result.pause_requested,
    )


def _require_trial(attempt: MonitorAttempt) -> str:
    if attempt.trial_id is None:
        raise HTTPException(409, "This attempt does not drive a trial")
    return attempt.trial_id


@router.post("/artifacts")
async def artifact_upload(attempt: Attempt) -> ArtifactUploadResponse:
    """A presigned PUT for this attempt's own artifact archive."""
    archive_key = StorageClient._monitor_archive_key(
        _require_trial(attempt), attempt.worker_job_id, attempt.attempt
    )
    upload_url = await get_storage_client().get_presigned_upload_url(
        archive_key, content_type="application/gzip"
    )
    return ArtifactUploadResponse(
        upload_url=upload_url,
        upload_headers={"Content-Type": "application/gzip"},
        archive_key=archive_key,
    )


@router.post("/result")
async def result(
    attempt: Attempt, body: ResultPayload, background: BackgroundTasks
) -> ResultResponse:
    """Accept the outcome. The verdict is what the monitor has and the
    sandbox is about to go: an archive it says it uploaded but that storage
    does not show is recorded as not uploaded (settlement keeps the verdict
    and records the lost files), never refused. A refusal would have the
    monitor retry for about half an hour, then exit failed and the finished
    trial re-run."""
    archive_key: str | None = None
    if body.archive_uploaded:
        archive_key = StorageClient._monitor_archive_key(
            _require_trial(attempt), attempt.worker_job_id, attempt.attempt
        )
        if not await get_storage_client().object_exists(archive_key):
            logger.warning(
                "metric=monitor_result outcome=archive_missing job_id=%s attempt=%s "
                "key=%s: accepting the result without artifacts",
                attempt.worker_job_id,
                attempt.attempt,
                archive_key,
            )
            archive_key = None
    try:
        accepted = await monitor_result.accept_monitor_result(
            attempt,
            outcome=body.outcome,
            archive_key=archive_key,
            execution_error=body.execution_error,
            retryable=body.retryable,
            monitor_stats=(
                body.monitor_stats.model_dump() if body.monitor_stats else None
            ),
        )
    except monitor_token.MonitorGone as exc:
        raise HTTPException(410, str(exc)) from exc
    if accepted == "accepted" and settings.monitor_settle_inline:
        from oddish.workers.launcher.settle import settle_monitor_result

        # The result is durable, so a crash here loses nothing: the reconciler
        # re-drives settlement for any accepted result.
        background.add_task(
            settle_monitor_result, attempt.worker_job_id, attempt=attempt.attempt
        )
    return ResultResponse(duplicate=accepted == "duplicate")


@router.post("/lifecycle")
async def lifecycle(attempt: Attempt, body: LifecycleRequest) -> HeartbeatResponse:
    """A Harbor lifecycle event, applied by the worker's own hook handler."""
    _require_trial(attempt)
    try:
        await monitor_lifecycle.apply_lifecycle_event(attempt, body.summary)
    except (KeyError, ValueError) as exc:
        raise HTTPException(422, f"Invalid lifecycle event: {exc}") from exc
    return HeartbeatResponse()


@router.get("/attempt")
async def attempt_spec(attempt: Attempt) -> AttemptSpec:
    """What this attempt has to run. No secrets: those arrive in the environment."""
    # Fetching the spec with the monitor's own credential is its check-in,
    # recorded before the spec is built: listing and presigning the task's
    # objects takes long enough for a reconciler pass at the launch deadline
    # to find no check-in and mark the attempt gone.
    await monitor_attempt.acknowledge_monitor(attempt)
    try:
        return await monitor_attempt.build_attempt_spec(attempt)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/skills")
async def selected_skills(attempt: Attempt) -> SkillsResponse:
    """The trial's selected skills, loaded as the worker's ``stage_org_skills``
    loads them. The monitor has no database to read them from."""
    async with get_read_session() as session:
        trial = await session.get(TrialModel, _require_trial(attempt))
    skill_ids = ((trial.harbor_config if trial else None) or {}).get("skill_ids")
    if trial is None or trial.org_id is None or not skill_ids:
        return SkillsResponse(skills=[])
    bundles = await load_org_skills(org_id=trial.org_id, skill_ids=skill_ids)
    return SkillsResponse(
        skills=[
            MonitorSkill(
                name=bundle.name,
                files=[
                    SkillFile(relative_path=path, content=content)
                    for path, content in bundle.files
                ],
            )
            for bundle in bundles
        ]
    )
