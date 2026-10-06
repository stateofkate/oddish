"""Settle a trial whose result the in-sandbox monitor reported.

Runs the worker's own settlement on the accepted result: the artifact archive
is unpacked and uploaded in today's layout, the trial is settled by
``settle_trial_attempt``, the worker-job outcome comes from the trial handler,
and ``record_outcome`` records it. The attempt context comes from
``load_trial_context`` on the observed attempt and its pinned task version.
Then the token is revoked and the queue slot and the capacity lease the
launcher recorded are released.

Built from the ``worker_jobs`` row alone, so it can run after the sandbox is
gone and can be re-run if interrupted: a job that is no longer RUNNING is
already settled, and every step before that overwrites or no-ops. The revoke
and release after it are repeated on a re-run, since both no-op once done.
"""

from __future__ import annotations

import logging
import tarfile
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import fields
from pathlib import Path
from typing import Literal

from sqlalchemy import update

from oddish.core.trial_artifacts import validate_uploaded_analysis_artifacts
from oddish.db import (
    TrialModel,
    WorkerJobModel,
    WorkerJobStatus,
    get_session,
)
from oddish.db.storage import get_storage_client
from oddish.workers.analysis_trials import ANALYSIS_ARTIFACTS, is_analysis_kind
from oddish.workers.console import console
from oddish.workers.harbor.outcome import HarborOutcome
from oddish.workers.jobs import ensure_builtin_handlers_registered
from oddish.workers.jobs.registry import get_handler
from oddish.workers.launcher import costs as monitor_costs
from oddish.workers.launcher import token as monitor_token
from oddish.workers.queue.sandbox_capacity import release_sandbox_capacity_lease
from oddish.workers.queue.slots import release_queue_slot
from oddish.workers.queue.trial_handler import (
    TrialExecutionResult,
    mirror_to_sauron,
    settle_compute_costs,
    settle_trial_attempt,
)
from oddish.workers.queue.trial_steps import (
    PreparedTrialRun,
    artifact_subprefix,
    load_trial_context,
)
from oddish.workers.queue.worker_job_single_job import (
    ClaimedWorkerJob,
    record_outcome,
    resolve_baseline_gate_after_outcome,
)

logger = logging.getLogger(__name__)

SettleOutcome = Literal["settled", "already_settled", "no_result"]
# Payload key where the launcher records the sandbox capacity lease a handed-off
# attempt holds ({"provider": ..., "slot": ...}); settlement releases it.
CAPACITY_PAYLOAD_KEY = "monitor_capacity"
# Set in ``worker_jobs.monitor_result`` once the attempt's trial write is done.
TRIAL_SETTLED_KEY = "trial_settled"
PostSuccessHook = Callable[[str], Awaitable[None]]


def _outcome_from_payload(
    payload: dict | None, job_dir: Path | None
) -> HarborOutcome | None:
    if payload is None:
        return None
    # The local paths are ours to set below; a payload that carries them
    # (a monitor built from the same dataclass) must not collide with them.
    known = {f.name for f in fields(HarborOutcome)} - {"job_dir", "job_result_path"}
    values = {k: v for k, v in payload.items() if k in known}
    result_path = job_dir / "result.json" if job_dir is not None else None
    return HarborOutcome(
        **values,
        job_dir=job_dir,
        job_result_path=result_path if result_path and result_path.exists() else None,
    )


