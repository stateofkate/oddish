"""Add worker_jobs.monitor_handed_off_at.

Revision ID: oddish_mon_003
Revises: oddish_mon_002

The launcher's hand-off is a fenced write like the rest of the launch: the row
is stamped handed off only while it is still this attempt's RUNNING job. A
re-claim between recording the sandbox handle and the hand-off clears the
handle, so a hand-off that writes no row stops the sandbox it started.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "oddish_mon_003"
down_revision: str | Sequence[str] | None = "oddish_mon_002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column() -> bool:
    columns = sa.inspect(op.get_bind()).get_columns("worker_jobs")
    return "monitor_handed_off_at" in {item["name"] for item in columns}


def upgrade() -> None:
    if not _has_column():
        op.add_column(
            "worker_jobs",
            sa.Column(
                "monitor_handed_off_at", sa.DateTime(timezone=True), nullable=True
            ),
        )


def downgrade() -> None:
    if _has_column():
        op.drop_column("worker_jobs", "monitor_handed_off_at")
