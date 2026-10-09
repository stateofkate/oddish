"""Let operators grant view-only sign-in to customer organizations."""

from alembic import op

revision = "org_viewer_access_001"
down_revision = "user_ui_layouts_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS viewer_access BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE organizations DROP COLUMN viewer_access")
