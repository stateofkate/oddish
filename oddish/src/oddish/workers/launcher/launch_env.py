"""The monitor sandbox's environment: control-plane handle, agent credentials,
the task's judge keys and any registry logins. Assembled by the launcher from
what it minted and resolved; nothing here reads the database."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping

from oddish.config import settings
from oddish.mon.contract import (
    AGENT_ENV_VAR,
    API_URL_VAR,
    DRIVER_ENV_VAR,
    TASK_ENV_VAR,
    REGISTRY_AUTH_VAR,
    TOKEN_VAR,
)
from oddish.workers.queue import job_tokens
from oddish.workers.queue.trial_steps import PreparedTrialRun


def build_sandbox_env(
    prepared: PreparedTrialRun,
    *,
    api_url: str,
    credential: str,
    byok_env: dict[str, str] | None,
    judge_env: Mapping[str, str],
    registry_credentials: list | None = None,
    ambient: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The process environment the monitor starts with.

    ``ambient`` is the launcher's own environment (``os.environ`` unless a
    test passes one): the worker's process holds the names some agents
    authenticate with and no settings field carries.
    """
    if ambient is None:
        ambient = os.environ
    provider = job_tokens.provider_env(
        agent=prepared.trial_agent,
        model=prepared.trial_model,
        settings=settings,
        byok_env=byok_env,
        raw_harbor_config=prepared.trial_harbor_config,
        ambient=ambient,
    )
    agent_env = {
        **provider.agent_env,
        **job_tokens.agent_ambient_env(prepared.trial_agent, ambient),
    }
    driver_env = {**provider.driver_env, **agent_env}
    if ambient.get("ODDISH_RELEASE"):
        driver_env["ODDISH_RELEASE"] = ambient["ODDISH_RELEASE"]
    return {
        API_URL_VAR: api_url,
        TOKEN_VAR: credential,
        DRIVER_ENV_VAR: json.dumps(driver_env),
        AGENT_ENV_VAR: json.dumps(agent_env),
        TASK_ENV_VAR: json.dumps(dict(judge_env)),
        **(
            {REGISTRY_AUTH_VAR: json.dumps([c.to_dict() for c in registry_credentials])}
            if registry_credentials
            else {}
        ),
    }
