"""QA goldens for one org: add, list, label, review and retire them, and the
queue of trials to label. A subject (a solver trial for post-trial QA, a task
version for pre-trial QA) has at most one live golden per stage.

Two rules here are shared with scoring: ``golden_state`` (whether a golden
counts) and ``original_id`` (which trial a copied trial's golden is keyed on).
``FINISHED_STATUSES`` (when a solver trial can be labeled) is shared with the
replay endpoint.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql import ColumnElement

from oddish.analyze.qa_stages import STAGES, QAStage
from oddish.config import is_nop_oracle_agent
from oddish.core.experiment_membership import experiment_trial_scope
from oddish.db import (
    AGENT_TRIAL_KIND,
    QAGoldenCaseModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    utcnow,
)
from oddish.queue import get_experiment_by_id_or_name
from oddish.schemas import (
    QAGoldenCreate,
    QAGoldenLabel,
    QAGoldenQueue,
    QAGoldenQueueTrial,
    QAGoldenRef,
    QAGoldenResponse,
    QAGoldenReview,
    QAGoldenState,
)


def golden_state() -> ColumnElement[str]:
    """``counted``, ``waiting`` or ``stale`` for the ``QAGoldenCaseModel``
    row in the enclosing query. Stale: the task version's content changed
    (or the version is gone) since the golden was labeled or last confirmed.
    Counted: reviewed and not stale. Only counted goldens score runs."""
    labeled = QAGoldenCaseModel.task_version_content_hash
    current = (
        select(TaskVersionModel.content_hash)
        .where(TaskVersionModel.id == QAGoldenCaseModel.task_version_id)
        .scalar_subquery()
    )
    return case(
        (and_(labeled.is_not(None), current.is_distinct_from(labeled)), "stale"),
        (QAGoldenCaseModel.reviewed_at.is_not(None), "counted"),
        else_="waiting",
    )


def original_id(trial) -> ColumnElement[str]:
    """The id ``trial`` (TrialModel or an alias) is labeled and scored under.
    A trial copied from another deployment records the original's id in
    ``harbor_config.imported_source.trial_id``; when that original exists in
    the same org, a golden on either trial is stored under the original's id.
    Otherwise it is the trial's own id."""
    original = aliased(TrialModel)
    copied_from = trial.harbor_config["imported_source"]["trial_id"].astext
    return func.coalesce(
        select(original.id)
        .where(
            original.id == copied_from,
            original.org_id.is_not_distinct_from(trial.org_id),
        )
        .scalar_subquery(),
        trial.id,
    )


# A solver trial can be labeled, queued for labeling and replayed once it
# has finished.
FINISHED_STATUSES = (TrialStatus.SUCCESS, TrialStatus.FAILED)
_REVIEW_TITLES = {"waiting": "Waiting for review", "stale": "Stale"}


def _with_state[M: QAGoldenRef](
    model: type[M], golden: QAGoldenCaseModel, state: str
) -> M:
    fields = {
        name: getattr(golden, name) for name in model.model_fields.keys() - {"state"}
    }
    return model(**fields, state=state)


async def _response(
    session: AsyncSession, golden: QAGoldenCaseModel
) -> QAGoldenResponse:
    state = await session.scalar(
        select(golden_state()).where(QAGoldenCaseModel.id == golden.id)
    )
    return _with_state(QAGoldenResponse, golden, state)


async def _golden_in_org(
    session: AsyncSession, golden_id: str, org_id: str | None
) -> QAGoldenCaseModel:
    golden = await session.scalar(
        select(QAGoldenCaseModel).where(
            QAGoldenCaseModel.id == golden_id, QAGoldenCaseModel.org_id == org_id
        )
    )
    if golden is None:
        raise HTTPException(status_code=404, detail="golden not found")
    return golden


async def _solver_trial(
    session: AsyncSession, trial_id: str, org_id: str | None
) -> TrialModel:
    """The trial a golden on ``trial_id`` is stored under."""
    stored_as = await session.scalar(
        select(original_id(TrialModel)).where(
            TrialModel.id == trial_id, TrialModel.org_id == org_id
        )
    )
    trial = await session.get(TrialModel, stored_as) if stored_as else None
    if trial is None:
        raise HTTPException(status_code=404, detail="trial not found")
    if trial.kind != AGENT_TRIAL_KIND or is_nop_oracle_agent(trial.agent):
        raise HTTPException(
            status_code=422, detail="trial is not an ordinary solver trial"
        )
    if trial.status not in FINISHED_STATUSES:
        raise HTTPException(status_code=422, detail="trial has not finished")
    return trial


