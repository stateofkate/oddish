"""Drop the reverted file index tables and triggers."""

from alembic import op

revision = "drop_file_index_001"
down_revision = "merge_file_feedback_001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP TRIGGER IF EXISTS file_directory_changed ON task_versions")
    op.execute("DROP TRIGGER IF EXISTS file_directory_changed ON trials")
    op.execute("DROP TRIGGER IF EXISTS legacy_file_directory_changed ON tasks")
    op.execute("DROP TRIGGER IF EXISTS task_archive_directory_changed ON task_versions")
    op.execute("DROP FUNCTION IF EXISTS queue_file_directory()")
    op.execute("DROP FUNCTION IF EXISTS queue_legacy_file_directory()")
    op.execute("DROP FUNCTION IF EXISTS invalidate_task_archive_directory()")
    op.execute("DROP TABLE IF EXISTS file_entries")
    op.execute("DROP TABLE IF EXISTS file_indexes")


def downgrade():
    pass
