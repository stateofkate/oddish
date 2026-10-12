"""Relace route: ``relace/<id>`` on claude-code (Anthropic) and mini-swe-agent (OpenAI)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from oddish.config import (
    RELACE_MODEL_IDS,
    curated_model_error,
    relace_api_model_id,
    settings,
)
from oddish.model_pricing import (
    RELACE_PRICING,
    estimate_cost_usd,
    is_native_cost_trusted,
)
from oddish.workers.agents.mini_swe_agent import OddishRelaceMiniSweAgent
from oddish.workers.harbor import runner as harbor_runner
from oddish.workers.harbor.model_hosts import outbound_hosts_for_model
from oddish.workers.harbor.restricted_network import (
    _model_transport_base_url_keys,
    agent_has_attested_restricted_profile,
    restricted_network_profile_for_config,
)
from oddish.workers.queue import job_tokens


@pytest.fixture(autouse=True)
def _default_relace_endpoint(monkeypatch):
    monkeypatch.delenv("RELACE_BASE_URL", raising=False)


@pytest.mark.parametrize(
    ("submitted", "canonical"),
    [
        ("relace/glm-5.3", "relace/glm-5.3"),
        # Relace's own slugs, as copied from their docs, share the bucket.
        ("relace/z-ai/glm-5.3", "relace/glm-5.3"),
        ("Relace/deepseek-ai/DeepSeek-V4-Pro-0813", "relace/deepseek-v4-pro-0813"),
        ("relace/kimi-k3", "relace/kimi-k3"),
    ],
)
def test_relace_ids_get_their_own_provider_and_queue(submitted, canonical):
    assert settings.normalize_trial_model("claude-code", submitted) == canonical
    assert settings.get_provider_for_trial("claude-code", submitted) == "relace"
    assert settings.get_queue_key_for_trial("claude-code", submitted) == canonical


def test_bare_glm_keeps_its_zai_route():
    assert settings.normalize_trial_model("claude-code", "glm-5.3") == "zai/glm-5.3"


def test_wire_ids_restore_relace_casing():
    assert relace_api_model_id("relace/deepseek-v4.1-flash") == (
        "deepseek-ai/DeepSeek-V4.1-Flash"
    )
    assert relace_api_model_id("relace/glm-5.3-flash") == "z-ai/glm-5.3-flash"


def test_unknown_relace_id_is_rejected_by_the_curated_check():
    assert curated_model_error("relace/glm-5.3") is None
    error = curated_model_error("relace/glm-9")
    assert error is not None
    assert "relace/glm-5.3-flash" in error


def test_claude_code_relace_uses_the_anthropic_endpoint():
    agent_config = harbor_runner._build_agent_config(
        agent="claude-code", model="relace/glm-5.3", raw_harbor_config={}
    )

    assert agent_config.model_name == "relace/glm-5.3"
    env = agent_config.env
    # Claude Code appends /v1/messages itself.
    assert env["ANTHROPIC_BASE_URL"] == "https://models.relace.ai"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "${RELACE_API_KEY}"
    assert env["ANTHROPIC_MODEL"] == "z-ai/glm-5.3"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "z-ai/glm-5.3"
    assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "1000000"
    # Ambient Bedrock/Anthropic credentials are blanked so Relace wins.
    assert env["CLAUDE_CODE_USE_BEDROCK"] == ""
    assert env["ANTHROPIC_API_KEY"] == ""
    assert agent_config.kwargs["thinking"] == "adaptive"
    assert "reasoning_effort" not in agent_config.kwargs


def test_claude_code_relace_keeps_caller_kwargs():
    agent_config = harbor_runner._build_agent_config(
        agent="claude-code",
        model="relace/kimi-k3",
        raw_harbor_config={
            "agent_config": {
                "kwargs": {"thinking": "disabled", "reasoning_effort": "high"}
            }
        },
    )

    assert agent_config.env["ANTHROPIC_MODEL"] == "moonshotai/kimi-k3"
    assert agent_config.kwargs == {"thinking": "disabled", "reasoning_effort": "high"}


def test_mini_swe_relace_uses_the_openai_endpoint(monkeypatch):
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "azure")

    agent_config = harbor_runner._build_agent_config(
        agent="mini-swe-agent", model="relace/deepseek-v4.1-flash", raw_harbor_config={}
    )

    assert agent_config.name is None
    assert agent_config.import_path == (
        "oddish.workers.agents.mini_swe_agent:OddishRelaceMiniSweAgent"
    )
    assert agent_config.model_name == "relace/deepseek-v4.1-flash"
    assert agent_config.env["OPENAI_BASE_URL"] == "https://models.relace.ai/v1"
    assert agent_config.env["OPENAI_API_KEY"] == "${RELACE_API_KEY}"
    assert agent_config.env["MSWEA_API_KEY"] == "${RELACE_API_KEY}"
    # The Azure default must not leak onto a Relace trial.
    assert "AZURE_OPENAI_API_KEY" not in agent_config.env


def test_relace_base_url_override_reaches_both_surfaces(monkeypatch):
    monkeypatch.setenv("RELACE_BASE_URL", "https://relace.example/v1/")

    claude = harbor_runner._build_agent_config(
        agent="claude-code", model="relace/glm-5.3", raw_harbor_config={}
    )
    mini = harbor_runner._build_agent_config(
        agent="mini-swe-agent", model="relace/glm-5.3", raw_harbor_config={}
    )

    assert claude.env["ANTHROPIC_BASE_URL"] == "https://relace.example"
    assert mini.env["OPENAI_BASE_URL"] == "https://relace.example/v1"
    assert outbound_hosts_for_model("relace/glm-5.3") == ["relace.example"]


class _FakeEnvironment:
    def __init__(self, commands: list[tuple[str, dict | None]]) -> None:
        self._commands = commands

    async def exec(self, command, user=None, env=None, cwd=None, timeout_sec=None):
        self._commands.append((command, env))
        return SimpleNamespace(return_code=0, stdout="", stderr="")


@pytest.mark.asyncio
async def test_relace_mini_swe_agent_sends_the_relace_wire_id(tmp_path):
    commands: list[tuple[str, dict | None]] = []
    agent = OddishRelaceMiniSweAgent(
        logs_dir=tmp_path,
        model_name="relace/deepseek-v4-pro",
        extra_env={"MSWEA_API_KEY": "relace-test-key"},
    )

    await agent.run("fix the task", _FakeEnvironment(commands), SimpleNamespace())

    run_command = commands[-1][0]
    assert "--model=openai/deepseek-ai/DeepSeek-V4-Pro" in run_command
    assert "-c /tmp/oddish-relace-mini-swe-agent.yaml" in run_command
    assert OddishRelaceMiniSweAgent.required_outbound_domains("relace/glm-5.3") == [
        "models.relace.ai"
    ]


def test_restricted_trials_reach_relace_and_read_its_route_keys():
    assert outbound_hosts_for_model("relace/glm-5.3") == ["models.relace.ai"]
    # Same shape as Geometric: its own key plus the OpenAI-family keys
    # mini-swe-agent reads.
    keys = _model_transport_base_url_keys("relace/glm-5.3")
    assert keys[0] == "RELACE_BASE_URL"
    assert keys[1:] == _model_transport_base_url_keys("geometric/glm-5.3")[1:]
    assert "OPENAI_BASE_URL" in keys


@pytest.mark.parametrize("agent", ["claude-code", "mini-swe-agent"])
def test_restricted_compose_resolves_a_relace_only_profile(agent):
    # Without a registered profile the Relace mini-swe wrapper was rejected
    # before job creation on restricted Compose.
    agent_config = harbor_runner._build_agent_config(
        agent=agent, model="relace/glm-5.3", raw_harbor_config={}
    )

    assert agent_has_attested_restricted_profile(agent_config)
    profile = restricted_network_profile_for_config(agent_config, resolved_env={})
    assert profile.outbound_hosts == ("models.relace.ai",)


def test_sandbox_gets_only_the_relace_key_from_ambient():
    ambient = {"RELACE_API_KEY": "rl-1", "ZAI_API_KEY": "zai-never"}
    env = job_tokens.provider_env(
        agent="claude-code", model="relace/glm-5.3", settings=settings, ambient=ambient
    )
    assert env.agent_env == {"RELACE_API_KEY": "rl-1"}


def test_every_relace_model_has_a_relace_price():
    assert set(RELACE_PRICING) == set(RELACE_MODEL_IDS)


def test_relace_cost_uses_relace_rates_not_claude_codes():
    # 1M uncached input + 1M cached input + 1M output on GLM 5.3 Flash:
    # $0.09 + $0.018 + $0.30.
    cost = estimate_cost_usd(
        "relace/glm-5.3-flash",
        input_tokens=2_000_000,
        output_tokens=1_000_000,
        cached_tokens=1_000_000,
    )
    assert cost == pytest.approx(0.408)
    # Claude Code prices every model from Anthropic's table; never trust it here.
    assert not is_native_cost_trusted(agent="claude-code", provider="relace")
