"""Launch an attempt into a monitor sandbox and hand the job off.

Launch succeeded is not trial finished. This claims the trial exactly as the
worker does, starts a sandbox whose entrypoint is the monitor, and returns a
handoff as soon as that sandbox exists. The job stays RUNNING under this
worker's id and keeps its slot and capacity lease; settlement releases them.
From the hand-off the reconciler owns the attempt: a monitor that never checks
in goes stale and is asked for (``monitor_status_check``), and the launch
deadline bounds it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from collections.abc import Awaitable, Callable

from sqlalchemy import exc as sa_exc
from sqlalchemy import literal, select, update
from sqlalchemy.dialects.postgresql import JSONB

from oddish.config import api_base_url_for_modal_app, settings
from oddish.db import WorkerJobModel, WorkerJobStatus, get_session
from oddish.db.models import utcnow
from oddish.db.storage import resolve_task_directory
from oddish.workers.console import console
from oddish.workers.harbor.runner import (
    capture_sandbox_resources,
    capture_verifier_resources,
)
from oddish.workers.jobs.registry import JobOutcome
from oddish.workers.launcher import costs as monitor_costs
from oddish.workers.launcher import image_cache as monitor_image_cache
from oddish.workers.launcher import launch_env
from oddish.workers.launcher import token as monitor_token
from oddish.workers.launcher.attempt import warm_source_for_trial
from oddish.workers.launcher.heartbeat import record_monitor_heartbeat
from oddish.workers.launcher.providers import base as monitor_sandbox
from oddish.workers.launcher.settle import CAPACITY_PAYLOAD_KEY
from oddish.workers.queue import judge_keys as verifier_secrets
from oddish.workers.queue.trial_handler import (
    TrialExecutionResult,
    begin_judge_costs_if_enabled,
    claim_funding_key,
    prepare_trial_run,
    settle_compute_costs,
    settle_trial_attempt,
)
from oddish.workers.queue.worker_job_single_job import ClaimedWorkerJob

logger = logging.getLogger(__name__)

# Optional, passed by the caller: (task_id, task_version) -> image URL built
# once outside the sandbox (or None). Without it the sandbox builds the task
# image itself, as compose tasks do today.
ImageResolver = Callable[[str, int], Awaitable[str | None]]


async def _resolve_image(
    job: ClaimedWorkerJob,
    task_id: str,
    version: int | None,
    resolver: ImageResolver | None,
) -> None:
    """Record the prebuilt image on the job row; the attempt spec hands it over."""
    if resolver is None or version is None:
        return
    image = await resolver(task_id, version)
    if not image:
        return
    # One fenced statement: a reaped or re-claimed row is left alone (rowcount
    # 0), and the launch's next fenced write or the pre-start check ends it.
    async with get_session() as session:
        await session.execute(
            update(WorkerJobModel)
            .where(*_owned_by(job))
            .values(
                payload=WorkerJobModel.payload.op("||")(
                    literal({"monitor_docker_image": image}, JSONB)
                )
            )
        )


def _owned_by(job: ClaimedWorkerJob) -> tuple:
    """The row is this attempt's: RUNNING, at this attempt, under this worker."""
    return (
        WorkerJobModel.id == job.id,
        WorkerJobModel.status == WorkerJobStatus.RUNNING,
        WorkerJobModel.attempts == job.attempts,
        WorkerJobModel.current_worker_id == job.worker_id,
    )


async def _still_owns(job: ClaimedWorkerJob) -> bool:
    """One read: is the row still RUNNING under this attempt and worker?"""
    async with get_session() as session:
        row = (
            await session.execute(
                select(
                    WorkerJobModel.status,
                    WorkerJobModel.attempts,
                    WorkerJobModel.current_worker_id,
                ).where(WorkerJobModel.id == job.id)
            )
        ).one_or_none()
    return (
        row is not None
        and row.status == WorkerJobStatus.RUNNING
        and row.attempts == job.attempts
        and row.current_worker_id == job.worker_id
    )


_HEARTBEAT_SECONDS = 30.0
# settings.monitor_ack_timeout_seconds is no longer read here: the reconciler's
# launch deadline (monitor_launch_deadline_seconds) replaced the check-in wait.


