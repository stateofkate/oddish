"""Job-scoped credential bundles (design spec §5.4 / §6.6).

A claimed trial can be issued a short-lived, **least-privilege** credential
bundle instead of the worker reading the blanket ``oddish-prod`` secret:

* the **model API key(s) for this job's provider only** — a Claude job's bundle
  carries the Anthropic key, never the OpenAI/Gemini keys — injected into the
  agent env in place of the blanket key set;
* an **S3 write prefix** that scopes oddish's *own* trial-artifact uploads to
  this trial's prefix — an oddish-side check in ``StorageClient`` (defense in
  depth against a path-traversal escape on that upload path). It does **not**
  cover Harbor's or other tools' uploads (they use their own credentials); full
  cross-path write isolation would need IAM/STS-scoped S3 credentials (§14.2);
* a random **token** whose SHA-256 hash is persisted on the ``worker_jobs`` row
  (the raw token is never stored) as the revocable credential handle, revoked on
  terminal status.

This module is pure (no DB / network). The gated wiring lives in the trial
handler (issue the bundle, inject the scoped model env, revoke on terminal) and
``StorageClient`` (enforce the prefix on oddish's trial uploads), behind
``settings.job_scoped_tokens_enabled`` (default off; dual-read hedge: bundle if
present, else the blanket secret).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

DEFAULT_TTL_SECONDS = (
    14400  # 4h: covers queue wait + run; trial timeouts are under this
)


@dataclass(frozen=True)
class JobCredentialBundle:
    """The credentials handed to a worker for one claimed job."""

    model_env: Mapping[str, str]
    s3_write_prefix: str
    expires_at: datetime


def mint_token() -> tuple[str, str]:
    """Return ``(raw_token, token_hash)``; only the hash is ever persisted."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def s3_write_prefix_for(trial_id: str) -> str:
    """Use the same deployment-scoped path as the artifact uploader."""
    from oddish.db.storage import StorageClient

    return StorageClient.trial_write_prefix(trial_id)


def authorize_s3_key(key: str, prefix: str) -> bool:
    """True iff ``key`` is within the job's authorized write ``prefix``."""
    return bool(key) and key.startswith(prefix)