def _skip_unsafe_members(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
    """The ``data`` filter, but a member it rejects is skipped instead of
    failing the whole unpack: a task's own symlink to an absolute path (Lean's
    ``.lake/packages``) is never a trial output and must not cost the trial
    its artifacts."""
    try:
        return tarfile.data_filter(member, path)
    except tarfile.FilterError:
        logger.warning("skipped unsafe archive member %s", member.name)
        return None


async def _unpack_archive(archive_key: str, dest: Path) -> Path:
    archive = dest / "archive.tar.gz"
    await get_storage_client().download_file(archive_key, archive)
    job_dir = dest / "job"
    job_dir.mkdir()
    with tarfile.open(archive, mode="r:gz") as tar:
        tar.extractall(job_dir, filter=_skip_unsafe_members)
    return job_dir


async def release_after_attempt(
    job_id: str,
    attempt: int,
    *,
    queue_key: str,
    slot: int | None,
    worker_id: str | None,
    payload: dict | None,
) -> None:
    """Revoke the attempt's token and free the queue slot and capacity lease
    its hand-off kept. Whoever ends a monitor attempt calls this: settlement,
    and the stale reap (for an attempt that never reported). Only after the
    outcome write and never before it (a free slot with the row still
    RUNNING lets the queue exceed its limit). All are no-ops once done: the
    revoke matches only this attempt's unrevoked token, each release only a
    slot this worker still holds. A failure in one never skips the others."""
    capacity = (payload or {}).get(CAPACITY_PAYLOAD_KEY) or {}
    try:
        await monitor_token.revoke_monitor_credential(job_id, attempt=attempt)
    except Exception:
        logger.exception("could not revoke the monitor token for job=%s", job_id)
    finally:
        try:
            if worker_id and slot is not None:
                try:
                    await release_queue_slot(
                        queue_key=queue_key, slot=slot, worker_id=worker_id
                    )
                except Exception:
                    logger.exception(
                        "could not release the queue slot for job=%s", job_id
                    )
        finally:
            if (
                worker_id
                and capacity.get("provider")
                and capacity.get("slot") is not None
            ):
                try:
                    await release_sandbox_capacity_lease(
                        provider=capacity["provider"],
                        slot=int(capacity["slot"]),
                        worker_id=worker_id,
                    )
                except Exception:
                    logger.exception(
                        "could not release the capacity lease for job=%s", job_id
                    )


_post_success_hook: PostSuccessHook | None = None


def set_post_success_hook(hook: PostSuccessHook | None) -> None:
    """The host registers the hook the worker path fires after a trial job
    succeeds (hosted: the GitHub notification), so both paths share it."""
    global _post_success_hook
    _post_success_hook = hook


async def settle_monitor_result(worker_job_id: str, *, attempt: int) -> SettleOutcome:
    """Settle the result of ``attempt``, the attempt the caller observed. A
    newer attempt owning the row means this result is history: do nothing."""
    async with get_session() as session:
        job = await session.get(WorkerJobModel, worker_job_id)
        if job is None or job.attempts != attempt:
            return "already_settled"
        if job.status != WorkerJobStatus.RUNNING:
            # A settle that crashed after the outcome write left its revoke and
            # release undone. Both are no-ops once done, so finish them here,
            # but only for this attempt's accepted result.
            if job.monitor_result_accepted_at is not None and (
                (job.monitor_result or {}).get("attempt") == attempt
            ):
                await release_after_attempt(
                    job.id,
                    attempt,
                    queue_key=job.queue_key,
                    slot=job.current_queue_slot,
                    worker_id=job.current_worker_id,
                    payload=job.payload,
                )
            return "already_settled"
        if job.monitor_result_accepted_at is None or not job.subject_id:
            return "no_result"
        result = dict(job.monitor_result or {})
        if result.get("attempt") != attempt:
            return "no_result"
        sandbox_provider, sandbox_id = job.provider, job.external_id
        trial = await session.get(TrialModel, job.subject_id)
        if trial is None:
            return "no_result"
        # A settle interrupted between the trial write and the outcome write
        # left this job RUNNING. Its upload and trial write already ran, so
        # only the outcome write is left to do: repeating the upload is what
        # kept the re-run from ever finishing. The trial's own status cannot
        # tell: Harbor's END lifecycle event marks it terminal before the
        # monitor reports, so only this attempt's settle mark counts.
        trial_already_settled = bool(result.get(TRIAL_SETTLED_KEY))
        # The observed attempt's context, read from its pinned task version.
        # Never the attempt starter: that would restart the attempt.
        prepared = (
            None if trial_already_settled else await load_trial_context(session, trial)
        )
        claimed = ClaimedWorkerJob(
            id=job.id,
            kind=job.kind,
            queue_key=job.queue_key,
            subject_table=job.subject_table,
            subject_id=job.subject_id,
            payload=dict(job.payload or {}),
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            org_id=job.org_id,
            parent_job_id=job.parent_job_id,
            worker_id=job.current_worker_id,
            queue_slot=job.current_queue_slot,
        )

    trial_id, worker_id = claimed.subject_id, claimed.worker_id
    if prepared is None:
        harbor_outcome = _outcome_from_payload(result.get("outcome"), None)
        upload_error = None
    else:
        harbor_outcome, upload_error = await _settle_trial(claimed, prepared, result)
        await _mark_trial_settled(claimed.id, attempt, result)

    ensure_builtin_handlers_registered()
    outcome = await get_handler(claimed.kind).outcome_for_settled_trial(
        claimed, harbor_outcome
    )
    persisted = await record_outcome(
        job_id=claimed.id,
        worker_id=worker_id,
        outcome=outcome,
        attempts=claimed.attempts,
        max_attempts=claimed.max_attempts,
        kind=claimed.kind,
        subject_table=claimed.subject_table,
        subject_id=claimed.subject_id,
    )
    console.print(
        "metric=monitor_settle outcome=settled "
        f"job_id={claimed.id} trial_id={trial_id} attempt={claimed.attempts} "
        f"job_status={getattr(persisted, 'value', persisted)} "
        f"had_archive={bool(result.get('archive_key'))} "
        f"upload_error={upload_error is not None} "
        f"trial_already_settled={trial_already_settled}"
    )
    await resolve_baseline_gate_after_outcome(
        kind=claimed.kind,
        queue_key=claimed.queue_key,
        subject_table=claimed.subject_table,
        subject_id=claimed.subject_id,
        persisted_status=persisted,
    )
    # The monitor has reported and exited; make sure its sandbox is gone too.
    if sandbox_provider and sandbox_id:
        try:
            from oddish.workers.launcher.providers import base as monitor_sandbox

            await monitor_sandbox.get_starter(sandbox_provider).stop(sandbox_id)
        except Exception:  # noqa: BLE001 - the provider timeout is the backstop
            logger.warning("could not stop monitor sandbox %s", sandbox_id)
    await release_after_attempt(
        claimed.id,
        attempt,
        queue_key=claimed.queue_key,
        slot=claimed.queue_slot,
        worker_id=worker_id,
        payload=claimed.payload,
    )
    hook = _post_success_hook
    # ``persisted`` is set for the one caller whose outcome write won, so two
    # settles of the same result fire the hook once.
    if persisted and outcome.success is not None and hook is not None:
        try:
            await hook(trial_id)
        except Exception:
            logger.exception("post-success hook failed for job=%s", claimed.id)
    # Only once its artifacts are uploaded: after a failed unpack or upload the
    # archive is the only copy left. A re-run that skipped the upload cannot
    # tell whether the interrupted one uploaded, so it keeps the archive.
    if prepared is not None and result.get("archive_key") and upload_error is None:
        try:
            await get_storage_client().delete_object(result["archive_key"])
        except Exception:  # noqa: BLE001 - staging object; harmless if it lingers
            logger.warning("could not delete the staging archive for %s", trial_id)
    return "settled"


async def _mark_trial_settled(job_id: str, attempt: int, result: dict) -> None:
    """Record on the job that this attempt's trial write finished, so a re-run
    after an interrupted outcome write skips the upload and trial write.

    Best effort: without the mark a re-run repeats the full settle, which is
    correct, only slower, so a failed write must not stop the outcome write.
    """
    try:
        async with get_session() as session:
            await session.execute(
                update(WorkerJobModel)
                .where(
                    WorkerJobModel.id == job_id,
                    WorkerJobModel.attempts == attempt,
                    WorkerJobModel.status == WorkerJobStatus.RUNNING,
                )
                .values(monitor_result={**result, TRIAL_SETTLED_KEY: True})
            )
    except Exception:  # noqa: BLE001 - the full re-settle is the fallback
        logger.exception("could not mark the trial settled for job=%s", job_id)


async def _settle_trial(
    claimed: ClaimedWorkerJob, prepared: PreparedTrialRun, result: dict
) -> tuple[HarborOutcome | None, str | None]:
    """Upload the attempt's artifacts and settle the trial row.

    Returns the Harbor outcome and the artifact upload error, if any.
    """
    trial_id, worker_id = claimed.subject_id, claimed.worker_id
    with tempfile.TemporaryDirectory(prefix="oddish-settle-") as tmp:
        job_dir: Path | None = None
        trial_s3_key: str | None = None
        upload_error: str | None = None
        if result.get("archive_key"):
            try:
                job_dir = await _unpack_archive(result["archive_key"], Path(tmp))
                storage = get_storage_client()
                trial_s3_key = await storage.upload_trial_results(
                    trial_id,
                    job_dir,
                    subprefix=artifact_subprefix(
                        prepared.trial_kind, prepared.trial_attempt
                    ),
                )
                # As the worker path does right after its upload: an analysis
                # trial's artifacts must be readable before settlement
                # publishes them. The key stays; the error records why.
                if result.get("outcome") is not None and is_analysis_kind(
                    prepared.trial_kind
                ):
                    await validate_uploaded_analysis_artifacts(
                        trial_id=trial_id,
                        trial_s3_key=trial_s3_key,
                        required_artifact=ANALYSIS_ARTIFACTS[prepared.trial_kind],
                        has_trajectory=bool(result["outcome"].get("has_trajectory")),
                        storage=storage,
                    )
            except Exception as exc:
                action = (
                    "Uploaded trial artifacts failed validation"
                    if trial_s3_key is not None
                    else "Failed to upload trial results to S3"
                )
                upload_error = f"{action}: {type(exc).__name__}: {exc}"
                logger.exception("monitor artifact unpack/upload failed")
        else:
            # The monitor's own upload was refused (or it had nothing to
            # upload). As on the worker path, _store_trial_results fails an
            # analysis trial retryably and records the loss on any other.
            upload_error = (
                "Failed to upload trial results to S3: the monitor reported no "
                "artifact archive"
            )
        if job_dir is not None:
            await mirror_to_sauron(prepared, job_dir)
        execution = TrialExecutionResult(
            outcome=_outcome_from_payload(result.get("outcome"), job_dir),
            execution_error=result.get("execution_error"),
            retryable=bool(result.get("retryable", True)),
        )
        cost_state = monitor_costs.cost_state_for_settle(
            payload=claimed.payload,
            job_id=claimed.id,
            job_attempt=claimed.attempts,
            trial_id=trial_id,
            prepared=prepared,
        )
        if cost_state is not None:
            await settle_compute_costs(cost_state, execution.outcome)
        await settle_trial_attempt(
            trial_id=trial_id,
            prepared_trial=prepared,
            execution=execution,
            worker_id=worker_id,
            worker_job_id=claimed.id,
            trial_s3_key=trial_s3_key,
            artifact_upload_error=upload_error,
        )
    return execution.outcome, upload_error
