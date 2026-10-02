"""grok-build gets its xAI key by name from the launcher's environment.

Regression: on the worker, grok-build read XAI_API_KEY (or the XAI_API_KEYS
pool) from the ambient environment (oddish/workers/agents/grok_build.py). The
launcher's key selector has no settings-backed xai branch, and the monitor
sandbox has no ambient environment, so grok-build would run keyless there.
The selector now takes the caller's environment and forwards exactly those two
names for the xai provider. Every other branch is unchanged, and the worker
path, which never passes ``ambient``, behaves exactly as before.

"""

from __future__ import annotations

import json
from types import SimpleNamespace

from oddish.workers.queue import job_tokens

AMBIENT = {
    "XAI_API_KEY": "xai-1",
    "XAI_API_KEYS": "xai-1,xai-2",
    "DATABASE_URL": "postgres://never",
    "SLACK_BOT_TOKEN": "xoxb-never",
    "ANTHROPIC_API_KEY": "ant-ambient",
    "AWS_BEARER_TOKEN_BEDROCK": "bearer-never",
}


def _settings():
    ns = SimpleNamespace(
        anthropic_api_key="ant-settings",
        anthropic_hdo_api_key=None,
        gemini_api_key=None,
        meta_api_key=None,
        geometric_api_key=None,
        claude_code_force_direct_api=True,
    )
    ns.get_provider_for_trial = lambda agent, model: {
        "grok-build": "xai",
        "terminus-2": "anthropic",
        "mystery": "mystery-provider",
    }.get(agent, "anthropic")
    ns.get_openai_agent_env = lambda model=None: {}
    return ns


def test_grok_build_gets_both_xai_names_present_in_ambient():
    env = job_tokens.provider_env(
        agent="grok-build", model="grok-4", settings=_settings(), ambient=AMBIENT
    ).agent_env
    assert env == {"XAI_API_KEY": "xai-1", "XAI_API_KEYS": "xai-1,xai-2"}


def test_only_the_pool_when_that_is_all_that_is_set():
    env = job_tokens.provider_env(
        agent="grok-build",
        model="grok-4",
        settings=_settings(),
        ambient={"XAI_API_KEYS": "a,b"},
    ).agent_env
    assert env == {"XAI_API_KEYS": "a,b"}


def test_nothing_outside_the_two_xai_names_is_forwarded():
    env = job_tokens.provider_env(
        agent="grok-build", model="grok-4", settings=_settings(), ambient=AMBIENT
    ).agent_env
    for never in (
        "DATABASE_URL",
        "SLACK_BOT_TOKEN",
        "ANTHROPIC_API_KEY",
        "AWS_BEARER_TOKEN_BEDROCK",
    ):
        assert never not in env
    assert set(env) <= {"XAI_API_KEY", "XAI_API_KEYS"}


def test_a_settings_backed_provider_ignores_ambient():
    env = job_tokens.provider_env(
        agent="terminus-2",
        model="anthropic/claude-sonnet-5",
        settings=_settings(),
        ambient=AMBIENT,
    ).agent_env
    assert env == {"ANTHROPIC_API_KEY": "ant-settings"}


def test_an_unknown_provider_still_gets_nothing():
    env = job_tokens.provider_env(
        agent="mystery", model="mystery/thing", settings=_settings(), ambient=AMBIENT
    ).agent_env
    assert env == {}


def test_the_worker_path_without_ambient_is_unchanged():
    env = job_tokens.provider_env(
        agent="grok-build", model="grok-4", settings=_settings()
    ).agent_env
    assert env == {}


def test_single_llm_on_bedrock_keeps_its_flag_and_gets_the_aws_chain_present():
    settings = _settings()
    settings.get_provider_for_trial = lambda agent, model: "bedrock"
    ambient = {
        "AWS_BEARER_TOKEN_BEDROCK": "bearer-1",
        "AWS_ACCESS_KEY_ID": "AKIA-1",
        "AWS_SECRET_ACCESS_KEY": "secret-1",
        "AWS_REGION": "us-west-2",
        "DATABASE_URL": "postgres://never",
    }
    env = job_tokens.provider_env(
        agent="single-llm",
        model="global.anthropic.claude-sonnet-4-6",
        settings=settings,
        ambient=ambient,
    ).agent_env
    assert env == {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_BEARER_TOKEN_BEDROCK": "bearer-1",
        "AWS_ACCESS_KEY_ID": "AKIA-1",
        "AWS_SECRET_ACCESS_KEY": "secret-1",
        "AWS_REGION": "us-west-2",
    }
    assert "AWS_SESSION_TOKEN" not in env  # not set: not invented
    # Worker path unchanged: flag only, the chain is ambient there already.
    assert job_tokens.provider_env(
        agent="single-llm",
        model="global.anthropic.claude-sonnet-4-6",
        settings=settings,
    ).agent_env == {"CLAUDE_CODE_USE_BEDROCK": "1"}


def test_cursor_cli_gets_cursor_api_key_on_top_of_its_provider_key():
    ambient = {"CURSOR_API_KEY": "cur-1", "DATABASE_URL": "postgres://never"}
    assert job_tokens.agent_ambient_env("cursor-cli", ambient) == {"CURSOR_API_KEY": "cur-1"}
    assert job_tokens.agent_ambient_env("cursor-cli", None) == {}
    assert job_tokens.agent_ambient_env("claude-code", ambient) == {}


