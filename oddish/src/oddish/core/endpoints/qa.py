from __future__ import annotations

import asyncio
import hashlib
from importlib import resources
from collections.abc import Collection
from pathlib import Path

from fastapi import HTTPException
from harbor.models.environment_type import EnvironmentType
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from oddish.core.endpoints._common import (
    _ACTIVE_WORKER_JOB_STATUSES_SQL,
    USER_CANCELLED_MESSAGE,
)
from oddish.core.adversarial import (
    ADVERSARIAL_TRIAL_KIND,
    ADVERSARIAL_MODEL,
    ATTACK_PROMPT_VERSION,
    ATTACKS_PER_LAUNCH,
    adversarial_conditions,
    adversarial_environment,
    adversarial_unsettled,
    attack_attempts,
    attack_prompt,
    replay_target,
)
from oddish.core.verdict_state import cancel_verdict, queue_verdict, reset_verdict
from oddish.config import (
    DisabledEnvironmentError,
    is_nop_oracle_agent,
    require_enabled_environment,
    settings,
)
from oddish.core.baseline_gate import baseline_agent_clause
from oddish.core.task_files import resolve_task_file_source
from oddish.db.storage import get_storage_client, resolve_s3_key, task_directory_sha256
from oddish.schemas import TrialSpec
from oddish.core.sweeps import resolve_trial_model
from oddish.core.trial_io import qa_source_evidence_errors
from oddish.db import (
    ACTIVE_TRIAL_STATUSES,
    AGENT_TRIAL_KIND,
    AnalysisStatus,
    TaskModel,
    TaskStatus,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    utcnow,
)

_QA_EVIDENCE_READ_CONCURRENCY = 2


def require_allowed_analysis_environment(environment: str | None) -> None:
    """Reject a sandbox provider this deployment did not register, before the
    trial can queue and fail at launch. ``None`` keeps the worker default."""
    if environment is None:
        return
    from harbor.models.environment_type import EnvironmentType

    from oddish.runtime.routing import allowed_cloud_environments

    allowed = allowed_cloud_environments()
    try:
        requested = EnvironmentType(environment.strip().lower())
    except ValueError:
        requested = None
    if requested is None or requested not in allowed:
        raise HTTPException(
            status_code=400,
            detail=(
                f"environment '{environment}' is not enabled on this deployment. "
                "Allowed: " + ", ".join(sorted(e.value for e in allowed))
            ),
        )


def _collect_cancel_metadata(rows: Collection[object]) -> dict[str, list[str]]:
    modal_fc_ids: list[str] = []
    for row in rows:
        get = getattr(row, "get", None)
        fc = get("modal_function_call_id") if get else None
        if fc:
            modal_fc_ids.append(str(fc))
    return {"modal_function_call_ids": list(dict.fromkeys(modal_fc_ids))}


async def _cancel_worker_jobs_for_kind(
    session: AsyncSession,
    *,
    kind: str,
    subject_table: str,
    subject_ids: Collection[str],
    reason: str,
):
    if not subject_ids:
        return []
    rows = (
        (
            await session.execute(
                text(
                    f"""
                WITH to_cancel AS (
                    SELECT id,
                           modal_function_call_id
                    FROM   worker_jobs
                    WHERE  kind::text = :kind
                      AND  subject_table = :subject_table
                      AND  subject_id = ANY(:subject_ids)
                      AND  status::text IN ({_ACTIVE_WORKER_JOB_STATUSES_SQL})
                    FOR UPDATE
                )
                UPDATE worker_jobs AS w
                SET    status = 'CANCELLED',
                       finished_at = NOW(),
                       error_message = :reason,
                       current_worker_id = NULL,
                       current_queue_slot = NULL,
                       modal_function_call_id = NULL
                FROM   to_cancel
                WHERE  w.id = to_cancel.id
                RETURNING w.id,
                          w.subject_id,
                          to_cancel.modal_function_call_id
                """
                ),
                {
                    "kind": kind,
                    "subject_table": subject_table,
                    "subject_ids": list(dict.fromkeys(subject_ids)),
                    "reason": reason,
                },
            )
        )
        .mappings()
        .all()
    )
    return rows


