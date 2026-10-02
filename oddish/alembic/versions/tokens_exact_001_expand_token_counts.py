"""Add exact 64-bit usage without rewriting populated token columns.

Revision ID: tokens_exact_001
Revises: summary_dirty_log_001
"""

from alembic import op

from oddish.db.migration_locks import run_with_lock_retry

revision = "tokens_exact_001"
down_revision = "summary_dirty_log_001"
branch_labels = None
depends_on = None

TOKEN_COLUMNS = {
    "trials": ("input_tokens", "output_tokens", "cache_tokens", "cache_write_tokens"),
    "analysis_costs": (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    ),
    "verifier_costs": (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    ),
}


def upgrade() -> None:
    for table, columns in TOKEN_COLUMNS.items():
        additions = ", ".join(
            f"ADD COLUMN IF NOT EXISTS {column}_exact bigint" for column in columns
        )
        run_with_lock_retry(
            lambda: op.execute(f"ALTER TABLE {table} {additions}"), table_name=table
        )
        for column in columns:
            # Explicit column lists distinguish old and new writers even when
            # the capped value stays INT32_MAX across successive checkpoints.
            # Mutating NEW in a BEFORE trigger does not fire UPDATE OF triggers.
            op.execute(f"""
                CREATE OR REPLACE FUNCTION {table}_{column}_mirror()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF TG_OP = 'UPDATE' OR NEW.{column}_exact IS NOT NULL THEN
                        NEW.{column} := LEAST(NEW.{column}_exact, 2147483647);
                        IF NEW.{column}_exact IS NULL THEN NEW.{column} := NULL; END IF;
                    END IF;
                    RETURN NEW;
                END $$
            """)
            op.execute(f"""
                CREATE OR REPLACE FUNCTION {table}_{column}_invalidate()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    NEW.{column}_exact := NULL;
                    RETURN NEW;
                END $$
            """)
            run_with_lock_retry(
                lambda: op.execute(f"""
                    CREATE OR REPLACE TRIGGER a_{column}_legacy
                    BEFORE UPDATE OF {column} ON {table}
                    FOR EACH ROW EXECUTE FUNCTION {table}_{column}_invalidate()
                """),
                table_name=table,
            )
            run_with_lock_retry(
                lambda: op.execute(f"""
                    CREATE OR REPLACE TRIGGER b_{column}_exact
                    BEFORE INSERT OR UPDATE OF {column}_exact ON {table}
                    FOR EACH ROW EXECUTE FUNCTION {table}_{column}_mirror()
                """),
                table_name=table,
            )


def downgrade() -> None:
    raise RuntimeError(
        "Exact token counts cannot be narrowed safely. Roll back application code "
        "while retaining these additive columns and compatibility triggers."
    )
