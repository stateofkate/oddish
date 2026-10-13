"""Track automatic adversarial QA per task version so QA can wait for it.

Revision ID: adversarial_status_001
Revises: task_cost_settled_002
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "adversarial_status_001"
down_revision = "task_cost_settled_002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A fresh database's base migration builds tables from the current models,
    # which already carry these columns.
    existing = {
        c["name"] for c in sa.inspect(op.get_bind()).get_columns("task_versions")
    }
    columns = (
        sa.Column(
            "adversarial_status",
            postgresql.ENUM(name="jobstatus", create_type=False),
            nullable=True,
        ),
        sa.Column("adversarial_error", sa.Text(), nullable=True),
        sa.Column("adversarial_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in columns:
        if column.name not in existing:
            op.add_column("task_versions", column)


def downgrade() -> None:
    op.drop_column("task_versions", "adversarial_started_at")
    op.drop_column("task_versions", "adversarial_error")
    op.drop_column("task_versions", "adversarial_status")
