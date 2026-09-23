"""Log this sandbox's Docker in to the run's private registries.

Writes the same Docker config the worker path stages for a sandbox VM. It lives
in the monitor's home on the outer VM; the task's container never mounts it.
"""

from __future__ import annotations

import os
from pathlib import Path

from oddish.registry_auth import normalize_credentials
from oddish.workers.harbor.patches import _registry_login_config


def login(registry_auth: list[dict], home: Path | None = None) -> list[str]:
    """Returns the registries logged in to (for the log line; never the secrets)."""
    creds = normalize_credentials(registry_auth)
    if not creds:
        return []
    config_json, _secrets, registries = _registry_login_config(creds)
    docker_dir = (home or Path.home()) / ".docker"
    docker_dir.mkdir(parents=True, exist_ok=True)
    config = docker_dir / "config.json"
    fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(config_json)
    return sorted(set(registries))
