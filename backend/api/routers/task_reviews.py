"""Internal reviews for a task version."""

from datetime import datetime
from typing import Annotated
from uuid import uuid4

from auth import AuthContext, AuthMethod, require_auth
from fastapi import APIRouter, Depends, HTTPException, Query
from models import (
    TaskReviewCommentModel as Comments,
)
from models import (
    TaskReviewRatingModel as Ratings,
)
from models import (
    UserModel,
    UserRole,
)
from oddish.db import get_read_session, get_session, utcnow
from oddish.db.models import (
    TaskModel,
    TaskVersionModel,
    TrialModel,
)
from sqlalchemy import func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.task_review_schemas import (
    Comment,
    CommentCreate,
    CommentDelete,
    CommentEdit,
    Rating,
    RatingWrite,
    Reviewer,
    ReviewPage,
    WriteResult,
)

router = APIRouter(
    prefix="/tasks/{task_id}/versions/{version_id}/reviews", tags=["Task reviews"]
)


def member(auth: Annotated[AuthContext, Depends(require_auth)]) -> AuthContext:
    if auth.method != AuthMethod.CLERK_JWT or not auth.user_id or not auth.org_id:
        raise HTTPException(403, "Task reviews require member login")
    return auth


Member = Annotated[AuthContext, Depends(member)]


async def subject(
    session: AsyncSession,
    task_id: str,
    version_id: str,
    auth: AuthContext,
    *,
    write: bool = False,
):
    statement = (
        select(TaskVersionModel.id)
        .join(TaskModel, TaskModel.id == TaskVersionModel.task_id)
        .where(
            TaskModel.id == task_id,
            TaskModel.org_id == auth.org_id,
            TaskModel.deleted_at.is_(None),
            TaskVersionModel.id == version_id,
            TaskVersionModel.deleted_at.is_(None),
        )
    )
    if write:
        statement = statement.with_for_update(of=TaskVersionModel)
    if await session.scalar(statement) is None:
        raise HTTPException(404, "Task version not found")


def scope(model, auth, version_id):
    return (model.org_id == auth.org_id, model.task_version_id == version_id)


def reviewer(user):
    return Reviewer(id=user.id, name=user.name or "Reviewer")


def rating(row, user):
    return Rating(
        id=row.id,
        reviewer=reviewer(user),
        score=row.score,
        revision=row.revision,
        updated_at=row.updated_at,
    )


