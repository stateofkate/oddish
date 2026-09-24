"""Stock Claude Code: stdin prompts and stream-trajectory recovery."""

from __future__ import annotations

import json

import pytest
from harbor.agents.installed.claude_code import ClaudeCode
from harbor.environments.base import ExecResult
from oddish.workers.agents.claude_code import (
    convert_claude_code_stream_text_to_trajectory,
)


def test_convert_claude_code_stream_text_to_trajectory_recovers_atif():
    stream = "\n".join(
        json.dumps(event)
        for event in (
            {
                "type": "system",
                "subtype": "init",
                "session_id": "session-1",
                "model": "claude-test",
                "tools": [],
            },
            {
                "type": "assistant",
                "session_id": "session-1",
                "message": {
                    "id": "message-1",
                    "role": "assistant",
                    "model": "claude-test",
                    "content": [{"type": "text", "text": "Recovered."}],
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 2,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                },
            },
            {
                "type": "result",
                "session_id": "session-1",
                "is_error": False,
                "result": "Recovered.",
                "total_cost_usd": 0.01,
            },
        )
    )

    trajectory = convert_claude_code_stream_text_to_trajectory(
        stream,
        model_name="anthropic/claude-test",
    )

    assert trajectory is not None
    assert trajectory["session_id"] == "session-1"
    assert [step["message"] for step in trajectory["steps"]] == ["Recovered."]


@pytest.mark.asyncio
async def test_run_delivers_exact_prompt_over_stdin_without_plaintext_on_argv(
    tmp_path, monkeypatch
):
    """Harbor -- not Oddish -- keeps the prompt off ``claude``'s argv.

    Oddish used to force this by overriding Harbor's ``_build_claude_command``
    seam; Harbor now does it natively, so this pins the *inherited* guarantees
    rather than a command string Oddish builds:

    * the task text never reaches ``claude``'s command line. Long-horizon tasks
      restart services with ``pkill -f``, and a service name lifted from an
      argv-borne prompt can match and kill the agent itself;
    * the prompt still arrives verbatim (exact UTF-8, quotes and newlines
      intact), piped in from a transient env var that is unset before the agent
      starts;
    * the stream is teed to ``/logs/agent/claude-code.txt``, the path live
      tailing, trajectory parsing, probe analysis and cost parsing read back.
    """
    agent = ClaudeCode(logs_dir=tmp_path, reasoning_effort="xhigh")
    instruction = "restart rj-rust; preserve 'quotes' and\nnewlines"

    calls: list[tuple[str, dict[str, str]]] = []

    async def _fake_exec(self, environment, *, command, env=None, **_kwargs):
        calls.append((command, dict(env or {})))
        return ExecResult(return_code=0, stdout="", stderr="")

    monkeypatch.setattr(ClaudeCode, "exec_as_agent", _fake_exec)

    await agent.run(instruction, environment=object(), context=object())

    command, env = calls[-1]

    # Off argv: no plaintext prompt, and no `--print -- <instruction>` form.
    assert instruction not in command
    assert "--print --" not in command

    # Delivered verbatim through exactly one transient env var...
    carriers = [name for name, value in env.items() if value == instruction]
    assert len(carriers) == 1
    carrier = carriers[0]
    # ...which is unset before `claude` runs, then piped onto its stdin.
    assert f"unset {carrier}" in command
    assert 'printf "%s"' in command
    assert "| claude --verbose --output-format=stream-json" in command

    # Agent kwargs still render as CLI flags.
    assert "--effort xhigh" in command

    # The log five downstream consumers read back.
    assert "| tee /logs/agent/claude-code.txt" in command
