import type { Trial } from "./types";

const active = new Set(["pending", "queued", "running", "paused", "retrying"]);

/** A fresh list lifecycle wins over stale detail without discarding result fields. */
export function reconcileTrialLifecycle(
  detail: Trial | null | undefined,
  row: Trial | null | undefined
): Trial | null {
  if (!detail) return row ?? null;
  if (!row || detail.id !== row.id) return detail;
  if (row.attempts > detail.attempts) return row;
  if (
    row.attempts === detail.attempts &&
    active.has(detail.status) &&
    !active.has(row.status)
  ) {
    return {
      ...detail,
      status: row.status,
      finished_at: row.finished_at,
      harbor_stage: row.harbor_stage,
      reward: row.reward,
      error_message: row.error_message,
      phase_timing: row.phase_timing ?? detail.phase_timing,
    };
  }
  return detail;
}
