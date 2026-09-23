from __future__ import annotations

import re
from pathlib import Path

from harbor.models.task.config import TaskConfig

from oddish.preflight.models import Finding, Severity

CHECK_ID = "solution_in_instruction"

_WHITESPACE_RE = re.compile(r"\s+")

# Shorter solutions (a bare `0`, `true`) match ordinary prose by chance. Eight
# still catches one-line expression answers such as `(x - 1) / m + 1`.
_MIN_SOLUTION_CHARS = 8


def _squash(text: str) -> str:
    return _WHITESPACE_RE.sub("", text)


def check(task_dir: Path, config: TaskConfig) -> list[Finding]:
    instruction_path = task_dir / "instruction.md"
    solution_dir = task_dir / "solution"
    if not instruction_path.is_file() or not solution_dir.is_dir():
        return []

    # Whitespace is dropped on both sides so re-indenting or re-wrapping the
    # answer inside a Markdown code block still counts as the same text.
    instruction = _squash(instruction_path.read_text(encoding="utf-8", errors="ignore"))
    findings: list[Finding] = []

    for path in sorted(solution_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            solution = _squash(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if len(solution) < _MIN_SOLUTION_CHARS or solution not in instruction:
            continue
        rel = path.relative_to(task_dir)
        findings.append(
            Finding(
                check_id=CHECK_ID,
                severity=Severity.ERROR,
                task_dir=task_dir,
                path=path,
                message=(
                    f"{rel} appears verbatim in instruction.md, so an agent can "
                    "pass by copying the answer from the task text."
                ),
                fix_hint=(
                    "State the requirement as a property the answer must meet, "
                    "not as the reference implementation. Saying that the shown "
                    "text is the answer does not fix the leak."
                ),
            )
        )

    return findings
