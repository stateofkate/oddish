"""Plain-English categories for the QA issue report.

The QA agent already labels each trial (``subtype``) and each finding
(``dimension/problem_type``). ``_TABLE`` maps each label to exactly one
category, so a report counts issues without another model call. A label that
is not listed goes to ``other``, so a new subtype shows up in the report.

Only trial subtypes that mark a task problem are listed. An answer shown in
the instruction is reported as ``info_leakage/mismatch``, the same as other
leaks, including disclosures about the task's evaluation, so all leak findings
share one category. Bump ``TAXONOMY_VERSION``
when a category or a mapping changes.
"""

from __future__ import annotations

from dataclasses import dataclass

TAXONOMY_VERSION = "2"

DIMENSIONS = {
    "grading": "Grading accuracy",
    "integrity": "Solution integrity",
    "design": "Task design",
    "environment": "Environment",
    "other": "Other",
}

# slug | dimension | name | description | Oddish labels
_TABLE = """
tests-pass-wrong-work | grading | Tests pass wrong work | The tests do not check the real requirement, so a wrong or trivial solution passes. | permissive_tests minimal_compliance verifier/incompleteness
tests-demand-unstated-details | grading | Tests demand unstated details | The tests need exact names, output text, or behavior that the instruction does not ask for. | unstated_interface exact_output_matching out_of_scope_tests
tests-give-random-results | grading | Tests give random results | Timing, order, randomness, or the network changes the result, so the same work can pass or fail. | nondeterministic_tests
task-already-solved | grading | Task is already solved | The starting files already contain working code, so no work is needed to pass. | task_pre_solved
reference-solution-broken | grading | Reference solution is broken | The author's reference solution fails, gives wrong results, or skips the required work. | oracle/incompleteness oracle/mismatch
baseline-check-failed | grading | Baseline check failed | The empty run passed or the reference solution failed, so the grade means nothing. |
agent-can-read-hidden-files | integrity | Agent can access restricted information | The agent can access hidden tests, reference solutions, answers, or disclosures about the task's evaluation. | hidden_file_leak test_inspection oracle_copying info_leakage/incompleteness info_leakage/mismatch
answer-in-instruction | integrity | Answer is in the instruction | The instruction shows text that already is the answer, so the task measures copying. | solution_leaked_in_instruction
agent-has-extra-access | integrity | Agent has access it should not have | The agent can use root, the Docker socket, host files, secrets, or the internet to get around the task. | unintended_access
agent-can-hard-code-answers | integrity | Agent can hard-code the answers | The agent passes by returning the expected values instead of computing them. | hardcoding
instruction-leaves-out-facts | design | Instruction leaves out needed facts | Needed behavior is only in the tests or the reference solution, not in the instruction. | underspecified_instruction
instruction-has-many-meanings | design | Instruction has more than one meaning | The instruction allows several valid readings, but the tests accept only one. | ambiguous_requirements
instruction-and-tests-disagree | design | Instruction and tests disagree | The instruction, or its examples, conflict with what the tests check. | contradictory_instruction verifier/mismatch
environment-broken | environment | Task environment is broken | The task's container, packages, or versions are wrong before the agent starts. | environment_defect
required-files-missing | environment | Required files are missing | The starting task cannot pass because files it needs are missing. | broken_baseline
rejected-reason-unknown | other | Rejected, reason unknown | QA rejected the task, but no finding or trial label says why. Read the verdict. |
other | other | Other | The issue fits no category above. Check these by hand. |
"""


@dataclass(frozen=True)
class IssueCategory:
    slug: str
    dimension: str
    name: str
    description: str
    labels: tuple[str, ...]


def _category(line: str) -> IssueCategory:
    slug, dimension, name, description, labels = (c.strip() for c in line.split("|"))
    return IssueCategory(slug, dimension, name, description, tuple(labels.split()))


CATEGORIES = tuple(_category(line) for line in _TABLE.strip().splitlines())
CATEGORY_BY_SLUG = {c.slug: c for c in CATEGORIES}
LABEL_CATEGORY = {label: c.slug for c in CATEGORIES for label in c.labels}


def is_task_problem_trial(classification: object, subtype: object) -> bool:
    """True for the trial labels that count as a task issue."""
    return classification in ("BAD_FAILURE", "BAD_SUCCESS") or (
        classification == "HARNESS_ERROR" and subtype == "hidden_file_leak"
    )


def category_for_subtype(subtype: object) -> str:
    return LABEL_CATEGORY.get(str(subtype or ""), "other")


def category_for_finding(finding: dict) -> str:
    label = f"{finding.get('dimension')}/{finding.get('problem_type')}"
    return LABEL_CATEGORY.get(label, "other")


def category_for_rejection(primary_issue: object) -> str | None:
    """The category a reject verdict implies on its own, if any."""
    if str(primary_issue or "").startswith("CRITICAL:"):
        return "baseline-check-failed"
    return None


def plain_label(label: str) -> str:
    """``unstated_interface`` -> ``Unstated interface``."""
    text = label.replace("_", " ").strip()
    return text[:1].upper() + text[1:]
