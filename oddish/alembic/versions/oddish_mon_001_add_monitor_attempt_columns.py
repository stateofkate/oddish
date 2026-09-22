"""Add in-sandbox monitor attempt columns to worker_jobs.

Revision ID: oddish_mon_001
Revises: task_run_coverage_001

The trial driver is moving into the sandbox as oddish-mon (ABT-1285). These
nullable columns hold what the long-running worker kept in memory: the
monitor's own attempt token, the status-check count, and the posted result.
Nothing reads them yet.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "oddish_mon_001"
down_revision: Union[str, Sequence[str], None] = "task_run_coverage_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("monitor_token_hash", sa.Text()),
    ("monitor_token_expires_at", sa.DateTime(timezone=True)),
    ("monitor_token_revoked_at", sa.DateTime(timezone=True)),
    ("monitor_status_check_count", sa.Integer()),
    ("monitor_status_checked_at", sa.DateTime(timezone=True)),
    ("monitor_result", postgresql.JSONB()),
    ("monitor_result_accepted_at", sa.DateTime(timezone=True)),
)


def _existing_columns() -> set[str]:
    return {
        item["name"] for item in sa.inspect(op.get_bind()).get_columns("worker_jobs")
    }


def upgrade() -> None:
    existing = _existing_columns()
    for name, column_type in _COLUMNS:
        if name not in existing:
            op.add_column("worker_jobs", sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    existing = _existing_columns()
    for name, _ in reversed(_COLUMNS):
        if name in existing:
            op.drop_column("worker_jobs", name)
