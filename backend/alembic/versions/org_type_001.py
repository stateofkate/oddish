"""Mark customer organizations, separately from what they may run.

Orgs created as customers (view-only sign-in) start as ``customer``; the
upsell to running jobs later leaves the type alone.
"""

from alembic import op

revision = "org_type_001"
down_revision = "viewer_reviews_merge_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE organizations "
        "ADD COLUMN IF NOT EXISTS org_type VARCHAR(16) NOT NULL DEFAULT 'internal'"
    )
    op.execute(
        "ALTER TABLE organizations DROP CONSTRAINT IF EXISTS ck_organizations_org_type"
    )
    op.execute(
        "ALTER TABLE organizations ADD CONSTRAINT ck_organizations_org_type "
        "CHECK (org_type IN ('internal', 'customer'))"
    )
    op.execute("UPDATE organizations SET org_type = 'customer' WHERE viewer_access")


def downgrade() -> None:
    op.execute("ALTER TABLE organizations DROP COLUMN org_type")
