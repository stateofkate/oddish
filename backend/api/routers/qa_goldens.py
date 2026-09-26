"""QA golden endpoints: org admins label solver trials for scoring QA."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from auth import AuthContext, require_admin
from oddish.analyze.qa_stages import QAStage
from oddish.core.qa_goldens import (
    add_golden,
    label_queue,
    label_trial,
    list_goldens,
    retire_golden,
    review_golden,
)
from oddish.db import get_read_session, get_session
from oddish.schemas import (
    QAGoldenCreate,
    QAGoldenLabel,
    QAGoldenQueue,
    QAGoldenResponse,
    QAGoldenReview,
    QAGoldenState,
)

router = APIRouter(tags=["QA evaluations"])


@router.post("/qa-goldens", response_model=QAGoldenResponse)
async def create_golden(
    data: QAGoldenCreate, auth: Annotated[AuthContext, Depends(require_admin)]
) -> QAGoldenResponse:
    async with get_session() as session:
        golden = await add_golden(
            session, data=data, org_id=auth.org_id, user_id=auth.user_id
        )
        await session.commit()
    return golden


@router.get("/qa-goldens", response_model=list[QAGoldenResponse])
async def get_goldens(
    auth: Annotated[AuthContext, Depends(require_admin)],
    stage: QAStage | None = None,
    source: str | None = None,
    state: QAGoldenState | None = None,
    trial: Annotated[list[str] | None, Query()] = None,
) -> list[QAGoldenResponse]:
    async with get_read_session() as session:
        return await list_goldens(
            session,
            org_id=auth.org_id,
            stage=stage,
            source=source,
            state=state,
            trial_ids=trial,
        )


@router.get("/qa-goldens/trials", response_model=QAGoldenQueue)
async def get_label_queue(
    auth: Annotated[AuthContext, Depends(require_admin)],
    experiment: Annotated[str | None, Query(min_length=1)] = None,
    review: QAGoldenReview | None = None,
) -> QAGoldenQueue:
    async with get_read_session() as session:
        return await label_queue(
            session, org_id=auth.org_id, experiment=experiment, review=review
        )


@router.put("/qa-goldens/trials/{trial_id}", response_model=QAGoldenResponse)
async def put_trial_label(
    trial_id: str,
    data: QAGoldenLabel,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> QAGoldenResponse:
    async with get_session() as session:
        golden = await label_trial(
            session,
            trial_id=trial_id,
            data=data,
            org_id=auth.org_id,
            user_id=auth.user_id,
        )
        await session.commit()
    return golden


@router.post("/qa-goldens/{golden_id}/review", response_model=QAGoldenResponse)
async def post_review(
    golden_id: str, auth: Annotated[AuthContext, Depends(require_admin)]
) -> QAGoldenResponse:
    async with get_session() as session:
        golden = await review_golden(
            session, golden_id=golden_id, org_id=auth.org_id, user_id=auth.user_id
        )
        await session.commit()
    return golden


@router.delete("/qa-goldens/{golden_id}", status_code=204)
async def delete_golden(
    golden_id: str, auth: Annotated[AuthContext, Depends(require_admin)]
) -> Response:
    async with get_session() as session:
        await retire_golden(session, golden_id=golden_id, org_id=auth.org_id)
        await session.commit()
    return Response(status_code=204)
