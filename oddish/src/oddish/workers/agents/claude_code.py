"""Recover Claude Code trajectories from captured stream output."""

from __future__ import annotations

import logging
from pathlib import Path
from tempfile import TemporaryDirectory

from harbor.agents.installed.claude_code import ClaudeCode

logger = logging.getLogger(__name__)


def convert_claude_code_stream_text_to_trajectory(
    text: str, *, model_name: str | None
) -> dict | None:
    """Recover ATIF from Claude Code's independently captured JSONL stream.

    Harbor uses this same converter when a run ends before its normal session
    tree is flushed. Keeping the compatibility boundary here avoids copying
    Harbor's event parser into Oddish's historical artifact reader.
    """
    if not text.strip():
        return None
    try:
        with TemporaryDirectory(prefix="oddish-claude-atif-") as temporary_dir:
            logs_dir = Path(temporary_dir)
            (logs_dir / "claude-code.txt").write_text(text, encoding="utf-8")
            trajectory = ClaudeCode(
                logs_dir,
                model_name=model_name,
            )._convert_stream_to_trajectory()
    except Exception as exc:
        logger.warning(
            "Could not recover Claude Code trajectory from stream JSONL: %s",
            type(exc).__name__,
        )
        return None
    if trajectory is None:
        return None
    serialized = trajectory.to_json_dict()
    return serialized if isinstance(serialized, dict) else None

