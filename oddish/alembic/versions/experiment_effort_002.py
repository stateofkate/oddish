"""Avoid rebuilding completed-model averages for running checkpoints."""

from alembic import op
from oddish.db.migration_locks import run_with_lock_retry

revision = "experiment_effort_002"
down_revision = "experiment_effort_001"
branch_labels = None
depends_on = None


def _replace_trigger(condition: str):
    # Freeze the columns in this revision; 001 already ran in the PR preview.
    run_with_lock_retry(
        lambda: op.execute(f"""
            CREATE OR REPLACE TRIGGER experiment_effort_changed
            AFTER UPDATE OF total_steps, input_tokens, input_tokens_exact,
                output_tokens, output_tokens_exact, cache_tokens, cache_tokens_exact,
                cost_usd, error_message, idempotency_key ON trials
            FOR EACH ROW {condition}
            EXECUTE FUNCTION record_experiment_summary_change()
        """),
        table_name="trials",
    )


def upgrade():
    # Settlement is covered by the existing status trigger. Only late terminal
    # measurements can change averages; running token checkpoints cannot.
    _replace_trigger("WHEN (NEW.status IN ('SUCCESS', 'FAILED'))")


def downgrade():
    _replace_trigger("")