# Providers whose key env is assembled by ``settings.get_openai_agent_env``.
_OPENAI_FAMILY = {"openai", "azure", "azure_openai"}
# The names grok-build authenticates with (workers/agents/grok_build.py reads
# XAI_API_KEY, or a comma-separated pool in XAI_API_KEYS).
_XAI_AUTH_ENV = ("XAI_API_KEY", "XAI_API_KEYS")
# The AWS chain LiteLLM/boto read for a ``bedrock/`` model, plus the region
# setting they require. Ambient on the worker; selected by name for the sandbox.
_BEDROCK_AUTH_ENV = (
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_REGION_NAME",
)
# Providers with no settings field. On the worker their agents read one key
# from the ambient environment (the ``${NAME}`` the claude-code profiles in
# harbor/agent_config.py resolve; dsh and other Harbor agents read the host).
# Names as in runner._PROVIDER_RUNTIME_SECRET_KEYS.
_AMBIENT_PROVIDER_KEYS: dict[str, tuple[str, ...]] = {
    "fireworks": ("FIREWORKS_API_KEY",),
    "zai": ("ZAI_API_KEY",),
    "minimax": ("MINIMAX_API_KEY",),
    "moonshot": ("MOONSHOT_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
}
# cursor-cli authenticates with CURSOR_API_KEY read from the host environment
# (Harbor agents/installed/cursor_cli.py), whatever model provider it fronts.
_CURSOR_AUTH_ENV = ("CURSOR_API_KEY",)


def bedrock_sandbox_env(ambient: Mapping[str, str]) -> dict[str, str]:
    """Resolve host-only AWS sources before crossing the sandbox boundary."""
    env = {name: ambient[name] for name in _BEDROCK_AUTH_ENV if ambient.get(name)}
    if env.get("AWS_BEARER_TOKEN_BEDROCK") or (env.get("AWS_ACCESS_KEY_ID") and env.get("AWS_SECRET_ACCESS_KEY")):
        return env
    sources = ("AWS_PROFILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI")
    if not any(ambient.get(name) for name in sources):
        return env
    import boto3
    from oddish.workers.harbor.agent_config import _temporary_env

    with _temporary_env(dict(ambient)):
        session = boto3.Session()
        credentials = session.get_credentials()
        if credentials is None:
            raise ValueError("Bedrock credentials could not be resolved on the worker")
        frozen = credentials.get_frozen_credentials()
        env.update(AWS_ACCESS_KEY_ID=frozen.access_key, AWS_SECRET_ACCESS_KEY=frozen.secret_key)
        if frozen.token:
            env["AWS_SESSION_TOKEN"] = frozen.token
        if session.region_name:
            env.setdefault("AWS_DEFAULT_REGION", session.region_name)
    return env


def scoped_model_env(
    *,
    agent: str,
    model: str | None,
    settings: Any,
    is_probe: bool = False,
    byok_env: Mapping[str, str] | None = None,
    ambient: Mapping[str, str] | None = None,
    raw_harbor_config: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Least-privilege model env for the job's provider only.

    Resolves the provider via ``settings.get_provider_for_trial`` and returns
    just that provider's key env — never the full blanket key set. Unknown
    providers return ``{}`` so the caller's dual-read falls back to the blanket
    secret rather than shipping an empty credential.

    ``ambient`` is the caller's own environment. The monitor launcher passes it
    (it runs in the worker) for agents that authenticate from ambient names with
    no settings field, so the sandbox, which has no ambient environment, gets
    the same names the worker path found in place. The worker path never passes
    it and is unchanged.

    ``raw_harbor_config`` is the trial's stored Harbor config: a row that
    carries no model still names one in its ``agent_config``, and the bundle
    must follow that effective model the way the runner does. Keyed on the
    bare row model, such a trial would get its agent's default bundle (the
    Bedrock flag, the Gemini key), which the runner applies last, over the
    provider profile it built for the stored model.
    """
    if raw_harbor_config:
        from oddish.workers.harbor.agent_config import _trial_requested_model

        agent, model = _trial_requested_model(
            agent=agent, model=model, raw_harbor_config=dict(raw_harbor_config)
        )
    provider = (settings.get_provider_for_trial(agent, model) or "").lower()

    if provider in _OPENAI_FAMILY:
        env = {k: v for k, v in settings.get_openai_agent_env(model=model).items() if v}
        if ambient is not None:
            # The monitor sandbox has no worker Settings, and its driver
            # re-resolves this route from two of them (runner, agent_config).
            # Seal those under the names Settings reads, so it resolves the
            # provider and deployment the worker did.
            openai_provider = getattr(settings, "openai_provider", None)
            deployments = getattr(settings, "azure_openai_deployments", None)
            if openai_provider:
                env["ODDISH_OPENAI_PROVIDER"] = openai_provider
            if deployments:
                env["ODDISH_AZURE_OPENAI_DEPLOYMENTS"] = json.dumps(deployments)
        return env
    if provider in ("anthropic", "claude"):
        key = getattr(settings, "anthropic_api_key", None)
        return {"ANTHROPIC_API_KEY": key} if key else {}
    if provider == "anthropic-hdo":
        # HDO-prefixed Claude trials overwrite ANTHROPIC_API_KEY with the
        # separate HDO credential (same shape Claude Code / litellm expect).
        key = getattr(settings, "anthropic_hdo_api_key", None)
        return {"ANTHROPIC_API_KEY": key} if key else {}
    if provider == "bedrock":
        # Bedrock keeps its selected transport for every agent. The monitor
        # sandbox has no worker ambient environment, so copy only the AWS
        # credential-chain names it needs; the existing worker path passes no
        # ``ambient`` mapping and continues using its own credential chain.
        return {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            **bedrock_sandbox_env(ambient or {}),
        }
    if provider == "gemini":
        key = getattr(settings, "gemini_api_key", None)
        # Publish the one platform Google key under BOTH names the agents read.
        # gemini-cli reads GEMINI_API_KEY; opencode is built on the AI SDK, whose
        # google provider reads GOOGLE_GENERATIVE_AI_API_KEY -- and Harbor's
        # opencode agent only forwards a var it finds in os.environ, so a bundle
        # carrying just GEMINI_API_KEY leaves opencode with no credential and it
        # exits before the first model call ("agent not started", 0 tokens).
        # Same value, two names: no second Modal secret, no new key to rotate.
        return (
            {"GEMINI_API_KEY": key, "GOOGLE_GENERATIVE_AI_API_KEY": key} if key else {}
        )
    if provider == "vertex_ai":
        # Google Vertex AI: non-secret coordinates only. The credential rides
        # the worker's sandbox upload (service-account file) or the runner's
        # ambient express-mode key, never the bundle.
        resolve = getattr(settings, "vertex_ai_config", None)
        if resolve is None:
            return {}
        try:
            config = resolve()
        except Exception:
            return {}
        env = {
            "VERTEXAI_LOCATION": config.location,
            "GOOGLE_CLOUD_LOCATION": config.location,
        }
        if config.project_id:
            env["VERTEXAI_PROJECT"] = config.project_id
            env["GOOGLE_CLOUD_PROJECT"] = config.project_id
        if ambient is not None:
            # The monitor sandbox has no worker Settings. Seal the names its
            # driver resolves this same config from (vertex_ai_config), so it
            # writes and uploads the same key file, or makes the same key
            # ambient, exactly as the worker's driver does.
            env.update(
                {
                    name: value
                    for name, value in (
                        ("VERTEX_AI_CREDENTIALS_JSON", config.credentials_json),
                        ("VERTEX_AI_API_KEY", config.api_key),
                        ("VERTEX_AI_PROJECT_ID", config.project_id),
                        ("VERTEX_AI_LOCATION", config.location),
                    )
                    if value
                }
            )
        return env
    if provider == "meta":
        key = getattr(settings, "meta_api_key", None)
        return {"META_API_KEY": key, "MSWEA_API_KEY": key} if key else {}
    if provider == "geometric":
        # mini-swe-agent authenticates from OPENAI_API_KEY (the litellm
        # ``openai/`` provider), so publish the one Geometric key under the
        # names the harness and the env shape both read.
        key = getattr(settings, "geometric_api_key", None)
        return (
            {"GEOMETRIC_API_KEY": key, "MSWEA_API_KEY": key, "OPENAI_API_KEY": key}
            if key
            else {}
        )
    if provider == "xai-public":
        key = os.environ.get("XAI_PUBLIC_API_KEY")
        return {"XAI_PUBLIC_API_KEY": key} if key else {}
    if provider == "xai":
        # grok-build has no settings field: on the worker it read XAI_API_KEY,
        # or the XAI_API_KEYS pool, from the ambient environment
        # (workers/agents/grok_build.py). Select those two names from the
        # caller's environment for the monitor sandbox, which has none.
        return {
            name: value
            for name in _XAI_AUTH_ENV
            if (value := (ambient or {}).get(name))
        }
    if provider in _AMBIENT_PROVIDER_KEYS:
        # Same as xai: select this provider's own name(s) from the caller's
        # environment for the monitor sandbox, which has none.
        return {
            name: value
            for name in _AMBIENT_PROVIDER_KEYS[provider]
            if (value := (ambient or {}).get(name))
        }
    return {}


def agent_ambient_env(
    agent: str | None, ambient: Mapping[str, str] | None
) -> dict[str, str]:
    """Credentials an agent harness reads from the host by name, whatever model
    provider it fronts. Only cursor-cli today. Empty without ``ambient``, so the
    worker path is unchanged."""
    if not ambient or "cursor-cli" not in (agent or "").strip().lower():
        return {}
    return {name: value for name in _CURSOR_AUTH_ENV if (value := ambient.get(name))}


def merge_agent_env(
    bundle: "JobCredentialBundle | None",
    probe_env: Mapping[str, str] | None,
) -> dict[str, str] | None:
    """Combine the job-scoped model env with any probe env for the agent.

    The agent receives the bundle's scoped model key(s) plus any probe creds;
    probe creds win on the (non-overlapping) off chance of a key clash. Returns
    ``None`` when there is nothing to inject, preserving the
    ``extra_agent_env: dict | None`` contract.
    """
    if bundle is None:
        # No bundle (the common, flag-off case): pass the caller's env through
        # unchanged so behavior is byte-for-byte identical.
        return dict(probe_env) if probe_env is not None else None
    merged = dict(bundle.model_env)
    if probe_env:
        merged.update(probe_env)
    return merged or None


def build_bundle(
    *,
    agent: str,
    model: str | None,
    is_probe: bool = False,
    byok_env: Mapping[str, str] | None = None,
    raw_harbor_config: Mapping[str, Any] | None = None,
    trial_id: str,
    settings: Any,
    now: datetime,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> tuple[JobCredentialBundle, str]:
    """Assemble a job-scoped bundle and its persistable token hash.

    Returns the bundle (model env + S3 prefix the worker uses) and the token
    hash (the revocable handle persisted on the row). The raw token is hashed
    and discarded -- it is not held by the worker today (in-process verification
    is not wired; the worker writes state to Postgres directly).
    """
    _, token_hash = mint_token()
    bundle = JobCredentialBundle(
        model_env=scoped_model_env(
            agent=agent,
            model=model,
            settings=settings,
            is_probe=is_probe,
            byok_env=byok_env,
            raw_harbor_config=raw_harbor_config,
        ),
        s3_write_prefix=s3_write_prefix_for(trial_id),
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    return bundle, token_hash
