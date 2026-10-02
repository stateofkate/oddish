"""The monitor's secrets: read once from the environment, then removed from it.

The launcher sets these through the provider's sandbox-creation slot. After
``load`` they live only in this process's memory, so nothing the monitor starts
(Docker, Compose, the agent) can inherit the token.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from oddish.mon.contract import (
    AGENT_ENV_VAR,
    DRIVER_ENV_VAR,
    TASK_ENV_VAR,
    API_URL_VAR,
    REGISTRY_AUTH_VAR,
    TOKEN_VAR,
)


class MonitorEnvError(RuntimeError):
    pass


@dataclass(frozen=True)
class MonitorEnv:
    api_url: str
    token: str = field(repr=False)
    agent_env: dict[str, str] = field(default_factory=dict, repr=False)
    driver_env: dict[str, str] = field(default_factory=dict, repr=False)
    task_env: dict[str, str] = field(default_factory=dict, repr=False)
    registry_auth: list[dict] = field(default_factory=list, repr=False)


def _require_https(api_url: str) -> str:
    parts = urlsplit(api_url)
    local = parts.hostname in {"localhost", "127.0.0.1", "host.docker.internal"}
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise MonitorEnvError(f"{API_URL_VAR} must be https")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise MonitorEnvError(f"{API_URL_VAR} must be a bare URL")
    return api_url.rstrip("/")


def load(environ: dict[str, str] | None = None) -> MonitorEnv:
    environ = os.environ if environ is None else environ
    api_url, token = environ.get(API_URL_VAR), environ.pop(TOKEN_VAR, None)
    if not api_url or not token:
        raise MonitorEnvError(f"{API_URL_VAR} and {TOKEN_VAR} are required")
    mappings = {}
    for name in (AGENT_ENV_VAR, DRIVER_ENV_VAR, TASK_ENV_VAR):
        raw = environ.pop(name, None)
        try:
            value = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise MonitorEnvError(f"{name} is not valid JSON") from exc
        if not isinstance(value, dict) or any(
            not isinstance(v, str) for v in value.values()
        ):
            raise MonitorEnvError(f"{name} must be a JSON object of strings")
        mappings[name] = value
    raw_registry_auth = environ.pop(REGISTRY_AUTH_VAR, None)
    try:
        registry_auth = json.loads(raw_registry_auth) if raw_registry_auth else []
    except json.JSONDecodeError as exc:
        raise MonitorEnvError(f"{REGISTRY_AUTH_VAR} is not valid JSON") from exc
    if not isinstance(registry_auth, list):
        raise MonitorEnvError(f"{REGISTRY_AUTH_VAR} must be a JSON list")
    return MonitorEnv(
        api_url=_require_https(api_url),
        token=token,
        agent_env=mappings[AGENT_ENV_VAR],
        driver_env=mappings[DRIVER_ENV_VAR],
        task_env=mappings[TASK_ENV_VAR],
        registry_auth=registry_auth,
    )
