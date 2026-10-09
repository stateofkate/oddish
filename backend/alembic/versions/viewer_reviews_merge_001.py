"""Join org_viewer_access_001 with task_reviews_001, which landed on staging
from the same parent. No schema change."""

revision = "viewer_reviews_merge_001"
down_revision = ("org_viewer_access_001", "task_reviews_001")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
