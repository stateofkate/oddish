"""QA prompt replay endpoint for org admins."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header

from auth import AuthContext, require_admin
from oddish.core.dashboard import invalidate_dashboard_cache
from oddish.core.idempotency import IdempotencyReplay, compute_request_hash
from oddish.core.endpoints.qa_eval import create_qa_eval_core
from oddish.db import get_session
from oddish.schemas import (
    QAEvalCreateRequest,
    QAEvalCreateResponse,
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
