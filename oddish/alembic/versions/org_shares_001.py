"""Let the operator org share experiments and deliveries with other orgs."""

from alembic import op

revision = "org_shares_001"
down_revision = "progress_hash_merge_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS org_shares (
            id VARCHAR(64) PRIMARY KEY,
            grantee_org_id VARCHAR(64) NOT NULL,
            resource_type VARCHAR(32) NOT NULL
                CONSTRAINT ck_org_shares_resource_type
                CHECK (resource_type IN ('experiment', 'delivery')),
            resource_id VARCHAR(64) NOT NULL,
            granted_by_user_id VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMPTZ
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_org_shares_live
        ON org_shares (grantee_org_id, resource_type, resource_id)
        WHERE deleted_at IS NULL
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_org_shares_resource ON org_shares (resource_type, resource_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE org_shares")
