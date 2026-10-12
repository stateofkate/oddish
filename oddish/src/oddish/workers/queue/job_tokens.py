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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping

from oddish.vendor_harnesses import vendor_harness_for_agent

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
    "relace": ("RELACE_API_KEY",),
    "zai": ("ZAI_API_KEY",),
    "minimax": ("MINIMAX_API_KEY",),
    "moonshot": ("MOONSHOT_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
}


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


@dataclass(frozen=True)
class ProviderEnv:
    """Separate driver startup inputs from the agent's execution environment."""

    driver_env: dict[str, str] = field(repr=False)
    agent_env: dict[str, str] = field(repr=False)


def provider_env(
    *,
    agent: str,
    model: str | None,
    settings: Any,
    byok_env: Mapping[str, str] | None = None,
    ambient: Mapping[str, str] | None = None,
    raw_harbor_config: Mapping[str, Any] | None = None,
) -> ProviderEnv:
    """Resolve one effective route before crossing the process boundary.

    HDO selects a separate platform account and wins over a user key. For
    direct Anthropic trials the already selected user key wins. Driver-only
    settings never enter the agent override mapping.
    """
    from oddish.workers.harbor.agent_config import _trial_requested_model

    agent, model = _trial_requested_model(
        agent=agent, model=model, raw_harbor_config=dict(raw_harbor_config or {})
    )
    ambient = os.environ if ambient is None else ambient
    provider = (settings.get_provider_for_trial(agent, model) or "").lower()
    driver: dict[str, str] = {}
    env: dict[str, str] = {}
    if provider in _OPENAI_FAMILY:
        env = {k: v for k, v in settings.get_openai_agent_env(model=model).items() if v}
        driver = {
            "ODDISH_OPENAI_PROVIDER": settings.openai_provider,
            "ODDISH_AZURE_OPENAI_DEPLOYMENTS": json.dumps(
                settings.azure_openai_deployments
            ),
        }
    elif provider in ("anthropic", "claude", "anthropic-hdo"):
        key = (
            settings.anthropic_hdo_api_key
            if provider == "anthropic-hdo"
            else (byok_env or {}).get("ANTHROPIC_API_KEY") or settings.anthropic_api_key
        )
        env = {"ANTHROPIC_API_KEY": key} if key else {}
        if provider == "anthropic-hdo" and key:
            driver["ANTHROPIC_HDO_API_KEY"] = key
    elif provider == "bedrock":
        env = {"CLAUDE_CODE_USE_BEDROCK": "1", **bedrock_sandbox_env(ambient)}
    elif provider == "gemini":
        key = settings.gemini_api_key
        env = (
            {"GEMINI_API_KEY": key, "GOOGLE_GENERATIVE_AI_API_KEY": key} if key else {}
        )
    elif provider == "vertex_ai":
        config = settings.vertex_ai_config()
        env = {
            "VERTEXAI_LOCATION": config.location,
            "GOOGLE_CLOUD_LOCATION": config.location,
        }
        if config.project_id:
            env.update(
                VERTEXAI_PROJECT=config.project_id,
                GOOGLE_CLOUD_PROJECT=config.project_id,
            )
        driver = {
            name: value
            for name, value in (
                ("VERTEX_AI_CREDENTIALS_JSON", config.credentials_json),
                ("VERTEX_AI_API_KEY", config.api_key),
                ("VERTEX_AI_PROJECT_ID", config.project_id),
                ("VERTEX_AI_LOCATION", config.location),
            )
            if value
        }
    elif provider in ("meta", "meta-public", "meta-public-2"):
        from oddish.config import META_PUBLIC_API_KEY_ENVS, resolve_meta_public_api_key

        public_env = META_PUBLIC_API_KEY_ENVS.get(provider)
        key = (
            resolve_meta_public_api_key(settings, provider)
            if public_env
            else settings.meta_api_key
        )
        env = {"META_API_KEY": key, "MSWEA_API_KEY": key} if key else {}
        if public_env and key:
            env[public_env] = key
        driver["META_BASE_URL"] = settings.meta_base_url
        for name, attribute in (
            ("ODDISH_META_EVAL_NAME", "meta_eval_name"),
            ("ODDISH_META_SESSION_ID", "meta_session_id"),
        ):
            if value := getattr(settings, attribute, None):
                driver[name] = value
    elif provider in ("geometric", "hunyuan"):
        key = getattr(settings, f"{provider}_api_key")
        name = provider.upper()
        env = (
            {f"{name}_API_KEY": key, "MSWEA_API_KEY": key, "OPENAI_API_KEY": key}
            if key
            else {}
        )
        driver[f"{name}_BASE_URL"] = getattr(settings, f"{provider}_base_url")
        if provider == "geometric" and settings.geometric_anthropic_base_url:
            driver["GEOMETRIC_ANTHROPIC_BASE_URL"] = (
                settings.geometric_anthropic_base_url
            )
    elif provider == "xai-public":
        env = (
            {"XAI_PUBLIC_API_KEY": ambient["XAI_PUBLIC_API_KEY"]}
            if ambient.get("XAI_PUBLIC_API_KEY")
            else {}
        )
    elif provider == "xai":
        env = {name: ambient[name] for name in _XAI_AUTH_ENV if ambient.get(name)}
    elif provider in _AMBIENT_PROVIDER_KEYS:
        env = {
            name: ambient[name]
            for name in _AMBIENT_PROVIDER_KEYS[provider]
            if ambient.get(name)
        }
        endpoint = f"{provider.upper()}_BASE_URL"
        if ambient.get(endpoint):
            driver[endpoint] = ambient[endpoint]
    return ProviderEnv(driver_env={**env, **driver}, agent_env=env)


def agent_ambient_env(
    agent: str | None, ambient: Mapping[str, str] | None
) -> dict[str, str]:
    """Credentials an agent harness reads from the host by name, whatever model
    provider it fronts: the vendor-service harnesses (cursor-cli, devin). Empty
    without ``ambient``, so the worker path is unchanged."""
    harness = vendor_harness_for_agent(agent)
    if not ambient or harness is None:
        return {}
    return {
        name: value for name in harness.auth_env_keys if (value := ambient.get(name))
    }


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
        model_env=provider_env(
            agent=agent,
            model=model,
            settings=settings,
            byok_env=byok_env,
            raw_harbor_config=raw_harbor_config,
        ).agent_env,
        s3_write_prefix=s3_write_prefix_for(trial_id),
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    return bundle, token_hash
