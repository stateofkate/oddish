import type {
  AnalysisClassification,
  PreTrialFinding,
  Task,
  Trial,
} from "@/lib/types";
import { formatLineRange } from "@/lib/line-range";
import { taskHasActiveVerdict, taskHasActiveAnalysis } from "@/lib/job-status";
import { isBaselineAgentName } from "@/lib/experiment-agent-grouping";
import { defaultVersionElsewhere } from "@/lib/experiment-task-version";
import {
  isCancelledVerdict,
  isInsufficientEvidence,
  type VerdictState,
} from "@/lib/verdict-state";

export const EXECUTION_LABELS: Record<AnalysisClassification, string> = {
  GOOD_SUCCESS: "Good success",
  GOOD_FAILURE: "Good failure",
  BAD_SUCCESS: "Bad success",
  BAD_FAILURE: "Bad failure",
  HARNESS_ERROR: "Harness error",
};

/** Exclude internal, superseded, and baseline runs from review coverage. */
export function isReviewableTrial(trial: Trial): boolean {
  return (
    (trial.kind ?? "agent") === "agent" &&
    !trial.superseded_by_trial_id &&
    !isBaselineAgentName(trial.agent)
  );
}

/** Analysis completion is independent of whether the run produced a valid grade. */
export function runReviewCounts(trials: Trial[]) {
  const counts = {
    total: 0,
    analyzed: 0,
    failed: 0,
    running: 0,
    queued: 0,
  };
  for (const trial of trials) {
    if (!isReviewableTrial(trial)) continue;
    counts.total++;
    if (trial.analysis_status === "running") counts.running++;
    else if (
      trial.analysis_status === "pending" ||
      trial.analysis_status === "queued"
    )
      counts.queued++;
    else if (trial.analysis_status === "failed") counts.failed++;
    else if (
      trial.analysis_status === "success" &&
      trial.analysis?.classification
    )
      counts.analyzed++;
  }
  return counts;
}

export function runReviewSummary(trials: Trial[]): string {
  const counts = runReviewCounts(trials);
  if (!counts.total) return "No runs";
  return [
    `${counts.analyzed}/${counts.total} analyzed`,
    counts.failed
      ? `${counts.failed} ${counts.failed === 1 ? "analysis" : "analyses"} failed`
      : null,
    counts.running ? `${counts.running} analyzing` : null,
    counts.queued ? `${counts.queued} queued` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

/** The task's one verdict state, checked in order: active QA, the failed
 * states, then a verdict owned by the version shown. Solver failure never
 * determines this. */
export function taskReviewStatus(task: Task): VerdictState {
  const verdict =
    task.verdict?.verdict ??
    (task.verdict?.is_good === true
      ? "accept"
      : task.verdict?.is_good === false
        ? "reject"
        : null);
  // The verdict fields and any QA run belong to the default version. An
  // experiment on another version shows only a verdict stamped for its own.
  if (defaultVersionElsewhere(task) != null) {
    if (task.review_version_matches !== true) return "no_verdict";
    if (verdict === "reject") return "rejected";
    return verdict === "accept" ? "accepted" : "no_verdict";
  }
  if (
    taskHasActiveVerdict(task) ||
    (task.verdict_status !== "failed" &&
      verdict == null &&
      taskHasActiveAnalysis(task))
  ) {
    return "queued";
  }
  if (task.verdict_status === "failed") {
    if (isInsufficientEvidence(task.verdict_error))
      return "insufficient_trials";
    return isCancelledVerdict(task.verdict_error) ? "no_verdict" : "failed";
  }
  if (task.verdict && task.review_version_matches === false)
    return "no_verdict";
  if (verdict === "reject") return "rejected";
  return verdict === "accept" ? "accepted" : "no_verdict";
}

/** Review counts, table filters and drawer navigation group by verdict state. */
export type TaskReviewFilter = "all" | VerdictState;

/** A finding address pins content even when that version is today's default. */
export function findingHref(
  taskId: string,
  version: number,
  finding: PreTrialFinding,
  file = false
): string {
  const params = new URLSearchParams({
    version: String(version),
    drawer: "task",
  });
  if (finding.id) params.set("finding", finding.id);
  params.set("taskPane", file && finding.file ? "file" : "overview");
  if (finding.file) params.set("taskFile", finding.file);
  if (finding.line_start) {
    params.set(
      "taskLines",
      formatLineRange({
        start: finding.line_start,
        end: finding.line_end ?? finding.line_start,
      })
    );
  }
  return `/tasks/${encodeURIComponent(taskId)}?${params}`;
}