class MonitorLaunchOwnershipLost(Exception):
    """A beat found the job no longer RUNNING under this attempt (reaped or
    re-claimed). Its token and trial belong to the newer attempt now."""


# Database errors the launch beater treats as a missed beat: the asyncpg
# connect timeout (a bare asyncio.TimeoutError, also raised as the built-in
# TimeoutError), a dropped connection, and SQLAlchemy's connection-level
# failures. Anything else (a lost capacity lease included) fails the launch
# before create, and the job, still ours, settles as a failed attempt.
_TRANSIENT_DB_ERRORS: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,
    TimeoutError,
    ConnectionError,
    sa_exc.OperationalError,
    sa_exc.InterfaceError,
    sa_exc.TimeoutError,
)


async def _persist_handle(
    job: ClaimedWorkerJob, provider: str, external_id: str
) -> bool:
    """Record the sandbox on the job row; ``False`` when the row is no longer
    this attempt's RUNNING job, so nothing recorded the sandbox."""
    values: dict = dict(
        provider=provider, external_id=external_id, sandbox_creating_at=utcnow()
    )
    if job.capacity_slot is not None:
        # The handed-off worker keeps its capacity lease; settlement, which
        # has only this row, releases it.
        values["payload"] = WorkerJobModel.payload.op("||")(
            literal(
                {
                    CAPACITY_PAYLOAD_KEY: {
                        "provider": job.capacity_provider,
                        "slot": job.capacity_slot,
                    }
                },
                JSONB,
            )
        )
    async with get_session() as session:
        result = await session.execute(
            update(WorkerJobModel).where(*_owned_by(job)).values(**values)
        )
    return result.rowcount == 1


async def _hand_off(job: ClaimedWorkerJob) -> bool:
    """The hand-off itself, fenced like every other write of this launch: the
    row is stamped handed off only while it is still this attempt's RUNNING
    job. ``False`` means the job was lost (reaped or re-claimed) after the
    handle was recorded; the newer claim cleared the handle, so nothing but
    this launch knows the sandbox."""
    async with get_session() as session:
        result = await session.execute(
            update(WorkerJobModel)
            .where(*_owned_by(job))
            .values(monitor_handed_off_at=utcnow())
        )
    return result.rowcount == 1


def control_plane_url() -> str:
    """Same resolution probe sandboxes use, with the monitor's own override first."""
    return (
        settings.monitor_api_url
        or os.environ.get("ODDISH_PUBLIC_API_BASE_URL")
        or api_base_url_for_modal_app()
    )


async def _start_from_cache(
    starter, job: ClaimedWorkerJob, image: str, cache_key: str | None, env: dict, size
) -> str:
    """Start from the resolved image. When that image is a cached snapshot the
    provider no longer has (expired, deleted), drop the entry and start cold,
    exactly as without a cache. Any other failure is the launch's own failure:
    the snapshot stays for the launches still booting from it."""
    try:
        return await _start(starter, job, image, env, size)
    except Exception as exc:
        if cache_key is None:
            raise
        unusable = isinstance(exc, monitor_sandbox.WarmImageUnusable)
        if not unusable and not await monitor_image_cache.image_is_gone(starter, image):
            raise
        logger.info(
            "cached image %s is gone on %s; starting cold", image, starter.provider
        )
        await monitor_image_cache.forget(starter, cache_key)
        return await _start(starter, job, settings.monitor_image, env, size)


async def _start(
    starter,
    job: ClaimedWorkerJob,
    image: str,
    env: dict,
    size: monitor_sandbox.SandboxSize | None,
) -> str:
    return await starter.start(
        name=f"oddish-mon-{job.id}-{job.attempts}", image=image, env=env, size=size
    )


