"""Which Docker builder a task's Dockerfile gets: BuildKit only when it asks.

Docker's built-in (classic) builder and BuildKit export the same instructions
differently. QA40 task 30's Cabal verifier records the ghc binary,
/usr/share/pkgconfig and the cabal store at build time and reconfigures when
they differ at run time; after a BuildKit export they differ, after a classic
export they do not. The old worker paths' builders preserved what Cabal
recorded. Tasks 33 and 36 need BuildKit (``# syntax=`` directive,
``RUN --network=none``); the classic builder cannot parse them. So the choice
is per Dockerfile, from its own text, on the warm-up prebuild and on the
trial's build alike.
"""

from __future__ import annotations

import re
from pathlib import Path

# A syntax directive selects a BuildKit frontend; the flags and heredocs exist
# only in BuildKit's Dockerfile grammar.
_BUILDKIT_ONLY = re.compile(
    r"^\s*#\s*syntax\s*="
    r"|^\s*RUN\s+(?:--\w+\S*\s+)*--(?:mount|network|security)\b"
    r"|^\s*(?:COPY|ADD)\s+(?:--\w+\S*\s+)*--(?:link|checksum|exclude|parents)\b"
    r"|^\s*(?:RUN|COPY|ADD)\b.*<<-?['\"]?\w+['\"]?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def uses_buildkit(dockerfile: str) -> bool:
    return _BUILDKIT_ONLY.search(dockerfile) is not None


def env_for(task: Path) -> dict[str, str]:
    """The builder selection for ``docker build`` and ``docker compose build``."""
    dockerfile = task / "environment" / "Dockerfile"
    choice = "1" if dockerfile.is_file() and uses_buildkit(dockerfile.read_text(errors="replace")) else "0"
    return {"DOCKER_BUILDKIT": choice, "COMPOSE_DOCKER_CLI_BUILD": choice}
