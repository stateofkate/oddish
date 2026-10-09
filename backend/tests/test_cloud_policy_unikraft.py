"""cloud_policy derives its allowed environments from the runtime registry, so
enabling Unikraft must surface ``unikraft`` there -- but only on a Harbor pin
that carries the environment, or ``EnvironmentType(b.name)`` would fail."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _probe(*, enabled: bool) -> tuple[bool, set[str]]:
    code = (
        "import cloud_policy;"
        "from harbor.models.environment_type import EnvironmentType as E;"
        "print(hasattr(E, 'UNIKRAFT'));"
        "print(','.join(sorted(e.value for e in cloud_policy.ALLOWED_CLOUD_ENVIRONMENTS)))"
    )
    env = {k: v for k, v in os.environ.items() if k != "ODDISH_UNIKRAFT_ENABLED"}
    if enabled:
        env["ODDISH_UNIKRAFT_ENABLED"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_BACKEND_ROOT),
    )
    assert result.returncode == 0, result.stderr
    has_env, allowed = result.stdout.strip().splitlines()
    return has_env == "True", set(allowed.split(","))


def test_unikraft_absent_when_disabled() -> None:
    _, allowed = _probe(enabled=False)
    assert "unikraft" not in allowed


def test_unikraft_admitted_when_enabled_on_a_pin_that_carries_it() -> None:
    has_env, allowed = _probe(enabled=True)
    assert ("unikraft" in allowed) is has_env
