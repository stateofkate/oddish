"use client";

import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { TrialDetailPanel } from "@/components/trial-detail-panel";
import { tasks } from "../../records";
import type { Trial } from "@/lib/types";

function Preview() {
  const params = useSearchParams();
  const [phase, setPhase] = useState<string | null>(null);
  const [startedAt] = useState(() =>
    new Date(Date.now() - 65000).toISOString()
  );
  const state = phase ?? params.get("state");
  const trial: Trial = {
    ...tasks[0].trials![0],
    status:
      state === "active" || state === "verification" ? "running" : "success",
    harbor_stage:
      state === "active"
        ? "agent_running"
        : state === "verification"
          ? "verification"
          : "completed",
    total_steps: state === "missing" ? null : 960,
    cost_usd: 3.21,
    input_tokens: state === "tokens-missing" ? null : 1000,
    cache_tokens:
      state === "tokens-missing" ? null : state === "tokens-zero" ? 0 : 800,
    output_tokens: 200,
    phase_timing:
      state === "missing"
        ? null
        : {
            ...(state === "verification"
              ? {
                  verifier: {
                    started_at: new Date().toISOString(),
                    finished_at: "",
                    duration_sec: 0,
                  },
                }
              : {}),
            agent_execution:
              state === "active" || state === "verification"
                ? { started_at: startedAt }
                : {
                    started_at: "2026-10-08T12:00:00Z",
                    finished_at: "2026-10-08T15:49:00Z",
                    duration_sec: 13740,
                  },
          },
  };
  if (state === "paused" || state === "retrying" || state === "cancelled" || state === "cancel-stage") {
    trial.status = state === "cancel-stage" ? "running" : state === "cancelled" ? "failed" : state;
    trial.harbor_stage = state === "cancelled" || state === "cancel-stage" ? "cancelled" : "agent_running";
    trial.phase_timing = { agent_execution: { started_at: startedAt } };
  }
  if (state === "retry-setup" || state === "retry-active") {
    const old = new Date(Date.parse(startedAt) - 120000).toISOString();
    trial.attempts = 2;
    trial.started_at = startedAt;
    trial.status = "running";
    trial.harbor_stage =
      state === "retry-setup" ? "environment_setup" : "agent_running";
    trial.phase_timing = {
      agent_execution: {
        started_at: state === "retry-setup" ? old : startedAt,
        finished_at: old,
      },
      verifier: { started_at: old, finished_at: old, duration_sec: 0 },
    };
  }
  if (params.has("duration")) {
    trial.started_at = "2026-10-08T11:00:00Z";
    trial.phase_timing = {
      agent_execution: {
        started_at: "2026-10-08T12:00:00Z",
        duration_sec: Number(params.get("duration")),
      },
    };
  }
  if (params.has("slim") && phase) trial.phase_timing = null;
  const task = { ...tasks[0], trials: [trial] };
  return (
    <div className="h-screen overflow-hidden">
      {params.get("state") === "active" && (
        <div>
          <button onClick={() => setPhase("verification")}>
            Start verification
          </button>
          <button onClick={() => setPhase("completed")}>Complete trial</button>
          {(["paused", "retrying", "cancelled", "cancel-stage"] as const).map(status =>
            <button key={status} onClick={() => setPhase(status)}>{status}</button>)}
        </div>
      )}
      <TrialDetailPanel
        isOpen
        trial={trial}
        task={task}
        contentOnly
        readOnly={params.has("share")}
        requireTrialDetail={params.has("fetch-detail")}
        onClose={() => {}}
        onRetried={() => {}}
        allowRetry={false}
        allowDelete={false}
      />
    </div>
  );
}
export default function Page() {
  return (
    <Suspense>
      <Preview />
    </Suspense>
  );
}
