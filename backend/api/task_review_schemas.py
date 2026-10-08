"""Contracts for internal task-version reviews."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_hash: str = Field(min_length=1, max_length=64)
    content_algorithm: str = Field(min_length=1, max_length=64)


class RatingWrite(Snapshot):
    score: int | None = Field(ge=1, le=5, strict=True)
    expected_revision: int = Field(ge=0, strict=True)


CommentBody = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=10000, strict=True
    ),
]


class CommentCreate(Snapshot):
    body: CommentBody
    client_request_id: UUID
    source_trial_id: str | None = Field(default=None, max_length=160)


class CommentEdit(Snapshot):
    body: CommentBody | None
    expected_revision: int = Field(ge=1, strict=True)


class Reviewer(BaseModel):
    id: str
    name: str


class Rating(BaseModel):
    id: str
    reviewer: Reviewer
    score: int
    revision: int
    updated_at: datetime


class Comment(BaseModel):
    id: str
    reviewer: Reviewer
    body: str
    source_trial_id: str | None
    revision: int
    created_at: datetime
    updated_at: datetime


class ReviewPage(Snapshot):
    task_version_id: str
    current_user_id: str
    can_moderate: bool
    average_score: float | None
    rating_count: int
    own_rating: Rating | None
    own_rating_revision: int
    ratings: list[Rating]
    comments: list[Comment]
    next_before: datetime | None = None
    next_before_id: str | None = None


class WriteResult(BaseModel):
    id: str
    revision: int
