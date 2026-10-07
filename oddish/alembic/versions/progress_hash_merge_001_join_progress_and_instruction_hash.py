"""Join setup history and staging's instruction hash migration.

Keep both applied histories intact: existing previews may already be at
trial_progress_001, while staging databases are at task_instruction_hash_001.
"""

revision = "progress_hash_merge_001"
down_revision = ("trial_progress_001", "task_instruction_hash_001")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
