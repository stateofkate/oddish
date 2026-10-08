"""Recorded completed-model effort, shared by detail and version summaries."""

from typing import Any

from sqlalchemy import func

from oddish.core.baseline_gate import baseline_agent_clause
from oddish.core.task_browse_metrics import trial_bucket_label
from oddish.db import TrialModel


def completed_model_clause() -> Any:
    return ~baseline_agent_clause(TrialModel.agent) & trial_bucket_label().in_(
        ("pass", "partial", "fail", "scoreless")
    )


def task_effort_columns() -> list[Any]:
    """Aggregate alongside existing scoped version summaries, inside their lock."""
    eligible = completed_model_clause()
    steps = TrialModel.total_steps
    tokens = TrialModel.input_tokens + TrialModel.output_tokens
    has_steps = eligible & (steps >= 0)
    has_tokens = (
        eligible & (TrialModel.input_tokens >= 0) & (TrialModel.output_tokens >= 0)
    )
    columns = [
        func.count().filter(eligible).label("effort_completed_trials"),
        func.coalesce(func.sum(steps).filter(has_steps), 0).label("steps_sum"),
        func.count().filter(has_steps).label("steps_trial_count"),
        func.coalesce(func.sum(tokens).filter(has_tokens), 0).label("tokens_sum"),
        func.count().filter(has_tokens).label("tokens_trial_count"),
    ]
    measurements = {
        "uncached_input_tokens": (
            TrialModel.input_tokens - TrialModel.cache_tokens,
            (TrialModel.cache_tokens >= 0)
            & (TrialModel.input_tokens >= TrialModel.cache_tokens),
        ),
        "cached_input_tokens": (TrialModel.cache_tokens, TrialModel.cache_tokens >= 0),
        "output_tokens": (TrialModel.output_tokens, TrialModel.output_tokens >= 0),
    }
    for name, (value, valid) in measurements.items():
        columns.extend(
            [
                func.coalesce(func.sum(value).filter(eligible & valid), 0).label(
                    f"{name}_sum"
                ),
                func.count().filter(eligible & valid).label(f"{name}_trial_count"),
            ]
        )
    return columns
