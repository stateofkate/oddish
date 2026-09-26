"""QA eval endpoints for org admins: replay QA prompts and read their scores."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header

from auth import AuthContext, require_admin
from oddish.analyze.qa_stages import QAStage
from oddish.core.dashboard import invalidate_dashboard_cache
from oddish.core.idempotency import IdempotencyReplay, compute_request_hash
from oddish.core.endpoints.qa_eval import create_qa_eval_core
from oddish.core.qa_eval_scores import (
    get_prompt_version_text_core,
    get_qa_eval_run_core,
    qa_eval_status_core,
)
from oddish.db import get_read_session, get_session
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
