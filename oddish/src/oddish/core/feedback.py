"""Persist append-only QA votes after validating experiment or task membership."""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.experiment_membership import trial_in_experiment
from oddish.db import ExperimentModel, FeedbackModel, TrialModel
from oddish.schemas import FeedbackCreate


async def create_feedback_core(
    session: AsyncSession,
    *,
    data: FeedbackCreate,
    experiment_id: str,
    org_id: str | None,
    user_id: str | None,
) -> FeedbackModel:
    """Persist one QA vote; the caller owns the transaction."""
    trial_id = (
        await session.execute(
            select(TrialModel.id).where(
                TrialModel.id == data.trial_id,
                TrialModel.org_id == org_id,
                trial_in_experiment(experiment_id),
                select(ExperimentModel.id)
                .where(
                    ExperimentModel.id == experiment_id,
                    ExperimentModel.org_id == org_id,
                )
                .exists(),
            )
        )
    ).scalar_one_or_none()
    if trial_id is None:
        raise HTTPException(
            status_code=404, detail="trial not found in this experiment"
        )

    await validate_feedback_report(session, data=data, org_id=org_id)

    feedback = FeedbackModel(
        org_id=org_id,
        created_by_user_id=user_id,
        experiment_id=experiment_id,
        trial_id=trial_id,
        report_trial_id=data.report_trial_id,
        target=data.target,
        target_key=data.target_key,
        vote=data.vote,
        body=data.body,
    )
    session.add(feedback)
    await session.flush()
    return feedback


async def create_task_feedback_core(
    session: AsyncSession,
    *,
    data: FeedbackCreate,
    task_id: str,
    org_id: str | None,
    user_id: str | None,
) -> FeedbackModel:
    """Persist one vote cast from the task page; the caller owns the transaction.

    The trial must belong to the task. A pre-trial audit is itself a trial
    (``kind='audit'``, homed in a shadow experiment), so a vote on one of its
    findings is an action-item vote anchored to that trial.
    """
    experiment_id = (
        await session.execute(
            select(TrialModel.experiment_id).where(
                TrialModel.id == data.trial_id,
                TrialModel.task_id == task_id,
                TrialModel.org_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if experiment_id is None:
        raise HTTPException(status_code=404, detail="trial not found for this task")

    await validate_feedback_report(session, data=data, org_id=org_id)

    feedback = FeedbackModel(
        org_id=org_id,
        created_by_user_id=user_id,
        experiment_id=experiment_id,
        trial_id=data.trial_id,
        report_trial_id=data.report_trial_id,
        target=data.target,
        target_key=data.target_key,
        vote=data.vote,
        body=data.body,
    )
    session.add(feedback)
    await session.flush()
    return feedback


async def validate_feedback_report(
    session: AsyncSession, *, data: FeedbackCreate, org_id: str | None
) -> None:
    """Bind new report votes to the producing run, including superseded runs."""
    if data.report_trial_id is None:
        return  # Legacy clients have no report identity.
    subject = await session.get(TrialModel, data.trial_id)
    report = await session.get(TrialModel, data.report_trial_id)
    if (
        subject is None
        or report is None
        or report.org_id != org_id
        or report.task_id != subject.task_id
        or report.task_version_id != subject.task_version_id
    ):
        raise HTTPException(status_code=404, detail="report not found for this trial")
    expected_kind = "audit" if data.target == "qa_pre_trial" else "qa"
    if report.kind != expected_kind:
        raise HTTPException(
            status_code=422, detail="report kind does not match feedback target"
        )
    if data.target in {"qa_pre_trial", "qa_task_verdict"}:
        if report.id != subject.id:
            raise HTTPException(
                status_code=422,
                detail="report feedback must reference its producing trial",
            )
    elif data.target == "qa_trajectory":
        # The trial's stored analysis names its grader; older QA runs may not
        # carry a well-formed analysis_payload.trial_ids list.
        analysis = subject.analysis if isinstance(subject.analysis, dict) else {}
        payload = (report.harbor_config or {}).get("analysis_payload")
        trial_ids = payload.get("trial_ids") if isinstance(payload, dict) else None
        if analysis.get("_graded_by") != report.id and (
            not isinstance(trial_ids, list) or subject.id not in trial_ids
        ):
            raise HTTPException(
                status_code=422, detail="report did not analyze this trial"
            )
