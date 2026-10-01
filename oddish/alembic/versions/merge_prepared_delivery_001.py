"""Join prepared dashboard summaries with staging's current head (task-browser, monitor columns)."""

revision = "merge_prepared_delivery_001"
down_revision = ("prepared_status_001", "probe_removal_001")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
