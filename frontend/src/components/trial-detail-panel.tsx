"use client";

import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { markOpenIntent } from "@/lib/open-intent";
import dynamic from "next/dynamic";
import Image from "next/image";
import useSWR from "swr";
import useSWRMutation from "swr/mutation";
import {
  ResizableDrawer,
  DrawerHeader,
  DrawerTitle,
  DrawerDescription,
} from "@/components/ui/resizable-drawer";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  FileText,
  FolderOpen,
  AlertCircle,
  CircleSlash,
  ChevronDown,
  ChevronUp,
  ChevronLeft,
  ChevronRight,
  RotateCcw,
  Loader2,
  Microscope,
  CheckCircle2,
  XCircle,
  ExternalLink,
  Play,
  Trash2,
  MoreHorizontal,
} from "lucide-react";
import { cn, encodeExperimentRouteParam, urlWithSearch } from "@/lib/utils";
import { isEditableTarget } from "@/lib/editable-target";
import {
  formatLineRange,
  parseLineRange,
} from "@/lib/line-range";
import { experimentModelLabel } from "@/lib/experiment-agent-grouping";
import { sameFilePath } from "@/lib/file-path";

/**
 * Decode the drawer location from the live URL. The panel keeps the URL
 * current via replaceState, which never refreshes Next's useSearchParams
 * hook — so live reads must come straight from location.
 */
function liveLocation(): DrawerLocation {
  return decodeDrawerLocation(
    new URLSearchParams(typeof window === "undefined" ? "" : window.location.search)
  );
}
import { Skeleton } from "@/components/ui/skeleton";
import { QaAssessmentReport } from "@/components/qa-report/qa-assessment-report";
import {
  feedbackRequestInit,
  type FeedbackRecord,
} from "@/components/qa-report/types";
import { TimingBreakdownBar } from "@/components/timing-breakdown-bar";
import { CodeBlock } from "@/components/code-block";
import type { Trial, Task } from "@/lib/types";
import { isAgentTrial } from "@/lib/types";
import { stepIdsLabel } from "@/lib/trajectory-segments";
import {
  costEstimateMarks,
  formatCostUsd,
  formatTokenCount,
  hasDisplayableCostUsd,
  sumTaskTrialCost,
} from "@/lib/format";
import {
  formatPartialRewardBadgeValue,
  formatRewardPercent,
  formatRewardValue,
  getMatrixStatus,
  getRewardStyle,
  STATUS_CONFIG,
  STATUS_GLYPH_BOX,
} from "@/lib/status-config";
import { HarborStageTimeline } from "@/components/harbor-stage-timeline";
import { HarborStageBadge } from "@/components/harbor-stage-badge";
import { QueueKeyIcon } from "@/components/queue-key-icon";
import { StatusIcon } from "@/components/status-icon";
import {
  QaCostSuffix,
  VerifierCostSuffix,
} from "@/components/qa-cost-suffix";
import {
  isActiveTrialStatus,
  isLiveQaTrial,
  isWorkerOwnedTrialStatus,
  taskHasActiveVerdict,
} from "@/lib/job-status";
import {
  isAnalysisStatusActive,
  preloadTrial,
  useTrial,
} from "@/lib/use-trial";
import { embeddedCtrfSummary } from "@/lib/verifier-results";
import { fetcher } from "@/lib/api";
import {
  decodeDrawerLocation,
  drawerTabParam,
  resolveDrawerView,
  type DrawerLocation,
  type DrawerSection,
  type RunView,
} from "@/lib/run-playback";
import { useRunPlayback } from "@/lib/use-run-playback";

const RunOutcome = dynamic(() => import("@/components/run-playback").then(m => m.RunOutcome), { ssr: false });

const RunPlayback = dynamic(() => import("@/components/run-playback").then(m => m.RunPlayback), { ssr: false });

const LiveTranscriptPanel = dynamic(
  () =>
    import("@/components/live-transcript-panel").then(
      (mod) => mod.LiveTranscriptPanel
    ),
  {
    ssr: false,
    loading: () => <DrawerPanelLoading label="Loading live transcript..." />,
  }
);

const ArtifactsViewer = dynamic(
  () =>
    import("@/components/artifacts-viewer").then((mod) => mod.ArtifactsViewer),
  {
    ssr: false,
    loading: () => <DrawerPanelLoading label="Loading artifacts..." />,
  }
);

const TrajectoryViewer = dynamic(
  () =>
    import("@/components/trajectory-viewer").then(
      (mod) => mod.TrajectoryViewer
    ),
  {
    ssr: false,
    loading: () => <DrawerPanelLoading label="Loading trajectory..." />,
  }
);

function DrawerPanelLoading({ label }: { label: string }) {
  return (
    <div className="flex min-h-[160px] flex-col gap-2 p-4">
      <span className="sr-only">{label}</span>
      <Skeleton className="h-4 w-1/3" />
      <Skeleton className="h-3 w-full" />
      <Skeleton className="h-3 w-11/12" />
      <Skeleton className="h-3 w-4/5" />
      <Skeleton className="mt-2 h-24 w-full rounded-lg" />
    </div>
  );
}

function ActiveTabContent({
  active,
  ...props
}: React.ComponentProps<typeof TabsContent> & { active: boolean }) {
  return active ? <TabsContent {...props} /> : null;
}

interface TrialDetailPanelProps {
  isOpen: boolean;
  onClose: () => void;
  trial: Trial | null;
  task: Task | null;
  orderedTrials?: Trial[] | null;
  trialIndex?: number | null;
  trialGroups?: Array<{
    agent: string;
    model: string | null;
    trials: Trial[];
  }> | null;
  onNavigate?: (trial: Trial, trialIndex: number | null) => void;
  onNavigateToTask?: () => void;
  onRetry?: (taskIds?: string[]) => void | Promise<void>;
  onRetried: (previousTrialId: string, replacement: Trial) => void;
  onDelete?: (trial: Trial, task: Task | null) => Promise<void>;
  apiBaseUrl?: string;
  allowRetry?: boolean;
  /** When false, hide all QA reports and actions. */
  showAnalysis?: boolean;
  /** Public QA uses shared reports without private logs or run controls. */
  readOnly?: boolean;
  /** Load fields omitted from compact task and experiment trial rows. */
  requireTrialDetail?: boolean;
  allowDelete?: boolean;
  /** Render content only without ResizableDrawer wrapper */
  contentOnly?: boolean;
  /** Slot rendered alongside the navigation row — e.g. a "hide task" toggle. */
  paneAction?: React.ReactNode;
}

// Hardcoded feature flag: shows the per-trial "Re-run analysis" button on the
// QA card. Testing-only for now — flip to false to hide it.
const ENABLE_RERUN_ANALYSIS_BUTTON = true;

