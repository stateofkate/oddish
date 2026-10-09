"""Move the per-org "show QA on shared samples" switch onto each share.

Runs after the oddish chain (prod order), so ``org_shares.show_qa`` exists.
Existing shares keep what their grantee saw: customer orgs keep their old
setting, every other org saw QA. The column stays because the previous
release still selects it while this deploy rolls out; drop it once that
release is gone.
"""

from alembic import op

revision = "org_shared_qa_002"
down_revision = "task_review_alert_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE org_shares s SET show_qa = (o.org_type <> 'customer' OR o.show_shared_qa) "
        "FROM organizations o WHERE o.id = s.grantee_org_id"
    )


def downgrade() -> None:
    pass