async def _task_facts(job: ClaimedWorkerJob, prepared, provider: str):
    """Read what the launcher needs from the task's own files, with the same
    functions the worker path uses: the agent's and the verifier's resources,
    and the task's opt-in to verifier judge costs (started here)."""
    task_path, temp_dir, _ = await resolve_task_directory(
        task_id=prepared.task_id,
        task_s3_key=prepared.task_s3_key,
        task_path=prepared.task_path,
    )
    try:
        await begin_judge_costs_if_enabled(
            trial_id=job.subject_id,
            prepared_trial=prepared,
            task_path=task_path,
            worker_id=job.worker_id,
            worker_job_id=job.id,
        )
        return (
            capture_sandbox_resources(
                task_path, prepared.trial_harbor_config, provider
            ),
            capture_verifier_resources(
                task_path, prepared.trial_harbor_config, provider
            ),
            # Only the judge keys this task's env blocks interpolate; empty for
            # a task that references none, so its sandbox stays secret-free.
            verifier_secrets.judge_env_for_task(task_path, settings),
        )
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


async def _close_sandbox_span_after_failed_launch(
    job: ClaimedWorkerJob, trial_id: str, prepared
) -> None:
    """Close the agent-sandbox span a failed launch opened, as settlement does
    for a stopped sandbox. Best effort: accounting never fails an attempt."""
    try:
        async with get_session() as session:
            row = await session.get(WorkerJobModel, job.id)
            payload = row.payload if row is not None else None
        state = monitor_costs.cost_state_for_settle(
            payload=payload,
            job_id=job.id,
            job_attempt=job.attempts,
            trial_id=trial_id,
            prepared=prepared,
        )
        if state is not None:
            await settle_compute_costs(state, None)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "could not close the cost span for job=%s after a failed launch: %s",
            job.id,
            exc,
        )


async def fail_launch(
    exc: Exception,
    *,
    job: ClaimedWorkerJob,
    trial_id: str,
    prepared,
    starter,
    external_id: str | None,
    span_opened: bool,
    launch_started: float,
) -> None:
    """Stop what this launch started and settle the attempt as failed. A launch
    that no longer owns its job (lost before create, or reaped and re-claimed
    while it ran) leaves the token and the trial, which belong to the newer
    attempt, alone."""
    logger.exception("monitor launch failed for job=%s", job.id)
    if external_id is not None:
        try:
            await starter.stop(external_id)
        except Exception:  # noqa: BLE001 - the attempt must still settle
            logger.warning(
                "could not stop sandbox %s after a failed launch", external_id
            )
    if span_opened:
        await _close_sandbox_span_after_failed_launch(job, trial_id, prepared)
    ownership_lost = isinstance(exc, MonitorLaunchOwnershipLost)
    if not ownership_lost:
        # Re-verify once, right before the writes that assume this attempt:
        # the launch may have outlived the stale-heartbeat reap.
        ownership_lost = not await _still_owns(job)
    console.print(
        f"metric=monitor_launch "
        f"outcome={'ownership_lost' if ownership_lost else 'failed'} "
        f"job_id={job.id} trial_id={trial_id} attempt={job.attempts} "
        f"provider={starter.provider} external_id={external_id} "
        f"error_type={type(exc).__name__} "
        f"seconds={time.monotonic() - launch_started:.1f}"
    )
    if ownership_lost:
        return
    await monitor_token.revoke_monitor_credential(job.id, attempt=job.attempts)
    await settle_trial_attempt(
        trial_id=trial_id,
        prepared_trial=prepared,
        execution=TrialExecutionResult(
            outcome=None,
            execution_error=f"Monitor launch failed: {type(exc).__name__}: {exc}",
            retryable=True,
        ),
        worker_id=job.worker_id,
        worker_job_id=job.id,
    )


