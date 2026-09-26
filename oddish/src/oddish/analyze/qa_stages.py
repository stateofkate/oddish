"""The two QA stages that prompt versions, replays and goldens are keyed on.

Pre-trial QA audits a task version before any solver runs; post-trial QA
classifies one finished solver trial. Everything that differs by stage lives
in its ``StageSpec``, so callers ask ``STAGES[stage]`` instead of branching
on the stage name.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from functools import cache
from importlib import resources
from typing import Any, Literal

from oddish.analyze.models import Classification


class QAStage(str, Enum):
    PRE_TRIAL = "pre_trial"
    POST_TRIAL = "post_trial"


@dataclass(frozen=True, slots=True)
class Grade:
    """How a replay is scored against its golden."""

    # QA's answer from the replay trial's stored ``analysis`` (None when the
    # replay stored none); None means QA's output was invalid.
    answer: Callable[[dict[str, Any] | None], str | None]
    is_correct: Callable[[dict[str, Any], str], bool]


@dataclass(frozen=True, slots=True)
class StageSpec:
    # The prompt this deployment ships, relative to ``oddish.analyze``.
    prompt_file: str
    # What a golden of this stage labels.
    subject: Literal["trial", "task_version"]
    # Checks and normalizes a golden's ``expected``; raises ValueError.
    parse_expected: Callable[[dict[str, Any]], dict[str, Any]]
    # The bucket a golden is counted under in the goldens summary.
    label: Callable[[dict[str, Any]], str]
    # None until the stage's prompts can be replayed and scored.
    grade: Grade | None

    @property
    def replayable(self) -> bool:
        return self.grade is not None


_CLASSIFICATIONS = frozenset(c.value for c in Classification)


def _classification(raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("classification") not in _CLASSIFICATIONS:
        raise ValueError(
            f"expected.classification must be one of {sorted(_CLASSIFICATIONS)}"
        )
    return {"classification": raw["classification"]}


def _required_findings(raw: dict[str, Any]) -> dict[str, Any]:
    # Answer-key findings are objects (id plus evidence), kept whole.
    findings = raw.get("required_findings")
    if (
        not isinstance(findings, list)
        or not findings
        or not all(
            isinstance(f, dict)
            and isinstance(f.get("finding_id"), str)
            and f["finding_id"].strip()
            for f in findings
        )
    ):
        raise ValueError(
            "expected.required_findings must be a non-empty list of objects "
            "with a finding_id"
        )
    return raw


def _answered_classification(analysis: dict[str, Any] | None) -> str | None:
    answer = (analysis or {}).get("classification")
    return answer if answer in _CLASSIFICATIONS else None


STAGES: dict[QAStage, StageSpec] = {
    QAStage.PRE_TRIAL: StageSpec(
        prompt_file="prompts/pre_trial_qa.txt",
        subject="task_version",
        parse_expected=_required_findings,
        label=lambda expected: "findings",
        grade=None,
    ),
    QAStage.POST_TRIAL: StageSpec(
        prompt_file="classify_prompt.txt",
        subject="trial",
        parse_expected=_classification,
        label=lambda expected: expected["classification"],
        grade=Grade(
            answer=_answered_classification,
            is_correct=lambda expected, answer: expected["classification"] == answer,
        ),
    ),
}


def prompt_sha256(text: str) -> str:
    """A prompt version's identity. ``/qa-evals`` strips the text it is sent,
    so the hash is of the stripped text and a replay of the shipped prompt
    hashes the same as the file. (``audit_policy_hash`` is older and hashes
    the unstripped audit file.)"""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


@cache
def shipped_prompt(stage: QAStage) -> str:
    """The stage's shipped prompt, stripped: the text a replay of it sends."""
    path = resources.files("oddish.analyze").joinpath(STAGES[stage].prompt_file)
    return path.read_text().strip()


@cache
def shipped_prompt_sha256(stage: QAStage) -> str:
    return prompt_sha256(shipped_prompt(stage))