def _has_active_analysis(trial: TrialModel) -> bool:
    return trial.analysis_status in (
        AnalysisStatus.PENDING,
        AnalysisStatus.QUEUED,
        AnalysisStatus.RUNNING,
    )


def _has_active_verdict(task: TaskModel) -> bool:
    return task.verdict_status in (
        VerdictStatus.PENDING,
        VerdictStatus.QUEUED,
        VerdictStatus.RUNNING,
    )


async def cancel_task_qa_core(
    session: AsyncSession,
    *,
    task_id: str,
    org_id: str | None = None,
) -> dict[str, str | int | list[str]]:
    """Cancel a task's live qa-kind, audit-kind and adversarial trials.

    A cancelled adversarial run is an incomplete check, not a passed one. A qa-kind trial classifies the eligible agent trials and may synthesize a
    verdict. Cancelling this endpoint also stops the task's live pre-trial
    audit and finalizes any classification left RUNNING by a killed worker.
    """
    from oddish.queue import task_audit_pending

    # The same task row lock the backfill and the reruns take: without it, a
    # cancel can interleave with their check-and-enqueue and either kill a
    # just-committed job's state or write verdict resets over a fresh enqueue.
    result = await session.execute(
        select(TaskModel)
        .options(selectinload(TaskModel.trials))
        .where(TaskModel.id == task_id)
        .with_for_update(of=TaskModel)
    )
    task = result.scalar_one_or_none()
    if not task or (org_id is not None and task.org_id != org_id):
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    now_value = utcnow()
    # Cancel the qa/audit trials' TRIAL jobs and fail their rows; the
    # settlement path and importer skip on the cancelled harbor_stage.
    analysis_trials = [
        trial
        for trial in task.trials or []
        if trial.superseded_by_trial_id is None
        and (trial.kind or "agent") in ("qa", "audit", ADVERSARIAL_TRIAL_KIND)
        and trial.status
        not in (TrialStatus.SUCCESS, TrialStatus.FAILED, TrialStatus.SKIPPED)
    ]
    rows = await _cancel_worker_jobs_for_kind(
        session,
        kind="TRIAL",
        subject_table="trials",
        subject_ids=[trial.id for trial in analysis_trials],
        reason=USER_CANCELLED_MESSAGE,
    )
    for trial in analysis_trials:
        trial.status = TrialStatus.FAILED
        trial.harbor_stage = "cancelled"
        trial.error_message = USER_CANCELLED_MESSAGE
        trial.finished_at = trial.finished_at or now_value
    waiting_for_admission = task.status in (
        TaskStatus.PENDING,
        TaskStatus.RUNNING,
    ) and not await _count_active_trials(
        session, task_id=task.id, task_version_id=task.current_version_id
    )
    if (
        # Stopping attacks alone is not cancelling QA: the verdict and the
        # task's status are left alone.
        any(trial.kind != ADVERSARIAL_TRIAL_KIND for trial in analysis_trials)
        or _has_active_verdict(task)
        or task.status == TaskStatus.VERDICT_PENDING
        or await task_audit_pending(session, task)
    ):
        cancel_verdict(task, error=USER_CANCELLED_MESSAGE, now=now_value)
        # Finalize trials whose classification the QA job had in flight so
        # they don't linger in a RUNNING analysis state.
        for trial in task.trials or []:
            if trial.superseded_by_trial_id is None and _has_active_analysis(trial):
                trial.analysis_status = AnalysisStatus.FAILED
                trial.analysis_error = USER_CANCELLED_MESSAGE
                trial.analysis_finished_at = now_value
        if task.status == TaskStatus.VERDICT_PENDING or waiting_for_admission:
            # Include the gap between audit settlement and import/admission:
            # leaving the task RUNNING would let cleanup restart cancelled QA.
            task.status = TaskStatus.FAILED
            task.finished_at = now_value
    # A pre-trial status left QUEUED/RUNNING with nothing behind it would
    # keep the card in a running state forever, so cancel always clears it.
    # An audit trial pins the version it audits, and that version can be
    # older than the current one after a re-upload — clear it as well.
    version_ids: set[str] = set()
    if task.current_version_id:
        version_ids.add(str(task.current_version_id))
    for trial in analysis_trials:
        if trial.kind == "audit" and trial.task_version_id:
            version_ids.add(str(trial.task_version_id))
    for version_id in version_ids:
        version = await session.get(TaskVersionModel, version_id, with_for_update=True)
        if version is not None and version.pre_trial_status in (
            VerdictStatus.PENDING,
            VerdictStatus.QUEUED,
            VerdictStatus.RUNNING,
        ):
            version.pre_trial_status = VerdictStatus.FAILED
            version.pre_trial_error = USER_CANCELLED_MESSAGE
            version.pre_trial_finished_at = now_value

    await session.commit()
    return {
        "status": "cancelled",
        "task_id": task_id,
        "qa_jobs_cancelled": len(rows),
        **_collect_cancel_metadata(rows),
    }