async def _add(
    session: AsyncSession, data: QAGoldenCreate, org_id: str | None, user_id: str | None
) -> QAGoldenCaseModel:
    subject_trial_id, version_id = None, data.task_version_id
    if STAGES[data.stage].subject == "trial":
        trial = await _solver_trial(session, data.source_trial_id, org_id)
        subject_trial_id, version_id = trial.id, trial.task_version_id
    version = await session.scalar(
        select(TaskVersionModel)
        .join(TaskModel, TaskModel.id == TaskVersionModel.task_id)
        .where(TaskVersionModel.id == version_id, TaskModel.org_id == org_id)
    )
    if version is None:
        raise HTTPException(status_code=404, detail="task version not found")
    golden = QAGoldenCaseModel(
        org_id=org_id,
        stage=data.stage.value,
        source=data.source,
        source_trial_id=subject_trial_id,
        task_version_id=version.id,
        task_version_content_hash=version.content_hash,
        expected=data.expected,
        note=data.note,
        created_by_user_id=user_id,
    )
    try:
        async with session.begin_nested():
            session.add(golden)
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="that subject already has a golden; retire it first",
        ) from exc
    return golden


async def _confirm(
    session: AsyncSession, golden: QAGoldenCaseModel, user_id: str | None
) -> QAGoldenResponse:
    """Someone checked the label against the task as it is now, so it
    counts: reviewed (the first reviewer is kept) and pinned to the task
    version's current content, which clears stale."""
    if golden.reviewed_at is None:
        golden.reviewed_at = utcnow()
        golden.reviewed_by_user_id = user_id
    version = await session.get(TaskVersionModel, golden.task_version_id)
    if version is not None:
        golden.task_version_content_hash = version.content_hash
    await session.flush()
    return await _response(session, golden)


async def add_golden(
    session: AsyncSession,
    *,
    data: QAGoldenCreate,
    org_id: str | None,
    user_id: str | None,
) -> QAGoldenResponse:
    """Store one unreviewed golden, as an import adds it. The caller commits."""
    return await _response(session, await _add(session, data, org_id, user_id))


async def list_goldens(
    session: AsyncSession,
    *,
    org_id: str | None,
    stage: QAStage | None = None,
    source: str | None = None,
    state: QAGoldenState | None = None,
    trial_ids: list[str] | None = None,
) -> list[QAGoldenResponse]:
    """Live goldens, oldest first. ``trial_ids`` finds a copy's golden too."""
    query = (
        select(QAGoldenCaseModel, golden_state())
        .where(QAGoldenCaseModel.org_id == org_id)
        .order_by(QAGoldenCaseModel.created_at, QAGoldenCaseModel.id)
    )
    if stage is not None:
        query = query.where(QAGoldenCaseModel.stage == stage.value)
    if source is not None:
        query = query.where(QAGoldenCaseModel.source == source)
    if state is not None:
        query = query.where(golden_state() == state)
    if trial_ids:
        query = query.where(
            QAGoldenCaseModel.source_trial_id.in_(
                select(original_id(TrialModel)).where(
                    TrialModel.id.in_(trial_ids), TrialModel.org_id == org_id
                )
            )
        )
    rows = await session.execute(query)
    return [_with_state(QAGoldenResponse, golden, state) for golden, state in rows]


async def review_golden(
    session: AsyncSession, *, golden_id: str, org_id: str | None, user_id: str | None
) -> QAGoldenResponse:
    """Confirm a golden, as a same-label ``label_trial`` does. The caller
    commits."""
    golden = await _golden_in_org(session, golden_id, org_id)
    return await _confirm(session, golden, user_id)


async def retire_golden(
    session: AsyncSession, *, golden_id: str, org_id: str | None
) -> None:
    """Soft-delete a golden, freeing its subject for a corrected label."""
    golden = await _golden_in_org(session, golden_id, org_id)
    golden.deleted_at = utcnow()
    await session.flush()