async def _after_cancelled_launch(
    *,
    job: ClaimedWorkerJob,
    trial_id: str,
    prepared,
    starter,
    external_id: str | None,
) -> None:
    """Best-effort teardown when the launcher is cancelled mid-launch: stop the
    sandbox we created, close its cost span and revoke this attempt's
    credential. Never raises an error; a cancel that lands during the teardown
    is re-raised only after all three steps ran."""
    console.print(
        "metric=monitor_launch outcome=cancelled "
        f"job_id={job.id} trial_id={trial_id} attempt={job.attempts} "
        f"provider={getattr(starter, 'provider', None)} external_id={external_id}"
    )

    async def _teardown() -> None:
        if external_id is not None:
            try:
                await starter.stop(external_id)
            except Exception:  # noqa: BLE001 - the provider timeout is the backstop
                logger.warning(
                    "could not stop sandbox %s after a cancelled launch", external_id
                )
            # The cancel can land inside open_sandbox_span, after the span
            # opened: close whatever the payload says was opened (nothing, if
            # nothing was).
            try:
                await _close_sandbox_span_after_failed_launch(job, trial_id, prepared)
            except Exception:  # noqa: BLE001 - it logs its own failures
                pass
        try:
            await monitor_token.revoke_monitor_credential(job.id, attempt=job.attempts)
        except Exception:  # noqa: BLE001 - the reconciler revokes finished jobs' tokens
            logger.warning(
                "could not revoke the credential of job=%s after a cancel", job.id
            )

    # One task, shielded as a whole: a cancel of the caller mid-teardown must
    # not abort the steps after the one it interrupted.
    teardown = asyncio.create_task(_teardown())
    cancelled = False
    while not teardown.done():
        try:
            await asyncio.shield(teardown)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


