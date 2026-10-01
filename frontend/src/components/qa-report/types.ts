export type FeedbackVote = "agree" | "disagree";

/** Emitted whenever a reviewer votes. `target` says what was voted on. */
export type FeedbackRecord = {
  target:
    | { kind: "trajectory"; classification: string; reportTrialId: string }
    | {
        kind: "task_verdict";
        verdict: "accept" | "reject";
        reportTrialId: string;
      }
    | { kind: "pre_trial"; reportTrialId: string }
    | { kind: "action_item"; id: string };
  vote: FeedbackVote;
  note?: string;
};

/** The hosted feedback routes' request for one vote on `trialId`'s QA output. */
export function feedbackRequestInit(
  record: FeedbackRecord,
  trialId: string
): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      body: record.note?.trim() ?? "",
      target: `qa_${record.target.kind}`,
      target_key:
        record.target.kind === "trajectory"
          ? record.target.classification
          : record.target.kind === "task_verdict"
            ? record.target.verdict
            : record.target.kind === "pre_trial"
              ? "report"
              : record.target.id,
      vote: record.vote,
      trial_id: trialId,
      ...(record.target.kind !== "action_item"
        ? { report_trial_id: record.target.reportTrialId }
        : {}),
    }),
  };
}
