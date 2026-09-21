from __future__ import annotations

from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.cost_basis import CANCELLED_HARBOR_STAGE
from oddish.core.task_browse_summary import refresh_task_browse_summaries
from oddish.db import (
    AnalysisStatus,
    TaskModel,
    TrialModel,
    TrialStatus,
    utcnow,
)
from oddish.queue import (
    ACTIVE_PIPELINE_STATUSES,
    ACTIVE_TRIAL_STATUSES,
    USER_CANCELLED_MESSAGE,
    maybe_start_task_qa_stage,
    release_gate_after_quota_cancel,
    settle_cancelled_audit_status,
)


async def cancel_trials_by_id(
    session: AsyncSession,
    *,
    trial_ids: list[str],
    org_id: str,
) -> dict[str, Any]:
    """Cancel exactly the requested org-owned trials without widening to a task."""
    requested = list(dict.fromkeys(trial_ids))
    task_ids = sorted(
        set(
            (
                await session.execute(
                    select(TrialModel.task_id).where(
                        TrialModel.id.in_(requested), TrialModel.org_id == org_id
                    )
                )
            ).scalars()
        )
    )
    if task_ids:
        # Preserve the global Task -> Trial -> WorkerJob lock order used by
        # cancellation and retry paths.
        await session.execute(
            select(TaskModel.id)
            .where(TaskModel.id.in_(task_ids), TaskModel.org_id == org_id)
            .order_by(TaskModel.id)
            .with_for_update()
        )
    rows = await session.execute(
        select(TrialModel)
        .where(TrialModel.id.in_(requested), TrialModel.org_id == org_id)
        .order_by(TrialModel.id)
        .with_for_update()
    )
    trials = list(rows.scalars().all())
    found = {trial.id for trial in trials}

    cancelled_jobs = (
        list(
            (
                await session.execute(
                    text(
                        """
                    WITH to_cancel AS (
                        SELECT id, kind::text AS kind, subject_id,
                               modal_function_call_id, provider, external_id
                        FROM worker_jobs
                        WHERE subject_table = 'trials'
                          AND subject_id = ANY(:trial_ids)
                          AND status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')
                        ORDER BY id
                        FOR UPDATE
                    )
                    UPDATE worker_jobs AS w
                    SET status = 'CANCELLED',
                        finished_at = NOW(),
                        error_message = :message,
                        current_worker_id = NULL,
                        current_queue_slot = NULL,
                        modal_function_call_id = NULL,
                        payload = w.payload - 'registry_auth_enc'
                    FROM to_cancel
                    WHERE w.id = to_cancel.id
                    RETURNING to_cancel.kind, to_cancel.subject_id,
                              to_cancel.modal_function_call_id,
                              to_cancel.provider, to_cancel.external_id
                    """
                    ),
                    {"trial_ids": sorted(found), "message": USER_CANCELLED_MESSAGE},
                )
            )
            .mappings()
            .all()
        )
        if found
        else []
    )

    cancelled_trial_jobs = {
        str(row["subject_id"])
        for row in cancelled_jobs
        if row["kind"] == "TRIAL" and row["subject_id"]
    }
    cancelled_analysis_jobs = {
        str(row["subject_id"])
        for row in cancelled_jobs
        if row["kind"] == "ANALYSIS" and row["subject_id"]
    }
    now = utcnow()
    cancelled_ids: list[str] = []
    for trial in trials:
        if trial.id in cancelled_trial_jobs or trial.status in ACTIVE_TRIAL_STATUSES:
            trial.status = TrialStatus.FAILED
            trial.error_message = USER_CANCELLED_MESSAGE
            trial.finished_at = now
            trial.harbor_stage = CANCELLED_HARBOR_STAGE
            trial.max_attempts = trial.attempts
            trial.current_worker_id = None
            trial.current_queue_slot = None
            cancelled_ids.append(trial.id)
        if (
            trial.id in cancelled_analysis_jobs
            or trial.analysis_status in ACTIVE_PIPELINE_STATUSES
        ):
            trial.analysis_status = AnalysisStatus.FAILED
            trial.analysis_error = USER_CANCELLED_MESSAGE
            trial.analysis_finished_at = now

    await session.flush()

    cancelled = set(cancelled_ids)
    for version_id in sorted(
        {
            trial.task_version_id
            for trial in trials
            if trial.id in cancelled
            and trial.kind == "audit"
            and trial.task_version_id is not None
        }
    ):
        await settle_cancelled_audit_status(session, version_id)

    # A cancelled nop/oracle has no verdict. Resolve its experiment/version
    # gate now so exact-ID cancellation cannot strand sibling LLM jobs in
    # BLOCKED. This must precede QA admission: a released solver is active work.
    released_trial_ids: list[str] = []
    for trial in trials:
        if trial.id in cancelled:
            released_trial_ids.extend(
                await release_gate_after_quota_cancel(session, trial.id)
            )

    for task_id in task_ids:
        await maybe_start_task_qa_stage(session, task_id)
    await refresh_task_browse_summaries(
        session, (trial.task_version_id for trial in trials)
    )
    return {
        "trial_ids": [trial_id for trial_id in requested if trial_id in found],
        "missing_trial_ids": [
            trial_id for trial_id in requested if trial_id not in found
        ],
        "cancelled_trial_ids": cancelled_ids,
        "released_trial_ids": list(dict.fromkeys(released_trial_ids)),
        "modal_function_call_ids": list(
            dict.fromkeys(
                str(row["modal_function_call_id"])
                for row in cancelled_jobs
                if row["modal_function_call_id"]
            )
        ),
        "worker_targets": sorted(
            {
                (str(row["provider"]), str(row["external_id"]))
                for row in cancelled_jobs
                if row["provider"] and row["external_id"]
            }
        ),
    }
