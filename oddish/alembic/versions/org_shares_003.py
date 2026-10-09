"""Share tasks with customer orgs, and map delivery customers to their org.

A finalized delivery whose customer has ``customer_org_id`` is shared with
that org. No FK: ``organizations`` belongs to the backend migration chain.
"""

from alembic import op

revision = "org_shares_003"
down_revision = "org_shares_tokens_merge_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE org_shares DROP CONSTRAINT IF EXISTS ck_org_shares_resource_type"
    )
    op.execute(
        "ALTER TABLE org_shares ADD CONSTRAINT ck_org_shares_resource_type "
        "CHECK (resource_type IN ('experiment', 'tag', 'delivery', 'task'))"
    )
    # Task ids run to 128 characters.
    op.execute("ALTER TABLE org_shares ALTER COLUMN resource_id TYPE VARCHAR(128)")
    op.execute(
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS customer_org_id VARCHAR(64)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE customers DROP COLUMN customer_org_id")
    op.execute("ALTER TABLE org_shares DROP CONSTRAINT ck_org_shares_resource_type")
    op.execute(
        "ALTER TABLE org_shares ADD CONSTRAINT ck_org_shares_resource_type "
        "CHECK (resource_type IN ('experiment', 'tag', 'delivery'))"
    )
