"""Authenticated endpoints for append-only QA review votes."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from auth import APIKeyScope, AuthContext, require_auth
from oddish.core.feedback import (
    create_feedback_core,
    create_task_feedback_core,
    list_feedback_core,
)
from oddish.db import get_read_session, get_session
from oddish.schemas import FeedbackCreate, FeedbackResponse, FeedbackVote

router = APIRouter()


@router.post(
    "/experiments/{experiment_id}/feedback", response_model=FeedbackResponse
)
async def create_feedback(
    experiment_id: str,
    data: FeedbackCreate,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> FeedbackResponse:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        row = await create_feedback_core(
            session,
            data=data,
            experiment_id=experiment_id,
            org_id=auth.org_id,
            user_id=auth.user_id,
        )
        await session.commit()
        return FeedbackResponse.model_validate(row)


@router.post("/tasks/{task_id}/feedback", response_model=FeedbackResponse)
async def create_task_feedback(
    task_id: str,
    data: FeedbackCreate,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> FeedbackResponse:
    """Vote on one of the task's trials: its pre-trial audit or a trajectory analysis."""
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        row = await create_task_feedback_core(
            session,
            data=data,
            task_id=task_id,
            org_id=auth.org_id,
            user_id=auth.user_id,
        )
        await session.commit()
        return FeedbackResponse.model_validate(row)


@router.get("/feedback", response_model=list[FeedbackResponse])
async def list_feedback(
    auth: Annotated[AuthContext, Depends(require_auth)],
    experiment_id: str | None = None,
    vote: FeedbackVote | None = None,
    since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 200,
) -> list[FeedbackResponse]:
    """The caller's org's QA votes, newest first."""
    auth.require_scope(APIKeyScope.TASKS)
    async with get_read_session() as session:
        rows = await list_feedback_core(
            session,
            org_id=auth.org_id,
            experiment_id=experiment_id,
            vote=vote,
            since=since,
            limit=limit,
        )
        return [FeedbackResponse.model_validate(row) for row in rows]
