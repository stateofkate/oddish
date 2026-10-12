"""Read the same stored cost average used by task detail and table cells."""

from enum import StrEnum
from sqlalchemy import case, func


class CostMetric(StrEnum):
    TOTAL = "total"
    TRIAL = "trial"
    QA = "qa"


def cost_average_expression(cost, metric: CostMetric):
    completed = cost["completed_trials"].as_integer()
    priced = cost["trial_count"].as_integer()
    trial = cost["trial_sum"].as_float()
    qa = cost["qa_sum"].as_float()
    if metric == CostMetric.TRIAL:
        return trial / func.nullif(priced, 0)
    known = (cost["qa_unpriced_jobs"].as_integer() == 0) & (
        cost["qa_unattributed_jobs"].as_integer() == 0
    )
    if metric == CostMetric.TOTAL:
        known = known & (priced == completed)
    return case(
        (
            known,
            ((trial + qa) if metric == CostMetric.TOTAL else qa)
            / func.nullif(completed, 0),
        )
    )
