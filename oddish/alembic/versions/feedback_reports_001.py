"""Separate trajectory, task-verdict, and pre-trial report feedback.

Revision ID: feedback_reports_001
Revises: qa_evals_tables_001
"""

from alembic import op
import sqlalchemy as sa

revision = "feedback_reports_001"
down_revision = "qa_evals_tables_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Fresh databases bootstrap from current model metadata.
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("feedback")
    }
    if "report_trial_id" not in columns:
        op.add_column(
            "feedback", sa.Column("report_trial_id", sa.String(160), nullable=True)
        )
    op.drop_constraint("ck_feedback_target", "feedback", type_="check")
    op.create_check_constraint(
        "ck_feedback_target",
        "feedback",
        "target IN ('qa_verdict', 'qa_action_item', 'qa_trajectory', 'qa_task_verdict', 'qa_pre_trial')",
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM feedback WHERE target NOT IN ('qa_verdict', 'qa_action_item'))"
            )
        )
        .scalar()
    ):
        raise RuntimeError(
            "Cannot downgrade while report feedback exists; archive those votes first"
        )
    op.drop_constraint("ck_feedback_target", "feedback", type_="check")
    op.create_check_constraint(
        "ck_feedback_target", "feedback", "target IN ('qa_verdict', 'qa_action_item')"
    )
    op.drop_column("feedback", "report_trial_id")
