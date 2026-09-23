"""The monitor sandbox's environment: control-plane handle, agent credentials,
the task's judge keys and any registry logins. Assembled by the launcher from
what it minted and resolved; nothing here reads the database."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping

from oddish.config import settings
from oddish.mon.contract import AGENT_ENV_VAR, API_URL_VAR, REGISTRY_AUTH_VAR, TOKEN_VAR
from oddish.workers.queue import byok, job_tokens
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
    # The sandbox has no ambient secrets, so the platform key for this
    # provider is passed in; a user key wins over it.
    agent_env = {
        **job_tokens.scoped_model_env(
            agent=prepared.trial_agent,
            model=prepared.trial_model,
            settings=settings,
            byok_env=byok_env,
            raw_harbor_config=prepared.trial_harbor_config,
            # The launcher runs in the worker, whose environment holds the
            # names some agents authenticate with and no settings field
            # carries (grok-build's xAI keys, single-llm's AWS chain on
            # Bedrock). Select those by name for the sandbox, which has no
            # ambient environment of its own.
            ambient=ambient,
        ),
        # Harness credentials read from the host by agent, not provider
        # (cursor-cli's CURSOR_API_KEY).
        **job_tokens.agent_ambient_env(prepared.trial_agent, ambient),
        **(byok.merge_byok_env(byok_env, None) or {}),
    }
    return {
        API_URL_VAR: api_url,
        TOKEN_VAR: credential,
        AGENT_ENV_VAR: json.dumps(agent_env),
        # Judge keys the task's env blocks interpolate (verifier, environment,
        # solution, step verifiers). They go into the monitor's process env,
        # the same place the agent's model keys land (mon/runner.py updates
        # os.environ with them) and where Harbor resolves ``${VAR}``. Harbor's
        # agent adapters forward only the provider keys they know by name, so
        # a judge key reaches the agent container only when the agent's own
        # provider is that judge's provider, as on the worker path.
        **judge_env,
        **(
            {REGISTRY_AUTH_VAR: json.dumps([c.to_dict() for c in registry_credentials])}
            if registry_credentials
            else {}
        ),
    }
