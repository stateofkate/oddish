"""Record each version's instruction.md hash next to its content hash.

Revision ID: task_instruction_hash_001
Revises: task_hash_idx_001
Create Date: 2026-10-06

A nullable column (no table rewrite) plus an index built ``CONCURRENTLY`` so
TASK_EXPAND keeps writing fingerprints while it builds.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "task_instruction_hash_001"
down_revision: Union[str, Sequence[str], None] = "task_hash_idx_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE task_version_fingerprints "
        "ADD COLUMN IF NOT EXISTS instruction_hash VARCHAR(64)"
    )
    # ``CREATE INDEX CONCURRENTLY`` cannot run inside a transaction.
    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                idx_task_version_fingerprints_instruction_hash
            ON task_version_fingerprints (instruction_hash)
            WHERE instruction_hash IS NOT NULL
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "idx_task_version_fingerprints_instruction_hash"
        )
    op.execute(
        "ALTER TABLE task_version_fingerprints DROP COLUMN IF EXISTS instruction_hash"
    )