def _reset_trial_analysis(trial: TrialModel) -> None:
    """Clear cached analysis state before re-running analysis."""
    trial.analysis = None
    trial.analysis_status = None
    trial.analysis_error = None
    trial.analysis_started_at = None
    trial.analysis_finished_at = None


async def _count_active_trials(
    session: AsyncSession,
    *,
    task_id: str,
    task_version_id: str | None,
) -> int:
    """Count non-terminal, non-superseded agent trials for one task version."""
    count = await session.scalar(
        select(func.count(TrialModel.id)).where(
            TrialModel.task_id == task_id,
            (
                TrialModel.task_version_id == task_version_id
                if task_version_id is not None
                else True
            ),
            TrialModel.kind == AGENT_TRIAL_KIND,
            TrialModel.superseded_by_trial_id.is_(None),
            TrialModel.status.in_(ACTIVE_TRIAL_STATUSES),
        )
    )
    return int(count or 0)


async def rerun_task_qa_core(
    session: AsyncSession,
    *,
    task_id: str,
    org_id: str | None = None,
    environment: str | None = None,
) -> dict[str, str | int]:
    """Create a replacement qa-kind trial for a finished task.

    Resets every live agent trial's classification, then creates one QA trial
    that reclassifies all eligible trials and requests a verdict once at least
    one exists. Queuing the replacement withdraws the old verdict.
    """
    return await backfill_task_analysis_core(
        session,
        task_id=task_id,
        org_id=org_id,
        trial_ids=None,
        force=True,
        environment=environment,
    )


