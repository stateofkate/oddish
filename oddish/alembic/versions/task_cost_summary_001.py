"""Populate version-scoped average cost in the existing browse snapshot."""

from alembic import op
from sqlalchemy import text
from oddish.db.task_cost_v1 import COST_SUMMARY_STATEMENTS

revision = "task_cost_summary_001"
down_revision = "org_shares_show_qa_001"
branch_labels = None
depends_on = None


def upgrade():
    for statement in COST_SUMMARY_STATEMENTS:
        op.execute(statement)
    # Resumable batches preserve the existing summary advisory-lock ordering.
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        after = ""
        while True:
            ids = list(
                conn.execute(
                    text(
                        "SELECT task_version_id FROM task_version_browse_summaries WHERE task_version_id > :after AND NOT coalesce(effort,'{}'::jsonb) ? 'cost' ORDER BY task_version_id LIMIT 200"
                    ),
                    {"after": after},
                ).scalars()
            )
            if not ids:
                break
            conn.execute(text("BEGIN"))
            try:
                conn.execute(text("SET LOCAL lock_timeout='5s'"))
                conn.execute(
                    text(
                        "SELECT pg_advisory_xact_lock(hashtextextended(id,0)) FROM (SELECT id FROM unnest(CAST(:ids AS text[])) ids(id) ORDER BY id) ordered"
                    ),
                    {"ids": ids},
                )
                conn.execute(
                    text(
                        "UPDATE task_version_browse_summaries SET effort=coalesce(effort,'{}'::jsonb) WHERE task_version_id=ANY(CAST(:ids AS text[]))"
                    ),
                    {"ids": ids},
                )
                conn.execute(text("COMMIT"))
                after = ids[-1]
            except BaseException:
                conn.execute(text("ROLLBACK"))
                raise


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS task_cost_legacy_v1 ON analysis_costs")
    op.execute("DROP FUNCTION IF EXISTS refresh_legacy_task_cost_v1()")
    op.execute(
        "DROP TRIGGER IF EXISTS task_effort_z_cost_compat ON task_version_browse_summaries"
    )
    op.execute("DROP FUNCTION IF EXISTS fill_task_cost_summary_v1()")
    op.execute("DROP FUNCTION IF EXISTS task_cost_summary_v1(text)")
