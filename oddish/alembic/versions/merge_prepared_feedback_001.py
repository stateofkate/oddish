"""Join prepared dashboard summaries with current staging migrations."""

revision = "merge_prepared_feedback_001"
down_revision = ("merge_prepared_delivery_001", "feedback_reports_001")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
