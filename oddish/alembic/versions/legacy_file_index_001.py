"""Queue versionless task directories without changing task or trial history.

The file index was reverted; this revision stays so databases that already
applied it keep a valid history. drop_file_index_001 removes its objects.
"""

revision = "legacy_file_index_001"
down_revision = "file_index_001"
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