// The QA assessment card. It renders before any analysis exists, so the
// run button is reachable. It shows queued/running state with elapsed
// time. The drawer's useTrial resource makes the result appear without
// reopening the drawer.
function TrialAnalysisCard({
  trial: trialProp,
  taskQaInProgress,
  apiBaseUrl,
  readOnly = false,
  actionsReady,
  onQueued,
  activeQaTrial,
  onOpenActiveQaTrial,
  onFeedback,
}: {
  trial: Trial;
  taskQaInProgress: boolean;
  apiBaseUrl: string;
  readOnly?: boolean;
  actionsReady: boolean;
  onQueued?: () => void | Promise<void>;
  activeQaTrial: Trial | null;
  onOpenActiveQaTrial?: (qaTrial: Trial) => void;
  onFeedback?: (record: FeedbackRecord) => Promise<void>;
}) {
  const trial = trialProp;
  const [queuing, setQueuing] = useState(false);
  const [queueError, setQueueError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  // QA is task-scoped: the rerun creates one qa trial that grades every
  // trial, and never stamps this row's analysis_status. Reading that field
  // alone showed "No analysis yet" while the run was live.
  const trialAnalysisInProgress = isAnalysisStatusActive(trial.analysis_status);
  const inProgress = trialAnalysisInProgress || taskQaInProgress;
  // Tick the elapsed timer once a second while in progress.
  useEffect(() => {
    if (!inProgress) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [inProgress]);

  const hasAnalysis =
    Boolean(trial.analysis_status || trial.analysis) || inProgress;
  // Always SHOW the button when the feature is on. Disable it (with the
  // reason) when a run cannot be queued right now. The action targets this
  // selected agent trial, while active task-level QA blocks concurrent runs.
  const showQueueButton = !readOnly && ENABLE_RERUN_ANALYSIS_BUTTON;
  // Mirrors _ANALYSIS_CLAIM_TTL_MINUTES: past the lease the backend treats
  // the worker as dead and allows a re-run, so the button must too. A
  // running row with no start time has no live lease, and the backend
  // allows a re-run there as well.
  const runStale =
    trial.analysis_status === "running" &&
    (trial.analysis_started_at == null ||
      now - new Date(trial.analysis_started_at).getTime() > 35 * 60_000);
  // Mirror the backend guards, so the button is disabled with the reason
  // instead of failing the request.
  let queueBlockedReason: string | null = null;
  if (!actionsReady) {
    queueBlockedReason = "Loading latest trial state.";
  } else if (taskQaInProgress) {
    queueBlockedReason = "Task QA is already running";
  } else if (trialAnalysisInProgress && !runStale) {
    queueBlockedReason =
      trial.analysis_status === "running"
        ? "QA is running for this trial"
        : "QA is queued for this trial";
  } else if (trial.status !== "success" && trial.status !== "failed") {
    queueBlockedReason = "QA requires a finished trial";
  }

  if (!hasAnalysis && !showQueueButton) return null;

  const queueRun = async () => {
    if (readOnly || queuing || !actionsReady || queueBlockedReason) return;
    setQueuing(true);
    setQueueError(null);
    try {
      const res = await fetch(
        `${apiBaseUrl}/trials/${trial.id}/analysis/rerun`,
        { method: "POST" }
      );
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(
          data.detail || data.error || "Failed to queue analysis"
        );
      }
      // The server created one task-level QA trial. Refresh the task-open
      // resource so its active_qa_trial becomes the source of truth.
      await onQueued?.();
    } catch (err) {
      setQueueError(
        err instanceof Error ? err.message : "Failed to queue analysis"
      );
    } finally {
      setQueuing(false);
    }
  };

  // Progress line: elapsed time while running, waiting state while queued.
  let progressLine: string | null = null;
  if (inProgress) {
    if (trial.analysis_status === "running") {
      if (trial.analysis_started_at) {
        const secs = Math.max(
          0,
          Math.floor(
            (now - new Date(trial.analysis_started_at).getTime()) / 1000
          )
        );
        progressLine = `${Math.floor(secs / 60)}m ${secs % 60}s`;
      }
    }
  }

  // Analysis wall-clock, shown in the report header.
  let analysisDuration: string | null = null;
  if (trial.analysis_started_at && trial.analysis_finished_at) {
    const secs = Math.round(
      (new Date(trial.analysis_finished_at).getTime() -
        new Date(trial.analysis_started_at).getTime()) /
        1000
    );
    if (Number.isFinite(secs) && secs >= 0) {
      analysisDuration =
        secs >= 60 ? `${Math.floor(secs / 60)}m ${secs % 60}s` : `${secs}s`;
    }
  }

  const showReport =
    hasAnalysis && !inProgress && !!trial.analysis?.classification;

  return (
    <Card
      className={
        inProgress
          ? "border-blue-500/30 bg-blue-500/5"
          : showReport
            ? // The report card carries the verdict tint itself.
              "border-border/60"
            : "border-slate-500/30 bg-slate-500/5"
      }
    >
      <CardContent className="px-4 py-3">
        {showQueueButton && (
          <div className="mb-2 flex justify-end">
            <button
              type="button"
              disabled={queuing || queueBlockedReason !== null}
              onClick={queueRun}
              className="text-muted-foreground hover:text-foreground rounded border px-1.5 py-0.5 text-[10px] font-medium disabled:cursor-not-allowed disabled:opacity-50"
              title={
                queueBlockedReason ??
                "Reruns task QA: re-analyzes every eligible trial and regenerates verdict."
              }
            >
              {queuing
                ? "Queuing…"
                : hasAnalysis
                  ? "Re-run Trajectory analysis"
                  : "Run analysis"}
            </button>
          </div>
        )}
        {queueError && (
          <p className="mb-2 text-[11px] text-red-500">{queueError}</p>
        )}
        {/* Disabled buttons swallow hover, so the title alone never shows.
            While a run is in progress the card body already says so. */}
        {!readOnly && queueBlockedReason && !inProgress && (
          <p className="text-muted-foreground mb-2 text-[11px]">
            {queueBlockedReason}
          </p>
        )}
        {showReport ? (
          <>
            {trial.analysis_status === "failed" && trial.analysis_error && (
              <p className="mb-2 text-xs text-red-500">
                Analysis failed: {trial.analysis_error}
              </p>
            )}
            <QaAssessmentReport
              key={`${trial.id}:${trial.analysis?._graded_by}`}
              reportTrialId={trial.analysis?._graded_by}
              classification={trial.analysis!.classification!}
              subtype={trial.analysis?.subtype}
              rootCause={trial.analysis?.root_cause || trial.analysis?.evidence}
              recommendation={trial.analysis?.recommendation}
              evidence={
                // Without a root cause the evidence IS the lead text above;
                // passing it again would render the same prose twice.
                trial.analysis?.root_cause &&
                trial.analysis?.evidence &&
                trial.analysis.evidence !== trial.analysis.root_cause
                  ? trial.analysis.evidence
                  : null
              }
              actionItems={trial.analysis?.action_items}
              duration={analysisDuration}
              raw={trial.analysis}
              onFeedback={readOnly ? undefined : onFeedback}
            />
            {!readOnly && trial.analysis?._graded_by && (
              <p className="mt-2 flex flex-wrap items-baseline gap-x-2 font-mono text-[11px]">
                <a
                  href={`/tasks/${encodeURIComponent(trial.task_id)}?trial=${encodeURIComponent(trial.analysis._graded_by)}&tab=trajectory`}
                  className="text-muted-foreground hover:text-foreground underline decoration-dotted underline-offset-2"
                >
                  graded by {trial.analysis._graded_by}
                </a>
                {(trial.analysis._graded_at_steps?.length ?? 0) > 0 && (
                  // A plain anchor on purpose: the trajectory viewer captures
                  // the #step- fragment at mount, which document navigation
                  // guarantees and in-place drawer switching does not.
                  <a
                    href={`/tasks/${encodeURIComponent(trial.task_id)}?trial=${encodeURIComponent(trial.analysis._graded_by)}&tab=trajectory#step-${trial.analysis._graded_at_steps![0]}`}
                    title="Open the QA run's trajectory at the steps that judged this trial"
                    className="text-muted-foreground hover:text-foreground underline decoration-dotted underline-offset-2"
                  >
                    at {stepIdsLabel(trial.analysis._graded_at_steps!)}
                  </a>
                )}
              </p>
            )}
          </>
        ) : (
          <div className="flex items-start gap-3">
            {inProgress ? (
              <Microscope className="mt-0.5 h-5 w-5 animate-pulse text-blue-500" />
            ) : hasAnalysis ? (
              <XCircle className="mt-0.5 h-5 w-5 text-slate-500" />
            ) : (
              <Microscope className="mt-0.5 h-5 w-5 text-slate-400" />
            )}
            <div className="min-w-0 flex-1">
              {inProgress ? (
                <div className="flex flex-col gap-1">
                  <span className="font-mono text-sm font-bold">
                    {trial.analysis_status === "running"
                      ? "Analyzing"
                      : trial.analysis_status
                        ? "Analysis queued"
                        : "Task QA running"}
                  </span>
                  {progressLine && (
                    <span className="text-muted-foreground text-xs">
                      {progressLine}
                    </span>
                  )}
                  {!readOnly && activeQaTrial && onOpenActiveQaTrial && (
                    <button
                      type="button"
                      onClick={() => onOpenActiveQaTrial(activeQaTrial)}
                      className="text-muted-foreground hover:text-foreground self-start font-mono text-[11px] underline decoration-dotted underline-offset-2"
                    >
                      Open QA run
                    </button>
                  )}
                </div>
              ) : hasAnalysis ? (
                // Analysis state exists but produced no report (e.g. failed
                // before the classifier returned).
                <div className="flex flex-col gap-1">
                  <span className="font-mono text-sm font-bold">Analysis</span>
                  {trial.analysis_status === "failed" &&
                  trial.analysis_error ? (
                    <span className="text-xs text-red-500">
                      Analysis failed: {trial.analysis_error}
                    </span>
                  ) : (
                    <span className="text-muted-foreground text-xs">
                      No QA report produced.
                    </span>
                  )}
                </div>
              ) : (
                <div className="flex flex-col gap-1">
                  <span className="font-mono text-sm font-bold">
                    No analysis yet
                  </span>
                </div>
              )}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export function buildOddishRunCommand(trial: Trial, task: Task): string {
  const parts: string[] = ["oddish run"];

  // `--task <task_id>` re-queues trials against the existing server-side
  // task, so it works even when the user doesn't have the task files locally.
  // Tasks have a many-to-many relationship with experiments (see
  // `task_experiments` in oddish/db/models.py), so we pass `--experiment`
  // explicitly to make sure new trials land in the experiment the user was
  // viewing rather than the task's oldest linked experiment.
  if (task.id) {
    parts.push(`--task ${task.id}`);
  }

  if (task.experiment_id) {
    parts.push(`--experiment ${task.experiment_id}`);
  }

  // Preserve every server-owned Harbor environment, even when the UI has no
  // branded badge for it yet. Legacy rows can still fall back to a recognized
  // sandbox job provider.
  const trialEnvironment = normalizeRunEnvironment(trial.environment);
  const runEnvironment = trialEnvironment ?? getSandboxBackend(trial)?.id;
  if (runEnvironment) {
    parts.push(`-e ${runEnvironment}`);
  }

  if (trial.agent) {
    parts.push(`-a ${trial.agent}`);
  }

  if (trial.model) {
    parts.push(`-m ${trial.queue_key || trial.model}`);
  }

  if (trial.reasoning_effort) {
    const effort = trial.reasoning_effort.replace(/'/g, "'\\''");
    parts.push(`--agent-kwarg 'reasoning_effort=${effort}'`);
  }
  return parts.join(" ");
}

function normalizeRunEnvironment(
  environment: string | null | undefined
): string | null {
  const normalized = environment?.trim().toLowerCase();
  return normalized && /^[a-z0-9][a-z0-9-]*$/.test(normalized)
    ? normalized
    : null;
}

function getQueueSnapshotItems(trial: Trial): string[] {
  const queueInfo = trial.queue_info;
  if (!queueInfo) return [];

  return [
    queueInfo.position != null
      ? `Queue #${queueInfo.position} of ${queueInfo.queued_count}`
      : null,
    queueInfo.ahead != null ? `${queueInfo.ahead} ahead` : null,
    `${queueInfo.running_count} running`,
    `${queueInfo.concurrency_limit} slots`,
  ].filter((value): value is string => Boolean(value));
}

function hasLiveQueueSnapshot(trial: Trial): boolean {
  return isActiveTrialStatus(trial.status);
}

type SandboxBackendId =
  | "daytona"
  | "modal"
  | "archil"
  | "ec2"
  | "numinous"
  | "thunder";

type SandboxBackend = {
  id: SandboxBackendId;
  label: string;
  logoSrc?: string;
  logoWidth?: number;
  // When true the logo is a full square mark and fills the badge edge to edge
  // (no white plate, no padding), e.g. an app-icon style logo.
  logoFill?: boolean;
  href?: string;
};

const SANDBOX_BACKENDS: Record<
  SandboxBackendId,
  Omit<SandboxBackend, "href">
> = {
  daytona: {
    id: "daytona",
    label: "Daytona",
    logoSrc: "/daytona-logotype.svg",
    logoWidth: 50,
  },
  modal: {
    id: "modal",
    label: "Modal",
    logoSrc: "/modal-logo-icon.png",
    logoWidth: 10,
  },
  archil: {
    id: "archil",
    label: "Archil",
  },
  ec2: {
    id: "ec2",
    label: "EC2",
  },
  numinous: {
    id: "numinous",
    label: "Numinous Cloud",
    logoSrc: "/numinous-logo.png",
    logoFill: true,
  },
  thunder: {
    id: "thunder",
    label: "Thunder Compute",
    logoSrc: "/thunder-compute-logo.svg",
    logoFill: true,
  },
};

function normalizeSandboxBackend(
  provider: string | null | undefined
): SandboxBackendId | null {
  const normalized = provider?.trim().toLowerCase();
  if (
    normalized === "daytona" ||
    normalized === "modal" ||
    normalized === "archil" ||
    normalized === "ec2" ||
    normalized === "numinous" ||
    normalized === "thunder"
  ) {
    return normalized;
  }
  return null;
}

function getSandboxBackend(trial: Trial): SandboxBackend | null {
  const trialBackendId = normalizeSandboxBackend(trial.environment);
  const sandboxJob = trial.jobs?.find((job) =>
    Boolean(normalizeSandboxBackend(job.provider))
  );
  const backendId =
    trialBackendId ?? normalizeSandboxBackend(sandboxJob?.provider);
  if (!backendId) return null;

  const backend = SANDBOX_BACKENDS[backendId];
  if (backendId === "daytona" && sandboxJob?.external_id) {
    return {
      ...backend,
      href: `https://app.daytona.io/dashboard/sandboxes?sandboxId=${encodeURIComponent(
        sandboxJob.external_id
      )}`,
    };
  }

  return backend;
}

function SandboxBackendBadge({ backend }: { backend: SandboxBackend }) {
  const content = (
    <>
      {backend.logoSrc &&
        (backend.logoFill ? (
          <span className="inline-flex h-4 w-4 items-center justify-center overflow-hidden rounded-sm">
            <Image
              src={backend.logoSrc}
              alt={`${backend.label} logo`}
              width={16}
              height={16}
              className="h-full w-full object-cover"
            />
          </span>
        ) : (
          <span className="inline-flex h-4 items-center justify-center rounded-sm bg-white px-1">
            <Image
              src={backend.logoSrc}
              alt={`${backend.label} logo`}
              width={backend.logoWidth ?? 10}
              height={10}
              className="h-2.5 w-auto object-contain"
            />
          </span>
        ))}
      {backend.id !== "daytona" && (
        <span className="text-muted-foreground font-sans text-[9px] font-semibold tracking-wide uppercase">
          {backend.label}
        </span>
      )}
    </>
  );
  const className =
    "border-border bg-muted/40 inline-flex shrink-0 items-center gap-1 rounded-md border px-1 py-0.5";

  if (backend.href) {
    return (
      <a
        href={backend.href}
        target="_blank"
        rel="noopener noreferrer"
        className={className}
        title={`Open ${backend.label} sandbox`}
      >
        {content}
      </a>
    );
  }

  return (
    <span className={className} title={`${backend.label} sandbox`}>
      {content}
    </span>
  );
}

/**
 * Short "org/repo" label for the Harbor source a trial executed against.
 * Strips a leading git+ and the github host so the drawer shows the ACTUAL
 * source (default, a blessed variant, or an arbitrary override), not a
 * hardcoded fork. Falls back to "harbor" for legacy rows with no stamped source.
 */
function harborRepoLabel(source: string | null | undefined): string {
  if (!source) return "harbor";
  return (
    source
      .replace(/^git\+/, "")
      .replace(/^https?:\/\/github\.com\//i, "")
      .replace(/\.git$/, "") || "harbor"
  );
}

export function TrialDetailPanel({
  isOpen,
  onClose,
  trial: selectedTrial,
  task,
  orderedTrials,
  trialIndex,
  trialGroups,
  onNavigate,
  onNavigateToTask,
  onRetry,
  onRetried,
  onDelete,
  apiBaseUrl = "/api",
  allowRetry = true,
  showAnalysis = true,
  readOnly = false,
  requireTrialDetail = true,
  allowDelete = false,
  contentOnly = false,
  paneAction,
}: TrialDetailPanelProps) {
  // Public result pages carry compact trial rows. Read the shared task to
  // fill in report evidence without using the private trial-detail route.
  const { data: publicTask } = useSWR<Task>(
    isOpen && readOnly && showAnalysis && task?.id
      ? `${apiBaseUrl}/tasks/${encodeURIComponent(task.id)}`
      : null,
    fetcher,
    { refreshInterval: 30000 }
  );
  const taskQaInProgress = taskHasActiveVerdict(
    readOnly ? (publicTask ?? task) : task
  );
  const loadTrialDetail = !readOnly && requireTrialDetail;
  const {
    data: refreshedTrial,
    error: trialDetailError,
    isValidating: isValidatingTrialDetail,
    mutate: revalidateTrial,
  } = useTrial(isOpen && loadTrialDetail ? selectedTrial?.id : null, {
    apiBaseUrl,
  });
  const previousTaskQaRef = useRef({
    taskId: task?.id ?? null,
    inProgress: taskQaInProgress,
  });

  // A task-level QA trial writes analysis onto the selected agent trial when
  // it settles. The agent trial is already terminal, so its own status cannot
  // keep useTrial polling. Refetch it once when the task QA lifecycle ends;
  // SWR still owns the request, cache key, deduplication, and retry policy.
  useEffect(() => {
    const previous = previousTaskQaRef.current;
    const current = {
      taskId: task?.id ?? null,
      inProgress: taskQaInProgress,
    };
    previousTaskQaRef.current = current;

    if (
      isOpen &&
      loadTrialDetail &&
      selectedTrial?.id &&
      previous.taskId === current.taskId &&
      previous.inProgress &&
      !current.inProgress
    ) {
      void revalidateTrial();
    }
  }, [
    isOpen,
    loadTrialDetail,
    revalidateTrial,
    selectedTrial?.id,
    task?.id,
    taskQaInProgress,
  ]);
  const canonicalTrial = readOnly
    ? (publicTask?.trials?.find((row) => row.id === selectedTrial?.id) ?? null)
    : refreshedTrial?.id === selectedTrial?.id
      ? refreshedTrial
      : null;
  const trialDetailFailed =
    loadTrialDetail && canonicalTrial === null && trialDetailError != null;
  const actionsReady = !loadTrialDetail || canonicalTrial !== null;
  const trial = canonicalTrial ?? selectedTrial;
  const verifierSummary = embeddedCtrfSummary(trial?.result);
  const finished = trial != null && ["success", "failed"].includes(trial.status);
  const filesUrl = trial ? `${apiBaseUrl}/trials/${trial.id}/files` : "";
  // A stored descriptor lets the index request start alongside trial details;
  // compact rows without one wait for the authoritative record.
  const playback = useRunPlayback(
    isOpen && finished && (trial.playback != null || actionsReady) ? filesUrl : null,
    trial?.playback,
  );
  const { trigger: preparePlayback, isMutating: preparingPlayback, error: preparationError } = useSWRMutation(
    trial && !readOnly ? `${apiBaseUrl}/trials/${trial.id}/playback/rebuild` : null,
    async (url: string) => {
      await fetcher(url, { method: "POST" });
      await revalidateTrial();
    },
  );
  const showLive =
    !readOnly &&
    trial != null &&
    (isWorkerOwnedTrialStatus(trial.status) || trial.status === "retrying");
  // QA votes require authenticated routes and an experiment anchor. Public
  // share drawers use a different apiBaseUrl, so they do not render controls.
  const feedbackExperimentId =
    !readOnly && apiBaseUrl === "/api"
      ? (trial?.experiment_id ?? null)
      : null;
  async function handleQaFeedback(record: FeedbackRecord): Promise<void> {
    if (!feedbackExperimentId || !trial) {
      throw new Error("QA feedback is unavailable for this trial");
    }
    await fetcher(
      `/api/experiments/${encodeExperimentRouteParam(feedbackExperimentId)}/feedback`,
      feedbackRequestInit(record, trial.id)
    );
  }

  // One owner for where the drawer points: section, Run view, file and
  // observation. Explicit selections and Back/Forward beat the defaults.
  const [location, setLocation] = useState<DrawerLocation>(liveLocation);
  const view = resolveDrawerView(location, {
    running: showLive,
    playback: finished ? playback.available : false,
  });

  // Selecting the trial is not the moment someone asks for its trajectory:
  // the drawer opens on Overview, and `ActiveTabContent` renders null for every
  // other view, so `TrajectoryViewer` -- a dynamic import -- does not mount
  // until the trace is chosen. Stamping only the trial click would fold however
  // long they read the summary into the trajectory's load time, or age out
  // past MAX_INTENT_AGE_MS and lose the chunk download the stamp exists to
  // capture. The intent map overwrites by key, so this supersedes the trial
  // click while leaving it correct for a `?tab=trajectory` deep link, where
  // the viewer really does mount with the drawer.
  function selectView(section: DrawerSection, runView?: RunView) {
    if (runView === "trace" && trial) markOpenIntent("ui.trajectory.open", trial.id);
    setLocation(current => ({
      ...current,
      section,
      runView: runView ?? current.runView,
    }));
  }
  const [showFullError, setShowFullError] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const hydratedFromUrl = useRef(false);

  // Hydrate from the URL on first open. Reads the live URL, not the
  // useSearchParams snapshot: replaceState never refreshes that hook, so on
  // a remount (e.g. the drawer's pane layout changing) the snapshot is the
  // stale page-load query and would reset the panel to where the user
  // started instead of where they are.
  useEffect(() => {
    if (!isOpen || hydratedFromUrl.current) return;
    hydratedFromUrl.current = true;
    setLocation(liveLocation());
  }, [isOpen]);

  // Browser Back/Forward restores the drawer location it recorded.
  useEffect(() => {
    if (!isOpen) return;
    const restore = () => setLocation(liveLocation());
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, [isOpen]);

  // The file browser reports every selection (clicks, resolved links and
  // cleared selections). A different file drops the line anchor — the old
  // range would otherwise highlight arbitrary lines of the new file.
  const handleSelectedFileChange = useCallback((path: string | null, options?: { preserveLines: boolean }) => {
    setLocation(current =>
      current.filePath === path
        ? current
        : {
            ...current,
            filePath: path,
            // A shortened link resolving to its full path is the same file.
            lines: options?.preserveLines || sameFilePath(current.filePath, path) ? current.lines : null,
          }
    );
  }, []);

  // Navigating to a different trial keeps the file path (attempts share
  // layouts, and comparing the same file across attempts is the point) but
  // drops the line anchor and observation — they addressed the previous
  // trial's content.
  const lastTrialIdRef = useRef<string | null>(trial?.id ?? null);
  useEffect(() => {
    const id = trial?.id ?? null;
    if (id && lastTrialIdRef.current && id !== lastTrialIdRef.current) {
      setLocation(current => ({ ...current, lines: null, observationId: null }));
    }
    lastTrialIdRef.current = id;
  }, [trial?.id]);

  const viewSection = view?.section ?? null;
  const viewRunView = view?.runView ?? null;
  // Sync the location to the URL (without triggering router navigation),
  // using the existing `tab` values. Based on the live URL rather than the
  // useSearchParams snapshot: replaceState doesn't refresh that hook, and the
  // experiment view writes its own params (task/trial/taskFile/taskLines) the
  // same way — a stale base would silently wipe them.
  useEffect(() => {
    if (!isOpen || !hydratedFromUrl.current) return;
    const current = new URLSearchParams(window.location.search);
    const next = new URLSearchParams(window.location.search);

    // This panel is mounted only for the displayed trial. Keep that resource
    // identity alongside tab/file state so a concurrent route-state commit
    // cannot turn a trial drawer URL back into a task-only URL.
    if (trial?.id) next.set("trial", trial.id);

    // While discovery decides the default, the URL keeps its explicit tab.
    if (viewSection && viewRunView)
      next.set("tab", drawerTabParam(viewSection, viewRunView, location.fileFilter));
    else if (!location.section) next.delete("tab");

    // ?file=/?lines= describe the file browser; ?observation= the frame a
    // trace link left from. Other views drop them from the URL while the
    // location keeps them, so returning restores and re-writes them.
    const files = viewSection === "files";
    const setOrDelete = (key: string, value: string | null) =>
      value ? next.set(key, value) : next.delete(key);
    setOrDelete("file", files ? location.filePath : null);
    setOrDelete("lines", files ? location.lines : null);
    setOrDelete("observation", viewSection === "run" ? location.observationId : null);

    if (next.toString() !== current.toString()) {
      const url = urlWithSearch(next.toString());
      window.history.replaceState(window.history.state, "", url);
    }
  }, [isOpen, viewSection, viewRunView, location, trial?.id]);

  // Frame captions link to the trace step that produced the frame. The link
  // is a new history entry, so Back returns to the inspected scene or file.
  function traceHref(step: number, observationId: string) {
    const params = new URLSearchParams(window.location.search);
    if (trial) params.set("trial", trial.id);
    params.set("tab", "trajectory");
    params.set("observation", observationId);
    params.delete("file");
    params.delete("lines");
    return `${urlWithSearch(params.toString())}#step-${step}`;
  }
  function openTrace(step: number, observationId: string) {
    if (trial) markOpenIntent("ui.trajectory.open", trial.id);
    // Files already records its selected image and filter in the URL. The
    // scene clock advances independently, so save its departure frame now.
    if (view?.section === "run" && view.runView === "scene") {
      const departure = new URLSearchParams(window.location.search);
      departure.set("tab", "playback");
      departure.set("observation", observationId);
      window.history.replaceState(window.history.state, "", urlWithSearch(departure.toString()));
    }
    window.history.pushState(window.history.state, "", traceHref(step, observationId));
    setLocation(current => ({ ...current, section: "run", runView: "trace", observationId }));
  }

  // The scene mounts when first shown for a trial attempt and then stays
  // mounted until the drawer closes or moves to another attempt.
  const sceneKey = trial ? `${trial.id}:${trial.attempts}` : null;
  const sceneShown = view?.section === "run" && view.runView === "scene";
  const [mountedScene, setMountedScene] = useState<string | null>(null);
  useEffect(() => {
    if (!isOpen) setMountedScene(null);
    else if (sceneShown) setMountedScene(sceneKey);
  }, [isOpen, sceneShown, sceneKey]);
  const sceneMounted = isOpen && sceneKey !== null && (sceneShown || mountedScene === sceneKey);
  // Sum the navigable trials for this view (version-scoped in both callers),
  // not task.trials, which on the task page spans every version.
  const taskCost = sumTaskTrialCost(orderedTrials);

  // Agent rows only: the generic retry endpoint refuses qa/audit kinds, so
  // offering the button on their drawers would only ever render its 400.
  const showRetry =
    !readOnly &&
    allowRetry &&
    Boolean(trial && isAgentTrial(trial)) &&
    (trial?.status === "failed" || trial?.status === "success");
  const canRetry = actionsReady && showRetry;
  const showDelete =
    !readOnly && allowDelete && Boolean(onDelete) && Boolean(trial);
  const canDelete = actionsReady && showDelete;
  const handleRetry = async () => {
    if (!trial || retrying || !canRetry) return;
    setRetrying(true);
    setRetryError(null);

    try {
      const res = await fetch(`${apiBaseUrl}/trials/${trial.id}/retry`, {
        method: "POST",
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || data.error || "Failed to retry trial");
      }

      try {
        const { trial_id: replacementId } = (await res.json()) as {
          trial_id: string;
        };
        // Retry creates a new row. Load it through the drawer's shared resource
        // so the replacement never inherits the old attempt's results or logs.
        const replacement = await preloadTrial(apiBaseUrl, replacementId);
        onRetried(trial.id, replacement);
      } catch (err) {
        onClose();
        throw err;
      } finally {
        await onRetry?.(task ? [task.id] : undefined);
      }
    } catch (err) {
      setRetryError(err instanceof Error ? err.message : "Failed to retry");
    } finally {
      setRetrying(false);
    }
  };

  const handleDelete = async () => {
    if (!trial || !onDelete || deleting || !canDelete) return;
    setDeleting(true);
    setDeleteError(null);

    try {
      await onDelete(trial, task);
      setDeleteDialogOpen(false);
      onClose();
    } catch (err) {
      setDeleteError(
        err instanceof Error ? err.message : "Failed to delete trial"
      );
    } finally {
      setDeleting(false);
    }
  };

  const STAGE_FILE_MAP: Record<string, string> = {
    starting: "agent/oracle.txt",
    trial_started: "agent/oracle.txt",
    environment_setup: "agent/setup/stdout.txt",
    agent_running: "agent",
    verification: "verifier/test-stdout.txt",
    completed: "verifier/test-stdout.txt",
  };

  const handleTimelineStageClick = (stageId: string) => {
    const filePath = STAGE_FILE_MAP[stageId] ?? null;
    // A different file drops the old line anchor with it.
    setLocation(current => ({
      ...current,
      section: "files",
      fileFilter: "all",
      filePath,
      lines: current.filePath === filePath ? current.lines : null,
    }));
  };

  // Reset state when panel closes
  useEffect(() => {
    if (!isOpen) {
      setLocation(decodeDrawerLocation(new URLSearchParams()));
      setShowFullError(false);
      setRetrying(false);
      setRetryError(null);
      setDeleteDialogOpen(false);
      setDeleting(false);
      setDeleteError(null);
      hydratedFromUrl.current = false;
    }
  }, [isOpen]);

  const orderedList = useMemo(
    () => orderedTrials ?? task?.trials ?? [],
    [orderedTrials, task?.trials]
  );
  const activeQaTrial =
    task?.active_qa_trial ?? task?.trials?.find(isLiveQaTrial) ?? null;
  const resolvedIndex =
    typeof trialIndex === "number" && trialIndex >= 0
      ? trialIndex
      : trial
        ? orderedList.findIndex((item) => item.id === trial.id)
        : -1;
  const hasNavigation = orderedList.length > 1 && resolvedIndex >= 0;
  // Can navigate to task if at first trial and callback exists
  const canGoToTask = onNavigateToTask && resolvedIndex === 0;
  const canGoPrev = hasNavigation && resolvedIndex > 0;
  const canGoNext = hasNavigation && resolvedIndex < orderedList.length - 1;

  const navigateTo = useCallback(
    (nextIndex: number) => {
      if (!onNavigate) return;
      const nextTrial = orderedList[nextIndex];
      if (!nextTrial) return;
      onNavigate(nextTrial, nextIndex);
    },
    [onNavigate, orderedList]
  );

  useEffect(() => {
    if (!isOpen || !hasNavigation || !onNavigate) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (isEditableTarget(event.target)) return;
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        if (canGoPrev) {
          navigateTo(resolvedIndex - 1);
        } else if (canGoToTask) {
          onNavigateToTask?.();
        }
      } else if (event.key === "ArrowRight" && canGoNext) {
        event.preventDefault();
        navigateTo(resolvedIndex + 1);
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [
    isOpen,
    hasNavigation,
    onNavigate,
    onNavigateToTask,
    canGoPrev,
    canGoNext,
    canGoToTask,
    resolvedIndex,
    navigateTo,
  ]);

  if (!trial || !task) {
    return null;
  }
  const trialStatus = getMatrixStatus(
    trial.status,
    trial.reward,
    trial.error_message
  );
  const runView = view?.section === "run" ? view.runView : null;
  const sceneVisible = runView === "scene";
  const trialDetailErrorContent = (
    <div className="p-4 sm:p-6">
      <Alert variant="destructive">
        <AlertTitle>Trial details could not be loaded</AlertTitle>
        <AlertDescription className="flex flex-wrap items-center gap-2">
          <span>
            This tab needs the authoritative trial record before it can decide
            which stored resources exist.
          </span>
          <Button
            type="button"
            variant="secondary"
            size="sm"
            className="h-7"
            onClick={() => void revalidateTrial()}
            disabled={isValidatingTrialDetail}
          >
            {isValidatingTrialDetail ? "Retrying…" : "Retry"}
          </Button>
        </AlertDescription>
      </Alert>
    </div>
  );
  const trialStatusConfig = STATUS_CONFIG[trialStatus];
  const TrialStatusIcon = trialStatusConfig.icon;
  const showQueueSnapshot =
    hasLiveQueueSnapshot(trial) && getQueueSnapshotItems(trial).length > 0;
  const sandboxBackend = getSandboxBackend(trial);
  const daytonaSandboxUrl =
    sandboxBackend?.id === "daytona" ? (sandboxBackend.href ?? null) : null;

  const resolvedGroups =
    trialGroups && trialGroups.length > 0
      ? trialGroups
      : [
          {
            agent: trial.agent,
            model: trial.model ?? null,
            trials: orderedList,
          },
        ];
  const currentGroupIndex = resolvedGroups.findIndex((group) =>
    group.trials.some((groupTrial) => groupTrial.id === trial.id)
  );
  const currentGroup =
    currentGroupIndex >= 0 ? resolvedGroups[currentGroupIndex] : null;
  const currentGroupTrials = currentGroup?.trials ?? [];
  const currentGroupTrialIndex = currentGroupTrials.findIndex(
    (groupTrial) => groupTrial.id === trial.id
  );

  const navigateToGroupTrial = (groupIndex: number) => {
    if (!onNavigate || !currentGroup) return;
    const nextTrial = currentGroup.trials[groupIndex];
    if (!nextTrial) return;
    const nextIndex = orderedList.findIndex((item) => item.id === nextTrial.id);
    if (nextIndex < 0) return;
    onNavigate(nextTrial, nextIndex);
  };

  const handleOpenActiveQaTrial = (qaTrial: Trial) => {
    if (isWorkerOwnedTrialStatus(qaTrial.status) || qaTrial.status === "retrying")
      selectView("run", "live");
    else selectView("overview");
    const index = orderedList.findIndex((item) => item.id === qaTrial.id);
    onNavigate?.(qaTrial, index >= 0 ? index : null);
  };

  const content = (
    <>
      <DrawerHeader className="border-border shrink-0 border-b px-4 py-2 sm:px-6">
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 pr-16">
          <div className="min-w-0">
            <DrawerTitle className="truncate font-mono text-sm">
              {trial.name}
              {showAnalysis && trial.task_version != null && (
                <span className="text-muted-foreground ml-2 text-xs">
                  v{trial.task_version}
                </span>
              )}
            </DrawerTitle>
            <DrawerDescription>{trial.id}</DrawerDescription>
            <div className="text-muted-foreground flex items-center gap-2 text-xs">
              <QueueKeyIcon
                queueKey={trial.provider}
                model={trial.model}
                agent={trial.agent}
                size={11}
              />
              <span>{trial.agent}</span>
              <span
                className="max-w-64 truncate"
                title={experimentModelLabel(trial.model, trial.reasoning_effort)}
              >
                {experimentModelLabel(trial.model, trial.reasoning_effort)}
              </span>
              {(trial.kind ?? "agent") !== "agent" && <span>{trial.kind}</span>}
            </div>
          </div>
          <div className="flex items-center gap-3 text-xs">
            <span
              className="flex items-center gap-1.5"
              title={`Reward: ${formatRewardPercent(trial.reward)}`}
            >
              <TrialStatusIcon className="size-3.5" />
              <span>
                {trialStatusConfig.shortLabel} · {formatRewardValue(trial.reward)}
              </span>
            </span>
            {(showRetry || showDelete || daytonaSandboxUrl) && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="size-7"
                    aria-label="Trial actions"
                  >
                    <MoreHorizontal className="size-4" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  {showRetry && (
                    <DropdownMenuItem
                      onSelect={() => void handleRetry()}
                      disabled={!canRetry || retrying}
                    >
                      <RotateCcw className="size-4" />
                      {retrying ? "Retrying…" : "Retry trial"}
                    </DropdownMenuItem>
                  )}
                  {daytonaSandboxUrl && (
                    <DropdownMenuItem asChild>
                      <a
                        href={daytonaSandboxUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        <ExternalLink className="size-4" />
                        Open sandbox
                      </a>
                    </DropdownMenuItem>
                  )}
                  {showDelete && (
                    <DropdownMenuItem
                      disabled={!canDelete || deleting}
                      className="text-destructive"
                      onSelect={() => {
                        setDeleteError(null);
                        setDeleteDialogOpen(true);
                      }}
                    >
                      <Trash2 className="size-4" />
                      Delete trial
                    </DropdownMenuItem>
                  )}
                </DropdownMenuContent>
              </DropdownMenu>
            )}
          </div>
        </div>
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-1">
            {paneAction}
            {hasNavigation && (
              <>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  onClick={() => {
                    if (canGoPrev) {
                      navigateTo(resolvedIndex - 1);
                    } else if (canGoToTask) {
                      onNavigateToTask?.();
                    }
                  }}
                  disabled={!canGoPrev && !canGoToTask}
                  className="h-7 w-7"
                  aria-label={
                    canGoPrev
                      ? "Previous trial"
                      : canGoToTask
                        ? "View task"
                        : "Previous"
                  }
                  title={
                    canGoPrev
                      ? "Previous trial"
                      : canGoToTask
                        ? "View task"
                        : "Previous"
                  }
                >
                  <ChevronLeft className="h-4 w-4" />
                </Button>

                {currentGroupTrials.map((groupTrial, index) => {
                  const groupStatus = getMatrixStatus(
                    groupTrial.status,
                    groupTrial.reward,
                    groupTrial.error_message,
                  );
                  const groupConfig = STATUS_CONFIG[groupStatus];
                  const isPartial = groupStatus === "partial";
                  const partialLabel = isPartial
                    ? formatPartialRewardBadgeValue(groupTrial.reward)
                    : null;
                  const isActive = index === currentGroupTrialIndex;
                  return (
                    <Button
                      key={groupTrial.id}
                      type="button"
                      variant="ghost"
                      size="icon"
                      onClick={() => navigateToGroupTrial(index)}
                      className={cn(
                        "flex items-center justify-center p-0 leading-none transition hover:opacity-90",
                        STATUS_GLYPH_BOX,
                        groupConfig.matrixClass,
                        isPartial
                          ? "font-mono text-[9.5px] font-semibold tracking-[-0.02em] tabular-nums"
                          : "",
                        isActive
                          ? "ring-primary/60 ring-offset-background ring-2 ring-offset-1"
                          : "",
                      )}
                      style={getRewardStyle(groupTrial.reward)}
                      aria-label={`Trial ${index + 1} ${groupConfig.shortLabel}`}
                      title={`${groupConfig.shortLabel} • Trial ${index + 1}`}
                    >
                      {isPartial ? (
                        partialLabel
                      ) : (
                        <StatusIcon status={groupStatus} />
                      )}
                    </Button>
                  );
                })}
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  onClick={() => navigateTo(resolvedIndex + 1)}
                  disabled={!canGoNext}
                  className="h-7 w-7"
                  aria-label="Next trial"
                  title="Next trial"
                >
                  <ChevronRight className="h-4 w-4" />
                </Button>
              </>
            )}
          </div>
          {sandboxBackend && <SandboxBackendBadge backend={sandboxBackend} />}
        </div>
        {retryError && (
          <p role="alert" className="text-destructive text-xs">
            {retryError}
          </p>
        )}
      </DrawerHeader>

      <Tabs
        value={view?.section === "overview" ? "summary" : view?.section ?? ""}
        onValueChange={value => selectView(value === "summary" ? "overview" : value as DrawerSection)}
        className="flex flex-1 flex-col overflow-hidden"
      >
        {view && <div className="border-border border-b px-4 sm:px-6">
          <TabsList className="h-10 gap-0 border-0 bg-transparent p-0 sm:h-12">
            <TabsTrigger
              value="summary"
              className="data-[state=active]:border-primary rounded-none px-3 text-xs data-[state=active]:border-b-2 data-[state=active]:bg-transparent sm:px-4 sm:text-sm"
            >
              <FileText className="mr-1 h-3.5 w-3.5 sm:mr-2 sm:h-4 sm:w-4" />
              Overview
            </TabsTrigger>
            <TabsTrigger value="run" className="data-[state=active]:border-primary rounded-none px-3 text-xs data-[state=active]:border-b-2 data-[state=active]:bg-transparent sm:px-4 sm:text-sm">
              <Play className="mr-1 size-4" />Run
            </TabsTrigger>
            <TabsTrigger
              value="files"
              className="data-[state=active]:border-primary rounded-none px-3 text-xs data-[state=active]:border-b-2 data-[state=active]:bg-transparent sm:px-4 sm:text-sm"
            >
              <FolderOpen className="mr-1 h-3.5 w-3.5 sm:mr-2 sm:h-4 sm:w-4" />
              Files
            </TabsTrigger>
          </TabsList>
        </div>}

        {runView && <div className="border-border flex shrink-0 items-center gap-2 border-b px-4 py-1.5 text-xs" aria-label="Run view">
          {(playback.available !== false || playback.preparation === "unchecked" || sceneVisible) && finished && <Button size="sm" variant={sceneVisible ? "secondary" : "ghost"} onClick={() => selectView("run", "scene")}>Scene and observations</Button>}
          <Button size="sm" variant={runView === "trace" ? "secondary" : "ghost"} onClick={() => selectView("run", "trace")}>Full agent trace</Button>
          {showLive && <Button size="sm" variant={runView === "live" ? "secondary" : "ghost"} onClick={() => selectView("run", "live")}>Live output</Button>}
        </div>}
        {finished && actionsReady && ["unchecked", "invalid"].includes(playback.preparation) && (
          <div className="border-border flex shrink-0 flex-wrap items-center gap-2 border-b px-4 py-2 text-xs" role="status">
            <span>{preparationError ? preparationError.message : playback.error?.message ?? "Playback has not been prepared for this run."}</span>
            {!readOnly && loadTrialDetail && <Button size="sm" variant="outline" disabled={preparingPlayback}
              onClick={() => { void preparePlayback(undefined, { throwOnError: false }); }}>
              {preparingPlayback ? "Preparing playback…" : "Prepare playback"}
            </Button>}
          </div>
        )}
        <div className="relative min-h-0 flex-1">
          {/* The scene stays mounted (paused, invisible) while other views of
              this trial cover it, so returning reuses its viewer. */}
          {sceneMounted && <div
            className={cn("absolute inset-0", sceneVisible ? "z-10 bg-background" : "invisible pointer-events-none")}
            aria-hidden={!sceneVisible}>
            <RunPlayback key={`${trial.id}:${trial.attempts}`} playback={playback} active={sceneVisible}
              observationId={location.observationId} agentTiming={trial.phase_timing?.agent_execution}
              traceHref={traceHref} onOpenTrace={openTrace} />
          </div>}
        <div className="h-full overflow-auto overscroll-contain">
          {!view && (trialDetailFailed ? trialDetailErrorContent : (
            <p role="status" className="p-6 text-sm">Loading run views…</p>
          ))}
          <ActiveTabContent
            active={view?.section === "overview"}
            value="summary"
            className="m-0 p-4 sm:p-6"
          >
            <div className="space-y-4 pb-4">
              <RunOutcome playback={playback} />
              {showQueueSnapshot && (
                <Card className="border-purple-500/30 bg-purple-500/5">
                  <CardHeader className="px-4 pt-2 pb-1">
                    <CardTitle className="text-muted-foreground text-[11px] font-semibold tracking-wider uppercase">
                      Queue Snapshot
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="px-4 pb-3">
                    <div className="flex flex-wrap gap-2">
                      {getQueueSnapshotItems(trial).map((item) => (
                        <span
                          key={item}
                          className="bg-background/60 text-foreground rounded border border-purple-500/20 px-2 py-1 font-mono text-[11px]"
                        >
                          {item}
                        </span>
                      ))}
                    </div>
                  </CardContent>
                </Card>
              )}
              {verifierSummary && verifierSummary.tests > 0 && (
                <div className="text-muted-foreground flex items-center gap-1.5 px-4 py-1 text-[11px]">
                  {verifierSummary.failed > 0 ? (
                    <XCircle className="h-3.5 w-3.5 text-red-500" />
                  ) : (
                    <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
                  )}
                  <span className="text-foreground/80 font-mono tabular-nums">
                    {verifierSummary.passed}/{verifierSummary.tests}
                  </span>
                  tests passed
                </div>
              )}
              {/* Analysis card: self-updating; also hosts the run/re-run
                  button so analysis can be started even when it never ran. */}
              {showAnalysis && (
                <TrialAnalysisCard
                  key={trial.id}
                  trial={trial}
                  taskQaInProgress={taskQaInProgress}
                  apiBaseUrl={apiBaseUrl}
                  readOnly={readOnly}
                  actionsReady={actionsReady}
                  activeQaTrial={activeQaTrial}
                  onFeedback={
                    feedbackExperimentId ? handleQaFeedback : undefined
                  }
                  onQueued={() => onRetry?.(task ? [task.id] : undefined)}
                  onOpenActiveQaTrial={
                    onNavigate ? handleOpenActiveQaTrial : undefined
                  }
                />
              )}

              {/* Execution Timeline - shows progress during running trials */}
              {trial.harbor_stage && (
                <Card>
                  <CardHeader className="px-4 pt-2 pb-1">
                    <CardTitle className="text-muted-foreground flex items-center justify-between text-[11px] font-semibold tracking-wider uppercase">
                      <span>Execution Timeline</span>
                      <HarborStageBadge stage={trial.harbor_stage} />
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="px-4 pb-2">
                    <HarborStageTimeline
                      currentStage={trial.harbor_stage}
                      status={trial.status}
                      isFailure={
                        trial.status === "failed" ||
                        Boolean(trial.error_message)
                      }
                      onStageClick={handleTimelineStageClick}
                      phaseTiming={trial.phase_timing}
                      startedAt={trial.started_at}
                      finishedAt={trial.finished_at}
                    />
                  </CardContent>
                </Card>
              )}

              {trial.harbor_sha && (
                <div className="text-muted-foreground px-4 py-1 text-[11px]">
                  Harbor:{" "}
                  <span className="font-mono">
                    {harborRepoLabel(trial.harbor_source)}@
                    {trial.harbor_sha.slice(0, 7)}
                  </span>
                </div>
              )}

              <TimingBreakdownBar
                createdAt={trial.created_at}
                startedAt={trial.started_at}
                finishedAt={trial.finished_at}
                compact
              />

              {/* Skip reason — a skipped trial carries its reason in
                  error_message, but it never ran, so render it as a neutral
                  note (CircleSlash + slate) rather than a red error card. */}
              {trial.error_message && trialStatus === "skipped" && (
                <Card className="border-slate-500/25 bg-slate-500/5">
                  <CardContent className="px-4 py-3">
                    <div className="flex items-start gap-2">
                      <CircleSlash className="mt-0.5 h-4 w-4 shrink-0 text-slate-500" />
                      <pre className="min-w-0 flex-1 font-mono text-sm wrap-break-word whitespace-pre-wrap text-slate-600 dark:text-slate-400">
                        {trial.error_message}
                      </pre>
                    </div>
                  </CardContent>
                </Card>
              )}

              {/* Error Card */}
              {trial.error_message && trialStatus !== "skipped" && (
                <Card className="border-red-500/30 bg-red-500/5">
                  <CardContent className="px-4 py-3">
                    <div className="flex items-start gap-2">
                      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-red-500" />
                      <div className="min-w-0 flex-1">
                        <pre className="font-mono text-sm wrap-break-word whitespace-pre-wrap text-red-600 dark:text-red-400">
                          {showFullError
                            ? trial.error_message
                            : trial.error_message.slice(0, 300)}
                          {trial.error_message.length > 300 &&
                            !showFullError &&
                            "..."}
                        </pre>
                        {trial.error_message.length > 300 && (
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            onClick={() => setShowFullError(!showFullError)}
                            className="mt-2 h-auto px-0 text-xs text-red-500/60 hover:text-red-600"
                          >
                            {showFullError ? (
                              <>
                                <ChevronUp className="h-3 w-3" />
                                Show less
                              </>
                            ) : (
                              <>
                                <ChevronDown className="h-3 w-3" />
                                Show full error
                              </>
                            )}
                          </Button>
                        )}
                      </div>
                    </div>
                  </CardContent>
                </Card>
              )}

              {(trial.cost_usd != null ||
                trial.input_tokens != null ||
                trial.output_tokens != null ||
                // A trial can be QA'd without the agent ever reporting a cost;
                // keep the section so its QA sidecar isn't hidden.
                hasDisplayableCostUsd(trial.qa_cost_usd) ||
                hasDisplayableCostUsd(trial.verifier_cost_usd)) && (
                <details className="border-border rounded-lg border px-4 py-2 text-xs">
                  <summary className="cursor-pointer font-medium">Execution details</summary>
                  <dl className="mt-2 grid grid-cols-[auto_1fr] items-baseline gap-x-4 gap-y-1.5">
                    <dt className="text-muted-foreground">Cost</dt>
                    <dd className="flex flex-wrap items-baseline gap-x-2 font-mono tabular-nums">
                      {hasDisplayableCostUsd(trial.cost_usd) ? (
                        <span>
                          {trial.cost_is_estimated ? "~" : ""}
                          {formatCostUsd(trial.cost_usd)}
                        </span>
                      ) : (
                        <span>—</span>
                      )}
                      {hasDisplayableCostUsd(trial.cost_usd) &&
                        taskCost.pricedCount > 1 &&
                        hasDisplayableCostUsd(taskCost.costUsd) &&
                        (() => {
                          const marks = costEstimateMarks(
                            taskCost.hasEstimated,
                            taskCost.hasNative
                          );
                          return (
                            <span className="text-muted-foreground">
                              of {marks.prefix}
                              {formatCostUsd(taskCost.costUsd)}
                              {marks.suffix} task
                            </span>
                          );
                        })()}
                      <QaCostSuffix
                        costUsd={trial.qa_cost_usd}
                        title="QA/analysis spend for this trial. Not included in the cost figure."
                      />
                      <VerifierCostSuffix costUsd={trial.verifier_cost_usd} />
                    </dd>
                    {(trial.input_tokens != null || trial.output_tokens != null) && (
                      <>
                        <dt className="text-muted-foreground">Tokens</dt>
                        <dd className="font-mono tabular-nums">
                          {formatTokenCount((trial.input_tokens ?? 0) + (trial.output_tokens ?? 0))}
                        </dd>
                      </>
                    )}
                  </dl>
                </details>
              )}

              {/* Equivalent retry command — hidden from public viewers */}
              {!readOnly && showAnalysis && (
                <div>
                  <p className="text-muted-foreground mb-1 text-[11px]">
                    Equivalent retry command, reconstructed.
                  </p>
                  <CodeBlock
                    code={buildOddishRunCommand(trial, task)}
                    language="bash"
                    maxHeight="none"
                    className="opacity-60 transition-opacity hover:opacity-100"
                  />
                </div>
              )}
            </div>
          </ActiveTabContent>

          <ActiveTabContent
            active={runView === "live" && showLive}
            value="run"
            className="m-0 h-full p-0"
          >
            <LiveTranscriptPanel
              key={trial.id}
              trialId={trial.id}
              agent={trial.agent}
              apiBaseUrl={apiBaseUrl}
            />
          </ActiveTabContent>

          <ActiveTabContent active={view?.section === "files"} value="files" className="m-0 h-full p-0">
            {trialDetailFailed ? trialDetailErrorContent : <ArtifactsViewer
              key={trial.id}
              filesUrl={filesUrl}
              artifactsOnly={location.fileFilter === "artifacts"}
              onArtifactsOnlyChange={value => setLocation(current => ({ ...current, fileFilter: value ? "artifacts" : "all" }))}
              trialId={trial.id}
              successfulAnalysisTrial={trial.status === "success" && ["qa", "qa_eval", "audit", "summarize"].includes(trial.kind ?? "agent")}
              filePath={location.filePath}
              selectedLines={parseLineRange(location.lines)}
              onSelectLinesChange={range => setLocation(current => ({ ...current, lines: range ? formatLineRange(range) : null }))}
              onSelectedFileChange={handleSelectedFileChange}
              playback={playback}
              onOpenScene={() => selectView("run", "scene")}
              traceHref={traceHref}
              onOpenTrace={openTrace}
            />}
          </ActiveTabContent>

          <ActiveTabContent
            active={runView === "trace"}
            value="run"
            className="m-0 h-full overflow-auto p-0"
          >
            {trialDetailFailed ? (
              trialDetailErrorContent
            ) : (
              trial.agent === "oracle" && trial.has_trajectory === false ? (
                <p className="p-6 text-sm text-muted-foreground">Oracle runs have no agent trace.</p>
              ) : <TrajectoryViewer
                agentTiming={trial.phase_timing?.agent_execution}
                agentRunning={trial.harbor_stage === "agent_running" && !trial.finished_at}
                trialId={trial.id}
                hasTrajectory={trial.has_trajectory}
                apiBaseUrl={apiBaseUrl}
                canRegenerateSummary={!readOnly && showAnalysis}
              />
            )}
          </ActiveTabContent>
        </div>
        </div>
      </Tabs>
    </>
  );

  const deleteDialog = showDelete ? (
    <AlertDialog
      open={deleteDialogOpen}
      onOpenChange={(open) => {
        if (!open && !deleting) {
          setDeleteDialogOpen(false);
          setDeleteError(null);
        }
      }}
    >
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete this trial?</AlertDialogTitle>
          <AlertDialogDescription>
            This permanently deletes the trial, its logs, and any stored
            artifacts. In-flight runs for this trial will be cancelled. This
            action cannot be undone.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {deleteError && (
          <Alert variant="destructive">
            <AlertTitle>Delete failed</AlertTitle>
            <AlertDescription>{deleteError}</AlertDescription>
          </Alert>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={deleting}>Cancel</AlertDialogCancel>
          <AlertDialogAction
            onClick={(event) => {
              event.preventDefault();
              void handleDelete();
            }}
            disabled={!canDelete || deleting}
            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
          >
            {deleting ? (
              <>
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                Deleting...
              </>
            ) : (
              "Delete trial"
            )}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  ) : null;

  if (contentOnly) {
    return (
      <div className="flex h-full flex-1 flex-col overflow-hidden">
        {content}
        {deleteDialog}
      </div>
    );
  }

  return (
    <>
      <ResizableDrawer
        open={isOpen}
        onOpenChange={(open) => !open && onClose()}
        defaultWidth={700}
        minWidth={420}
        maxWidth={900}
      >
        {content}
      </ResizableDrawer>
      {deleteDialog}
    </>
  );
}