async def label_trial(
    session: AsyncSession,
    *,
    trial_id: str,
    data: QAGoldenLabel,
    org_id: str | None,
    user_id: str | None,
) -> QAGoldenResponse:
    """Set a solver trial's post-trial label, stored confirmed. The same
    label confirms the live golden (and sets its note when one is sent); a
    different one retires it and adds a new golden, so history keeps both.
    The caller commits."""
    trial = await _solver_trial(session, trial_id, org_id)
    current = await session.scalar(
        select(QAGoldenCaseModel).where(
            QAGoldenCaseModel.org_id == org_id,
            QAGoldenCaseModel.stage == QAStage.POST_TRIAL.value,
            QAGoldenCaseModel.source_trial_id == trial.id,
        )
    )
    if current is not None and current.expected["classification"] == (
        data.classification
    ):
        if data.note is not None:
            current.note = data.note
        return await _confirm(session, current, user_id)
    if current is not None:
        current.deleted_at = utcnow()
        await session.flush()
    golden = await _add(
        session,
        QAGoldenCreate(
            stage=QAStage.POST_TRIAL,
            source_trial_id=trial.id,
            expected={"classification": data.classification},
            note=data.note or "",
        ),
        org_id,
        user_id,
    )
    return await _confirm(session, golden, user_id)


async def label_queue(
    session: AsyncSession,
    *,
    org_id: str | None,
    experiment: str | None = None,
    review: QAGoldenReview | None = None,
) -> QAGoldenQueue:
    """Finished solver trials to label, nop and oracle left out: an
    experiment's (by id or name), or with ``review`` the trials of the
    post-trial goldens in that state. Each carries production QA's answer
    and its live golden; failed first, then by task."""
    if (experiment is None) == (review is None):
        raise HTTPException(
            status_code=422, detail="give exactly one of experiment and review"
        )
    if review is not None:
        trials, title = TrialModel, _REVIEW_TITLES[review]
        # The golden's own trial; its copies would list it again.
        where: tuple[Any, ...] = (
            TrialModel.org_id == org_id,
            TrialModel.id == QAGoldenCaseModel.source_trial_id,
            golden_state() == review,
        )
    else:
        found = await get_experiment_by_id_or_name(session, experiment.strip(), org_id)
        if found is None:
            raise HTTPException(
                status_code=404, detail=f"no experiment with id or name {experiment!r}"
            )
        # Every trial the experiment page shows, gathered ones included.
        scope = experiment_trial_scope(found.id, org_id=org_id)
        trials, title = scope.trials, found.name
        where = (*scope.visible_predicates(), trials.status.in_(FINISHED_STATUSES))
    rows = await session.execute(
        select(
            trials.id,
            trials.task_id,
            TaskModel.name,
            trials.reward,
            trials.agent,
            trials.analysis["classification"].astext,
            trials.analysis["subtype"].astext,
            trials.analysis["root_cause"].astext,
            QAGoldenCaseModel,
            golden_state(),
        )
        .join(TaskModel, TaskModel.id == trials.task_id)
        .outerjoin(
            QAGoldenCaseModel,
            and_(
                QAGoldenCaseModel.org_id == org_id,
                QAGoldenCaseModel.stage == QAStage.POST_TRIAL.value,
                QAGoldenCaseModel.source_trial_id == original_id(trials),
            ),
        )
        .where(trials.kind == AGENT_TRIAL_KIND, *where)
    )
    queued = [
        QAGoldenQueueTrial(
            trial_id=trial_id,
            task_id=task_id,
            task_name=task_name,
            reward=reward,
            group=(
                "labeled"
                if state == "counted"
                else "passed"
                if reward == 1
                else "failed"
            ),
            qa_classification=classification,
            qa_subtype=subtype,
            qa_reason=reason,
            golden=golden and _with_state(QAGoldenRef, golden, state),
        )
        for (
            trial_id,
            task_id,
            task_name,
            reward,
            agent,
            classification,
            subtype,
            reason,
            golden,
            state,
        ) in rows
        if not is_nop_oracle_agent(agent)
    ]
    queued.sort(key=lambda t: (t.reward == 1, t.task_name, t.trial_id))
    return QAGoldenQueue(title=title, trials=queued)
