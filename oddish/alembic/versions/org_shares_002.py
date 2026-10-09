"""Share by tag, and let each share show or hide experiment costs.

Also joins the org_shares branch with task_effort_001, which landed on staging
from the same parent.
"""

from alembic import op

revision = "org_shares_002"
down_revision = ("org_shares_001", "task_effort_001")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE org_shares DROP CONSTRAINT IF EXISTS ck_org_shares_resource_type"
    )
    op.execute(
        "ALTER TABLE org_shares ADD CONSTRAINT ck_org_shares_resource_type "
        "CHECK (resource_type IN ('experiment', 'tag', 'delivery'))"
    )
    op.execute(
        "ALTER TABLE org_shares "
        "ADD COLUMN IF NOT EXISTS show_costs BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE org_shares DROP COLUMN show_costs")
    op.execute("ALTER TABLE org_shares DROP CONSTRAINT ck_org_shares_resource_type")
    op.execute(
        "ALTER TABLE org_shares ADD CONSTRAINT ck_org_shares_resource_type "
        "CHECK (resource_type IN ('experiment', 'delivery'))"
    )
