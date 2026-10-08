"""Persist completed-model effort in the existing version browse summary.

The resumable, locked backfill freezes the same cohort as task detail.
"""

from alembic import op
from sqlalchemy import text

revision = "task_effort_001"
down_revision = "progress_hash_merge_001"
branch_labels = None
depends_on = None

_BACKFILL = """
WITH scoped AS (
 SELECT task_version_id, total_steps,
        coalesce(input_tokens_exact, input_tokens::bigint) AS input,
        coalesce(output_tokens_exact, output_tokens::bigint) AS output
 FROM trials
 WHERE task_version_id = ANY(CAST(:ids AS text[]))
   AND deleted_at IS NULL AND superseded_by_trial_id IS NULL AND kind = 'agent'
   AND (idempotency_key IS NULL OR idempotency_key NOT LIKE 'combine:%')
   AND coalesce(lower(agent), '') !~ '^(oracle$|oracle-|agent-oracle|nop$|nop-|agent-nop)'
   AND ((status = 'SUCCESS' AND error_message IS NULL)
     OR (status IN ('SUCCESS', 'FAILED') AND reward IS NOT NULL
       AND (error_message LIKE '%AgentTimeoutError%'
         OR error_message LIKE '%Agent execution timed out%')))
), aggregates AS (
 SELECT task_version_id, count(*) AS completed_trials,
        coalesce(sum(total_steps) FILTER (WHERE total_steps >= 0), 0) AS steps_sum,
        count(*) FILTER (WHERE total_steps >= 0) AS steps_trial_count,
        coalesce(sum(input + output) FILTER (WHERE input >= 0 AND output >= 0), 0) AS tokens_sum,
        count(*) FILTER (WHERE input >= 0 AND output >= 0) AS tokens_trial_count
 FROM scoped GROUP BY task_version_id
)
UPDATE task_version_browse_summaries s SET effort = coalesce(
  (SELECT to_jsonb(a) - 'task_version_id' FROM aggregates a
   WHERE a.task_version_id = s.task_version_id),
  jsonb_build_object('completed_trials', 0, 'steps_sum', 0, 'steps_trial_count', 0, 'tokens_sum', 0, 'tokens_trial_count', 0)
) WHERE s.task_version_id = ANY(CAST(:ids AS text[]))
"""


def upgrade() -> None:
    with op.get_context().autocommit_block():
        connection = op.get_bind()
        previous_timeout = connection.execute(text("SHOW lock_timeout")).scalar_one()
        try:
            connection.execute(text("SET lock_timeout = '5s'"))
            connection.execute(
                text(
                    "ALTER TABLE task_version_browse_summaries ADD COLUMN IF NOT EXISTS effort jsonb"
                )
            )
        finally:
            connection.execute(
                text("SELECT set_config('lock_timeout', :value, false)"),
                {"value": previous_timeout},
            )
        while True:
            ids = list(
                connection.execute(
                    text(
                        "SELECT task_version_id FROM task_version_browse_summaries "
                        "WHERE effort IS NULL ORDER BY task_version_id LIMIT 200"
                    )
                ).scalars()
            )
            if not ids:
                break
            connection.execute(text("BEGIN"))
            try:
                connection.execute(text("SET LOCAL lock_timeout = '5s'"))
                connection.execute(
                    text("""
                    WITH ordered AS MATERIALIZED (
                      SELECT id FROM unnest(CAST(:ids AS text[])) AS ids(id) ORDER BY id
                    ) SELECT pg_advisory_xact_lock(hashtextextended(id, 0)) FROM ordered
                """),
                    {"ids": ids},
                )
                connection.execute(text(_BACKFILL), {"ids": ids})
                connection.execute(text("COMMIT"))
            except BaseException:
                connection.execute(text("ROLLBACK"))
                raise


def downgrade() -> None:
    op.drop_column("task_version_browse_summaries", "effort")
