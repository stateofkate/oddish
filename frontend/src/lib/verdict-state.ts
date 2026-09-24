import type { DeliveryQAStatus } from "@/lib/types";

/** One label per verdict state; every task is in exactly one. The server
 * builds delivery labels from the same text (oddish/core/verdict_state.py). */
export const VERDICT_STATE_LABELS = {
  no_verdict: "No verdict yet",
  insufficient_trials: "No verdict: insufficient trials",
  queued: "Verdict queued",
  accepted: "Verdict accepted",
  rejected: "Verdict rejected",
  failed: "Verdict failed",
} as const;

export type VerdictState = keyof typeof VERDICT_STATE_LABELS;

/** A verdict label with the task's required-finding count. Must-fix findings
 * make QA reject the task once it runs, but until a rejection is published the
 * label keeps the task's actual state and shows the count beside it. */
export function verdictMustFixLabel(
  state: VerdictState,
  count: number
): string {
  if (count <= 0) return VERDICT_STATE_LABELS[state];
  return state === "rejected"
    ? `Verdict rejected: ${count} Must fix`
    : `${VERDICT_STATE_LABELS[state]} · ${count} Must fix`;
}

export const VERDICT_STATES = Object.keys(
  VERDICT_STATE_LABELS
) as VerdictState[];

/** The server records a settled task with no QA-eligible runs as a failed
 * verdict with this prefix (oddish/core/verdict_state.py). */
export function isInsufficientEvidence(
  error: string | null | undefined
): boolean {
  return (error ?? "").startsWith("Insufficient evidence");
}

/** A cancelled QA pass is also stored as a failed verdict, with one of these
 * errors (CANCELLED_VERDICT_ERRORS in oddish/core/verdict_state.py). It left no
 * verdict; the QA run did not break. */
export function isCancelledVerdict(error: string | null | undefined): boolean {
  return (
    error === "Cancelled by user" ||
    error === "Cancelled because quota was reached"
  );
}

/** Deliveries grade their own QA run; "outdated" means that run's verdict no
 * longer covers the delivered version and trials. */
export function deliveryVerdictState(
  status: DeliveryQAStatus["status"]
): VerdictState {
  switch (status) {
    case "accepted":
      return "accepted";
    case "needs_fixes":
      return "rejected";
    case "queued":
    case "running":
      return "queued";
    case "error":
      return "failed";
    case "outdated":
    case "never":
      return "no_verdict";
  }
}
