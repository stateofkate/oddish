"""Drop the reverted file index tables and triggers."""

from alembic import op

revision = "drop_file_index_001"
down_revision = "merge_file_feedback_001"
branch_labels = None
depends_on = None

TRIGGERS = [
    ("file_directory_changed", "task_versions"),
    ("file_directory_changed", "trials"),
    ("legacy_file_directory_changed", "tasks"),
    ("task_archive_directory_changed", "task_versions"),
]


def upgrade():
    # DROP TRIGGER IF EXISTS still takes ACCESS EXCLUSIVE on the table, which
    # deadlocks with live writers once earlier revisions in the same upgrade
    # hold weaker locks there. Only databases that ran #1662 have these.
    for trigger, table in TRIGGERS:
        op.execute(f"""
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = '{trigger}'
                           AND tgrelid = '{table}'::regclass) THEN
                    DROP TRIGGER {trigger} ON {table};
                END IF;
            END $$
        """)
    op.execute("DROP FUNCTION IF EXISTS queue_file_directory()")
    op.execute("DROP FUNCTION IF EXISTS queue_legacy_file_directory()")
    op.execute("DROP FUNCTION IF EXISTS invalidate_task_archive_directory()")
    op.execute("DROP TABLE IF EXISTS file_entries")
    op.execute("DROP TABLE IF EXISTS file_indexes")


def downgrade():
    pass
