"use client";

import { useState, type ReactNode } from "react";
import { Check, Clock, AlertCircle, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { Trial } from "@/lib/types";
import { cn } from "@/lib/utils";

type PhaseTiming = NonNullable<Trial["phase_timing"]>;

export type HarborStageId =
  | "starting"
  | "trial_started"
  | "environment_setup"
  | "agent_running"
  | "verification"
  | "completed";

export type MeasuredStageState = "completed" | "active" | "failed" | "cancelled" | "not recorded";

interface HarborStageTimelineProps {
  stageStates?: Partial<Record<HarborStageId, MeasuredStageState>>;
  stageDurations?: Partial<Record<HarborStageId, ReactNode>>;
  collapsible?: boolean;
  stageContent?: Partial<Record<HarborStageId, ReactNode>>;
  currentStage: string | null | undefined;
  status: string;
  isFailure?: boolean;
  onStageClick?: (stageId: string) => void;
  phaseTiming?: PhaseTiming | null;
  startedAt?: string | null;
  finishedAt?: string | null;
}

interface StageInfo {
  id: HarborStageId;
  label: string;
}

const HARBOR_STAGES: StageInfo[] = [
  { id: "starting", label: "Initializing" },
  { id: "trial_started", label: "Trial Started" },
  { id: "environment_setup", label: "Environment Setup" },
  { id: "agent_running", label: "Agent Running" },
  { id: "verification", label: "Verification" },
  { id: "completed", label: "Completed" },
];

function formatDuration(sec: number): string {
  if (sec < 1) return `${Math.round(sec * 1000)}ms`;
  if (sec < 60) return `${Math.round(sec)}s`;
  const min = Math.floor(sec / 60);
  const remainSec = Math.round(sec % 60);
  if (min < 60) return remainSec > 0 ? `${min}m ${remainSec}s` : `${min}m`;
  const hr = Math.floor(min / 60);
  const remainMin = min % 60;
  return remainMin > 0 ? `${hr}h ${remainMin}m` : `${hr}h`;
}

function getStageDuration(
  stageId: string,
  phaseTiming: PhaseTiming | null | undefined,
  startedAt: string | null | undefined,
  finishedAt: string | null | undefined,
): string | null {
  if (!phaseTiming) return null;

  switch (stageId) {
    case "environment_setup": {
      const envSec = phaseTiming.environment_setup?.duration_sec ?? 0;
      const total = envSec + (phaseTiming.agent_setup?.duration_sec ?? 0);
      return total > 0 ? formatDuration(total) : null;
    }
    case "agent_running":
      return phaseTiming.agent_execution?.duration_sec != null
        ? formatDuration(phaseTiming.agent_execution.duration_sec)
        : null;
    case "verification":
      return phaseTiming.verifier?.duration_sec != null
        ? formatDuration(phaseTiming.verifier.duration_sec)
        : null;
    case "completed": {
      if (!startedAt || !finishedAt) return null;
      const totalSec =
        (new Date(finishedAt).getTime() - new Date(startedAt).getTime()) / 1000;
      return totalSec > 0 ? formatDuration(totalSec) : null;
    }
    default:
      return null;
  }
}

export function HarborStageTimeline({
  currentStage,
  status,
  isFailure = false,
  onStageClick,
  phaseTiming,
  startedAt,
  finishedAt,
  stageContent,
  stageStates,
  stageDurations,
  collapsible = false,
}: HarborStageTimelineProps) {
  if (!currentStage && !stageStates) return null;
  const currentIndex = HARBOR_STAGES.findIndex((s) => s.id === currentStage);
  const isFailed = status === "failed";
  const isCancelled = currentStage === "cancelled";

  return (
    <div className="space-y-0">
      {HARBOR_STAGES.map((stage, index) => {
        const measured = stageStates?.[stage.id];
        const isCompleted = measured ? measured === "completed" || measured === "failed" || measured === "cancelled" : index < currentIndex ||
            (index === currentIndex && currentStage === "completed");
        const isCurrent = measured ? measured === "active" :
          index === currentIndex && currentStage !== "completed";
        const isLast = index === HARBOR_STAGES.length - 1;
        const isTerminalFailed = measured ? measured === "failed" || measured === "cancelled" : isFailure && currentStage === "completed";
        const completedTone = isTerminalFailed
          ? "text-red-400"
          : "text-green-400";
        const completedBg = isTerminalFailed
          ? "bg-red-500/20 border-red-500"
          : "bg-green-500/20 border-green-500";
        const completedLine = isTerminalFailed ? "bg-red-500" : "bg-green-500";

        const duration = stageDurations?.[stage.id] ?? (
          !stageContent?.[stage.id] && (isCompleted || isCurrent)
            ? getStageDuration(stage.id, phaseTiming, startedAt, finishedAt)
            : null);

        const stageLabel = measured === "cancelled" && stage.id === "completed" ? "Cancelled" :
          isTerminalFailed && stage.id === "completed"
            ? "Completed (Failed)"
            : stage.label;

        return (
          <div
            key={stage.id}
            role="group"
            aria-label={stage.label}
            className="flex gap-2"
          >
            {/* Left column: indicator + line */}
            <div className="flex flex-col items-center">
              <div
                className="relative z-10 shrink-0"
                role="img"
                aria-label={`${stage.label}: ${
                  measured ?? (isTerminalFailed || (isCurrent && isFailed)
                    ? "failed"
                    : isCompleted
                      ? "completed"
                      : isCurrent
                        ? "active"
                        : "not recorded")
                }`}
              >
                {isCompleted ? (
                  <div
                    className={`flex h-5 w-5 items-center justify-center rounded-full border ${completedBg}`}
                  >
                    {isTerminalFailed ? (
                      <AlertCircle className="h-2.5 w-2.5 text-red-400" />
                    ) : (
                      <Check className={`h-2.5 w-2.5 ${completedTone}`} />
                    )}
                  </div>
                ) : isCurrent ? (
                  <div className="flex h-5 w-5 items-center justify-center rounded-full border border-blue-500 bg-blue-500/20">
                    {isFailed || isCancelled ? (
                      <AlertCircle className="h-2.5 w-2.5 text-red-400" />
                    ) : (
                      <Clock className="h-2.5 w-2.5 animate-pulse text-blue-400" />
                    )}
                  </div>
                ) : (
                  <div className="h-5 w-5 rounded-full border border-muted-foreground/20 bg-muted" />
                )}
              </div>

              {!isLast && (
                <div
                  className={`min-h-6 w-0.5 flex-1 ${isCompleted ? completedLine : "bg-muted"}`}
                />
              )}
            </div>

            {/* Stage info */}
            <div className="min-w-0 flex-1 pb-2.5">
              <StageDetails enabled={collapsible && Boolean(stageContent?.[stage.id])} open={isCurrent || stage.id === "environment_setup"} heading={
              <div className="flex items-baseline justify-between gap-2">
                {(isCompleted || isCurrent) && onStageClick && !(collapsible && stageContent?.[stage.id]) ? (
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={() => onStageClick(stage.id)}
                    className={cn(
                      "-mx-1.5 -my-0.5 h-auto px-1.5 py-0.5 text-left text-xs font-medium leading-normal hover:bg-muted hover:underline hover:underline-offset-2",
                      isCompleted || isCurrent
                        ? "text-foreground"
                        : "text-muted-foreground",
                    )}
                  >
                    {stageLabel}
                  </Button>
                ) : (
                  <p
                    className={cn(
                      "flex items-center gap-1 text-xs font-medium",
                      isCompleted || isCurrent
                        ? "text-foreground"
                        : "text-muted-foreground",
                    )}
                  >
                    {stageLabel}
                    {collapsible && stageContent?.[stage.id] && (
                      <ChevronRight aria-hidden="true" className="h-3 w-3 shrink-0 text-muted-foreground" />
                    )}
                  </p>
                )}

                {duration && (
                  <span className="shrink-0 text-[10px] tabular-nums text-muted-foreground">
                    {duration}
                  </span>
                )}
              </div>
              }>
              {stageContent?.[stage.id] && (
                <div className="mt-2 space-y-2">{stageContent[stage.id]}</div>
              )}
              </StageDetails>
            </div>
          </div>
        );
      })}

      {!stageStates && (isCancelled || currentStage === "cleanup") && (
        <div className="flex gap-2">
          <div className="relative z-10 shrink-0">
            <div
              className={`flex h-5 w-5 items-center justify-center rounded-full border ${
                isCancelled
                  ? "border-red-500 bg-red-500/20"
                  : "border-gray-500 bg-gray-500/20"
              }`}
            >
              <AlertCircle
                className={`h-2.5 w-2.5 ${isCancelled ? "text-red-400" : "text-gray-400"}`}
              />
            </div>
          </div>
          <div className="flex-1 pt-0">
            <p className="text-xs font-medium text-foreground">
              {isCancelled ? "Cancelled" : "Cleanup"}
            </p>
            <p className="text-[10px] text-muted-foreground">
              {isCancelled ? "Trial was cancelled" : "Cleaning up resources"}
            </p>
          </div>
        </div>
      )}
    </div>
  );
}

function StageDetails({ enabled, open, heading, children }: { enabled: boolean; open: boolean; heading: ReactNode; children: ReactNode }) {
  const [expanded, setExpanded] = useState<boolean | null>(null);
  if (!enabled) return <>{heading}{children}</>;
  return <details open={expanded ?? open} onToggle={event => setExpanded(event.currentTarget.open)} className="[&[open]>summary_svg]:rotate-90">
    <summary className="flex cursor-pointer list-none items-center gap-1 rounded hover:bg-muted focus-visible:outline [&::-webkit-details-marker]:hidden">
      <div className="min-w-0 flex-1">{heading}</div>
    </summary>
    {children}
  </details>;
}
