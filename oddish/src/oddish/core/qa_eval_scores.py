"""QA prompt versions and goldens as the replay endpoint needs them: the
recorded prompt a hash names, and the goldens that count."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.analyze.qa_stages import (
    STAGES,
    QAStage,
    shipped_prompt,
    shipped_prompt_sha256,
)
from oddish.core.qa_goldens import golden_state
from oddish.db import QAGoldenCaseModel, QAPromptModel, TaskVersionModel, TrialModel


def _shipped_name(stage: QAStage) -> str:
    return f"production {shipped_prompt_sha256(stage)[:8]}"


async def _prompt_rows(
    session: AsyncSession, org_id: str | None, stage: QAStage, sha256: str | None
) -> dict[str, QAPromptModel]:
    """Recorded versions visible to the org by hash. A shipped (global) row
    wins over the org's copy of the same text."""
    query = select(QAPromptModel).where(
        QAPromptModel.stage == stage.value,
        or_(QAPromptModel.org_id == org_id, QAPromptModel.org_id.is_(None)),
    )
    if sha256 is not None:
        query = query.where(QAPromptModel.sha256 == sha256)
    rows: dict[str, QAPromptModel] = {}
    for row in await session.scalars(
        query.order_by(QAPromptModel.org_id.is_not(None), QAPromptModel.created_at)
    ):
        rows.setdefault(row.sha256, row)
    return rows


async def prompt_version(
    session: AsyncSession, org_id: str | None, stage: QAStage, sha256: str
) -> tuple[str, str] | None:
    """The name and text of a recorded or shipped prompt version."""
    row = (await _prompt_rows(session, org_id, stage, sha256)).get(sha256)
    if row is not None:
        return row.name, row.text
    if sha256 == shipped_prompt_sha256(stage):
        return _shipped_name(stage), shipped_prompt(stage)
    return None


_Golden = tuple[str, QAGoldenCaseModel, str]


async def _goldens(
    session: AsyncSession, org_id: str | None, stage: QAStage
) -> list[_Golden]:
    """Live goldens whose subject still exists, as (subject id, golden,
    state)."""
    if STAGES[stage].subject == "trial":
        subject = QAGoldenCaseModel.source_trial_id
        exists = select(TrialModel.id).where(TrialModel.id == subject).exists()
    else:
        subject = QAGoldenCaseModel.task_version_id
        exists = (
            select(TaskVersionModel.id).where(TaskVersionModel.id == subject).exists()
        )
    result = await session.execute(
        select(subject, QAGoldenCaseModel, golden_state()).where(
            QAGoldenCaseModel.org_id == org_id,
            QAGoldenCaseModel.stage == stage.value,
            exists,
        )
    )
    return [(subject, golden, state) for subject, golden, state in result.all()]


def _counted(goldens: list[_Golden]) -> dict[str, QAGoldenCaseModel]:
    return {subject: golden for subject, golden, state in goldens if state == "counted"}


async def counted_goldens(
    session: AsyncSession, org_id: str | None, stage: QAStage
) -> dict[str, QAGoldenCaseModel]:
    """The goldens that score runs, by subject id."""
    return _counted(await _goldens(session, org_id, stage))
