"""Queue review DMs in the review transaction; the existing Slack sender delivers."""

import html
import os
from urllib.parse import quote

from api.routers.task_submission import resolve_connected_user
from models import (
    OrganizationModel,
    SlackExpenseAlertModel,
    TaskReviewCommentModel,
    TaskReviewRatingModel,
    UserModel,
)
from oddish.db import utcnow
from oddish.db.models import TaskModel, TaskVersionModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from user_alert_prefs import get_user_alert_prefs


async def queue_review_notification(session: AsyncSession, comment_id: str) -> None:
    if os.environ.get("ODDISH_ENABLE_SLACK_EXPENSE_NOTIFICATIONS", "").lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return
    comment, version, task = (
        await session.execute(
            select(TaskReviewCommentModel, TaskVersionModel, TaskModel)
            .join(
                TaskVersionModel,
                TaskVersionModel.id == TaskReviewCommentModel.task_version_id,
            )
            .join(TaskModel, TaskModel.id == TaskVersionModel.task_id)
            .where(TaskReviewCommentModel.id == comment_id)
        )
    ).one()
    tags = task.tags or {}
    if tags.get("github_id") or tags.get("github_username"):
        author = await resolve_connected_user(
            session,
            org_id=task.org_id,
            github_id=tags.get("github_id"),
            github_username=tags.get("github_username"),
        )
    else:
        author = await session.scalar(
            select(UserModel).where(
                UserModel.id == task.created_by_user_id,
                UserModel.org_id == task.org_id,
                UserModel.is_active.is_(True),
                UserModel.deleted_at.is_(None),
            )
        )
        # Imported tasks can belong to someone other than their uploader.
        if (
            author
            and task.user != author.id
            and (task.user or "").strip().lower()
            != (author.email or "").strip().lower()
        ):
            author = None
    if (
        not author
        or author.deleted_at
        or not (author.email or "").strip()
    ):
        return
    if not (await get_user_alert_prefs(session, author.id)).task_review_enabled:
        return
    reviewer = await session.get(UserModel, comment.reviewer_id)
    score = await session.scalar(
        select(TaskReviewRatingModel.score)
        .where(
            TaskReviewRatingModel.org_id == comment.org_id,
            TaskReviewRatingModel.task_version_id == version.id,
            TaskReviewRatingModel.reviewer_id == comment.reviewer_id,
            TaskReviewRatingModel.deleted_at.is_(None),
        )
        .order_by(TaskReviewRatingModel.updated_at.desc())
        .limit(1)
    )
    base = os.environ.get("ODDISH_DASHBOARD_URL", "https://www.oddish.app").rstrip("/")
    slug = await session.scalar(
        select(OrganizationModel.slug).where(OrganizationModel.id == task.org_id)
    )
    url = f"{base}/orgs/{quote(slug, safe='')}/tasks/{quote(task.id, safe='')}?version={quote(version.id, safe='')}"
    action = "reviewed" if comment.revision == 1 else "updated their review of"
    text = (
        f"*{html.escape(reviewer.name or 'Reviewer', quote=False)}* {action} "
        f"*{html.escape(task.name, quote=False)}* · v{version.version}"
        + (f" · {score}/5" if score is not None else "")
        + f"\n{html.escape(comment.body[:2000], quote=False)}\n<{url}|Open task>"
    )
    await session.execute(
        insert(SlackExpenseAlertModel)
        .values(
            alert_key=f"task-review:{comment.id}:{comment.revision}",
            claimed_at=utcnow(),
            payload=text,
            recipient_email=author.email.strip().lower(),
            recipient_clerk_user_id=author.clerk_user_id,
        )
        .on_conflict_do_nothing(index_elements=[SlackExpenseAlertModel.alert_key])
    )
