"""Tencent Hunyuan (Hy4) route for mini-swe-agent, mirroring the Geometric tests."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import (  # noqa: E402
    HUNYUAN_DEFAULT_BASE_URL,
    normalize_model_id,
    settings,
)
from oddish.core.sweeps import build_trial_specs_from_sweep  # noqa: E402
from oddish.schemas import TaskSweepSubmission  # noqa: E402
from oddish.workers.agents.mini_swe_agent import OddishHunyuanMiniSweAgent  # noqa: E402
from oddish.workers.harbor import model_hosts  # noqa: E402
from oddish.workers.harbor import runner as harbor_runner  # noqa: E402
from oddish.workers.harbor.model_hosts import outbound_hosts_for_model  # noqa: E402
from oddish.workers.queue import job_tokens  # noqa: E402

MODEL = "hunyuan/hy4-preview-offline"


class _FakeEnvironment:
    def __init__(self, commands: list[tuple[str, dict | None]]) -> None:
        self._commands = commands

    async def exec(self, command, user=None, env=None, cwd=None, timeout_sec=None):
        self._commands.append((command, env))
        return SimpleNamespace(return_code=0, stdout="", stderr="")


@pytest.mark.asyncio
async def test_hunyuan_mini_swe_agent_uses_openai_model_and_own_config(tmp_path):
    commands: list[tuple[str, dict | None]] = []
    agent = OddishHunyuanMiniSweAgent(
        logs_dir=tmp_path,
        model_name=MODEL,
        extra_env={
            "MSWEA_API_KEY": "hunyuan-test-key",
            "OPENAI_BASE_URL": HUNYUAN_DEFAULT_BASE_URL,
        },
    )

    await agent.run("fix the task", _FakeEnvironment(commands), SimpleNamespace())

    config_command = commands[-2][0]
    run_command, run_env = commands[-1]
    assert "--model=openai/hy4-preview-offline" in run_command
    assert "-c mini -c /tmp/oddish-hunyuan-mini-swe-agent.yaml" in run_command
    assert "--task=" not in run_command
    assert "x-session-id" not in config_command
    assert run_env["OPENAI_BASE_URL"] == HUNYUAN_DEFAULT_BASE_URL


def test_hunyuan_agent_refuses_a_model_the_endpoint_does_not_serve():
    with pytest.raises(ValueError):
        OddishHunyuanMiniSweAgent._oddish_bare_model_id("hunyuan/gpt-4o")
    assert (
        OddishHunyuanMiniSweAgent._oddish_bare_model_id(MODEL) == "hy4-preview-offline"
    )


def test_hunyuan_required_outbound_domains_includes_configured_default():
    domains = OddishHunyuanMiniSweAgent.required_outbound_domains(model_name=MODEL)
    assert "eval.hunyuan.tencent.com" in domains


def test_mini_swe_hunyuan_agent_config_sets_hunyuan_route(monkeypatch) -> None:
    monkeypatch.setattr(harbor_runner.settings, "openai_provider", "azure")

    agent_config = harbor_runner._build_agent_config(
        agent="mini-swe-agent", model=MODEL, raw_harbor_config={}
    )

    assert agent_config.name is None
    assert (
        agent_config.import_path
        == "oddish.workers.agents.mini_swe_agent:OddishHunyuanMiniSweAgent"
    )
    assert agent_config.model_name == MODEL
    assert agent_config.env["MSWEA_API_KEY"] == "${HUNYUAN_API_KEY}"
    assert agent_config.env["OPENAI_API_KEY"] == "${HUNYUAN_API_KEY}"
    assert agent_config.env["OPENAI_BASE_URL"] == HUNYUAN_DEFAULT_BASE_URL
    # The Azure default must not leak onto a Hunyuan trial.
    assert "AZURE_OPENAI_API_KEY" not in agent_config.env


def test_hunyuan_model_gets_its_own_provider_and_queue_key():
    assert normalize_model_id(" Hunyuan / HY4-Preview-Offline ") == MODEL
    assert settings.get_provider_for_trial("mini-swe-agent", MODEL) == "hunyuan"
    assert settings.get_queue_key_for_trial("mini-swe-agent", MODEL) == MODEL


def test_outbound_hosts_for_hunyuan_model(monkeypatch):
    monkeypatch.setattr(
        model_hosts.settings, "hunyuan_base_url", HUNYUAN_DEFAULT_BASE_URL
    )
    assert outbound_hosts_for_model(MODEL) == ["eval.hunyuan.tencent.com"]


def _submission(model: str, agent: str = "mini-swe-agent") -> TaskSweepSubmission:
    return TaskSweepSubmission.model_validate(
        {
            "task_id": "task-under-test",
            "configs": [{"agent": agent, "model": model, "n_trials": 1}],
        }
    )


def test_served_hunyuan_model_is_admitted():
    specs = build_trial_specs_from_sweep(_submission(MODEL))
    assert [spec.model for spec in specs] == [MODEL]


@pytest.mark.parametrize(
    ("model", "agent"),
    [("hunyuan/hy4-preview", "mini-swe-agent"), (MODEL, "claude-code")],
)
def test_unsupported_hunyuan_submission_is_rejected(model, agent):
    with pytest.raises(HTTPException) as exc:
        build_trial_specs_from_sweep(_submission(model, agent))
    assert exc.value.status_code == 400


def test_provider_env_hunyuan_only_carries_hunyuan_key() -> None:
    fake = types.SimpleNamespace(
        anthropic_api_key="sk-ant",
        openai_api_key="sk-oai",
        hunyuan_api_key="hy-key",
        hunyuan_base_url="https://hunyuan.test/v1",
    )
    fake.get_provider_for_trial = lambda agent, model: "hunyuan"
    env = job_tokens.provider_env(
        agent="mini-swe-agent", model=MODEL, settings=fake
    ).agent_env
    assert env == {
        "HUNYUAN_API_KEY": "hy-key",
        "MSWEA_API_KEY": "hy-key",
        "OPENAI_API_KEY": "hy-key",
    }
