"""Add worker_jobs.monitor_acked_at.

Revision ID: oddish_mon_002
Revises: oddish_mon_001

The launcher may only hand a job off once the in-sandbox monitor has checked
in with its own attempt credential. Launcher heartbeats share the worker id,
so they cannot serve as that signal; this timestamp does.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "oddish_mon_002"
down_revision: Union[str, Sequence[str], None] = "oddish_mon_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column() -> bool:
    columns = sa.inspect(op.get_bind()).get_columns("worker_jobs")
    return "monitor_acked_at" in {item["name"] for item in columns}


def upgrade() -> None:
    if not _has_column():
        op.add_column(
            "worker_jobs",
            sa.Column("monitor_acked_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    if _has_column():
        op.drop_column("worker_jobs", "monitor_acked_at")
