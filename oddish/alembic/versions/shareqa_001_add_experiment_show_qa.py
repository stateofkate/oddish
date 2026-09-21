"""Add the opt-in QA setting for public experiment links.

Revision ID: shareqa_001
Revises: delivery_selection_002
"""

from alembic import op
import sqlalchemy as sa

revision = "shareqa_001"
down_revision = "delivery_selection_002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Fresh databases already have this column from 000_initial_schema's
    # live model metadata. Existing databases still need it added.
    op.add_column(
        "experiments",
        sa.Column("show_qa", sa.Boolean(), nullable=False, server_default=sa.false()),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_column("experiments", "show_qa")
