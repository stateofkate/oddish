"""Invalidate prepared experiment averages when recorded measurements change."""

from alembic import op
from oddish.db.migration_locks import run_with_lock_retry

revision = "experiment_effort_001"
down_revision = "org_shares_003"
branch_labels = None
depends_on = None


def upgrade():
    # Reuse the durable marker function; duplicate status/effort triggers still
    # enqueue only one marker per experiment per transaction. Old payloads are
    # upgraded by the bounded summary maintainer, not a migration-wide scan.
    run_with_lock_retry(
        lambda: op.execute("""
            CREATE OR REPLACE TRIGGER experiment_effort_changed
            AFTER UPDATE OF total_steps, input_tokens, input_tokens_exact,
                output_tokens, output_tokens_exact, cache_tokens, cache_tokens_exact,
                cost_usd, error_message, idempotency_key ON trials
            FOR EACH ROW EXECUTE FUNCTION record_experiment_summary_change()
        """),
        table_name="trials",
    )


def downgrade():
    run_with_lock_retry(
        lambda: op.execute("DROP TRIGGER IF EXISTS experiment_effort_changed ON trials"),
        table_name="trials",
    )
