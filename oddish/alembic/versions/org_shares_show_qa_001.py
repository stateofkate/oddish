"""Let each org share choose whether its grantee sees QA.

On by default. The backend chain's ``org_shared_qa_002`` copies the old
per-org setting onto existing shares, then drops it.
"""

from alembic import op

revision = "org_shares_show_qa_001"
down_revision = "experiment_effort_002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE org_shares "
        "ADD COLUMN IF NOT EXISTS show_qa BOOLEAN NOT NULL DEFAULT true"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE org_shares DROP COLUMN show_qa")
