"""Join org_shares_002 with task_effort_tokens_001, which landed on staging
from task_effort_001. No schema change."""

revision = "org_shares_tokens_merge_001"
down_revision = ("org_shares_002", "task_effort_tokens_001")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
