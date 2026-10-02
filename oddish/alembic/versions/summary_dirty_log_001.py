"""Record dashboard summary invalidations without locking summary rows.

`dirty_experiment_summary` used to upsert the experiment's summary row, which
locks that row until the writing transaction commits. A task belongs to its
experiment and the experiment's QA shadow, and write paths reach the two in
different orders, so concurrent writers deadlocked on the pair. Writers now
append one marker per experiment per transaction; the summary maintainer folds
markers into `experiment_summaries.revision`.
"""

from alembic import op
import sqlalchemy as sa

revision = "summary_dirty_log_001"
down_revision = "drop_file_index_001"
branch_labels = None
depends_on = None


def upgrade():
    # No foreign key: its check would share-lock the experiments row. The key
    # includes the writer's own transaction id, so the conflict check can only
    # meet that transaction's earlier marker and never waits on another writer.
    op.create_table(
        "experiment_summary_dirty",
        sa.Column(
            "txid",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("txid_current()"),
        ),
        sa.Column("experiment_id", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("txid", "experiment_id"),
        if_not_exists=True,
    )
    # Replacing the body upgrades every installed trigger without trigger DDL,
    # which would take table locks on live tables.
    op.execute("""
        CREATE OR REPLACE FUNCTION dirty_experiment_summary(eid text) RETURNS void LANGUAGE sql AS $$
            INSERT INTO experiment_summary_dirty (experiment_id)
            SELECT eid WHERE eid IS NOT NULL
            ON CONFLICT DO NOTHING
        $$
    """)


def downgrade():
    # Frozen copy of the prepared_reads_001 body.
    op.execute("""
        CREATE OR REPLACE FUNCTION dirty_experiment_summary(eid text) RETURNS void LANGUAGE sql AS $$
            INSERT INTO experiment_summaries (experiment_id)
            SELECT id FROM experiments WHERE id = eid
            ON CONFLICT (experiment_id) DO UPDATE SET
                revision = experiment_summaries.revision + 1,
                last_dirty_txid = txid_current(),
                dirty_since = CASE WHEN experiment_summaries.revision = experiment_summaries.built_revision
                    THEN now() ELSE experiment_summaries.dirty_since END
            WHERE experiment_summaries.last_dirty_txid IS DISTINCT FROM txid_current()
        $$
    """)
    # Markers not yet folded become revisions so their rebuilds are not lost.
    op.execute("""
        INSERT INTO experiment_summaries (experiment_id)
        SELECT DISTINCT d.experiment_id FROM experiment_summary_dirty d
        JOIN experiments e ON e.id = d.experiment_id
        ON CONFLICT (experiment_id) DO UPDATE SET
            revision = experiment_summaries.revision + 1
    """)
    op.drop_table("experiment_summary_dirty")
