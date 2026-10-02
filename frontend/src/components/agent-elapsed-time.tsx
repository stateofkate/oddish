"use client";

import { useEffect, useState } from "react";

export interface AgentTiming {
  started_at?: string | null;
  finished_at?: string | null;
  duration_sec?: number | null;
}

/** Wall time from the worker's agent phase, independent of simulation time. */
export function AgentElapsedTime({ timing, running = false }: {
  timing?: AgentTiming | null;
  running?: boolean;
}) {
  const [now, setNow] = useState(() => Date.now());
  const start = timing?.started_at ? Date.parse(timing.started_at) : NaN;
  const end = timing?.finished_at ? Date.parse(timing.finished_at) : NaN;
  const ticking = running && Number.isFinite(start) && !Number.isFinite(end);
  useEffect(() => {
    if (!ticking) return;
    setNow(Date.now());
    const interval = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(interval);
  }, [ticking]);

  const seconds = Number.isFinite(end) && Number.isFinite(start)
    ? (end - start) / 1000
    : ticking ? (now - start) / 1000 : timing?.duration_sec;
  const elapsed = seconds != null && Number.isFinite(seconds)
    ? Math.max(0, Math.floor(seconds)) : null;
  const label = elapsed === null ? "—" : [
    ...(elapsed >= 3600 ? [Math.floor(elapsed / 3600)] : []),
    Math.floor(elapsed / 60) % 60,
    elapsed % 60,
  ].map(value => String(value).padStart(2, "0")).join(":");
  return <span role="timer" aria-label="Agent elapsed time"
    title={elapsed === null ? "Agent execution timing is unavailable." : "Real elapsed time in the agent phase; excludes queue, environment setup and verification."}
    className="text-muted-foreground whitespace-nowrap text-xs font-normal tabular-nums">
    Agent time {label}
  </span>;
}
