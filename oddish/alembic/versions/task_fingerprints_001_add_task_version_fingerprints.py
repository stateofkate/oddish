"""Add server-computed task version content hashes.

A new table only: ``task_versions`` is not altered.

Revision ID: task_fingerprints_001
Revises: tokens_exact_001
Create Date: 2026-10-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "task_fingerprints_001"
down_revision: Union[str, Sequence[str], None] = "tokens_exact_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Fresh databases bootstrap from current models (000_initial), which
    # already include this table; every step is guarded.
    op.create_table(
        "task_version_fingerprints",
        sa.Column(
            "task_version_id",
            sa.String(160),
            sa.ForeignKey("task_versions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("algorithm", sa.String(64), nullable=False),
        sa.Column("client_content_hash", sa.String(128), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        if_not_exists=True,
    )
    op.create_index(
        "idx_task_version_fingerprints_content_hash",
        "task_version_fingerprints",
        ["content_hash"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_task_version_fingerprints_content_hash",
        table_name="task_version_fingerprints",
        if_exists=True,
    )
    op.drop_table("task_version_fingerprints", if_exists=True)