async def backfill_task_analysis_core(
    session: AsyncSession,
    *,
    task_id: str,
    org_id: str | None = None,
    trial_ids: list[str] | None = None,
    force: bool = False,
    environment: str | None = None,
) -> dict[str, str | int]:
    """(Re)run task-level QA for a task.

    Queues a replacement verdict and withdraws the published result.
    The QA trial re-reads and re-classifies every eligible trial either
    way; ``force`` only controls which stored analyses are cleared up
    front so the UI shows them as pending (all live trials, or just
    ``trial_ids``).
    """
    require_allowed_analysis_environment(environment)
    from oddish.queue import (
        live_analysis_trial_id,
        qa_eligible_trial_ids,
        start_qa_for_task,
        task_audit_pending,
    )

    # The task row lock serializes this check-and-enqueue against the audit
    # rerun (which takes the same lock): without it, two concurrent requests
    # can each pass the job guards below and enqueue conflicting jobs.
    result = await session.execute(
        select(TaskModel)
        .options(selectinload(TaskModel.trials))
        .where(TaskModel.id == task_id)
        .with_for_update(of=TaskModel)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    if org_id is not None and task.org_id != org_id:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    if not task.trials:
        raise HTTPException(status_code=400, detail="Task has no trials to QA")

    live_trials = [
        trial
        for trial in task.trials
        if trial.deleted_at is None
        and trial.superseded_by_trial_id is None
        and (trial.kind or "agent") == AGENT_TRIAL_KIND
    ]
    if task.current_version_id is not None:
        live_trials = [
            trial
            for trial in live_trials
            if trial.task_version_id == task.current_version_id
        ]
    if not live_trials:
        raise HTTPException(status_code=400, detail="Task has no live trials to QA")

    active_trials = await _count_active_trials(
        session,
        task_id=task.id,
        task_version_id=task.current_version_id,
    )
    if active_trials > 0:
        raise HTTPException(
            status_code=400,
            detail="Can only run QA after all trials finish",
        )

    # The live qa trial IS the in-progress marker. Old status flags
    # (verdict_status, per-trial analysis_status) can be stale after a
    # crash and must not wedge the rerun button.
    live_qa = await live_analysis_trial_id(session, task_id, kind="qa")
    if live_qa is not None:
        raise HTTPException(
            status_code=400,
            detail="QA is already in progress for this task",
        )

    # A finished job may still await import; do not snapshot missing findings.
    if await task_audit_pending(session, task):
        raise HTTPException(
            status_code=400,
            detail="A pre-trial audit is running or awaiting import; wait for it to finish",
        )

    eligible_ids = await qa_eligible_trial_ids(
        session,
        task.id,
        task_version_id=task.current_version_id,
    )
    trials_by_id = {trial.id: trial for trial in live_trials}
    evidence_read_slots = asyncio.Semaphore(_QA_EVIDENCE_READ_CONCURRENCY)

    async def check_source_evidence(trial: TrialModel) -> tuple[str, ...]:
        async with evidence_read_slots:
            return await qa_source_evidence_errors(trial)

    evidence_checks = await asyncio.gather(
        *(check_source_evidence(trials_by_id[trial_id]) for trial_id in eligible_ids)
    )
    blocked = [
        f"{trial_id}: {'; '.join(errors)}"
        for trial_id, errors in zip(eligible_ids, evidence_checks, strict=True)
        if errors
    ]
    if blocked:
        raise HTTPException(
            status_code=409,
            detail=(
                "Cannot queue QA because source evidence is unavailable:\n"
                + "\n".join(blocked)
            ),
        )

    reset_count = 0
    if force:
        from oddish.core.task_findings import preserve_task_findings

        await preserve_task_findings(session, task.current_version_id)
        if trial_ids is not None:
            wanted = set(trial_ids)
            to_reset = [t for t in live_trials if t.id in wanted]
        else:
            to_reset = live_trials
        for trial in to_reset:
            _reset_trial_analysis(trial)
            reset_count += 1

    task.finished_at = None
    await start_qa_for_task(session, task, environment=environment)

    await session.commit()
    return {
        "status": "queued",
        "task_id": task_id,
        "trial_count": len(live_trials),
        "reset_count": reset_count,
    }


async def rerun_pre_trial_audit_core(
    session: AsyncSession,
    *,
    task_id: str,
    org_id: str | None = None,
    environment: str | None = None,
    full_qa: bool = False,
    expected_version_id: str | None = None,
) -> dict[str, str]:
    """Queue the pre-trial audit for the task's current version.

    Replaces the audit evidence and withdraws the old task decision. Once
    this audit and any existing runs finish, normal QA admission reconciles
    the task against the new audit without interrupting running jobs.
    """
    require_allowed_analysis_environment(environment)
    from datetime import timedelta

    from oddish.queue import live_analysis_trial_id

    # The task row lock serializes this check-and-enqueue against the QA
    # backfill (which takes the same lock): without it, two concurrent
    # requests can each pass the job guards below and enqueue conflicting
    # jobs.
    task = await session.get(TaskModel, task_id, with_for_update=True)
    if not task or (org_id is not None and task.org_id != org_id):
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    if not task.current_version_id:
        raise HTTPException(status_code=400, detail="Task has no version to audit")
    if (
        expected_version_id is not None
        and task.current_version_id != expected_version_id
    ):
        raise HTTPException(
            status_code=409,
            detail="Task version changed; refresh the delivery and retry",
        )

    version = await session.get(
        TaskVersionModel, task.current_version_id, with_for_update=True
    )
    if version is None:
        raise HTTPException(status_code=400, detail="Task has no version to audit")

    # The audit trial's own timeout bounds a live run; block a re-request
    # only while one is plausibly in flight.
    from oddish.workers.analysis_trials import ANALYSIS_TRIAL_TIMEOUT_MINUTES

    lease = timedelta(minutes=ANALYSIS_TRIAL_TIMEOUT_MINUTES * 2)
    live_audit_id = await live_analysis_trial_id(session, task_id, kind="audit")
    if full_qa and live_audit_id is not None:
        live_audit = await session.get(TrialModel, live_audit_id)
        if live_audit.task_version_id != version.id:
            raise HTTPException(
                status_code=409,
                detail="Another task version has a live source audit; wait for it to finish",
            )
    audit_active = (
        version.pre_trial_status in (VerdictStatus.QUEUED, VerdictStatus.RUNNING)
        and version.pre_trial_started_at is not None
        and utcnow() - version.pre_trial_started_at < lease
    ) or live_audit_id is not None
    if audit_active and full_qa:
        # Adopt the in-flight source check and durably request its follow-up.
        # Do not change the task's automatic-analysis preference.
        queue_verdict(task)
        task.status = TaskStatus.RUNNING
        task.finished_at = None
        await session.commit()
        return {"status": "already_running", "task_id": task_id}
    if audit_active:
        raise HTTPException(
            status_code=400,
            detail="An audit is already running for this version",
        )

    # A live QA trial may finish, but its audit fingerprint prevents it from
    # publishing the obsolete decision. Admission waits for both jobs before
    # creating replacement QA, so this does not duplicate a live QA job.
    if full_qa:
        queue_verdict(task)
    else:
        reset_verdict(task)
    task.status = TaskStatus.RUNNING
    task.finished_at = None

    # Reset the previous audit and queue a new one. QUEUED (not None) keeps
    # the card showing progress while the trial waits for a worker.
    from oddish.core.task_findings import preserve_task_findings

    await preserve_task_findings(session, version.id)
    version.pre_trial_status = VerdictStatus.QUEUED
    version.pre_trial = None
    version.pre_trial_error = None
    version.pre_trial_started_at = utcnow()
    version.pre_trial_finished_at = None

    from oddish.workers.analysis_trials import build_audit_brief, create_analysis_trial

    await create_analysis_trial(
        session,
        task=task,
        kind="audit",
        brief=build_audit_brief(task_name=task.name),
        task_version_id=str(version.id),
        environment=environment,
    )
    await session.commit()
    return {"status": "queued", "task_id": task_id}


async def launch_adversarial_trial_core(
    session: AsyncSession,
    *,
    task_id: str,
    expected_version_id: str,
    request_id: str,
    org_id: str | None = None,
    environment: str | None = None,
    experiment_id: str | None = None,
    execution: TrialSpec | None = None,
    max_trial_attempts: int = 1,
    requested_by_user_id: str | None = None,
) -> dict:
    """Queue organization-funded adversarial QA on the task's current version.

    Check access and capture source identity under the task row lock. Release
    locks during archive transfer, then recheck identity and admission before
    atomically publishing ``ATTACKS_PER_LAUNCH`` trials and their worker jobs.
    The published verdict is left alone; each attack's exploit is replayed when
    it settles, and only a passing replay reaches QA.
    """
    from oddish.queue import (
        enqueue_trial_worker_job,
        reserve_next_trial_index,
    )

    task = await session.get(TaskModel, task_id, with_for_update=True)
    if (
        not task
        or task.deleted_at is not None
        or (org_id is not None and task.org_id != org_id)
    ):
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    version_id = task.current_version_id
    if version_id is None:
        raise HTTPException(status_code=400, detail="Task has no version to attack")
    if version_id != expected_version_id:
        raise HTTPException(
            status_code=409,
            detail="Task version changed; refresh the task and retry",
        )

    existing = await _existing_adversarial_result(
        session, task_id=task_id, version_id=version_id, request_id=request_id
    )
    if existing is not None:
        return existing

    version = await session.get(TaskVersionModel, version_id, with_for_update=True)

    if version is None:
        raise HTTPException(409, "Selected task version no longer exists")
    source = await resolve_task_file_source(
        session,
        task_id=task_id,
        version=version.version,
        org_id=org_id,
    )
    source_identity = (
        task.org_id,
        version.content_hash,
        version.task_path,
        version.task_s3_key,
        source.task_s3_prefix,
    )
    task_path = version.task_path
    prefix = (
        source.task_s3_prefix
        if version.task_s3_key
        else resolve_s3_key(None, task_path)
    )
    await session.commit()

    # No database transaction or row lock spans hashing, downloads or uploads.
    storage = None
    created_snapshot_key = None
    try:
        if not prefix and task_path and Path(task_path).is_dir():
            artifact = {
                "sha256": await asyncio.to_thread(
                    task_directory_sha256, Path(task_path)
                )
            }
        else:
            storage = get_storage_client()
            pinned = await storage.pin_task_archive(prefix or source.task_s3_prefix)
            artifact = pinned.artifact
            created_snapshot_key = pinned.created_snapshot_key
    except Exception as exc:
        raise HTTPException(409, f"Cannot pin selected task version: {exc}") from exc

    retain_snapshot = False
    try:
        task = await session.get(
            TaskModel, task_id, with_for_update=True, populate_existing=True
        )
        if (
            not task
            or task.deleted_at is not None
            or task.org_id != source_identity[0]
            or task.current_version_id != version_id
        ):
            raise HTTPException(409, "Task source changed; refresh the task and retry")
        version = await session.get(
            TaskVersionModel, version_id, with_for_update=True, populate_existing=True
        )
        if version is None:
            raise HTTPException(409, "Task source changed; refresh the task and retry")
        existing = await _existing_adversarial_result(
            session, task_id=task_id, version_id=version_id, request_id=request_id
        )
        if existing is not None:
            return existing
        current_source = await resolve_task_file_source(
            session,
            task_id=task_id,
            version=version.version,
            org_id=org_id,
        )
        if source_identity != (
            task.org_id,
            version.content_hash,
            version.task_path,
            version.task_s3_key,
            current_source.task_s3_prefix,
        ):
            raise HTTPException(409, "Task source changed; refresh the task and retry")
        # An explicit association needs only an existence check. Implicit selection
        # keeps the most recently used experiment for this version.
        experiment_query = """
            SELECT e.id FROM task_experiments te
            JOIN experiments e ON e.id = te.experiment_id
            WHERE te.task_id = :task_id AND te.deleted_at IS NULL
              AND e.deleted_at IS NULL AND e.shadow_of IS NULL
        """
        if experiment_id is not None:
            experiment_query += " AND e.id = :experiment_id"
        else:
            experiment_query += """
            ORDER BY (
              SELECT max(tr.created_at) FROM trials tr
              WHERE tr.task_id = te.task_id AND tr.experiment_id = e.id
                AND tr.task_version_id = :version_id AND tr.deleted_at IS NULL
                AND tr.kind IN ('agent', 'adversarial')
            ) DESC NULLS LAST, te.created_at DESC, e.id
            """
        selected_experiment_id = await session.scalar(
            text(experiment_query + " LIMIT 1"),
            {
                "task_id": task_id,
                "version_id": version_id,
                "experiment_id": experiment_id,
            },
        )
        if selected_experiment_id is None:
            if experiment_id is not None:
                raise HTTPException(404, "Experiment is not linked to this task")
            raise HTTPException(
                409, "This task has no active experiment for an adversarial run"
            )
        experiment_id = selected_experiment_id
        # Model choice and environment provenance have different owners. A costly
        # source solver must never silently become the default audit model.
        execution = execution or TrialSpec(agent="auto", model=ADVERSARIAL_MODEL)
        if not execution.model:
            execution.model = ADVERSARIAL_MODEL
        if is_nop_oracle_agent(execution.agent):
            raise HTTPException(422, "An adversarial run requires a model agent")
        model = resolve_trial_model(execution)
        if not model:
            raise HTTPException(422, "An adversarial run requires an explicit model")
        source_query = (
            select(
                TrialModel.id,
                TrialModel.environment,
                TrialModel.harbor_config,
            )
            .where(
                TrialModel.task_id == task.id,
                TrialModel.task_version_id == version_id,
                TrialModel.kind == AGENT_TRIAL_KIND,
                TrialModel.deleted_at.is_(None),
                TrialModel.superseded_by_trial_id.is_(None),
            )
            .order_by(TrialModel.created_at.desc(), TrialModel.id.desc())
            .limit(1)
        )
        source_run = (
            await session.execute(
                source_query.where(~baseline_agent_clause(TrialModel.agent))
            )
        ).one_or_none()
        if source_run is None:
            source_run = (await session.execute(source_query)).one_or_none()
        source_config = (source_run.harbor_config or {}) if source_run else {}
        environment_config = {
            key: source_config[key]
            for key in (
                "environment",
                "docker_image",
                "mcp_servers",
                "verifier",
                "artifacts",
                "source",
                "ref",
                "resolved_sha",
                "variant_id",
                "environment_build_timeout_multiplier",
                "verifier_timeout_multiplier",
            )
            if key in source_config
        }
        from oddish.runtime.routing import allowed_cloud_environments

        backend = (
            execution.environment
            or environment
            or adversarial_environment(
                source_run.environment if source_run else None,
                source_config,
                {env.value for env in allowed_cloud_environments()},
            )
            or settings.harbor_environment
        )
        try:
            backend = EnvironmentType(backend).value
            require_enabled_environment(backend)
        except (DisabledEnvironmentError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        attempts = attack_attempts(model, max_trial_attempts)
        from oddish.core.quota_admission import admit_organization_qa_trials

        await admit_organization_qa_trials(
            session, task.org_id, count=ATTACKS_PER_LAUNCH
        )
        # Black box: the attacker gets no audit findings or other knowledge of
        # the grader beyond what any solver of the task has. White-box attacks
        # also get the grader's code, added by the worker from the pinned copy.
        template = (
            resources.files("oddish.analyze")
            .joinpath("adversarial_prompt.txt")
            .read_bytes()
            .decode("utf-8")
        )
        if created_snapshot_key is not None:
            version.task_s3_key = created_snapshot_key.rsplit("/", 1)[0] + "/"
        # One reservation under the task lock: rows added below are not
        # flushed yet, so a second reservation would return the same index.
        first_index = await reserve_next_trial_index(session, task_id=task.id)
        trials: list[TrialModel] = []
        for offset in range(ATTACKS_PER_LAUNCH):
            next_index = first_index + offset
            prompt_text, white_box = attack_prompt(template, offset)
            trials.append(
                TrialModel(
                    id=f"{task.id}-{next_index}",
                    name=f"{task.name}-adversarial-qa-{next_index}",
                    task_id=task.id,
                    task_version_id=version_id,
                    experiment_id=experiment_id,
                    org_id=task.org_id,
                    billed_user_id=None,
                    agent=execution.agent,
                    model=model,
                    provider=settings.get_provider_for_trial(execution.agent, model),
                    queue_key=settings.get_queue_key_for_trial(execution.agent, model),
                    environment=backend,
                    kind=ADVERSARIAL_TRIAL_KIND,
                    status=TrialStatus.QUEUED,
                    max_attempts=attempts,
                    harbor_config={
                        **environment_config,
                        "task_artifact": artifact,
                        "agent_config": execution.agent_overrides(),
                        ADVERSARIAL_TRIAL_KIND: {
                            "request_id": request_id,
                            "requested_by_user_id": requested_by_user_id,
                            "task_content_hash": version.content_hash,
                            "prompt_version": ATTACK_PROMPT_VERSION,
                            "prompt_text": prompt_text,
                            "prompt_sha256": hashlib.sha256(
                                prompt_text.encode("utf-8")
                            ).hexdigest(),
                            "white_box": white_box,
                            "execution": {
                                **execution.model_dump(mode="json", exclude_none=True),
                                "model": model,
                                "environment": backend,
                                "source_trial_id": source_run.id
                                if source_run
                                else None,
                                "max_attempts": attempts,
                            },
                        },
                    },
                )
            )
        session.add_all(trials)
        await session.flush()
        for trial in trials:
            await enqueue_trial_worker_job(
                session,
                trial_id=trial.id,
                queue_key=trial.queue_key,
                org_id=task.org_id,
                max_attempts=attempts,
            )
        # QA admission waits for these attacks and their replays.
        version.adversarial_status = VerdictStatus.RUNNING
        version.adversarial_error = None
        version.adversarial_started_at = utcnow()
        # The published verdict stays: QA never reads attacks.
        # Keep the snapshot on an ambiguous commit outcome; deleting committed
        # execution evidence would be worse than leaving an unreferenced object.
        retain_snapshot = True
        await session.commit()
        return {
            "status": "queued",
            "trial_id": trials[0].id,
            "trial_ids": [trial.id for trial in trials],
            "task_id": task_id,
            "task_version_id": version_id,
        }
    finally:
        if not retain_snapshot:
            await session.rollback()
            if created_snapshot_key is not None:
                await storage.delete_object(created_snapshot_key)


async def _existing_adversarial_result(
    session: AsyncSession,
    *,
    task_id: str,
    version_id: str,
    request_id: str,
) -> dict | None:
    """Resolve idempotency under the task lock, before and after source transfer.

    Both answers name the whole batch, in launch order, so a retry gets the
    same ``trial_id`` and ``trial_ids`` the original launch returned.
    """
    request_id_column = TrialModel.harbor_config["adversarial"]["request_id"].astext
    status = "existing"
    batch_request_id: str | None = request_id
    live = None
    if not (
        await session.execute(
            select(TrialModel.id).where(
                *adversarial_conditions(task_id, None),
                request_id_column == request_id,
            )
        )
    ).first():
        # A batch runs until its replays settle too; a replay names its attack.
        live = (
            await session.execute(
                select(TrialModel.id, TrialModel.harbor_config)
                .where(
                    *adversarial_conditions(task_id, version_id, replays=None),
                    TrialModel.status.in_(ACTIVE_TRIAL_STATUSES),
                )
                .order_by(TrialModel.created_at.desc(), TrialModel.id.desc())
                .limit(1)
            )
        ).one_or_none()
        if live is None and await adversarial_unsettled(session, task_id, version_id):
            # Attacks settled but their replay or findings hook has not: the
            # batch is still in flight, so a rerun must not start another.
            live = (
                await session.execute(
                    select(TrialModel.id, TrialModel.harbor_config)
                    .where(*adversarial_conditions(task_id, version_id))
                    .order_by(TrialModel.created_at.desc(), TrialModel.id.desc())
                    .limit(1)
                )
            ).one_or_none()
        if live is None:
            return None
        target = replay_target(live.harbor_config)
        attack_config = (
            live.harbor_config
            if target is None
            else await session.scalar(
                select(TrialModel.harbor_config).where(TrialModel.id == target[0])
            )
        )
        batch_request_id = ((attack_config or {}).get("adversarial") or {}).get(
            "request_id"
        )
        status = "already_running"
    rows = (
        await session.execute(
            select(TrialModel.id, TrialModel.task_version_id).where(
                *adversarial_conditions(task_id, None),
                request_id_column == batch_request_id,
            )
        )
    ).all()
    if not rows:
        if live is None:
            return None
        # The replay's attack is gone (deleted or superseded); the live replay
        # alone still keeps the batch running.
        return {
            "status": status,
            "trial_id": live.id,
            "trial_ids": [live.id],
            "task_id": task_id,
            "task_version_id": str(version_id),
        }
    if status == "existing" and rows[0].task_version_id != version_id:
        raise HTTPException(
            409, "request_id was already used for a different task version"
        )
    # Ids end in the launch's reserved trial index; sort numerically.
    trial_ids = sorted(
        (row.id for row in rows), key=lambda trial_id: int(trial_id.rsplit("-", 1)[1])
    )
    return {
        "status": status,
        "trial_id": trial_ids[0],
        "trial_ids": trial_ids,
        "task_id": task_id,
        "task_version_id": str(rows[0].task_version_id),
    }
