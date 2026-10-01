"""Merge the status fix and invalidate archive indexes on in-place uploads.

The file index was reverted; this revision stays so databases that already
applied it keep a valid history. drop_file_index_001 removes its objects.
"""

revision = "archive_index_001"
down_revision = ("legacy_file_index_001", "prepared_status_001")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
