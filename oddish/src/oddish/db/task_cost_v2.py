"""Frozen v2 cost cohort; retain the stable SQL function signature for old callers."""

from oddish.db.task_cost_v1 import COST_SUMMARY_STATEMENTS as V1

# Reuse the frozen v1 attribution/price guards, narrowing QA to current,
# settled records. The stable function and triggers need no caller cutover.
COST_SUMMARY_STATEMENTS = (
    V1[0]
    .replace(
        "SELECT CASE WHEN tr.status IN ('SUCCESS','FAILED') THEN tr.cost_usd END AS cost_usd",
        "SELECT tr.cost_usd",
    )
    .replace(
        "AND tr.status <> 'SKIPPED'",
        "AND tr.status IN ('SUCCESS','FAILED') AND tr.superseded_by_trial_id IS NULL",
    )
    .replace(
        "WHERE a.deleted_at IS NULL AND a.org_id",
        "WHERE a.deleted_at IS NULL AND tr.deleted_at IS NULL AND tr.superseded_by_trial_id IS NULL\n"
        "     AND (tr.idempotency_key IS NULL OR tr.idempotency_key NOT LIKE 'combine:%') AND a.org_id",
    )
    .replace(
        "tr.task_version_id IS NULL",
        "tr.task_version_id IS NULL AND tr.deleted_at IS NULL AND tr.superseded_by_trial_id IS NULL\n"
        "     AND (tr.idempotency_key IS NULL OR tr.idempotency_key NOT LIKE 'combine:%')",
    )
    .replace(
        "a.deleted_at IS NULL", "a.deleted_at IS NULL AND a.cost_source <> 'pending'"
    )
    .replace("'completed_trials',", "'cohort_version', 2,\n   'completed_trials',"),
    *V1[1:],
)
