"""seed built-in directive skills (cheat-detector, verifier-critic, ...)

Revision ID: skills_seed_directives_001
Revises: skills_from_presets_001
Create Date: 2026-06-25 00:00:00.000000
"""

from typing import Sequence, Union

revision = "skills_seed_directives_001"
down_revision: Union[str, Sequence[str], None] = "skills_from_presets_001"
branch_labels = None
depends_on = None


# The seeded skills fed the removed operator-probe feature; probe_removal_001
# drops the skills tables, so this revision no longer seeds anything.
def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
