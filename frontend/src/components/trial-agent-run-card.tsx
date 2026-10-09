"use client";

import { useEffect, useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { formatDurationSec } from "@/lib/format";
import { trialAgentRun } from "@/lib/trial-agent-run";
import type { Trial } from "@/lib/types";

export function TrialAgentRunCard({
  trial,
  updating = false,
}: {
  trial: Trial;
  updating?: boolean;
}) {
  const [now, setNow] = useState(() => Date.now());
  const measured = trialAgentRun(trial, now);
  const runKey = `${trial.id}:${trial.attempts}:${trial.started_at}`;
  const agentStart = Date.parse(trial.phase_timing?.agent_execution?.started_at ?? "");
  const [lastLive, setLastLive] = useState<{
    key: string;
    start: number;
    seconds: number;
  } | null>(null);
  useEffect(() => {
    if (measured.running && measured.seconds != null)
      setLastLive({ key: runKey, start: agentStart, seconds: measured.seconds });
    else if (trial.status !== "paused") setLastLive(null);
  }, [runKey, agentStart, trial.status, measured.running, measured.seconds]);
  const frozen = measured.seconds == null && trial.status === "paused" &&
    trial.harbor_stage === "agent_running" && lastLive?.key === runKey &&
    agentStart <= lastLive.start;
  const seconds = frozen ? lastLive.seconds : measured.seconds;
  const { steps, running } = measured;
  useEffect(() => {
    if (!running) return;
    setNow(Date.now());
    const interval = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(interval);
  }, [running, trial.id, trial.attempts]);
  return (
    <Card
      className="min-w-0 border sm:min-w-[120px]"
      role="region"
      aria-label="Agent run"
    >
      <CardContent className="flex h-full items-center px-2 py-1">
        <div
          className="min-w-0"
          title="Agent execution only; excludes queue, environment setup, agent setup and verification."
        >
          <div className="text-muted-foreground text-[8px] leading-none tracking-wider uppercase">
            Agent run
          </div>
          <div
            className="mt-1 font-mono text-sm leading-none font-bold tabular-nums"
            role={running ? "timer" : undefined}
            aria-label="Agent execution time"
            title={frozen ? "Last observed agent execution time before pause." : undefined}
          >
            {seconds == null
              ? updating
                ? "Updating…"
                : "—"
              : seconds < 60
                ? `${seconds}s`
                : formatDurationSec(seconds)}
          </div>
          <div className="text-muted-foreground mt-1 font-mono text-[9px] leading-none tabular-nums">
            {steps == null ? "—" : steps.toLocaleString()} steps
            {running && <span> · live</span>}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
