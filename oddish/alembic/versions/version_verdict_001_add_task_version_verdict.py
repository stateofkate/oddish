"""Add task_versions.verdict and copy each task's current verdict onto it.

Revision ID: version_verdict_001
Revises: oddish_mon_003

``tasks.verdict`` keeps only the newest QA verdict, so a version loses its
own when a later version is reviewed. Each version now keeps the last
verdict published for it. The copy gives the owning version of every current
verdict its row: the version the verdict names, or for older verdicts the
version of the exact QA run that produced it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "version_verdict_001"
down_revision: str | Sequence[str] | None = "oddish_mon_003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column() -> bool:
    columns = sa.inspect(op.get_bind()).get_columns("task_versions")
    return "verdict" in {item["name"] for item in columns}


def upgrade() -> None:
    if not _has_column():
        op.add_column("task_versions", sa.Column("verdict", JSONB, nullable=True))
    op.execute(
        """
        UPDATE task_versions v
        SET verdict = t.verdict || jsonb_build_object('_task_version_id', v.id)
        FROM tasks t
        WHERE v.task_id = t.id AND v.verdict IS NULL
          AND jsonb_typeof(t.verdict) = 'object'
          AND v.id = COALESCE(t.verdict->>'_task_version_id', (
            SELECT q.task_version_id FROM trials q
            WHERE q.id = t.verdict->>'_graded_by' AND q.task_id = t.id
              AND q.kind = 'qa' AND q.status = 'SUCCESS' AND q.deleted_at IS NULL
          ))
        """
    )


def downgrade() -> None:
    if _has_column():
        op.drop_column("task_versions", "verdict")