def test_fireworks_and_moonshot_get_only_their_own_key_from_ambient():
    """No settings field: on the worker these keys were ambient (the claude-code
    profiles resolve ``${FIREWORKS_API_KEY}`` and friends from the environment),
    so the keyless sandbox needs the provider's own name, and only that."""
    settings = _settings()
    settings.get_provider_for_trial = lambda agent, model: model.split("/", 1)[0]
    ambient = {
        "FIREWORKS_API_KEY": "fw-1",
        "ZAI_API_KEY": "zai-never",
        "MOONSHOT_API_KEY": "ms-1",
        "DATABASE_URL": "postgres://never",
    }
    assert job_tokens.provider_env(
        agent="claude-code",
        model="fireworks/glm-5p2",
        settings=settings,
        ambient=ambient,
    ).agent_env == {"FIREWORKS_API_KEY": "fw-1"}
    assert job_tokens.provider_env(
        agent="claude-code",
        model="moonshot/kimi-k2.7-code",
        settings=settings,
        ambient=ambient,
    ).agent_env == {"MOONSHOT_API_KEY": "ms-1"}
    # Worker path unchanged: the key is ambient there already.
    assert (
        job_tokens.provider_env(
            agent="claude-code", model="fireworks/glm-5p2", settings=settings
        ).agent_env
        == {}
    )


def test_vertex_ai_gets_the_settings_its_driver_resolves_from():
    """The monitor's driver resolves a vertex_ai/ trial from Settings, as the
    worker's does, and the sandbox has none: those names are sealed for that
    provider only. The worker path keeps its coordinates-only bundle."""
    from oddish.config import VertexAiConfig

    settings = _settings()
    settings.get_provider_for_trial = lambda agent, model: (
        "vertex_ai" if (model or "").startswith("vertex_ai/") else "anthropic"
    )
    settings.vertex_ai_config = lambda: VertexAiConfig(
        mode="service_account",
        project_id="oddish-vertex",
        location="us-east5",
        credentials_json='{"type": "service_account"}',
        api_key=None,
    )
    coordinates = {
        "VERTEXAI_LOCATION": "us-east5",
        "GOOGLE_CLOUD_LOCATION": "us-east5",
        "VERTEXAI_PROJECT": "oddish-vertex",
        "GOOGLE_CLOUD_PROJECT": "oddish-vertex",
    }
    model = "vertex_ai/claude-sonnet-5"
    env = job_tokens.provider_env(
        agent="claude-code", model=model, settings=settings, ambient=AMBIENT
    )
    assert env.agent_env == coordinates
    assert env.driver_env == {
        **coordinates,
        "VERTEX_AI_CREDENTIALS_JSON": '{"type": "service_account"}',
        "VERTEX_AI_PROJECT_ID": "oddish-vertex",
        "VERTEX_AI_LOCATION": "us-east5",
    }
    assert "VERTEX_AI_API_KEY" not in env.driver_env  # not configured: not invented
    # Worker path unchanged: the credential is in place there.
    assert (
        job_tokens.provider_env(
            agent="claude-code", model=model, settings=settings
        ).agent_env
        == coordinates
    )
    # Another provider on the same worker never gets it.
    assert job_tokens.provider_env(
        agent="terminus-2",
        model="anthropic/claude-sonnet-5",
        settings=settings,
        ambient=AMBIENT,
    ).agent_env == {"ANTHROPIC_API_KEY": "ant-settings"}


def test_openai_family_gets_the_routing_settings_its_driver_resolves_from(monkeypatch):
    """The monitor's driver re-resolves an openai/ trial's route from Settings
    (the provider selector and the model to Azure deployment map), as the
    worker's does, and the sandbox has neither: both are sealed, read from the
    launcher's Settings (a .env-only value included), so the sandbox's Settings
    resolves the same route. The worker path and other providers never get them."""
    from oddish.config import Settings

    for name in (
        "ODDISH_OPENAI_PROVIDER",
        "ODDISH_AZURE_OPENAI_DEPLOYMENTS",
        "OPENAI_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_VERSION",
        "AZURE_OPENAI_DEPLOYMENT",
    ):
        monkeypatch.delenv(name, raising=False)
    names = {"ODDISH_OPENAI_PROVIDER", "ODDISH_AZURE_OPENAI_DEPLOYMENTS"}
    model = "openai/gpt-5.2"
    azure = Settings(
        _env_file=None,
        AZURE_OPENAI_API_KEY="az-key",
        AZURE_OPENAI_ENDPOINT="https://example.openai.azure.com/openai/v1",
        AZURE_OPENAI_API_VERSION="2025-01-01-preview",
        azure_openai_deployments={model: "oddish-gpt", "gpt-5.4": "oddish-gpt-54"},
        ANTHROPIC_API_KEY="ant",
    )
    public = Settings(_env_file=None, openai_provider="openai", OPENAI_API_KEY="sk-oai")
    for launcher in (azure, public):
        env = job_tokens.provider_env(
            agent="codex", model=model, settings=launcher, ambient=AMBIENT
        )
        assert not names & set(env.agent_env)
        with monkeypatch.context() as sandbox_env:
            for name, value in env.driver_env.items():
                sandbox_env.setenv(name, value)
            sandbox = Settings(_env_file=None)
        assert sandbox.azure_openai_deployments == launcher.azure_openai_deployments
        assert sandbox.openai_provider == launcher.openai_provider
        assert sandbox.get_openai_agent_env(model=model) == (
            launcher.get_openai_agent_env(model=model)
        )
        # Worker path unchanged: its own Settings resolves the route.
        worker = job_tokens.provider_env(
            agent="codex", model=model, settings=launcher
        ).agent_env
        assert not names & set(worker)
    assert (
        json.loads(env.driver_env["ODDISH_AZURE_OPENAI_DEPLOYMENTS"]) == {}
    )  # public: none configured
    # Another provider on the same worker never gets them.
    assert job_tokens.provider_env(
        agent="terminus-2",
        model="anthropic/claude-sonnet-5",
        settings=azure,
        ambient=AMBIENT,
    ).agent_env == {"ANTHROPIC_API_KEY": "ant"}
