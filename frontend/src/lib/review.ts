import type {
  AnalysisClassification,
  PreTrialFinding,
  Task,
  Trial,
} from "@/lib/types";
import { formatLineRange } from "@/lib/line-range";
import { taskHasActiveVerdict, taskHasActiveAnalysis, isActiveTrialStatus } from "@/lib/job-status";
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

/** The stored agent of a run that replays an attack's exploit.sh alone. */
export const REPLAY_AGENT = "exploit-replay";

/** A current attack: superseded attacks stay listed only so finding links resolve. */
export function isAttackTrial(trial: Trial): boolean {
  return (
    trial.kind === "adversarial" &&
    trial.agent !== REPLAY_AGENT &&
    !trial.superseded_by_trial_id
  );
}

/** The replay of the attack's current attempt, if one was queued. */
export function attackReplay(attack: Trial, trials: Trial[]): Trial | null {
  return (
    trials.find((trial) => {
      const pins = trial.harbor_config?.adversarial;
      return (
        trial.agent === REPLAY_AGENT &&
        !trial.superseded_by_trial_id &&
        pins?.replay_of === attack.id &&
        pins.attack_attempt === attack.attempts
      );
    }) ?? null
  );
}

// Matches HOOK_DEADLINE in oddish/core/adversarial.py.
const HOOK_DEADLINE_MS = 60 * 60 * 1000;

/**
 * True while an attack, its replay, or either's settle hook is outstanding,
 * as adversarial_unsettled decides it on the server. A replay is queued, and
 * its findings recorded, a few seconds after the trial row settles.
 */
export function adversarialUnsettled(trials: Trial[], now = Date.now()): boolean {
  const pending = (
    trial: Trial,
    marker: "replay_checked_attempt" | "findings_recorded",
    value: number | boolean
  ) => {
    if (isActiveTrialStatus(trial.status)) return true;
    if (
      (trial.status !== "success" && trial.status !== "failed") ||
      trial.harbor_stage === "cancelled"
    )
      return false;
    // Without a finish time there is no deadline to wait out; never hold the
    // section on it.
    if (!trial.finished_at) return false;
    return (
      trial.harbor_config?.adversarial?.[marker] !== value &&
      now - Date.parse(trial.finished_at) <= HOOK_DEADLINE_MS
    );
  };
  return trials.some((attack) => {
    if (!isAttackTrial(attack)) return false;
    if (pending(attack, "replay_checked_attempt", attack.attempts)) return true;
    const replay = attackReplay(attack, trials);
    return replay != null && pending(replay, "findings_recorded", true);
  });
}

/** An attack's result comes from its run and its replay; QA never reviews it. */
export function adversarialOutcome(
  trial: Trial,
  replay?: Trial | null,
  /** The replay recorded a must-fix finding, e.g. it copied out a protected file. */
  replayFinding = false,
): {
  label: string;
  tone: "defect" | "incomplete" | "neutral";
  detail: string;
  execution: string;
  replay: string;
} {
  const active = isActiveTrialStatus(trial.status);
  // The trajectory upload can land after the attack settles and polling
  // stops, so it does not decide completeness (the CLI uses the same rule).
  const incomplete = trial.status !== "success" || trial.reward == null ||
    Boolean(trial.error_message);
  const facts = {
    execution: active ? trial.status.charAt(0).toUpperCase() + trial.status.slice(1)
      : trial.harbor_stage === "cancelled" ? "Cancelled"
      : trial.status === "failed" ? "Failed"
      : trial.status === "skipped" ? "Skipped"
      : incomplete ? "Incomplete" : "Completed",
    replay: !replay ? "No exploit.sh"
      : isActiveTrialStatus(replay.status) ? "Running"
      : replay.status === "success" && replay.reward === 1 ? "Passed"
      : replay.reward != null ? `Failed (reward ${replay.reward})` : "Failed",
  };
  if (active) return {
    ...facts, label: facts.execution, tone: "neutral",
    detail: "The attack has not finished.",
  };
  // A replay runs exploit.sh alone on a fresh copy, so a pass is the defect.
  if (facts.replay === "Passed") return {
    ...facts, label: "Must fix", tone: "defect",
    detail: "Its exploit.sh, run alone in a fresh copy of the task, passed the grader.",
  };
  if (replayFinding) return {
    ...facts, label: "Must fix", tone: "defect",
    detail: "Its exploit.sh, run alone in a fresh copy of the task, read a file the solver should not see.",
  };
  if (facts.replay === "Running") return {
    ...facts, label: "Replaying exploit", tone: "neutral",
    detail: "The attacker's exploit.sh is running alone in a fresh copy of the task.",
  };
  if (trial.harbor_stage === "cancelled") return {
    ...facts, label: "Cancelled", tone: "incomplete",
    detail: "This check was cancelled.",
  };
  if (trial.reward === 1) return {
    ...facts, label: "Needs a human", tone: "incomplete",
    detail: replay
      ? "The attack passed the grader, but its exploit.sh failed when replayed alone."
      : "The attack passed the grader but left no exploit.sh to replay.",
  };
  if (incomplete) return {
    ...facts, label: "Check incomplete", tone: "incomplete",
    detail: "The attack did not finish cleanly, so it cannot show the task is secure.",
  };
  return {
    ...facts, label: "No defect demonstrated", tone: "neutral",
    detail: replay
      ? "The attack did not pass the grader, and its replayed exploit did not either."
      : "The attack did not pass the grader.",
  };
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
