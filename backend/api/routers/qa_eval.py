"""QA eval endpoints for org admins: replay QA prompts and read their scores."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel
from sqlalchemy import text

from auth import AuthContext, require_admin
from auth.permissions import require_operator_org
from oddish.analyze.qa_stages import QAStage
from oddish.core.dashboard import invalidate_dashboard_cache
from oddish.core.idempotency import IdempotencyReplay, compute_request_hash
from oddish.core.endpoints.qa_eval import create_qa_eval_core
from oddish.core.qa_eval_scores import (
    get_prompt_version_text_core,
    get_qa_eval_run_core,
    qa_eval_status_core,
)
from oddish.config import settings
from oddish.db import get_read_session, get_session, utcnow
from oddish.schemas import (
    QAEvalCreateRequest,
    QAEvalCreateResponse,
    QAEvalRunResponse,
    QAEvalStatusResponse,
    QAPromptVersionText,
)
from idempotency_store import SubmissionIdempotencyStore

router = APIRouter(tags=["QA evaluations"])

Admin = Annotated[AuthContext, Depends(require_admin)]


class QAEvalModelOption(BaseModel):
    # The queue key a trial runs under, e.g. anthropic/claude-sonnet-5-5.
    model: str
    # healthy: the latest scheduled check passed within the stale window;
    # failing: it failed; unchecked: no recent check, or the check itself broke.
    health: Literal["healthy", "failing", "unchecked"]
    last_checked_at: datetime | None


class QAEvalModels(BaseModel):
    production_model: str
    # Healthy first, then by name.
    models: list[QAEvalModelOption]


@router.post("/qa-evals", response_model=QAEvalCreateResponse)
async def create_qa_eval(
    payload: QAEvalCreateRequest,
    auth: Admin,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> QAEvalCreateResponse:
    async with get_session() as session:
        try:
            result = await create_qa_eval_core(
                session,
                request=payload,
                org_id=auth.org_id,
                owner_user_id=auth.user_id,
                billed_user_id=auth.user_id,
                idempotency_key=idempotency_key,
                idempotency_store=SubmissionIdempotencyStore(session),
                request_hash=compute_request_hash(payload),
            )
        except IdempotencyReplay as replay:
            return QAEvalCreateResponse.model_validate(replay.response_json)
        await session.commit()
    invalidate_dashboard_cache(org_id=auth.org_id)
    return result


@router.get("/qa-evals/models", response_model=QAEvalModels)
async def get_qa_eval_models(auth: Admin) -> QAEvalModels:
    """The models the endpoint monitor checks, for choosing what a run uses."""
    from endpoint_health import STALE_AFTER

    require_operator_org(auth)
    now = utcnow()
    async with get_read_session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT name, last_outcome, last_checked_at "
                    "FROM endpoint_monitors WHERE enabled"
                )
            )
        ).all()
    options = []
    for name, outcome, checked_at in rows:
        fresh = checked_at is not None and now - checked_at <= STALE_AFTER
        health = "unchecked"
        if fresh and outcome in ("success", "failure"):
            health = "healthy" if outcome == "success" else "failing"
        options.append(
            QAEvalModelOption(model=name, health=health, last_checked_at=checked_at)
        )
    order = {"healthy": 0, "unchecked": 1, "failing": 2}
    options.sort(key=lambda option: (order[option.health], option.model))
    return QAEvalModels(production_model=settings.qa_model, models=options)


@router.get("/qa-evals/status", response_model=QAEvalStatusResponse)
async def get_qa_eval_status(
    auth: Admin, stage: QAStage = QAStage.POST_TRIAL
) -> QAEvalStatusResponse:
    async with get_read_session() as session:
        return await qa_eval_status_core(session, org_id=auth.org_id, stage=stage)


@router.get("/qa-evals/versions/{sha256}", response_model=QAPromptVersionText)
async def get_qa_prompt_version_text(
    sha256: str, auth: Admin, stage: QAStage = QAStage.POST_TRIAL
) -> QAPromptVersionText:
    async with get_read_session() as session:
        return await get_prompt_version_text_core(
            session, org_id=auth.org_id, stage=stage, sha256=sha256
        )


@router.get("/qa-evals/runs/{experiment_id}", response_model=QAEvalRunResponse)
async def get_qa_eval_run(experiment_id: str, auth: Admin) -> QAEvalRunResponse:
    async with get_read_session() as session:
        return await get_qa_eval_run_core(
            session, org_id=auth.org_id, experiment_id=experiment_id
        )
