"""add qa_prompts and qa_golden_cases

Revision ID: qa_evals_tables_001
Revises: probe_removal_001
Create Date: 2026-09-25 00:00:00.000000

Two tables for scoring QA prompts. ``qa_prompts`` stores each prompt version
once, keyed by the SHA-256 of its stripped text, so replays (which carry only
the hash) can show and diff their text. ``qa_golden_cases`` stores people's
labels of the answer QA should give, at most one live label per subject: a
solver trial for post-trial QA, a task version for pre-trial QA. Reference
columns stay plain strings, as in ``feedback``, so this migration takes no
lock on the hot tables.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "qa_evals_tables_001"
down_revision: Union[str, Sequence[str], None] = "probe_removal_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STAGE_CHECK = "stage IN ('pre_trial', 'post_trial')"


def upgrade() -> None:
    # ``000_initial_schema`` builds a fresh database from the current models,
    # so on that path both tables already exist.
    if sa.inspect(op.get_bind()).has_table("qa_prompts"):
        return
    op.create_table(
        "qa_prompts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(64), nullable=True),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False, server_default=""),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("created_by_user_id", sa.String(64), nullable=True),
        sa.Column("git_commit", sa.String(64), nullable=True),
        sa.Column("git_author", sa.String(255), nullable=True),
        sa.Column("git_committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_STAGE_CHECK, name="ck_qa_prompts_stage"),
        sa.CheckConstraint(
            "source IN ('production', 'replay')", name="ck_qa_prompts_source"
        ),
    )
    op.create_index(
        "uq_qa_prompts_org_stage_sha256",
        "qa_prompts",
        [sa.text("COALESCE(org_id, '')"), "stage", "sha256"],
        unique=True,
    )
    op.create_table(
        "qa_golden_cases",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(64), nullable=True),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("source", sa.String(128), nullable=True),
        sa.Column("source_trial_id", sa.String(160), nullable=True),
        sa.Column("task_version_id", sa.String(160), nullable=False),
        sa.Column("task_version_content_hash", sa.String(128), nullable=True),
        sa.Column("expected", JSONB(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by_user_id", sa.String(64), nullable=True),
        sa.Column("reviewed_by_user_id", sa.String(64), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_STAGE_CHECK, name="ck_qa_golden_cases_stage"),
        sa.CheckConstraint(
            "(stage = 'post_trial' AND source_trial_id IS NOT NULL) OR "
            "(stage = 'pre_trial' AND source_trial_id IS NULL)",
            name="ck_qa_golden_cases_subject",
        ),
    )
    op.create_index(
        "uq_qa_golden_cases_live_subject",
        "qa_golden_cases",
        [
            sa.text("COALESCE(org_id, '')"),
            "stage",
            sa.text("COALESCE(source_trial_id, task_version_id)"),
        ],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("qa_golden_cases")
    op.drop_table("qa_prompts")
