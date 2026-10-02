from __future__ import annotations

import json
from copy import deepcopy

import pytest
from harbor.agents.installed.mini_swe_agent import convert_mini_swe_agent_to_atif
from harbor.models.agent.context import AgentContext

from oddish.workers.agents.mini_swe_agent import OddishMiniSweAgent
from oddish.workers.agents.mini_swe_trajectory import (
    extract_mini_swe_reasoning,
    restore_mini_swe_reasoning,
)


def raw_trajectory(*assistant_messages):
    return {
        "info": {"config": {"model": {"model_name": "anthropic/claude-opus-5"}}},
        "messages": [
            {"role": "system", "content": "Solve the task."},
            {"role": "user", "content": "Inspect the config."},
            *assistant_messages,
        ],
    }


def assistant(content, **fields):
    return {"role": "assistant", "content": content, **fields}


def atif(raw):
    return convert_mini_swe_agent_to_atif(raw, session_id="test-session").to_json_dict()


def agent_steps(trajectory):
    return [step for step in trajectory["steps"] if step["source"] == "agent"]


def test_restore_full_reasoning_keeps_all_other_saved_fields():
    raw = raw_trajectory(
        assistant(
            "I will inspect the config.",
            reasoning_content="The config sets the route. I need to check each rule before changing it.",
            tool_calls=[
                {
                    "id": "call-1",
                    "function": {
                        "name": "bash",
                        "arguments": '{"command":"cat config.json"}',
                    },
                },
                {
                    "id": "call-2",
                    "function": {
                        "name": "bash",
                        "arguments": '{"command":"cat rules.json"}',
                    },
                },
            ],
        ),
        {"role": "tool", "content": "Saved config.", "tool_call_id": "call-1"},
        assistant(
            "I found the route.",
            thinking_blocks=[
                {"type": "thinking", "thinking": "The first rule accepts the route."},
                {"type": "thinking", "thinking": "The second rule needs a new value."},
            ],
        ),
    )
    saved = atif(raw)
    before = deepcopy(saved)
    restored = restore_mini_swe_reasoning(saved, raw)
    steps = agent_steps(restored)
    assert steps[0]["reasoning_content"] == raw["messages"][2]["reasoning_content"]
    assert steps[1]["reasoning_content"] == (
        "The first rule accepts the route.\nThe second rule needs a new value."
    )
    assert restored["agent"]["extra"]["oddish_reasoning_version"] == 1
    assert saved == before
    unchanged = deepcopy(restored)
    for saved_step, restored_step in zip(
        saved["steps"], unchanged["steps"], strict=True
    ):
        if "reasoning_content" in restored_step:
            restored_step["reasoning_content"] = saved_step["reasoning_content"]
    unchanged["agent"]["extra"].pop("oddish_reasoning_version")
    assert unchanged == saved


def test_reasoning_uses_longest_plain_saved_form():
    message = assistant(
        "Brief text.",
        reasoning_content="Short reasoning.",
        thinking_blocks=[
            {
                "type": "thinking",
                "thinking": "Full reasoning with the checks and the next action.",
            },
            {"type": "redacted_thinking", "data": "do-not-decode"},
        ],
    )
    assert extract_mini_swe_reasoning(message) == (
        "Full reasoning with the checks and the next action."
    )


def test_reasoning_falls_back_to_saved_primary_response_message():
    message = assistant(
        "Brief text.",
        extra={
            "response": {
                "choices": [
                    {
                        "message": {
                            "reasoning_content": "Full reasoning from the saved response."
                        }
                    },
                    {
                        "message": {
                            "reasoning_content": "Another choice must not be used."
                        }
                    },
                ]
            }
        },
    )
    raw = raw_trajectory(message)
    restored = restore_mini_swe_reasoning(atif(raw), raw)
    assert agent_steps(restored)[0]["reasoning_content"] == (
        "Full reasoning from the saved response."
    )


def test_reasoning_reads_provider_specific_plain_thinking():
    message = assistant(
        "Brief text.",
        provider_specific_fields={
            "thinking_blocks": [
                {"type": "thinking", "thinking": "Full provider reasoning."},
                {"type": "redacted_thinking", "data": "do-not-decode"},
            ]
        },
    )
    assert extract_mini_swe_reasoning(message) == "Full provider reasoning."


