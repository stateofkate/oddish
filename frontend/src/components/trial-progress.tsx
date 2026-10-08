"use client";

import useSWR from "swr";
import { ChevronRight } from "lucide-react";
import { useEffect, useState } from "react";
import { HarborStageTimeline, type HarborStageId, type MeasuredStageState } from "@/components/harbor-stage-timeline";
import { HarborStageBadge } from "@/components/harbor-stage-badge";
import { Badge } from "@/components/ui/badge";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { fetcher } from "@/lib/api";
import { isActiveTrialStatus } from "@/lib/job-status";
import {
  foldSetupEvents,
  hasSetupInfo,
  isSetupEvent,
  secondarySetupSteps,
} from "@/lib/setup-events";
import {
  useTrialLiveEvents,
  type LiveEvent,
} from "@/lib/use-trial-live-events";
import type { Trial } from "@/lib/types";
import type { ProgressHistory } from "@/lib/progress-history";
import { formatDurationSec } from "@/lib/format";
import type { AgentTiming } from "@/components/agent-elapsed-time";
import {
  EnvironmentSetupSteps,
} from "@/components/environment-setup-steps";

const summaryStyle =
  "cursor-pointer rounded text-xs hover:bg-muted focus-visible:outline active:bg-muted";
function elapsed(start?: string | null, end?: string | null) {
  const seconds = (Date.parse(end ?? "") - Date.parse(start ?? "")) / 1000;
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null;
}
// Use the next phase boundary only when this phase has no recorded finish.
function concludeAtNextStart(timing: AgentTiming | null | undefined, nextStart?: string | null) {
  if (!timing?.started_at || timing.finished_at || elapsed(timing.started_at, nextStart) == null)
    return timing;
  return { ...timing, finished_at: nextStart, duration_sec: timing.duration_sec ?? elapsed(timing.started_at, nextStart)! };
}
function Duration({
  timing,
  running = false,
}: {
  timing?: AgentTiming | null;
  running?: boolean;
}) {
  const [now, setNow] = useState(() => new Date().toISOString());
  const ticking =
    running && Boolean(timing?.started_at) && !timing?.finished_at;
  useEffect(() => {
    if (!ticking) return;
    const timer = setInterval(() => setNow(new Date().toISOString()), 1000);
    return () => clearInterval(timer);
  }, [ticking]);
  const seconds = ticking
    ? elapsed(timing?.started_at, now)
    : (timing?.duration_sec ??
      elapsed(timing?.started_at, timing?.finished_at));
  if (seconds == null) return null;
  return (
    <span
      className="shrink-0 text-[10px] tabular-nums text-muted-foreground"
      role={ticking ? "timer" : undefined}
    >
      {seconds == null
        ? "Not recorded"
        : `${seconds === 0 ? "0s" : formatDurationSec(seconds)}${ticking ? " elapsed" : ""}`}
    </span>
  );
}
/** Compact measured phases; operational timestamps remain in metadata. */
export function TrialProgress({
  trial,
  apiBaseUrl = "/api",
  readOnly = false,
  liveEvents,
  liveAttempt,
  environmentOnly = false,
  liveAgentTiming,
  onStageClick,
}: {
  trial: Trial;
  apiBaseUrl?: string;
  readOnly?: boolean;
  onStageClick?: (stageId: string) => void;
  liveEvents?: LiveEvent[];
  liveAttempt?: number;
  environmentOnly?: boolean;
  liveAgentTiming?: AgentTiming | null;
}) {
  const active = isActiveTrialStatus(trial.status);
  // Queue/retry rows may still carry the previous attempt's stage and timestamps.
  const awaitingAttempt = ["pending", "queued", "retrying"].includes(trial.status);
  const phaseTiming = awaitingAttempt ? null : trial.phase_timing;
  const executing = active && !awaitingAttempt;
  const [timelineOpen, setTimelineOpen] = useState(true);
  useEffect(() => setTimelineOpen(true), [trial.id, trial.attempts]);
  const {
    data: history,
    error,
    mutate,
  } = useSWR<ProgressHistory>(
    readOnly
      ? null
      : `${apiBaseUrl}/trials/${encodeURIComponent(trial.id)}/progress-history`,
    fetcher,
    { refreshInterval: active ? 5000 : 0, revalidateOnFocus: false }
  );
  useEffect(() => {
    void mutate();
  }, [trial.status, trial.attempts, mutate]);
  const { events: fetchedEvents, last } = useTrialLiveEvents(trial.id, {
    apiBaseUrl,
    enabled: active && !readOnly && !liveEvents,
    resetKey: trial.attempts,
    stopWhen: (data) =>
      data.done || data.events.some((event) => !isSetupEvent(event)),
  });
  const events =
    (liveAttempt ?? last?.attempt) === trial.attempts
      ? (liveEvents ?? fetchedEvents).filter(isSetupEvent)
      : [];
  const retained = history?.events ?? [];
  const setupFor = (attempt: number) => {
    const saved = retained
      .filter((event) => event.attempt === attempt)
      .map((event, index) => ({ ...event, seq: event.source_seq ?? index }));
    // Join by original stream identity, so replay never duplicates a v1 start.
    const merged = new Map<number, LiveEvent>(
      saved.map((event) => [event.seq, event])
    );
    if (attempt === trial.attempts)
      for (const event of events)
        if (!merged.has(event.seq)) merged.set(event.seq, event);
    return foldSetupEvents([...merged.values()].sort((a, b) => a.seq - b.seq));
  };
  const setup = setupFor(trial.attempts);
  const root = awaitingAttempt ? undefined : setup.steps.find(
    (step) => step.category === "setup" && !step.parentOperationId
  );
  const rootTiming = root ? {
    started_at: root.startedAt,
    finished_at: root.finishedAt,
    duration_sec: root.durationSeconds,
  } : null;
  const recordedEnvironment = root?.hasCompletedActivities
    ? rootTiming
    : phaseTiming?.environment_setup ?? rootTiming;
  const recordedAgent =
    awaitingAttempt ? null : liveAttempt === trial.attempts
      ? (liveAgentTiming ?? phaseTiming?.agent_execution)
      : phaseTiming?.agent_execution;
  const verifier = concludeAtNextStart(phaseTiming?.verifier, trial.status === "success" ? trial.finished_at : null);
  // Agent setup has no row of its own, so Agent Running starts with it until execution is recorded.
  const agentSetupStart = awaitingAttempt ? undefined : phaseTiming?.agent_setup?.started_at;
  const agent = concludeAtNextStart(recordedAgent ?? (agentSetupStart ? { started_at: agentSetupStart } : null), verifier?.started_at);
  const environment = concludeAtNextStart(recordedEnvironment,
    phaseTiming?.agent_setup?.started_at ?? agent?.started_at ?? verifier?.started_at);
  // Advancing past setup proves completion even when older workers omit timings.
  const environmentCompleted = !awaitingAttempt && Boolean(
    root?.status === "success" || environment?.finished_at || phaseTiming?.agent_setup?.started_at || agent?.started_at ||
    phaseTiming?.verifier?.started_at ||
    ["agent_setup", "agent_running", "verification", "cleanup"].includes(trial.harbor_stage ?? "") ||
    (trial.harbor_stage === "completed" && trial.status === "success")
  );
  const trialCompleted = !awaitingAttempt && trial.harbor_stage === "completed" && trial.status === "success";
  const agentCompleted = !awaitingAttempt && Boolean(verifier?.started_at || ["verification", "cleanup"].includes(trial.harbor_stage ?? "") || trialCompleted);
  const verifierCompleted = Boolean(verifier?.started_at && ["cleanup", "completed"].includes(trial.harbor_stage ?? ""));
  const agentRunning = executing && ["agent_setup", "agent_running"].includes(trial.harbor_stage ?? "");
  const envRunning =
    executing &&
    !environmentCompleted &&
    (!root || root.status === "started") &&
    !agent?.started_at &&
    Boolean(environment?.started_at || setup.current || trial.harbor_stage === "environment_setup");
  const priorAttempts = [...new Set(retained.map((event) => event.attempt))]
    .filter((attempt) => attempt !== trial.attempts)
    .sort((a, b) => b - a);
  const missing = error
    ? "Setup history could not be loaded."
    : !history && !readOnly
      ? "Loading setup history…"
      : "No setup history recorded for this attempt.";
  const setupDetails =
    secondarySetupSteps(setup).length || priorAttempts.length ? (
      <>
        <EnvironmentSetupSteps state={setup} live={envRunning} />
        {priorAttempts.map((attempt) => {
          const state = setupFor(attempt);
          return (
            <details key={attempt}>
              <summary className={summaryStyle}>
                Environment setup · Attempt {attempt}
              </summary>
              <EnvironmentSetupSteps
                state={state}
                live={false}
                className="mt-2"
              />
            </details>
          );
        })}
      </>
    ) : undefined;
  const measuredState = (stage: HarborStageId, timing?: AgentTiming | null, running = false, completed = false): MeasuredStageState =>
    trial.status === "failed" && trial.harbor_stage === stage ? "failed" : (completed || timing?.finished_at) ? "completed" : running ? "active" : "not recorded";
  const executionStarted = !awaitingAttempt && Boolean(
    environment?.started_at || agent?.started_at || phaseTiming?.verifier?.started_at ||
    ["trial_started", "environment_setup", "agent_setup", "agent_running", "verification", "cleanup"].includes(trial.harbor_stage ?? "") ||
    (trial.harbor_stage === "completed" && trial.status === "success")
  );
  const initializingRunning = trial.status === "running" && !executionStarted &&
    (!trial.harbor_stage || trial.harbor_stage === "starting");
  // Without a recorded next boundary, stage progress alone cannot supply a duration.
  const initializing = awaitingAttempt ? null : { started_at: trial.started_at, finished_at: environment?.started_at, duration_sec: elapsed(trial.started_at, environment?.started_at) };
  if (environmentOnly) return (
    <details open={timelineOpen} onToggle={event => setTimelineOpen(event.currentTarget.open)} className="border-border border-b px-4 py-2">
      <summary className={`${summaryStyle} flex list-none items-center gap-1 [&::-webkit-details-marker]:hidden`}>
        Environment Setup
        <ChevronRight aria-hidden="true" className="h-3 w-3 shrink-0 text-muted-foreground" />
      </summary>
      <div className="mt-2">
        {setupDetails ?? <p className="text-muted-foreground text-xs">{missing}</p>}
      </div>
    </details>
  );
  return (
    <Card role="region" aria-label="Trial progress" data-attempt={trial.attempts}>
      <details open={timelineOpen} onToggle={event => setTimelineOpen(event.currentTarget.open)}>
        <summary className="cursor-pointer list-none rounded focus-visible:outline [&::-webkit-details-marker]:hidden">
          <CardHeader className="px-4 pt-2 pb-1">
            <CardTitle className="text-muted-foreground flex items-center justify-between text-[11px] font-semibold tracking-wider uppercase">
              <span>Execution Timeline</span>
              {trial.status === "failed" && trial.harbor_stage !== "cancelled"
                ? <Badge variant="destructive">Failed</Badge>
                : <HarborStageBadge stage={trial.harbor_stage ?? "starting"} />}
            </CardTitle>
          </CardHeader>
        </summary>
        <CardContent className="px-4 pb-2">
          <HarborStageTimeline
            key={`${trial.id}:${trial.attempts}`}
            onStageClick={onStageClick}
            currentStage={trial.harbor_stage}
            status={trial.status}
            phaseTiming={phaseTiming}
            startedAt={awaitingAttempt ? null : trial.started_at}
            finishedAt={awaitingAttempt ? null : trial.finished_at}
            collapsible
            stageStates={{
              starting: executionStarted ? "completed" : initializingRunning ? "active" : "not recorded",
              trial_started: executionStarted ? "completed" : "not recorded",
              environment_setup: root?.status === "cancelled" ? "cancelled" : root?.status === "error" ? "failed" : measuredState("environment_setup", environment, envRunning, environmentCompleted),
              agent_running: measuredState("agent_running", agent, agentRunning, agentCompleted),
              verification: measuredState("verification", verifier, executing && trial.harbor_stage === "verification", verifierCompleted),
              completed: active ? "not recorded" : trial.harbor_stage === "cancelled" ? "cancelled" : trial.status === "failed" ? "failed" : trial.finished_at ? "completed" : "not recorded",
            }}
            stageDurations={{
              starting: <Duration timing={initializing} running={initializingRunning} />,
              environment_setup: <Duration timing={environment} running={envRunning} />,
              agent_running: <Duration timing={agent} running={agentRunning} />,
              verification: <Duration timing={verifier} running={executing && trial.harbor_stage === "verification"} />,
            }}
            stageContent={{
              environment_setup: setupDetails,
            }}
          />
          {!hasSetupInfo(setup) && <p className="text-muted-foreground text-xs">{missing}</p>}
          {trial.error_message && (
            <p className="text-paper-fail pl-7 text-xs">
              {trial.error_message}
            </p>
          )}
          {history?.history_truncated && (
            <p className="text-muted-foreground text-xs">
              Some setup history exceeded the history display limit.
            </p>
          )}
        </CardContent>
      </details>
    </Card>
  );
}
