"""Separate model results from validation in bounded task summaries.

Backfill in sorted, advisory-locked batches. Each batch commits; rerunning
only visits missing summaries. SQL freezes this revision's population rules.
"""

from alembic import op
from sqlalchemy import text

revision = "task_run_coverage_001"
down_revision = "shareqa_001"
branch_labels = None
depends_on = None

_BACKFILL = """
WITH scoped AS (
 SELECT tr.task_version_id, tr.id, tr.reward,
        GREATEST(tr.created_at, tr.started_at, tr.finished_at) AS activity,
        CASE WHEN lower(tr.agent) ~ '^(oracle$|oracle-|agent-oracle)' THEN 'oracle'
             WHEN lower(tr.agent) ~ '^(nop$|nop-|agent-nop)' THEN 'nop'
             ELSE 'model' END AS role,
        CASE
          WHEN tr.status = 'SKIPPED' THEN 'skipped'
          WHEN tr.error_message IS NOT NULL AND NOT (
            (tr.error_message LIKE '%AgentTimeoutError%'
             OR tr.error_message LIKE '%Agent execution timed out%')
            AND tr.reward IS NOT NULL
          ) THEN 'harness'
          WHEN tr.status = 'FAILED' THEN CASE
            WHEN (tr.error_message LIKE '%AgentTimeoutError%'
                  OR tr.error_message LIKE '%Agent execution timed out%')
                 AND tr.reward IS NOT NULL
            THEN CASE WHEN tr.reward = 1 THEN 'pass'
                      WHEN tr.reward = 0 THEN 'fail' ELSE 'partial' END
            ELSE 'harness' END
          WHEN tr.status = 'SUCCESS' THEN CASE
            WHEN tr.reward IS NULL THEN 'scoreless'
            WHEN tr.reward = 1 THEN 'pass'
            WHEN tr.reward = 0 THEN 'fail' ELSE 'partial' END
          ELSE 'other'
        END AS bucket
 FROM trials tr
 WHERE tr.task_version_id = ANY(CAST(:ids AS text[]))
   AND tr.deleted_at IS NULL AND tr.superseded_by_trial_id IS NULL
   AND tr.is_probe IS NOT TRUE AND tr.kind = 'agent'
   AND (tr.idempotency_key IS NULL OR tr.idempotency_key NOT LIKE 'combine:%')
), grouped AS (
 SELECT v.id AS task_version_id, role.name AS role,
        count(s.id) AS total,
        count(*) FILTER (WHERE bucket IN ('pass','partial','fail')) AS scored,
        coalesce(sum(reward) FILTER (WHERE bucket IN ('pass','partial','fail')), 0.0) AS reward_sum,
        max(activity) AS last_activity,
        count(*) FILTER (WHERE bucket = 'pass') AS pass_count,
        count(*) FILTER (WHERE bucket = 'partial') AS partial_count,
        count(*) FILTER (WHERE bucket = 'fail') AS fail_count,
        count(*) FILTER (WHERE bucket = 'harness') AS harness_count,
        count(*) FILTER (WHERE bucket = 'skipped') AS skipped_count,
        count(*) FILTER (WHERE bucket = 'scoreless') AS scoreless_count,
        count(*) FILTER (WHERE bucket = 'other') AS other_count
 FROM task_versions v CROSS JOIN (VALUES ('model'),('oracle'),('nop')) AS role(name)
 LEFT JOIN scoped s ON s.task_version_id = v.id AND s.role = role.name
 WHERE v.id = ANY(CAST(:ids AS text[]))
 GROUP BY v.id, role.name
), packed AS (
 SELECT task_version_id, jsonb_object_agg(role, to_jsonb(grouped) - 'role' - 'task_version_id') AS summary
 FROM grouped GROUP BY task_version_id
)
INSERT INTO task_version_browse_summaries (task_version_id, task_id, last_run_at, total_trials, completed_trials, failed_trials, reward_success, reward_sum, reward_total, pass_count, partial_count, fail_count, harness_count, skipped_count, pending_count, steps_present, steps_p25, steps_p50, steps_p75, agent_count, cost_breakdown, run_summary)
SELECT v.id, v.task_id, NULL, 0, 0, 0, 0, 0.0, 0, 0, 0, 0, 0, 0, 0, 0, NULL, NULL, NULL, 0, '[]'::jsonb, p.summary FROM packed p JOIN task_versions v ON v.id = p.task_version_id
ON CONFLICT (task_version_id) DO UPDATE SET run_summary = EXCLUDED.run_summary
"""


def upgrade() -> None:
    with op.get_context().autocommit_block():
        connection = op.get_bind()
        previous_timeout = connection.execute(text("SHOW lock_timeout")).scalar_one()
        try:
            connection.execute(text("SET lock_timeout = '5s'"))
            connection.execute(
                text(
                    "ALTER TABLE task_version_browse_summaries ADD COLUMN IF NOT EXISTS run_summary jsonb"
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
                    text("""
                SELECT v.id FROM task_versions v
                LEFT JOIN task_version_browse_summaries s ON s.task_version_id = v.id
                WHERE s.run_summary IS NULL ORDER BY v.id LIMIT 200
            """)
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
    op.drop_column("task_version_browse_summaries", "run_summary")
