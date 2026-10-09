import type { Trial } from "./types";

const AgentRunStage = {
  Starting: "starting",
  TrialStarted: "trial_started",
  EnvironmentSetup: "environment_setup",
  AgentSetup: "agent_setup",
  AgentRunning: "agent_running",
  Verification: "verification",
  Cleanup: "cleanup",
  Completed: "completed",
  Cancelled: "cancelled",
} as const;
const agentRunStages = Object.values(AgentRunStage);
const stageIndex = (stage: string | null | undefined) =>
  agentRunStages.findIndex(value => value === stage);

export function hasAgentRunEnded(stage: Trial["harbor_stage"]): boolean {
  return stageIndex(stage) > stageIndex(AgentRunStage.AgentRunning);
}

/** Agent execution only. Trial/trajectory wall time includes other work. */
export function trialAgentRun(trial: Trial, now: number) {
  // Setup and queued rows can retain measurements from the previous attempt.
  if (
    ["pending", "queued", "retrying"].includes(trial.status) ||
    (stageIndex(trial.harbor_stage) >= 0 &&
      stageIndex(trial.harbor_stage) < stageIndex(AgentRunStage.AgentRunning))
  )
    return { seconds: null, steps: null, running: false };
  const timing = trial.phase_timing?.agent_execution;
  const start = Date.parse(timing?.started_at ?? "");
  const attemptStart = Date.parse(trial.started_at ?? "");
  if (
    (trial.attempts > 1 &&
      (!Number.isFinite(attemptStart) || !Number.isFinite(start))) ||
    (Number.isFinite(attemptStart) &&
      Number.isFinite(start) &&
      start < attemptStart)
  )
    return { seconds: null, steps: null, running: false };
  const boundary = (value?: string | null) => {
    const parsed = Date.parse(value ?? "");
    return Number.isFinite(start) && parsed >= start ? parsed : NaN;
  };
  const finish = boundary(timing?.finished_at);
  const verifier = boundary(trial.phase_timing?.verifier?.started_at);
  const end = Number.isFinite(finish) ? finish : verifier;
  const running =
    trial.status === "running" &&
    trial.harbor_stage === AgentRunStage.AgentRunning &&
    Number.isFinite(start) &&
    !Number.isFinite(end);
  const invalidFinish =
    Boolean(timing?.finished_at) && !Number.isFinite(finish);
  let duration = invalidFinish ? null : timing?.duration_sec;
  if (Number.isFinite(start) && Number.isFinite(end))
    duration = (end - start) / 1000;
  else if (running) duration = (now - start) / 1000;
  return {
    seconds:
      duration != null && Number.isFinite(duration) && duration >= 0
        ? Math.floor(duration)
        : null,
    steps:
      !(trial.attempts > 1 &&
        ["running", "paused"].includes(trial.status)) &&
      trial.total_steps != null &&
      Number.isFinite(trial.total_steps) &&
      trial.total_steps >= 0
        ? trial.total_steps
        : null,
    running,
  };
}

function mergePhase<
  T extends {
    started_at: string;
    finished_at?: string | null;
    duration_sec?: number;
  },
>(previous: T | undefined, incoming: T | undefined): T | undefined {
  if (!incoming) return previous;
  const newStart = Date.parse(incoming.started_at);
  if (!Number.isFinite(newStart)) return previous;
  if (!previous) return incoming;
  const oldStart = Date.parse(previous.started_at);
  if (
    Number.isFinite(newStart) &&
    (!Number.isFinite(oldStart) || newStart > oldStart)
  )
    return incoming;
  if (newStart < oldStart) return previous;
  const oldFinish = Date.parse(previous.finished_at ?? "");
  const hasValidFinish = Number.isFinite(oldFinish) && oldFinish >= oldStart;
  return {
    ...incoming,
    ...previous,
    finished_at: hasValidFinish ? previous.finished_at : incoming.finished_at,
    duration_sec: previous.duration_sec ?? incoming.duration_sec,
  };
}

/** Enrich the card from same-attempt list progress without replacing detail results. */
export function reconcileAgentRunProgress(
  detail: Trial | null,
  row: Trial | null | undefined
): Trial | null {
  if (
    !detail ||
    !row ||
    detail.id !== row.id ||
    detail.attempts !== row.attempts
  )
    return detail;
  if (detail.status === "running" && row.status !== "running")
    return { ...detail, status: row.status, harbor_stage: row.harbor_stage };
  if (detail.status !== "running" || row.status !== "running") return detail;
  const detailStage = stageIndex(detail.harbor_stage);
  const rowStage = stageIndex(row.harbor_stage);
  if (rowStage < 0 || rowStage < detailStage) return detail;
  const previous = detail.phase_timing;
  const incoming = row.phase_timing;

  return {
    ...detail,
    harbor_stage: row.harbor_stage,
    phase_timing: {
      ...previous,
      agent_execution: mergePhase(
        previous?.agent_execution,
        incoming?.agent_execution
      ),
      verifier: mergePhase(previous?.verifier, incoming?.verifier),
    },
  };
}
