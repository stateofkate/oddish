"""The Oddish antigravity wrapper prefers Vertex when the profile is present."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import settings
from oddish.workers.agents.antigravity_cli import OddishAntigravityCli
from oddish.workers.harbor import vertex_ai

_SA_JSON = (
    '{"type": "service_account", "project_id": "p", "private_key_id": "k", '
    '"private_key": "-----BEGIN PRIVATE KEY-----\\nX\\n-----END PRIVATE KEY-----\\n", '
    '"client_email": "t@p.iam.gserviceaccount.com", '
    '"token_uri": "https://oauth2.googleapis.com/token"}'
)


@pytest.fixture
def vertex_profile(monkeypatch):
    monkeypatch.setattr(settings, "vertex_ai_project_id", "p")
    monkeypatch.setattr(settings, "vertex_ai_location", "global")
    monkeypatch.setattr(settings, "vertex_ai_credentials_json", SecretStr(_SA_JSON))
    monkeypatch.setattr(settings, "vertex_ai_api_key", None)
    for key in ("AGY_ADC_AUTH", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return vertex_ai.vertex_ai_agent_env(
        settings.vertex_ai_config(), "vertex_ai/gemini-3.8-flash"
    )


def _agent(tmp_path: Path, model: str, env: dict[str, str], **kwargs):
    return OddishAntigravityCli(
        logs_dir=tmp_path, model_name=model, extra_env=dict(env), **kwargs
    )


def test_vertex_profile_turns_on_adc_mode(tmp_path, vertex_profile):
    agent = _agent(tmp_path, "vertex_ai/gemini-3.8-flash", vertex_profile)
    assert agent._use_adc_auth() is True
    # ADC mode carries no API-key material into the container.
    assert "GEMINI_API_KEY" not in agent._extra_env
    assert "GOOGLE_API_KEY" not in agent._extra_env


def test_explicit_opt_out_beats_the_profile(tmp_path, vertex_profile):
    agent = _agent(
        tmp_path,
        "vertex_ai/gemini-3.8-flash",
        {**vertex_profile, "AGY_ADC_AUTH": "false"},
    )
    assert agent._use_adc_auth() is False


def test_opt_out_survives_persistence_only_as_a_template(tmp_path, vertex_profile):
    """``AGY_ADC_AUTH`` matches Harbor's ``AUTH`` redaction, so a submitted
    literal is persisted as ``****`` and fails agy's boolean parse on the
    worker; the self-defaulting template is what a run has to submit."""
    from harbor.models.trial.config import AgentConfig
    from harbor.utils.env import resolve_env_vars

    from oddish.workers.harbor.restricted_network import _antigravity_profile

    def persisted(value: str) -> str:
        submitted = AgentConfig(
            name="antigravity-cli",
            model_name="vertex_ai/gemini-3.8-flash",
            env={"AGY_ADC_AUTH": value},
        )
        dumped = submitted.model_dump(mode="json", exclude_defaults=True)
        return dumped["env"]["AGY_ADC_AUTH"]

    assert persisted("false") == "****"
    with pytest.raises(ValueError, match="AGY_ADC_AUTH"):
        _agent(
            tmp_path,
            "vertex_ai/gemini-3.8-flash",
            {**vertex_profile, "AGY_ADC_AUTH": "****"},
        )

    template = "${AGY_ADC_AUTH:-false}"
    assert persisted(template) == template
    # The worker applies the profile and resolves templates before the agent
    # and the restricted profile see the env.
    resolved = resolve_env_vars({**vertex_profile, "AGY_ADC_AUTH": template})
    assert resolved["AGY_ADC_AUTH"] == "false"
    agent = _agent(tmp_path, "vertex_ai/gemini-3.8-flash", resolved)
    assert agent._use_adc_auth() is False
    config = AgentConfig(
        import_path="oddish.workers.agents.antigravity_cli:OddishAntigravityCli",
        model_name="vertex_ai/gemini-3.8-flash",
    )
    profile = _antigravity_profile(OddishAntigravityCli, config, resolved)
    assert profile.outbound_hosts[0] == "aiplatform.googleapis.com"


def test_ai_studio_trials_keep_the_key_path(tmp_path, monkeypatch):
    for key in ("AGY_ADC_AUTH", "GOOGLE_GENAI_USE_VERTEXAI"):
        monkeypatch.delenv(key, raising=False)
    agent = _agent(tmp_path, "gemini/gemini-3.8-flash", {"GEMINI_API_KEY": "studio"})
    assert agent._use_adc_auth() is False
    assert agent._extra_env["GEMINI_API_KEY"] == "studio"


def test_reasoning_effort_defaults_to_high_for_gemini_3(tmp_path, vertex_profile):
    agent = _agent(tmp_path, "vertex_ai/gemini-3.8-flash", vertex_profile)
    assert agent._reasoning_effort == "high"
    explicit = _agent(
        tmp_path, "vertex_ai/gemini-3.8-flash", vertex_profile, reasoning_effort="low"
    )
    assert explicit._reasoning_effort == "low"
    studio = _agent(tmp_path, "gemini/gemini-3.8-pro", {"GEMINI_API_KEY": "studio"})
    assert studio._reasoning_effort == "high"


def test_reasoning_effort_stays_unset_where_agy_rejects_it(tmp_path):
    for model in ("gemini/gemini-2.5-pro", "gemini-2.0-flash", "gemini-1.5-pro"):
        agent = _agent(tmp_path, model, {"GEMINI_API_KEY": "studio"})
        assert agent._reasoning_effort is None, model
    for model in ("gemini-3-pro-preview", "vertex_ai/gemini-3.5-flash"):
        assert _agent(tmp_path, model, {})._reasoning_effort == "high", model


def test_effort_default_reads_the_initialised_model(tmp_path, monkeypatch):
    """Harbor's second positional slot is the prompt template, not the model,
    so the default must come from the model the base class resolved."""
    for key in ("AGY_ADC_AUTH", "GOOGLE_GENAI_USE_VERTEXAI"):
        monkeypatch.delenv(key, raising=False)
    template = tmp_path / "gemini-3-pro-preview"
    assert OddishAntigravityCli(tmp_path, template)._reasoning_effort is None
    assert OddishAntigravityCli(tmp_path, str(template))._reasoning_effort is None
    keyed = OddishAntigravityCli(tmp_path, template, model_name="gemini-3-pro-preview")
    assert keyed._reasoning_effort == "high"


def test_express_mode_keeps_agy_on_its_key_path(tmp_path, monkeypatch):
    """The express profile carries only an API key; ADC would drop it and fail."""
    monkeypatch.setattr(settings, "vertex_ai_project_id", None)
    monkeypatch.setattr(settings, "vertex_ai_credentials_json", None)
    monkeypatch.setattr(settings, "vertex_ai_api_key", SecretStr("express-key"))
    for key in ("AGY_ADC_AUTH", "GOOGLE_GENAI_USE_VERTEXAI"):
        monkeypatch.delenv(key, raising=False)
    express = vertex_ai.vertex_ai_agent_env(
        settings.vertex_ai_config(), "vertex_ai/gemini-3.8-flash"
    )
    resolved = {**express, "GOOGLE_API_KEY": "express-key"}
    agent = _agent(tmp_path, "vertex_ai/gemini-3.8-flash", resolved)
    assert agent._use_adc_auth() is False
    assert agent._extra_env["GEMINI_API_KEY"] == "express-key"


def test_ordinary_trials_run_through_the_wrapper(vertex_profile):
    """Public and plain Dockerfile trials must get the wrapper too, not only the
    restricted paths, or the ADC and effort defaults never apply."""
    from oddish.workers.harbor.agent_config import _build_agent_config

    raw = {
        "agent_config": {
            "name": "antigravity-cli",
            "model_name": "vertex_ai/gemini-3.8-flash",
            "env": {},
        }
    }
    built = _build_agent_config(
        agent="antigravity-cli",
        model="vertex_ai/gemini-3.8-flash",
        raw_harbor_config=raw,
    )
    assert (
        built.import_path
        == "oddish.workers.agents.antigravity_cli:OddishAntigravityCli"
    )
    assert built.name is None
    # A caller's own import path is left alone.
    custom = {**raw, "agent_config": {**raw["agent_config"], "import_path": "x.y:Z"}}
    assert (
        _build_agent_config(
            agent="antigravity-cli",
            model="vertex_ai/gemini-3.8-flash",
            raw_harbor_config=custom,
        ).import_path
        == "x.y:Z"
    )


def test_restricted_networks_grant_the_adc_host_set(vertex_profile):
    """On the service-account profile the wrapper puts agy into ADC mode, whose
    egress was captured live: the startup hosts, the token host, and the
    location's endpoint. The restricted profile grants exactly that union."""
    from harbor.models.trial.config import AgentConfig

    from oddish.workers.harbor.model_hosts import ANTIGRAVITY_STARTUP_HOSTS
    from oddish.workers.harbor.restricted_network import _antigravity_profile

    config = AgentConfig(
        import_path="oddish.workers.agents.antigravity_cli:OddishAntigravityCli",
        model_name="vertex_ai/gemini-3.8-flash",
    )
    profile = _antigravity_profile(OddishAntigravityCli, config, vertex_profile)
    assert profile.outbound_hosts == (
        "aiplatform.googleapis.com",
        "oauth2.googleapis.com",
        *ANTIGRAVITY_STARTUP_HOSTS,
    )
    assert profile.server_web_disabled is True
    # An explicit opt-in on the same profile is the same path; the opt-out
    # keeps the bounded Vertex allowlist too.
    for setting in ("true", "false"):
        same = _antigravity_profile(
            OddishAntigravityCli, config, {**vertex_profile, "AGY_ADC_AUTH": setting}
        )
        assert same.outbound_hosts == profile.outbound_hosts, setting
    # A regional location moves the endpoint and keeps the token host.
    regional = _antigravity_profile(
        OddishAntigravityCli,
        config,
        {**vertex_profile, "GOOGLE_CLOUD_LOCATION": "us-east5"},
    )
    assert regional.outbound_hosts[:2] == (
        "us-east5-aiplatform.googleapis.com",
        "oauth2.googleapis.com",
    )
