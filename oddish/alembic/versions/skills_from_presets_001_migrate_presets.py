"""migrate probe_presets rows into skills (+ SKILL.md), de-duplicating names

Revision ID: skills_from_presets_001
Revises: skills_directive_001
Create Date: 2026-06-25 00:00:00.000000
"""

from typing import Sequence, Union

revision: str = "skills_from_presets_001"
down_revision: Union[str, Sequence[str], None] = "skills_directive_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Copied probe presets into skills for the removed operator-probe feature;
# probe_removal_001 drops the skills tables, so this revision no longer
# migrates anything.
def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