async def launch_monitor_attempt(
    job: ClaimedWorkerJob,
    registry_credentials: list | None = None,
    *,
    image_resolver: ImageResolver | None = None,
) -> JobOutcome | None:
    """Returns a handoff, or ``None`` when the trial settled here instead
    (nothing to run, or the launch failed and was settled as a failed attempt)."""
    api_url = control_plane_url()
    if not api_url:
        raise RuntimeError(
            "No control-plane URL for the monitor: set ODDISH_MONITOR_API_URL"
        )
    trial_id = job.subject_id
    prepared = await prepare_trial_run(
        trial_id=trial_id,
        worker_id=job.worker_id,
        queue_slot=job.queue_slot,
        modal_function_call_id=job.modal_function_call_id,
        fallback_from_environment=job.reroute_from_environment,
    )
    if prepared is None:
        return None

    provider = monitor_sandbox.provider_for_environment(prepared.trial_environment)
    if provider is None:  # the handler's gate keeps these on the worker path
        raise RuntimeError(
            f"No monitor sandbox starter for {prepared.trial_environment!r}"
        )
    starter = monitor_sandbox.get_starter(provider)
    external_id: str | None = None
    span_opened = False
    launch_started = time.monotonic()

    # The heartbeat identity is known at claim time. Beat from here so the
    # worker-job row stays fresh while the image resolves, the warm snapshot
    # builds and the sandbox boots. Without this the row's heartbeat_at is left
    # at claim time and the reaper kills a healthy launch whose image work runs
    # past the stale threshold (seen on tasks whose image takes >15 min).
    launch_attempt = monitor_token.MonitorAttempt(
        job.id,
        job.attempts,
        job.worker_id,
        trial_id=trial_id,
        queue_slot=job.queue_slot,
    )

    # Only a False beat means the job is gone; any other beat failure is
    # recorded and fails the launch of a job this worker still owns.
    ownership_lost = False
    beat_error: Exception | None = None

    async def _heartbeat_during_launch() -> None:
        nonlocal ownership_lost, beat_error
        while True:
            try:
                owned = await record_monitor_heartbeat(launch_attempt)
            except _TRANSIENT_DB_ERRORS:  # a missed beat must not fail launch
                logger.warning("launch heartbeat failed for job=%s", job.id)
            except Exception as exc:  # noqa: BLE001 - re-raised before create
                # Anything else (a lost capacity lease included) is real: this
                # launch must not go on to create, and settles as failed.
                logger.warning(
                    "launch heartbeat failed for job=%s: %s: %s",
                    job.id,
                    type(exc).__name__,
                    exc,
                )
                beat_error = exc
                return
            else:
                if owned is False:
                    # Not cancelled mid-call: a provider create cut short could
                    # leave a sandbox nobody knows. The launch checks the flag
                    # once, right before create.
                    ownership_lost = True
                    return
            await asyncio.sleep(_HEARTBEAT_SECONDS)

    launch_heartbeat = asyncio.create_task(_heartbeat_during_launch())
    try:
        await _resolve_image(
            job, prepared.task_id, prepared.task_version, image_resolver
        )
        # The user's own model key, and which key funds this attempt.
        owned, byok_env = await claim_funding_key(
            trial_id=trial_id, prepared_trial=prepared, worker_id=job.worker_id
        )
        if not owned:
            return None  # superseded, as on the worker path
        resources, verifier_resources, judge_env = await _task_facts(
            job, prepared, provider
        )
        size = monitor_sandbox.SandboxSize.from_span_resources(
            resources,
            verifier=verifier_resources,
            memory_headroom_mb=settings.monitor_memory_headroom_mb,
        )
        if size is not None:
            # Price what is really provisioned: provider rounding included.
            resources = starter.provisioned(size).billed_as(resources)
        credential = await monitor_token.mint_monitor_credential(job.id, job.attempts)
        env = launch_env.build_sandbox_env(
            prepared,
            api_url=api_url,
            credential=credential,
            byok_env=byok_env,
            judge_env=judge_env,
            registry_credentials=registry_credentials,
        )
        image, cache_key = await monitor_image_cache.resolve(
            starter,
            monitor_image=settings.monitor_image,
            warm_source=await warm_source_for_trial(trial_id, job.id),
            has_registry_login=bool(registry_credentials),
            size=size,
        )
        if ownership_lost:
            raise MonitorLaunchOwnershipLost(
                f"job {job.id} attempt {job.attempts} no longer owns the run"
            )
        if beat_error is not None:
            raise beat_error
        external_id = await _start_from_cache(starter, job, image, cache_key, env, size)
        if not await _persist_handle(job, starter.provider, external_id):
            # Lost between the pre-start check and create: no row names this
            # sandbox, so the failure path stops it; no span was opened.
            raise MonitorLaunchOwnershipLost(
                f"job {job.id} attempt {job.attempts} lost the run at create"
            )
        await monitor_costs.open_sandbox_span(
            job_id=job.id,
            job_attempt=job.attempts,
            worker_id=job.worker_id,
            trial_id=trial_id,
            prepared=prepared,
            provider=str(starter.provider),
            external_id=external_id,
            resources=resources,
            verifier_resources=verifier_resources,
        )
        span_opened = True
        # The sandbox exists: hand off, fenced. A re-claim since the handle
        # was recorded means no row names our sandbox: the failure path stops
        # it and closes the span. The beater stops below, so from here the
        # monitor's own heartbeat keeps the row fresh; one that never checks
        # in goes stale and the reconciler asks the provider.
        if not await _hand_off(job):
            raise MonitorLaunchOwnershipLost(
                f"job {job.id} attempt {job.attempts} lost the run at hand-off"
            )
        console.print(
            "metric=monitor_launch outcome=handed_off "
            f"job_id={job.id} trial_id={trial_id} attempt={job.attempts} "
            f"provider={starter.provider} external_id={external_id} "
            f"cpu={size.cpu if size else 'default'} "
            f"memory_mb={size.memory_mb if size else 'default'} "
            f"warm={cache_key is not None} "
            f"seconds={time.monotonic() - launch_started:.1f}"
        )
        return JobOutcome.handed_off()
    except asyncio.CancelledError:
        # The worker is going away before the hand-off: nothing recorded the
        # sandbox as handed off, so its slot and lease will be released by the
        # caller's cleanup. Do not leave the sandbox, its cost span and its
        # credential live behind that release; the reconciler's launch deadline
        # is the backstop.
        await _after_cancelled_launch(
            job=job,
            trial_id=trial_id,
            prepared=prepared,
            starter=starter,
            external_id=external_id,
        )
        raise
    except Exception as exc:
        await fail_launch(
            exc,
            job=job,
            trial_id=trial_id,
            prepared=prepared,
            starter=starter,
            external_id=external_id,
            span_opened=span_opened,
            launch_started=launch_started,
        )
        return None
    finally:
        # Stop the background beater on every exit (early return, handoff or
        # error). Cancelling an already-finished task is a no-op.
        launch_heartbeat.cancel()
        try:
            await launch_heartbeat
        except asyncio.CancelledError:
            pass
