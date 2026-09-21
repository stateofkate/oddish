"""Allowlisted projections for task data sent to anonymous clients."""

from collections.abc import Mapping

from oddish.schemas import (
    ExperimentTrialAnalysis,
    ExperimentTrialCell,
    PublicExperimentTaskRow,
)

PUBLIC_TASK_GITHUB_META_KEYS = frozenset(
    {
        "category",
        "task_category",
        "benchmark_category",
        "track_category",
        "world",
        "task_world",
        "benchmark_world",
        "domain",
        "task_domain",
        "benchmark_domain",
        "track_domain",
    }
)


def public_task_github_meta(
    github_meta: Mapping[str, str] | None,
) -> dict[str, str] | None:
    """Keep public dataset taxonomy while removing repository and author data."""
    if not github_meta:
        return None
    projected = {
        key: value
        for key, value in github_meta.items()
        if key in PUBLIC_TASK_GITHUB_META_KEYS
    }
    return projected or None


def apply_public_task_row_qa_visibility(
    task: PublicExperimentTaskRow, *, show_qa: bool
) -> None:
    """Keep compact public task rows free of QA unless the share allows it."""
    if not show_qa:
        task.run_analysis = False
        task.review_version_matches = None
        task.verdict = None
        task.verdict_status = None
        task.verdict_error = None


def apply_public_trial_cell_qa_visibility(
    trial: ExperimentTrialCell, *, show_qa: bool
) -> None:
    """Apply the share setting to the bounded trial row used by the grid."""
    if not show_qa:
        trial.analysis = ExperimentTrialAnalysis()
