"""Backfill recorded token breakdowns in existing version browse summaries.

The resumable, locked backfill freezes the same cohort as task detail.
"""

from alembic import op
from sqlalchemy import text

revision = "task_effort_tokens_001"
down_revision = "task_effort_001"
branch_labels = None
depends_on = None

_BACKFILL = """
WITH scoped AS (
 SELECT task_version_id, total_steps,
        coalesce(input_tokens_exact, input_tokens::bigint) AS input,
        coalesce(output_tokens_exact, output_tokens::bigint) AS output,
        coalesce(cache_tokens_exact, cache_tokens::bigint) AS cached
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
        count(*) FILTER (WHERE input >= 0 AND output >= 0) AS tokens_trial_count,
        coalesce(sum(input - cached) FILTER (WHERE cached >= 0 AND input >= cached), 0) AS uncached_input_tokens_sum,
        count(*) FILTER (WHERE cached >= 0 AND input >= cached) AS uncached_input_tokens_trial_count,
        coalesce(sum(cached) FILTER (WHERE cached >= 0), 0) AS cached_input_tokens_sum,
        count(*) FILTER (WHERE cached >= 0) AS cached_input_tokens_trial_count,
        coalesce(sum(output) FILTER (WHERE output >= 0), 0) AS output_tokens_sum,
        count(*) FILTER (WHERE output >= 0) AS output_tokens_trial_count
 FROM scoped GROUP BY task_version_id
)
UPDATE task_version_browse_summaries s SET effort = coalesce(
  (SELECT to_jsonb(a) - 'task_version_id' FROM aggregates a
   WHERE a.task_version_id = s.task_version_id),
  jsonb_build_object('completed_trials', 0, 'steps_sum', 0, 'steps_trial_count', 0, 'tokens_sum', 0, 'tokens_trial_count', 0,
    'uncached_input_tokens_sum', 0, 'uncached_input_tokens_trial_count', 0,
    'cached_input_tokens_sum', 0, 'cached_input_tokens_trial_count', 0,
    'output_tokens_sum', 0, 'output_tokens_trial_count', 0)
) WHERE s.task_version_id = ANY(CAST(:ids AS text[]))
"""


# Older worker images replace the entire JSON snapshot. Complete only the new
# fields for those writes; current writers supply them and do no extra work.
_COMPAT_FUNCTION = """
CREATE OR REPLACE FUNCTION fill_task_effort_token_breakdown() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.effort IS NULL OR NOT (NEW.effort ? 'uncached_input_tokens_trial_count') THEN
   WITH scoped AS (
     SELECT total_steps, coalesce(input_tokens_exact, input_tokens::bigint) AS input,
            coalesce(output_tokens_exact, output_tokens::bigint) AS output,
            coalesce(cache_tokens_exact, cache_tokens::bigint) AS cached
     FROM trials
     WHERE task_version_id = NEW.task_version_id
       AND deleted_at IS NULL AND superseded_by_trial_id IS NULL AND kind = 'agent'
       AND (idempotency_key IS NULL OR idempotency_key NOT LIKE 'combine:%')
       AND coalesce(lower(agent), '') !~ '^(oracle$|oracle-|agent-oracle|nop$|nop-|agent-nop)'
       AND ((status = 'SUCCESS' AND error_message IS NULL)
         OR (status IN ('SUCCESS', 'FAILED') AND reward IS NOT NULL
           AND (error_message LIKE '%AgentTimeoutError%'
             OR error_message LIKE '%Agent execution timed out%')))
   )
   SELECT coalesce(NEW.effort, jsonb_build_object(
     'completed_trials', count(*),
     'steps_sum', coalesce(sum(total_steps) FILTER (WHERE total_steps >= 0), 0),
     'steps_trial_count', count(*) FILTER (WHERE total_steps >= 0),
     'tokens_sum', coalesce(sum(input + output) FILTER (WHERE input >= 0 AND output >= 0), 0),
     'tokens_trial_count', count(*) FILTER (WHERE input >= 0 AND output >= 0)
   )) || jsonb_build_object(
     'uncached_input_tokens_sum', coalesce(sum(input - cached) FILTER (WHERE cached >= 0 AND input >= cached), 0),
     'uncached_input_tokens_trial_count', count(*) FILTER (WHERE cached >= 0 AND input >= cached),
     'cached_input_tokens_sum', coalesce(sum(cached) FILTER (WHERE cached >= 0), 0),
     'cached_input_tokens_trial_count', count(*) FILTER (WHERE cached >= 0),
     'output_tokens_sum', coalesce(sum(output) FILTER (WHERE output >= 0), 0),
     'output_tokens_trial_count', count(*) FILTER (WHERE output >= 0)
   ) INTO NEW.effort FROM scoped;
 END IF;
 RETURN NEW;
END $$
"""
_COMPAT_TRIGGER = """
CREATE OR REPLACE TRIGGER task_effort_token_compat
BEFORE INSERT OR UPDATE OF effort ON task_version_browse_summaries
FOR EACH ROW EXECUTE FUNCTION fill_task_effort_token_breakdown()
"""


def upgrade() -> None:
    with op.get_context().autocommit_block():
        connection = op.get_bind()
        connection.execute(text("BEGIN"))
        try:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            connection.execute(text(_COMPAT_FUNCTION))
            connection.execute(text(_COMPAT_TRIGGER))
            connection.execute(text("COMMIT"))
        except BaseException:
            connection.execute(text("ROLLBACK"))
            raise
        while True:
            ids = list(
                connection.execute(
                    text(
                        "SELECT task_version_id FROM task_version_browse_summaries "
                        "WHERE effort IS NULL OR NOT (effort ? 'uncached_input_tokens_trial_count') "
                        "ORDER BY task_version_id LIMIT 200"
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
    # Additive JSON keys are safe for older readers; preserve recorded summaries.
    op.execute("DROP TRIGGER IF EXISTS task_effort_token_compat ON task_version_browse_summaries")
    op.execute("DROP FUNCTION IF EXISTS fill_task_effort_token_breakdown()")
