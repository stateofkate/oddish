"""Shared automatic harness admission and trusted setup network lifetime."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from harbor.models.task.config import NetworkMode, NetworkPolicy
from harbor.models.trial.config import AgentConfig

from oddish.config import settings
from oddish.core.sweeps import resolve_trial_model, build_trial_specs_from_sweep
from oddish.schemas import TrialSpec, TaskSweepSubmission
from oddish.workers.harbor.agent_config import _build_routed_agent_config


@pytest.mark.parametrize(
    "model,agent",
    [
        ("openai/gpt-6-luna", "codex"),
        ("anthropic/claude-sonnet-5-5", "claude-code"),
        ("vertex_ai/claude-sonnet-5-5", "claude-code"),
        ("vertex_ai/gemini-3-pro-preview", "gemini-cli"),
        ("gemini/gemini-3-flash-preview", "gemini-cli"),
        ("hunyuan/hy4-preview-offline", "mini-swe-agent"),
        ("fireworks/glm-5", "mini-swe-agent"),
        ("zai/glm-5", "claude-code"),
        ("minimax/MiniMax-M3", "claude-code"),
        ("moonshot/kimi-k2.7-code", "claude-code"),
        ("openrouter/openai/gpt-5", "mini-swe-agent"),
    ],
)
def test_auto_records_concrete_execution_and_uses_shared_routing(model, agent):
    spec = TrialSpec(agent="auto", model=model)
    canonical = resolve_trial_model(spec)
    assert spec.agent == agent
    explicit = TrialSpec(agent=agent, model=model)
    assert canonical == resolve_trial_model(explicit)
    assert spec.agent_overrides() == explicit.agent_overrides()
    if model.startswith("vertex_ai/"):
        return  # Existing Vertex routing tests provide a configured worker.
    routed = _build_routed_agent_config(
        agent=spec.agent,
        model=canonical,
        raw_harbor_config={"agent_config": spec.agent_overrides()},
    )
    assert routed.kwargs["version"] == settings.harness_versions[agent]


def test_sweep_stores_resolved_agent_and_honors_explicit_package_version():
    submission = TaskSweepSubmission(
        task_id="task",
        configs=[
            {
                "agent": "auto",
                "model": "openai/gpt-6-luna",
                "agent_config": {"kwargs": {"version": "0.150.0"}},
            }
        ],
    )
    spec = build_trial_specs_from_sweep(submission)[0]
    assert spec.agent == "codex"
    assert spec.agent_config.kwargs["version"] == "0.150.0"


def test_auto_requires_model_and_leaves_custom_harness_implementation_intact():
    with pytest.raises(HTTPException, match="Automatic harness"):
        resolve_trial_model(TrialSpec(agent="auto"))
    spec = TrialSpec(
        agent="codex",
        model="openai/gpt-6-luna",
        agent_config=AgentConfig(import_path="custom:Agent"),
    )
    resolve_trial_model(spec)
    assert "version" not in spec.agent_config.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("install_fails", [False, True])
async def test_trusted_setup_restores_task_network_policy_even_when_install_fails(
    monkeypatch, install_fails
):
    from harbor.agents.installed.base import BaseInstalledAgent
    from harbor.environments.factory import EnvironmentFactory
    from harbor.trial.trial import Trial
    from oddish.workers.harbor.patches import _patch_installed_agent_setup

    baseline = NetworkPolicy(network_mode=NetworkMode.NO_NETWORK)
    events = []
    env = SimpleNamespace(
        network_policy=baseline,
        capabilities=SimpleNamespace(dynamic_network_policy=True),
    )

    async def switch(policy):
        env.network_policy = policy
        events.append(policy.network_mode)

    async def installed_setup(self, environment):
        assert environment.network_policy.network_mode == NetworkMode.PUBLIC
        events.append("install")
        if install_fails:
            raise RuntimeError("installation failed")

    monkeypatch.setattr(env, "set_network_policy", switch, raising=False)
    monkeypatch.setattr(BaseInstalledAgent, "setup", installed_setup)
    monkeypatch.setattr(Trial, "_init_agent_environment", Trial._init_agent_environment)
    monkeypatch.setattr(
        EnvironmentFactory,
        "create_environment_from_config",
        EnvironmentFactory.__dict__["create_environment_from_config"],
    )
    _patch_installed_agent_setup()
    if install_fails:
        with pytest.raises(RuntimeError, match="installation failed"):
            await BaseInstalledAgent.setup(object(), env)
    else:
        await BaseInstalledAgent.setup(object(), env)
    assert env.network_policy == baseline
    assert events == [NetworkMode.PUBLIC, "install", NetworkMode.NO_NETWORK]


def test_network_transport_prepares_install_phase_only_for_installed_agent(
    monkeypatch, tmp_path
):
    from harbor.agents.installed.base import BaseInstalledAgent
    from harbor.environments.factory import EnvironmentFactory
    from harbor.environments.modal import ModalEnvironment
    from harbor.trial.trial import Trial
    from oddish.workers.agents.codex import OddishCodex
    from oddish.workers.harbor.patches import _patch_installed_agent_setup

    seen = []
    baseline = NetworkPolicy(network_mode=NetworkMode.NO_NETWORK)

    def create(cls, **kwargs):
        seen.append(kwargs)
        return object()

    def initialize(self):
        return EnvironmentFactory.create_environment_from_config(
            network_policy=baseline, phase_network_policies=[baseline]
        )

    async def setup(self, environment):
        pass

    monkeypatch.setattr(
        EnvironmentFactory, "create_environment_from_config", classmethod(create)
    )
    monkeypatch.setattr(Trial, "_init_agent_environment", initialize)
    monkeypatch.setattr(BaseInstalledAgent, "setup", setup)
    _patch_installed_agent_setup()
    Trial._init_agent_environment(
        SimpleNamespace(
            agent=OddishCodex(logs_dir=tmp_path, model_name="openai/gpt-6-luna")
        )
    )
    Trial._init_agent_environment(SimpleNamespace(agent=object()))
    assert seen[0]["network_policy"] == baseline
    assert seen[0]["phase_network_policies"] == [
        baseline,
        NetworkPolicy(network_mode=NetworkMode.PUBLIC),
    ]
    assert ModalEnvironment._requires_dynamic_network(
        startup_network_policy=baseline,
        phase_network_policies=seen[0]["phase_network_policies"],
    )
    assert seen[1]["phase_network_policies"] == [baseline]
