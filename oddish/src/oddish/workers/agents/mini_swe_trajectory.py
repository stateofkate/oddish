"""Keep plain reasoning that Harbor's mini-swe converter does not read."""

from __future__ import annotations

from typing import Any

from harbor.agents.installed.mini_swe_agent import convert_mini_swe_agent_to_atif

ODDISH_REASONING_VERSION = 1


def _text_parts(value: Any) -> str | None:
    if isinstance(value, str):
        return value if value.strip() else None
    if not isinstance(value, list):
        return None
    parts = []
    for block in value:
        if isinstance(block, str) and block.strip():
            parts.append(block)
        elif isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text)
    return "\n".join(parts) or None


def _reasoning_fields(message: dict[str, Any]) -> list[str]:
    candidates = []
    direct = _text_parts(message.get("reasoning_content"))
    if direct:
        candidates.append(direct)
    blocks = message.get("thinking_blocks")
    if isinstance(blocks, list):
        parts = []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") not in (
                "thinking",
                "reasoning",
                "analysis",
            ):
                continue
            text = block.get("thinking", block.get("text"))
            if isinstance(text, str) and text.strip():
                parts.append(text)
        if parts:
            candidates.append("\n".join(parts))
    return candidates


def extract_mini_swe_reasoning(message: dict[str, Any]) -> str | None:
    """Read plain saved reasoning, without decoding encrypted or redacted data."""
    candidates = _reasoning_fields(message)
    provider_fields = message.get("provider_specific_fields")
    if isinstance(provider_fields, dict):
        candidates.extend(_reasoning_fields(provider_fields))
    extra = message.get("extra")
    response = extra.get("response") if isinstance(extra, dict) else None
    choices = response.get("choices") if isinstance(response, dict) else None
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        response_message = choices[0].get("message")
        if isinstance(response_message, dict):
            candidates.extend(_reasoning_fields(response_message))
            provider_fields = response_message.get("provider_specific_fields")
            if isinstance(provider_fields, dict):
                candidates.extend(_reasoning_fields(provider_fields))

    if message.get("object") == "response":
        output = message.get("output")
        if isinstance(output, list):
            parts = []
            for item in output:
                if not isinstance(item, dict) or item.get("type") != "reasoning":
                    continue
                # Some Responses providers save full plain content. Otherwise,
                # retain their plain summary; encrypted_content is never read.
                text = _text_parts(item.get("content")) or _text_parts(
                    item.get("summary")
                )
                if text:
                    parts.append(text)
            if parts:
                candidates.append("\n".join(parts))
    return max(candidates, key=len) if candidates else None


def restore_mini_swe_reasoning(trajectory: dict[str, Any], raw: Any) -> dict[str, Any]:
    """Add saved reasoning only when the raw assistant turns match all steps.

    Harbor owns conversion of messages, tools, observations, and metrics. Its
    converter also provides the expected turn shape for this safety check. A
    count, message, or tool mismatch leaves the saved trajectory unchanged.
    """
    if not isinstance(trajectory, dict) or not isinstance(raw, dict):
        return trajectory
    steps = trajectory.get("steps")
    agent = trajectory.get("agent")
    messages = raw.get("messages")
    if not isinstance(steps, list) or not isinstance(agent, dict):
        return trajectory
    if not isinstance(messages, list) or not messages:
        return trajectory
    if any(not isinstance(message, dict) for message in messages):
        return trajectory
    assistant_messages = [
        message
        for message in messages
        if message.get("role") not in ("system", "user", "tool")
        and message.get("type") != "function_call_output"
        and (message.get("role") == "assistant" or message.get("object") == "response")
    ]
    agent_steps = [
        step
        for step in steps
        if isinstance(step, dict) and step.get("source") == "agent"
    ]
    if not assistant_messages or len(assistant_messages) != len(agent_steps):
        return trajectory
    try:
        converted = convert_mini_swe_agent_to_atif(raw, session_id="reasoning-check")
        expected_steps = [
            step
            for step in converted.to_json_dict()["steps"]
            if step.get("source") == "agent"
        ]
    except Exception:
        return trajectory
    if len(expected_steps) != len(agent_steps):
        return trajectory
    for saved, expected in zip(agent_steps, expected_steps, strict=True):
        if saved.get("message") != expected.get("message") or (
            saved.get("tool_calls") or []
        ) != (expected.get("tool_calls") or []):
            return trajectory

    replacements = {}
    for step, message in zip(agent_steps, assistant_messages, strict=True):
        reasoning = extract_mini_swe_reasoning(message)
        existing = step.get("reasoning_content")
        if (
            isinstance(existing, str)
            and existing.strip()
            and existing != step.get("message")
            and reasoning is not None
            and len(existing) > len(reasoning)
        ):
            continue
        if reasoning:
            replacements[id(step)] = {**step, "reasoning_content": reasoning}
    extra = agent.get("extra")
    if extra is not None and not isinstance(extra, dict):
        return trajectory
    return {
        **trajectory,
        "steps": [replacements.get(id(step), step) for step in steps],
        "agent": {
            **agent,
            "extra": {
                **(extra or {}),
                "oddish_reasoning_version": ODDISH_REASONING_VERSION,
            },
        },
    }
