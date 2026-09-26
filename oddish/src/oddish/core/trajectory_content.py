"""Keep Codex tool output blocks as ATIF text parts instead of Python reprs.

A Codex code-mode ``exec`` call returns a list of blocks such as
``[{"type": "input_text", "text": "..."}]``. Harbor's converter ``str()``s any
list output, so the stored trajectory holds the Python repr
``"[{'type': 'input_text', 'text': '...'}]"``, which no browser can parse.
``OddishCodex`` keeps new outputs typed; ``normalize_codex_trajectory`` repairs
trajectories that were stored before that fix, at read time.
"""

import ast
import re
from typing import Any

_TEXT_BLOCK_TYPES = {"text", "input_text", "output_text"}
# literal_eval cost grows with input size; larger reprs stay verbatim.
_MAX_REPAIR_CHARS = 1_000_000
_SURROGATE = re.compile(r"[\ud800-\udfff]")


def repair_trajectory_unicode(value: Any) -> Any:
    """Make decoded JSON UTF-8 serializable, preserving valid Unicode pairs."""
    if isinstance(value, str):
        if _SURROGATE.search(value):
            # JSON escapes and legacy Python reprs can contain lone UTF-16
            # surrogates. Join valid pairs; replace only unpaired units with U+FFFD.
            return value.encode("utf-16-le", "surrogatepass").decode(
                "utf-16-le", "replace"
            )
        return value
    if isinstance(value, list):
        return [repair_trajectory_unicode(item) for item in value]
    if isinstance(value, dict):
        return {
            repair_trajectory_unicode(key): repair_trajectory_unicode(item)
            for key, item in value.items()
        }
    return value


def codex_text_parts(value: object) -> list[dict[str, Any]] | None:
    """ATIF text parts for a non-empty list of Codex text blocks, else None."""
    if not isinstance(value, list) or not value:
        return None
    if not all(
        isinstance(block, dict)
        and block.get("type") in _TEXT_BLOCK_TYPES
        and isinstance(block.get("text"), str)
        for block in value
    ):
        return None
    return [{"type": "text", "text": block["text"]} for block in value]


def normalize_codex_trajectory(trajectory: dict | None) -> dict | None:
    """Replace stored Codex text-block reprs with text parts, in place."""
    agent = (trajectory or {}).get("agent")
    if not isinstance(agent, dict) or agent.get("name") != "codex":
        return trajectory
    for step in trajectory.get("steps") or []:
        for result in (step.get("observation") or {}).get("results") or []:
            raw = result.get("content")
            if not (
                isinstance(raw, str)
                and raw.startswith("[{")
                and len(raw) <= _MAX_REPAIR_CHARS
            ):
                continue
            try:
                parts = codex_text_parts(ast.literal_eval(raw))
            except (ValueError, SyntaxError, MemoryError, RecursionError):
                continue
            if parts is not None:
                result["content"] = parts
    return trajectory
