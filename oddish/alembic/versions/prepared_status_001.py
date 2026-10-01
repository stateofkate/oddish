"""Invalidate dashboard summaries when only task execution status changes."""

from alembic import op

revision = "prepared_status_001"
down_revision = "prepared_reads_001"
branch_labels = None
depends_on = None


# Freeze this revision's dependency set: importing the original migration would
# make an upgrade of an existing database depend on future edits to that file.
TASK_COLUMNS = """created_at, "user", tags, link, created_by_user_id,
    current_version_id, run_analysis, verdict, verdict_status, deleted_at, org_id"""


def upgrade():
    # Replace in place: DROP TRIGGER needs an ACCESS EXCLUSIVE lock on tasks
    # after prepared_reads_001 already holds a weaker one in this transaction,
    # and that upgrade deadlocked with a live writer on the 2026-10-01 deploy.
    op.execute(
        f"CREATE OR REPLACE TRIGGER experiment_summary_changed AFTER INSERT OR DELETE OR "
        f"UPDATE OF {TASK_COLUMNS}, status ON tasks "
        "FOR EACH ROW EXECUTE FUNCTION record_experiment_summary_change()"
    )


def downgrade():
    op.execute(
        f"CREATE OR REPLACE TRIGGER experiment_summary_changed AFTER INSERT OR DELETE OR "
        f"UPDATE OF {TASK_COLUMNS} ON tasks "
        "FOR EACH ROW EXECUTE FUNCTION record_experiment_summary_change()"
    )
