"""Add the task review Slack preference."""

from alembic import op

revision = "task_review_alert_001"
down_revision = "task_reviews_version_002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE user_alert_preferences ADD COLUMN task_review_enabled BOOLEAN NOT NULL DEFAULT true"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE user_alert_preferences DROP COLUMN task_review_enabled")