@router.get("", response_model=ReviewPage)
async def read_reviews(
    task_id: str,
    version_id: str,
    auth: Member,
    before: datetime | None = None,
    before_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    if (before is None) != (before_id is None) or (
        before is not None and before.tzinfo is None
    ):
        raise HTTPException(422, "Use both cursor fields with a timezone")
    async with get_read_session() as session:
        await subject(session, task_id, version_id, auth)
        filters = scope(Ratings, auth, version_id)
        average, count = (
            await session.execute(
                select(func.avg(Ratings.score), func.count()).where(
                    *filters, Ratings.deleted_at.is_(None)
                )
            )
        ).one()
        rows = (
            await session.execute(
                select(Ratings, UserModel)
                .join(UserModel, UserModel.id == Ratings.reviewer_id)
                .where(*filters, Ratings.deleted_at.is_(None))
                .order_by(Ratings.updated_at.desc(), Ratings.id)
                .limit(100)
            )
        ).all()
        own = await session.scalar(
            select(Ratings)
            .where(*filters, Ratings.reviewer_id == auth.user_id)
            .order_by(Ratings.updated_at.desc(), Ratings.id.desc())
            .limit(1)
        )
        comments_query = (
            select(Comments, UserModel)
            .join(UserModel, UserModel.id == Comments.reviewer_id)
            .where(
                *scope(Comments, auth, version_id),
                Comments.deleted_at.is_(None),
            )
        )
        if before is not None:
            comments_query = comments_query.where(
                tuple_(Comments.created_at, Comments.id) < tuple_(before, before_id)
            )
        comments = (
            await session.execute(
                comments_query.order_by(
                    Comments.created_at.desc(), Comments.id.desc()
                ).limit(limit + 1)
            )
        ).all()
        page = comments[:limit]
        own_user = (
            await session.get(UserModel, auth.user_id) if own is not None else None
        )
        return ReviewPage(
            task_version_id=version_id,
            current_user_id=auth.user_id,
            can_moderate=auth.user_role == UserRole.ADMIN,
            average_score=float(average) if average is not None else None,
            rating_count=count,
            ratings=[rating(r, u) for r, u in rows],
            own_rating=rating(own, own_user)
            if own and own.deleted_at is None
            else None,
            own_rating_revision=own.revision if own else 0,
            comments=[
                Comment(
                    id=r.id,
                    reviewer=reviewer(u),
                    body=r.body,
                    source_trial_id=r.source_trial_id,
                    revision=r.revision,
                    created_at=r.created_at,
                    updated_at=r.updated_at,
                )
                for r, u in page
            ],
            next_before=page[-1][0].created_at if len(comments) > limit else None,
            next_before_id=page[-1][0].id if len(comments) > limit else None,
        )


@router.put("/rating", response_model=WriteResult)
async def write_rating(task_id: str, version_id: str, data: RatingWrite, auth: Member):
    async with get_session() as session:
        await subject(session, task_id, version_id, auth, write=True)
        filters = (
            *scope(Ratings, auth, version_id),
            Ratings.reviewer_id == auth.user_id,
        )
        existing = await session.scalar(
            select(Ratings)
            .where(*filters)
            .order_by(Ratings.updated_at.desc(), Ratings.id.desc())
            .limit(1)
        )
        if data.expected_revision != (existing.revision if existing else 0):
            raise HTTPException(409, "Rating changed")
        if existing is None:
            if data.score is None:
                raise HTTPException(409, "Rating does not exist")
            existing = Ratings(
                id=str(uuid4()),
                org_id=auth.org_id,
                reviewer_id=auth.user_id,
                task_version_id=version_id,
                score=data.score,
                revision=1,
            )
            session.add(existing)
        else:
            if data.score is not None:
                existing.score = data.score
            existing.deleted_at = utcnow() if data.score is None else None
            existing.updated_at = utcnow()
            existing.revision += 1
        return WriteResult(id=existing.id, revision=existing.revision)


@router.post("/comments", response_model=WriteResult)
async def create_comment(
    task_id: str, version_id: str, data: CommentCreate, auth: Member
):
    async with get_session() as session:
        await subject(session, task_id, version_id, auth, write=True)
        if (
            data.source_trial_id is not None
            and await session.scalar(
                select(TrialModel.id).where(
                    TrialModel.id == data.source_trial_id,
                    TrialModel.task_id == task_id,
                    TrialModel.task_version_id == version_id,
                    TrialModel.org_id == auth.org_id,
                    TrialModel.deleted_at.is_(None),
                )
            )
            is None
        ):
            raise HTTPException(422, "Trial does not belong to this task version")
        values = {
            "id": str(uuid4()),
            "org_id": auth.org_id,
            "reviewer_id": auth.user_id,
            "task_version_id": version_id,
            "body": data.body,
            "source_trial_id": data.source_trial_id,
            "client_request_id": str(data.client_request_id),
        }
        saved = (
            await session.execute(
                insert(Comments)
                .values(**values)
                .on_conflict_do_nothing()
                .returning(Comments.id, Comments.revision)
            )
        ).one_or_none()
        if saved is None:
            existing = await session.scalar(
                select(Comments).where(
                    Comments.org_id == auth.org_id,
                    Comments.reviewer_id == auth.user_id,
                    Comments.client_request_id == str(data.client_request_id),
                )
            )
            if (
                existing is None
                or any(
                    getattr(existing, field) != values[field]
                    for field in (
                        "task_version_id",
                        "body",
                        "source_trial_id",
                    )
                )
                or existing.deleted_at is not None
            ):
                raise HTTPException(409, "Comment request already used")
            return WriteResult(id=existing.id, revision=existing.revision)
        return WriteResult(id=saved.id, revision=saved.revision)


@router.patch("/comments/{comment_id}", response_model=WriteResult)
async def edit_comment(
    task_id: str, version_id: str, comment_id: str, data: CommentEdit, auth: Member
):
    async with get_session() as session:
        await subject(session, task_id, version_id, auth, write=True)
        filters = [
            *scope(Comments, auth, version_id),
            Comments.id == comment_id,
            Comments.deleted_at.is_(None),
            Comments.revision == data.expected_revision,
        ]
        if auth.user_role != UserRole.ADMIN:
            filters.append(Comments.reviewer_id == auth.user_id)
        values = {"updated_at": utcnow(), "revision": Comments.revision + 1}
        if data.body is None:
            values["deleted_at"] = utcnow()
        else:
            values["body"] = data.body
        saved = (
            await session.execute(
                update(Comments)
                .where(*filters)
                .values(**values)
                .returning(Comments.id, Comments.revision)
            )
        ).one_or_none()
        if saved is None:
            raise HTTPException(409, "Comment changed or cannot be edited")
        return WriteResult(id=saved.id, revision=saved.revision)


@router.delete("/comments/{comment_id}", response_model=WriteResult)
async def delete_comment(
    task_id: str, version_id: str, comment_id: str, data: CommentDelete, auth: Member
):
    return await edit_comment(
        task_id,
        version_id,
        comment_id,
        CommentEdit(body=None, expected_revision=data.expected_revision),
        auth,
    )
