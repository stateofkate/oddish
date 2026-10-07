"""Retain setup events independently of ephemeral live transcripts."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "trial_progress_001"
down_revision = "task_hash_idx_001"
branch_labels = None
depends_on = None


def upgrade():
    # The initial migration calls current Base.metadata.create_all on fresh DBs.
    if sa.inspect(op.get_bind()).has_table("trial_progress_events"):
        return
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "trial_progress_events",
        sa.Column(
            "trial_id",
            sa.String(160),
            sa.ForeignKey("trials.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("attempt", sa.Integer(), primary_key=True),
        sa.Column("event_key", sa.String(96), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("source_seq", sa.Integer(), nullable=False),
        sa.Column(
            "schema_version", sa.SmallInteger(), server_default="1", nullable=False
        ),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("attempt > 0", name="ck_progress_attempt"),
        sa.CheckConstraint("source_seq >= 0", name="ck_progress_seq"),
        sa.CheckConstraint("schema_version > 0", name="ck_progress_version"),
        sa.CheckConstraint(
            "kind IN ('env_subphase', 'env_info')", name="ck_progress_kind"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name="ck_progress_payload"
        ),
    )


def downgrade():
    op.drop_table("trial_progress_events")
