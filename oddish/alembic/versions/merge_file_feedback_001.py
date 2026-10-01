"""Join file indexes with the refreshed prepared dashboard migration chain."""

revision = "merge_file_feedback_001"
down_revision = ("merge_file_delivery_001", "merge_prepared_feedback_001")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
