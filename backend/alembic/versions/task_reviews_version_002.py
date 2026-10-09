"""Bind internal reviews to task versions, without archive fingerprints."""

import sqlalchemy as sa
from alembic import op

revision = "task_reviews_version_002"
down_revision = "org_shared_qa_001"
branch_labels = None
depends_on = None


def upgrade():
    # Retain legacy metadata and every review row for deployment compatibility.
    for table in ("task_review_ratings", "task_review_comments"):
        for column in ("content_hash", "content_algorithm"):
            op.alter_column(table, column, existing_type=sa.String(64), nullable=True)
    # A reviewer may have rated several old content snapshots of one version.
    # Keep their latest action current, including a cleared rating.
    op.execute("""
        WITH ranked AS (
            SELECT id, row_number() OVER (
                PARTITION BY org_id, task_version_id, reviewer_id
                ORDER BY updated_at DESC, id DESC
            ) AS position FROM task_review_ratings
        )
        UPDATE task_review_ratings SET deleted_at = now()
        FROM ranked WHERE task_review_ratings.id = ranked.id
          AND ranked.position > 1 AND task_review_ratings.deleted_at IS NULL
    """)
    op.drop_constraint(
        "uq_task_review_rating_reviewer", "task_review_ratings", type_="unique"
    )
    op.create_index(
        "uq_task_review_rating_reviewer",
        "task_review_ratings",
        ["org_id", "task_version_id", "reviewer_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_index("idx_task_review_comments_version", table_name="task_review_comments")
    op.create_index(
        "idx_task_review_comments_version",
        "task_review_comments",
        ["org_id", "task_version_id", "created_at", "id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade():
    op.drop_index("uq_task_review_rating_reviewer", table_name="task_review_ratings")
    op.create_unique_constraint(
        "uq_task_review_rating_reviewer",
        "task_review_ratings",
        [
            "org_id",
            "task_version_id",
            "content_algorithm",
            "content_hash",
            "reviewer_id",
        ],
    )
    op.drop_index("idx_task_review_comments_version", table_name="task_review_comments")
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
    # New reviews have no archive metadata; retain them on schema rollback.
