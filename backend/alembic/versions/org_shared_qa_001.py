"""Let Abundant choose, per customer org, whether shared samples show QA.

Off by default: a customer sees results (Pass@k, leaderboard, tasks) without
Abundant's QA verdicts, timeline, or categories until an admin turns it on.
"""

from alembic import op

revision = "org_shared_qa_001"
down_revision = "org_type_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE organizations "
        "ADD COLUMN IF NOT EXISTS show_shared_qa BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE organizations DROP COLUMN show_shared_qa")
