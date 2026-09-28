"""Create QA prompt replays that point at historical solver trials."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.analyze.qa_stages import STAGES, prompt_sha256
from oddish.config import is_nop_oracle_agent, settings
from oddish.core.analysis_payload import qa_trial_evidence
from oddish.core.idempotency import (
    _canonical_digest,
    IdempotencyConflict,
    IdempotencyStore,
    Reservation,
    compute_request_hash,
    reserve_idempotency_slot,
)
from oddish.core.qa_eval_scores import counted_goldens, prompt_version
from oddish.core.qa_goldens import FINISHED_STATUSES
from oddish.core.quota_admission import admit_trials
from oddish.db import (
    AnalysisStatus,
    ExperimentModel,
    QAPromptModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    utcnow,
)
from oddish.schemas import (
    QAEvalCreateRequest,
    QAEvalCreateResponse,
    QAEvalTrialResponse,
)
from oddish.workers.analysis_trials import (
    build_qa_brief,
    create_analysis_trial,
    pre_trial_item_ids,
)

_QA_EVAL_ROUTE = "POST /qa-evals"


async def create_qa_eval_core(
    session: AsyncSession,
    *,
    request: QAEvalCreateRequest,
    org_id: str | None,
    owner_user_id: str | None,
    billed_user_id: str | None,
    idempotency_key: str | None = None,
    idempotency_store: IdempotencyStore | None = None,
    request_hash: str | None = None,
) -> QAEvalCreateResponse:
    """Create one replay experiment and one QA trial per source trial: the
    listed trials, or every counted golden's."""
    reservation: Reservation | None = None
    if idempotency_store is not None and idempotency_key and org_id:
        try:
            reservation = await reserve_idempotency_slot(
                idempotency_store,
                org_id=org_id,
                route=_QA_EVAL_ROUTE,
                raw_key=idempotency_key,
                request_hash=request_hash or compute_request_hash(request),
                now=utcnow(),
            )
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    stage = request.stage
    if request.prompt_text is not None:
        prompt_name, prompt_text = request.prompt_name or "", request.prompt_text
    else:
        assert request.prompt_sha256 is not None
        version = await prompt_version(session, org_id, stage, request.prompt_sha256)
        if version is None:
            raise HTTPException(
                status_code=404,
                detail=f"no recorded {stage.value} prompt {request.prompt_sha256}",
            )
        prompt_name, prompt_text = request.prompt_name or version[0], version[1]

    source_trial_ids = request.source_trial_ids
    goldens = await counted_goldens(session, org_id, stage)
    if request.goldens:
        source_trial_ids = sorted(goldens)
        if not source_trial_ids:
            raise HTTPException(
                status_code=422, detail=f"no counted {stage.value} goldens to replay"
            )
    source_rows = (
        (
            await session.execute(
                select(TrialModel).where(
                    TrialModel.id.in_(source_trial_ids),
                    TrialModel.org_id == org_id,
                )
            )
        )
        .scalars()
        .all()
    )
    source_by_id = {row.id: row for row in source_rows}
    task_ids = list(dict.fromkeys(row.task_id for row in source_rows))
    version_ids = list(
        dict.fromkeys(row.task_version_id for row in source_rows if row.task_version_id)
    )
    tasks = (
        (
            await session.execute(
                select(TaskModel).where(
                    TaskModel.id.in_(task_ids), TaskModel.org_id == org_id
                )
            )
        )
        .scalars()
        .all()
    )
    versions = (
        (
            await session.execute(
                select(TaskVersionModel).where(TaskVersionModel.id.in_(version_ids))
            )
        )
        .scalars()
        .all()
        if version_ids
        else []
    )
    task_by_id = {row.id: row for row in tasks}
    version_by_id = {row.id: row for row in versions}

    rejected: dict[str, str] = {}
    for source_trial_id in source_trial_ids:
        source = source_by_id.get(source_trial_id)
        reason = None
        if source is None:
            reason = "not found or inaccessible"
        elif source.kind != "agent" or is_nop_oracle_agent(source.agent):
            reason = "not an ordinary solver trial"
        elif source.superseded_by_trial_id is not None:
            reason = "superseded"
        elif source.status not in FINISHED_STATUSES:
            reason = f"not terminal ({source.status.value})"
        elif source.task_version_id is None:
            reason = "missing an exact task version"
        elif source.task_id not in task_by_id:
            reason = "missing its task"
        elif source.task_version_id not in version_by_id:
            reason = "missing its exact task version"
        elif version_by_id[source.task_version_id].task_id != source.task_id:
            reason = "task version belongs to another task"
        elif not version_by_id[source.task_version_id].task_s3_key:
            reason = "exact task version has no stored files"
        if reason:
            rejected[source_trial_id] = reason

    skipped = [f"{trial_id}: {reason}" for trial_id, reason in rejected.items()]
    # A golden's trial can stop being replayable (retried, deleted), so a
    # full run skips it; a listed trial must replay.
    if rejected and not request.goldens:
        raise HTTPException(
            status_code=409,
            detail="QA replay rejected; fix these source rows: " + "; ".join(skipped),
        )
    source_trial_ids = [i for i in source_trial_ids if i not in rejected]
    if not source_trial_ids:
        raise HTTPException(
            status_code=422,
            detail="no golden trial can be replayed: " + "; ".join(skipped),
        )

    canonical_model = settings.normalize_trial_model(
        request.agent, request.model or settings.analysis_model
    )
    prompt_hash = prompt_sha256(prompt_text)
    configuration_hash = _canonical_digest({
        "agent": request.agent, "model": canonical_model,
        "agent_config": request.agent_overrides(), "environment": request.environment,
    })
    case_versions = {
        source_id: version_by_id[source_by_id[source_id].task_version_id]
        for source_id in source_trial_ids
    }
    cases = {}
    for source_id, version in case_versions.items():
        imported = (source_by_id[source_id].harbor_config or {}).get("imported_source")
        original = imported.get("trial_id") if isinstance(imported, dict) else None
        golden = goldens.get(original) if isinstance(original, str) and original in goldens else goldens.get(source_id)
        cases[source_id] = {
            "version": version.id, "content_hash": version.content_hash,
            "expected": golden.expected if golden else None,
        }
    golden_hash = _canonical_digest(cases)
    input_hash = _canonical_digest({
        "configuration": configuration_hash, "goldens": golden_hash,
        "prompt": prompt_hash, "stage": stage.value,
        "audit_context": request.audit_context,
        "audit": {source_id: version.pre_trial for source_id, version in case_versions.items()}
        if request.audit_context == "current" else None,
    })
    if request.preflight_run_id:
        probe = await session.scalar(select(TrialModel).where(
            TrialModel.experiment_id == request.preflight_run_id,
            TrialModel.org_id == org_id, TrialModel.kind == "qa_eval",
        ))
        payload = (probe.harbor_config or {}).get("analysis_payload", {}) if probe else {}
        if not (
            probe and payload.get("preflight")
            and payload.get("input_sha256") == input_hash
            and probe.status == TrialStatus.SUCCESS
            and probe.analysis_status == AnalysisStatus.SUCCESS
            and STAGES[stage].grade.answer(probe.analysis) is not None
        ):
            raise HTTPException(409, "A completed, valid preflight with the same configuration, prompt and cases is required")
    if request.preflight:
        source_trial_ids = source_trial_ids[:1]
    await admit_trials(session, org_id, billed_user_id, count=len(source_trial_ids))
    experiment = ExperimentModel(
        name=request.name,
        org_id=org_id,
        owner_user_id=owner_user_id,
        is_collection=False,
        last_activity_at=utcnow(),
    )
    session.add(experiment)
    await session.flush()
    if request.prompt_text is not None:
        await session.execute(
            pg_insert(QAPromptModel)
            .values(
                org_id=org_id,
                stage=stage.value,
                sha256=prompt_hash,
                text=prompt_text,
                name=prompt_name,
                source="replay",
                created_by_user_id=owner_user_id,
            )
            .on_conflict_do_nothing()
        )

    created: list[QAEvalTrialResponse] = []
    for source_trial_id in source_trial_ids:
        source = source_by_id[source_trial_id]
        task_version_id = source.task_version_id
        assert task_version_id is not None
        task = task_by_id[source.task_id]
        version = version_by_id[task_version_id]
        include_current_audit = request.audit_context == "current"
        pre_trial_items = (
            (version.pre_trial or {}).get("items")
            if include_current_audit and isinstance(version.pre_trial, dict)
            else None
        )
        evidence = [qa_trial_evidence(source)]
        item_ids, must_fix_ids = pre_trial_item_ids(pre_trial_items)
        trial = await create_analysis_trial(
            session,
            task=task,
            kind="qa_eval",
            brief=build_qa_brief(
                task_name=task.name,
                trial_ids=[source.id],
                pre_trial_items=pre_trial_items,
                with_verdict=False,
                classification_prompt=prompt_text,
                trial_evidence=evidence,
                pre_trial_status=(
                    version.pre_trial_status.value
                    if include_current_audit and version.pre_trial_status is not None
                    else "omitted for historical replay"
                    if not include_current_audit
                    else None
                ),
                pre_trial_error=(
                    version.pre_trial_error if include_current_audit else None
                ),
                verdict_omission_reason="QA replay does not synthesize a task verdict",
            ),
            task_version_id=task_version_id,
            experiment_id=experiment.id,
            model=canonical_model,
            execution=request,
            environment=request.environment,
            billed_user_id=billed_user_id,
            payload={
                "trial_ids": [source.id],
                "trial_evidence": evidence,
                "baseline_evidence": [],
                "pre_trial_item_ids": item_ids,
                "pre_trial_must_fix_ids": must_fix_ids,
                "with_verdict": False,
                "stage": stage.value,
                "prompt_name": prompt_name,
                "prompt_sha256": prompt_hash,
                "subset": not request.goldens or request.preflight,
                "preflight": request.preflight,
                "configuration_sha256": configuration_hash,
                "golden_set_sha256": golden_hash,
                "input_sha256": input_hash,
                "audit_context": request.audit_context,
            },
        )
        created.append(
            QAEvalTrialResponse(source_trial_id=source.id, qa_eval_trial_id=trial.id)
        )

    response = QAEvalCreateResponse(
        experiment_id=experiment.id,
        experiment_name=experiment.name,
        prompt_name=prompt_name,
        prompt_sha256=prompt_hash,
        model=canonical_model,
        agent=request.agent,
        preflight=request.preflight,
        trials=created,
        skipped=skipped,
    )
    if reservation is not None and idempotency_store is not None and org_id is not None:
        await session.flush()
        await idempotency_store.complete(
            org_id,
            _QA_EVAL_ROUTE,
            reservation.key_hash,
            response.model_dump(mode="json"),
        )
    return response