def test_existing_full_reasoning_survives_shorter_plain_raw_text():
    raw = raw_trajectory(assistant("Brief text.", reasoning_content="Short reasoning."))
    saved = atif(raw)
    agent_steps(saved)[0][
        "reasoning_content"
    ] = "Complete saved reasoning with all the details."
    restored = restore_mini_swe_reasoning(saved, raw)
    assert agent_steps(restored)[0]["reasoning_content"] == (
        "Complete saved reasoning with all the details."
    )


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"reasoning_content": "", "thinking_blocks": []},
        {"reasoning_content": {"encrypted_content": "do-not-decode"}},
        {"thinking_blocks": [{"type": "redacted_thinking", "data": "do-not-decode"}]},
        {"reasoning_content": 123, "thinking_blocks": "bad", "extra": []},
        {"extra": {"response": {"choices": [{"message": "bad"}]}}},
    ],
)
def test_absent_or_malformed_reasoning_keeps_existing_reasoning(fields):
    raw = raw_trajectory(assistant("Brief text.", **fields))
    saved = atif(raw)
    agent_steps(saved)[0]["reasoning_content"] = "Saved complete reasoning."
    restored = restore_mini_swe_reasoning(saved, raw)
    assert agent_steps(restored)[0]["reasoning_content"] == "Saved complete reasoning."
    assert restored["agent"]["extra"]["oddish_reasoning_version"] == 1


@pytest.mark.parametrize("mismatch", ["count", "message", "tools"])
def test_alignment_mismatch_does_not_add_reasoning_or_marker(mismatch):
    raw = raw_trajectory(assistant("Brief text.", reasoning_content="Full reasoning."))
    saved = atif(raw)
    if mismatch == "count":
        raw["messages"].append(assistant("Second turn."))
    elif mismatch == "message":
        raw["messages"][2]["content"] = "Another trial."
    else:
        raw["messages"][2]["tool_calls"] = [
            {"id": "other-call", "function": {"name": "bash", "arguments": "{}"}}
        ]
    assert restore_mini_swe_reasoning(saved, raw) is saved
    assert "oddish_reasoning_version" not in saved["agent"].get("extra", {})


@pytest.mark.parametrize("messages", [None, [], ["bad"]])
def test_bad_raw_messages_are_ignored(messages):
    saved = atif(raw_trajectory(assistant("Brief text.")))
    assert restore_mini_swe_reasoning(saved, {"messages": messages}) is saved


@pytest.mark.parametrize("raw", [None, [], "bad", 123, {"messages": [{"role": []}]}])
def test_bad_raw_document_is_ignored(raw):
    saved = atif(raw_trajectory(assistant("Brief text.")))
    assert restore_mini_swe_reasoning(saved, raw) is saved


def test_responses_prefers_plain_content_over_summary():
    raw = raw_trajectory(
        {
            "object": "response",
            "output": [
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "Brief reasoning."}],
                    "content": [
                        {
                            "type": "reasoning_text",
                            "text": "Full plain reasoning from this response.",
                        }
                    ],
                    "encrypted_content": "do-not-decode",
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Done."}],
                },
            ],
        }
    )
    saved = atif(raw)
    agent_steps(saved)[0]["reasoning_content"] = "Brief reasoning."
    restored = restore_mini_swe_reasoning(saved, raw)
    assert agent_steps(restored)[0]["reasoning_content"] == (
        "Full plain reasoning from this response."
    )


def test_wrapper_saves_full_reasoning_after_harbor_conversion(tmp_path):
    raw = raw_trajectory(
        assistant("Brief text.", reasoning_content="Full saved reasoning.")
    )
    (tmp_path / "mini-swe-agent.trajectory.json").write_text(json.dumps(raw))
    agent = OddishMiniSweAgent(logs_dir=tmp_path, model_name="anthropic/claude-opus-5")
    agent.populate_context_post_run(AgentContext())
    saved = json.loads((tmp_path / "trajectory.json").read_text())
    assert agent_steps(saved)[0]["reasoning_content"] == "Full saved reasoning."
    assert saved["agent"]["extra"]["oddish_reasoning_version"] == 1
