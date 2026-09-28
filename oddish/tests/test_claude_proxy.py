"""Exercise the actual LiteLLM converter with a deterministic upstream."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import litellm
import pytest
from aiohttp.test_utils import TestClient, TestServer
from harbor.agents.installed.claude_code import ClaudeCode
from harbor.models.agent.context import AgentContext
from litellm.types.llms.openai import ResponsesAPIResponse

from oddish.config import settings
from oddish.workers.agents.claude_proxy import create_app
from oddish.workers.agents.claude_code_proxy import ProxiedClaudeCode
from oddish.workers.harbor.agent_config import _build_agent_config


@asynccontextmanager
async def proxy(model="openai/gpt-6-sol", effort=None):
    async with TestClient(
        TestServer(create_app(model, "local-token", effort))
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_converter_preserves_tool_round_trip_and_pins_provider(monkeypatch):
    upstream = AsyncMock(
        return_value=ResponsesAPIResponse(
            id="call-1",
            created_at=1,
            model="gpt-6-sol",
            status="completed",
            output=[
                {
                    "id": "fc-1",
                    "type": "function_call",
                    "call_id": "tool-1",
                    "name": "Bash",
                    "arguments": '{"command":"pwd"}',
                    "status": "completed",
                }
            ],
            usage={"input_tokens": 15, "output_tokens": 5, "total_tokens": 20},
        )
    )
    monkeypatch.setattr(litellm, "aresponses", upstream)
    monkeypatch.setattr(
        litellm,
        "acompletion",
        AsyncMock(side_effect=AssertionError("Unexpected provider path")),
    )
    async with proxy(effort="high") as client:
        payload = {
            "model": "untrusted-model",
            "api_base": "https://untrusted.invalid",
            "api_key": "untrusted-key",
            "max_tokens": 100,
            "system": "Audit the trial.",
            "messages": [{"role": "user", "content": "Inspect"}],
            "tools": [
                {
                    "name": "Bash",
                    "description": "Run command",
                    "input_schema": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                    },
                }
            ],
        }
        response = await client.post(
            "/v1/messages", json=payload, headers={"x-api-key": "local-token"}
        )
        assert response.status == 200, await response.text()
        result = await response.json()
        assert result["stop_reason"] == "tool_use"
        tool = next(block for block in result["content"] if block["type"] == "tool_use")
        assert tool["id"] == "tool-1" and tool["input"] == {"command": "pwd"}
        sent = upstream.call_args.kwargs
        assert sent["model"] == "gpt-6-sol"
        assert sent["custom_llm_provider"] == "openai"
        assert sent.get("api_base") != payload["api_base"]
        assert sent.get("api_key") != payload["api_key"]
        assert sent["reasoning"]["effort"] == "high"
        assert sent["tools"][0]["name"] == "Bash"
        payload["messages"] += [
            {"role": "assistant", "content": result["content"]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tool-1", "content": "/app"}
                ],
            },
        ]
        response = await client.post(
            "/v1/messages", json=payload, headers={"x-api-key": "local-token"}
        )
        assert response.status == 200
        sent = upstream.call_args.kwargs
        assert sent["input"][-1]["type"] == "function_call_output"
        assert sent["input"][-1]["call_id"] == "tool-1"
        assert sent["input"][-1]["output"] == "/app"


@pytest.mark.asyncio
async def test_proxy_auth_error_and_stream_cleanup(monkeypatch):
    upstream = AsyncMock(
        side_effect=litellm.AuthenticationError(
            "invalid key secret-value", model="gpt-6-sol", llm_provider="openai"
        )
    )
    monkeypatch.setenv("OPENAI_API_KEY", "secret-value")
    monkeypatch.setattr(litellm.anthropic.messages, "acreate", upstream)
    async with proxy() as client:
        payload = {"model": "gpt-6-sol", "max_tokens": 100, "messages": []}
        response = await client.post("/v1/messages", json=payload)
        assert response.status == 401
        upstream.assert_not_called()
        response = await client.post(
            "/v1/messages", json=payload, headers={"x-api-key": "local-token"}
        )
        assert response.status == 401
        assert "invalid key [REDACTED]" in await response.text()
        closed = []

        async def events():
            try:
                yield {
                    "type": "message_start",
                    "message": {"usage": {"input_tokens": 5}},
                }
                yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
            finally:
                closed.append(True)

        upstream.side_effect = None
        upstream.return_value = events()
        response = await client.post(
            "/v1/messages",
            json={**payload, "stream": True},
            headers={"x-api-key": "local-token"},
        )
        text = await response.text()
        assert "event: message_start" in text and "event: message_stop" in text
        assert closed == [True]


@pytest.mark.parametrize(
    "model",
    [
        "openai/gpt-6-sol",
        "openai/gpt-6-luna",
        "gemini/gemini-3.1-pro-preview",
        "deepseek/deepseek-chat",
    ],
)
def test_normal_and_qa_use_identical_claude_adapter(monkeypatch, model):
    monkeypatch.setattr(settings, "openai_provider", "openai")
    ordinary = _build_agent_config(
        agent="claude-code", model=model, raw_harbor_config={}
    )
    qa = _build_agent_config(
        agent="claude-code",
        model=model,
        raw_harbor_config={
            "extra_instructions": "audit",
            "analysis_payload": {"trial_ids": ["source"]},
        },
    )
    assert ordinary == qa
    assert (
        ordinary.import_path
        == "oddish.workers.agents.claude_code_proxy:ProxiedClaudeCode"
    )
    assert ordinary.kwargs["proxy_model"] == model


@pytest.mark.asyncio
async def test_adapter_cleans_process_and_scoped_env_after_agent_failure(
    monkeypatch, tmp_path
):
    from harbor.environments.docker.docker import DockerEnvironment
    from harbor.models.task.config import EnvironmentConfig
    from harbor.models.trial.paths import TrialPaths

    monkeypatch.setattr(settings, "openai_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "provider-key")
    environment = DockerEnvironment(
        environment_dir=tmp_path,
        environment_name="proxy-test",
        session_id="test",
        trial_paths=TrialPaths(tmp_path / "trial"),
        task_env_config=EnvironmentConfig(docker_image="python:3.13-slim"),
    )
    calls = []

    async def execute(*, command, env=None, **kwargs):
        calls.append((command, environment._merge_env(env)))
        return SimpleNamespace(return_code=0, stdout="", stderr="")

    environment.exec = execute

    async def run(self, instruction, env, context):
        resolved = env._merge_env({"CLAUDE_CODE_USE_BEDROCK": "1"})
        assert resolved["CLAUDE_CODE_USE_BEDROCK"] == ""
        assert resolved["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8787"
        assert resolved["ANTHROPIC_API_KEY"] not in {"old-key", "provider-key"}
        raise RuntimeError("upstream failed")

    monkeypatch.setattr(ClaudeCode, "run", run)
    agent = ProxiedClaudeCode(
        tmp_path,
        model_name="gpt-6-sol",
        proxy_model="openai/gpt-6-sol",
        extra_env={"ANTHROPIC_API_KEY": "old-key"},
    )
    with pytest.raises(RuntimeError, match="upstream failed"):
        await agent.run("audit", environment, AgentContext())
    assert "nohup" in calls[0][0] and calls[0][1]["OPENAI_API_KEY"] == "provider-key"
    assert "kill" in calls[-1][0]
    assert agent.extra_env == {"ANTHROPIC_API_KEY": "old-key"}
    assert not environment._merge_env({})


@pytest.mark.asyncio
async def test_token_estimate_includes_system_messages_and_tools(monkeypatch):
    calls = []

    def count(*, model, text):
        calls.append((model, text))
        return 12

    monkeypatch.setattr(litellm, "token_counter", count)
    async with proxy() as client:
        response = await client.post(
            "/v1/messages/count_tokens",
            headers={"x-api-key": "local-token"},
            json={
                "model": "alias",
                "system": "audit instructions",
                "messages": [{"role": "user", "content": "evidence"}],
                "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
            },
        )
        assert response.status == 200
        assert await response.json() == {"input_tokens": 12}
    model, text = calls[0]
    assert model == "openai/gpt-6-sol"
    assert all(value in text for value in ("audit instructions", "evidence", "Bash"))


def test_claude_adapter_uses_litellm_gemini_alias():
    config = _build_agent_config(
        agent="claude-code", model="google/gemini-3.1-pro-preview", raw_harbor_config={}
    )
    assert config.kwargs["proxy_model"] == "gemini/gemini-3.1-pro-preview"
