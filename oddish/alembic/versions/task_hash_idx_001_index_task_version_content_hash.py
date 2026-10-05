"""Index task_versions.content_hash for cross-task upload matching.

Revision ID: task_hash_idx_001
Revises: task_fingerprints_001
Create Date: 2026-10-05

Built ``CONCURRENTLY`` so uploads keep writing ``task_versions`` while the
index builds.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "task_hash_idx_001"
down_revision: Union[str, Sequence[str], None] = "task_fingerprints_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ``CREATE INDEX CONCURRENTLY`` cannot run inside a transaction.
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_task_versions_content_hash
            ON task_versions (content_hash)
            WHERE content_hash IS NOT NULL
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_task_versions_content_hash")
