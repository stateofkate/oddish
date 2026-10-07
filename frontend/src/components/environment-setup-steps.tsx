"use client";

import { useEffect, useId, useState } from "react";
import { AlertCircle, Check, ChevronRight, Circle, Loader2 } from "lucide-react";

import { formatDurationSec } from "@/lib/format";
import {
  setupStepLabel,
  ec2HealthRows,
  secondarySetupSteps,
  type SetupState,
  type SetupStep,
} from "@/lib/setup-events";
import { cn } from "@/lib/utils";

function useElapsedSeconds(startedAt: string | null): number | null {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!startedAt) return;
    setNow(Date.now());
    const interval = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(interval);
  }, [startedAt]);
  if (!startedAt) return null;
  const start = Date.parse(startedAt);
  return Number.isFinite(start) ? Math.max(0, (now - start) / 1000) : null;
}

function StepRow({ step, live }: { step: SetupStep; live: boolean }) {
  const elapsed = useElapsedSeconds(live ? step.startedAt : null);
  const seconds = live ? elapsed : step.durationSeconds;
  const [healthOpen, setHealthOpen] = useState(false);
  const healthId = useId();
  const healthRows = ec2HealthRows(step);
  return (
    <>
    <div className="flex items-center gap-2 py-1 text-xs">
      {step.status === "success" ? (
        <Check className="text-paper-pass h-3 w-3 shrink-0" aria-label="done" />
      ) : step.status === "error" || step.status === "cancelled" ? (
        <AlertCircle
          className="text-paper-fail h-3 w-3 shrink-0"
          aria-label={step.status === "cancelled" ? "cancelled" : "failed"}
        />
      ) : !live ? (
        <Circle
          className="text-muted-foreground h-3 w-3 shrink-0"
          aria-label="End not recorded"
        />
      ) : (
        <Loader2
          className="text-paper-running h-3 w-3 shrink-0 animate-spin"
          aria-label="running"
        />
      )}
      <span
        className={cn(
          "min-w-0 flex-1 truncate font-mono",
          step.status === "started"
            ? "text-foreground"
            : "text-muted-foreground"
        )}
        title={step.phase}
      >
        {healthRows.length ? (
          <button
            type="button"
            className="inline-flex max-w-full items-center gap-1 rounded text-left hover:text-foreground focus-visible:outline active:text-foreground"
            aria-expanded={healthOpen}
            aria-controls={healthId}
            onClick={() => setHealthOpen(open => !open)}
          >
            <span className="truncate">{setupStepLabel(step)}</span>
            <ChevronRight aria-hidden="true" className={cn("h-3 w-3 shrink-0", healthOpen && "rotate-90")} />
          </button>
        ) : setupStepLabel(step)}
        {!live && step.status === "started" && (
          <span className="text-muted-foreground ml-2">End not recorded</span>
        )}
        {step.errorType && (
          <span className="text-paper-fail ml-2">{step.errorType}</span>
        )}
      </span>
      <span
        className={cn(
          "shrink-0 font-mono text-xs tabular-nums",
          live ? "text-paper-running" : "text-muted-foreground"
        )}
      >
        {seconds != null ? (seconds === 0 ? "0s" : formatDurationSec(seconds)) : "—"}
      </span>
    </div>
    {healthRows.length > 0 && (
      <div
        id={healthId}
        hidden={!healthOpen}
        className="text-muted-foreground pb-1 pl-5 font-mono text-xs break-words"
        title={`AWS status reported ${step.ec2Health?.observed_at}`}
      >
        <p className="mb-1">Time from health-check start</p>
        <ul aria-label="Instance health observations">
          {healthRows.map(row => (
            <li key={row.field} className="flex items-baseline justify-between gap-2 py-1">
              <span>{row.label}</span>
              <span className="shrink-0 tabular-nums" title={`First reported by AWS at ${row.observedAt}`}>
                {row.secondsAfterStart == null ? "—" : row.secondsAfterStart === 0 ? "0s" : formatDurationSec(row.secondsAfterStart)}
              </span>
            </li>
          ))}
        </ul>
      </div>
    )}
    </>
  );
}

/** Secondary setup operations only; activity details remain retained but unrendered. */
export function EnvironmentSetupSteps({ state, className, live = true }: {
  live?: boolean;
  state: SetupState;
  className?: string;
}) {
  const steps = secondarySetupSteps(state);
  if (!steps.length) return null;
  return <ul className={cn("space-y-0", className)} aria-label="Environment setup steps">
    {steps.map((step, index) => <li key={step.operationId ?? `${step.phase}-${index}`} className="list-none">
      <StepRow step={step} live={live && step.status === "started"} />
    </li>)}
  </ul>;
}
