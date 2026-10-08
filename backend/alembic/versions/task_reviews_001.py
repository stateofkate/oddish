"""Add internal version-bound task ratings and comments."""

from alembic import op
import sqlalchemy as sa

revision = "task_reviews_001"
down_revision = "user_ui_layouts_001"
branch_labels = None
depends_on = None


def _columns():
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "reviewer_id",
            sa.String(64),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_version_id",
            sa.String(160),
            sa.ForeignKey("task_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("content_algorithm", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    ]


def upgrade():
    op.create_table(
        "task_review_ratings",
        *_columns(),
        sa.Column("score", sa.SmallInteger(), nullable=False),
        sa.CheckConstraint("score BETWEEN 1 AND 5", name="ck_task_review_rating_score"),
        sa.CheckConstraint("revision > 0", name="ck_task_review_rating_revision"),
        sa.UniqueConstraint(
            "org_id",
            "task_version_id",
            "content_algorithm",
            "content_hash",
            "reviewer_id",
            name="uq_task_review_rating_reviewer",
        ),
    )
    op.create_table(
        "task_review_comments",
        *_columns(),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("client_request_id", sa.String(36), nullable=False),
        sa.Column(
            "source_trial_id",
            sa.String(160),
            sa.ForeignKey("trials.id", ondelete="SET NULL"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(body)) BETWEEN 1 AND 10000",
            name="ck_task_review_comment_body",
        ),
        sa.CheckConstraint("revision > 0", name="ck_task_review_comment_revision"),
        sa.UniqueConstraint(
            "org_id",
            "reviewer_id",
            "client_request_id",
            name="uq_task_review_comment_request",
        ),
    )
    op.create_index(
        "idx_task_review_comments_version",
        "task_review_comments",
        [
            "org_id",
            "task_version_id",
            "content_algorithm",
            "content_hash",
            "created_at",
            "id",
        ],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade():
    op.drop_table("task_review_comments")
    op.drop_table("task_review_ratings")
