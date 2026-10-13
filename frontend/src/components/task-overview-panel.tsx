"use client";

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import useSWR from "swr";
import { ArrowUpRight, CheckCircle2, Loader2, SearchCode } from "lucide-react";

import {
  EXECUTION_LABELS,
  findingHref,
  isReviewableTrial,
  adversarialOutcome,
  adversarialUnsettled,
  attackReplay,
  REPLAY_AGENT,
  isAttackTrial,
  runReviewSummary,
  taskReviewStatus,
} from "@/lib/review";
import { cn, encodeExperimentRouteParam } from "@/lib/utils";
import { apiFetch, fetcher } from "@/lib/api";
import { Skeleton } from "@/components/ui/skeleton";
import { AnalysisProse } from "@/components/analysis-prose";
import { FindingList } from "@/components/qa-report/action-items";
import { CopyJsonButton } from "@/components/qa-report/copy-json-button";
import { FeedbackControl } from "@/components/qa-report/feedback-control";
import {
  feedbackRequestInit,
  type FeedbackRecord,
} from "@/components/qa-report/types";
import { FALLBACK_TOKEN, VERDICT_TOKENS, TIER_BADGE } from "@/components/qa-report/tokens";
import { TaskVerdictBadge } from "@/components/task-verdict-badge";
import { isActivePipelineStatus, isActiveTrialStatus } from "@/lib/job-status";
import { expandTrialParam } from "@/lib/trial-url";
import type {
  AnalysisClassification,
  PreTrialFinding,
  Task,
  Trial,
} from "@/lib/types";

// Matches ATTACKS_PER_LAUNCH in oddish/core/adversarial.py.
const ATTACKS_PER_LAUNCH = 4;

/** Harbor prefixes the full shell command, which embeds the whole prompt. */
function errorOutput(message: string) {
  return message.replace(/^(Command failed \(exit \d+\)): [\s\S]*?\nstdout: /, "$1\n");
}

export type StaticCheckState =
  | "unaudited"
  | "queued"
  | "running"
  | "failed"
  | "clean"
  | "findings";

/**
 * What to say for a task's source review. Empty findings mean three
 * different things depending on status: only `success` with no items is
 * genuinely "we looked and found nothing".
 */
export function staticCheckState(
  status: string | null | undefined,
  findingCount: number
): StaticCheckState {
  if (!status) return "unaudited";
  const normalized = status.toLowerCase();
  if (normalized === "pending" || normalized === "queued") return "queued";
  if (normalized === "running") return "running";
  if (normalized === "success") return findingCount > 0 ? "findings" : "clean";
  return "failed";
}

/** Problems first: a cheated success or broken task outranks a clean run. */
const CLASSIFICATION_ORDER: AnalysisClassification[] = [
  "BAD_SUCCESS",
  "BAD_FAILURE",
  "HARNESS_ERROR",
  "GOOD_FAILURE",
  "GOOD_SUCCESS",
];

/** A finding plus where it came from: the source review, execution review, or both. */
interface SourcedFinding extends PreTrialFinding {
  fromAudit: boolean;
  trials: Trial[];
}

function findingKey(item: PreTrialFinding): string {
  // Server ids are content hashes that include the analyzer source, so they
  // dedupe within one source only — the cross-source join is `links_to`.
  // Items without an id fall back to a content key.
  return item.id ?? `${item.tier ?? ""}|${item.title ?? ""}|${item.file ?? ""}`;
}

function classificationRank(trial: Trial): number {
  const classification = trial.analysis?.classification;
  if (!classification) return CLASSIFICATION_ORDER.length;
  const rank = CLASSIFICATION_ORDER.indexOf(classification);
  return rank >= 0 ? rank : CLASSIFICATION_ORDER.length;
}

/** The attacker's /logs/agent/adversarial_report.md: what the hole is. */
function AttackReport({ trialId, apiBaseUrl }: { trialId: string; apiBaseUrl: string }) {
  const { data, error } = useSWR(
    `${apiBaseUrl}/trials/${encodeURIComponent(trialId)}/files/agent/adversarial_report.md`,
    async (url: string) => {
      const res = await apiFetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.text();
    },
    { revalidateOnFocus: false }
  );
  if (error) return <p className="text-muted-foreground text-xs">No report saved. Open the run.</p>;
  if (data == null) return <p className="text-muted-foreground text-xs">Loading…</p>;
  // The attack prompt asks for a "Summary: " first line; older reports lack it.
  const summary = data.match(/^\s*\**summary\**:\**\s*(.+)$/im)?.[1];
  return (
    <div className="space-y-1 text-xs">
      {summary && <p className="text-foreground text-sm">{summary}</p>}
      <details>
        <summary className="text-muted-foreground cursor-pointer">Attacker&apos;s full report</summary>
        <div className="mt-1 max-h-64 overflow-auto">
          <AnalysisProse text={data} />
        </div>
      </details>
    </div>
  );
}

function trialLabel(trial: Trial): string {
  const model = trial.model?.split("/").pop();
  const label = trial.kind === "adversarial" ? "Adversarial QA" : trial.agent;
  return model ? `${label} · ${model}` : label;
}

/**
 * The task overview: the task's own QA (verdict + the source-review findings)
 * merged with the trial-level QA aggregated across the shown version's
 * trials, each finding and classification linking back to the trial that
 * surfaced it.
 */
export function TaskOverviewPanel({
  taskId,
  taskVersionId,
  experimentId,
  apiBaseUrl = "/api",
  readOnly = false,
  version,
  scopeTrials,
  showRuns = true,
  scopeLoading,
  verdictTask,
  checksFindings,
  checksTrialId,
  checksStatus,
  checksError,
  onRerunChecks,
  onAdversarialFindings,
  checksDisabledReason,
  checksRerunning,
  checksQueueError,
  checksLoading,
  checksLoadError,
  qaActive,
  onOpenTrial,
  onOpenSource,
  executionReviewAction,
  executionReviewError,
  experiments = [],
  className,
}: {
  executionReviewAction?: ReactNode;
  executionReviewError?: string | null;
  experiments?: { id: string; name: string }[];
  taskId: string | null;
  taskVersionId?: string | null;
  experimentId?: string | null;
  apiBaseUrl?: string;
  readOnly?: boolean;
  /** Version the pane is scoped to: a number pins, null deliberately
   *  aggregates every trial, and undefined means still resolving — the
   *  trial aggregation waits instead of briefly spanning all versions. */
  version?: number | null;
  /** The trials that belong to the host's context (an experiment drawer
   *  passes its own; the task page passes the version's). Trials outside
   *  this set still render, marked as from elsewhere. Null = no context. */
  scopeTrials?: Trial[] | null;
  /** False hides the runs section, for a task shared without any runs. */
  showRuns?: boolean;
  /** The host is still streaming its trial rows — an empty scope renders
   *  as loading, not as "no trials". */
  scopeLoading?: boolean;
  /** Render the QA verdict inline — for panes whose host shows no verdict
   *  card of its own (the side-by-side "Task definition" pane). */
  verdictTask?: Task | null;
  checksFindings?: PreTrialFinding[] | null;
  /** The audit trial behind `checksFindings`; votes on those anchor to it. */
  checksTrialId?: string | null;
  checksStatus?: string | null;
  checksError?: string | null;
  onRerunChecks?: () => void;
  /** A replay recorded its findings; the host refetches `checksFindings`. */
  onAdversarialFindings?: () => void;
  /** Why the audit rerun is unavailable for the shown version. */
  checksDisabledReason?: string | null;
  checksRerunning: boolean;
  checksQueueError?: string | null;
  /** The checks state is still being fetched: an absent status must not
   * read as "unaudited" — a Run click on that misread wipes real findings. */
  checksLoading?: boolean;
  checksLoadError?: string | null;
  /** Task-level QA in flight — keeps the trial list polling until it lands. */
  qaActive?: boolean;
  /**
   * Open a trial in the caller's own context (drawer / panel). Returns
   * false when the trial isn't addressable there; the panel then falls
   * back to the task page deep link.
   */
  onOpenTrial?: (trial: Trial) => boolean;
  onOpenSource?: (item: PreTrialFinding) => void;
  className?: string;
}) {
  const router = useRouter();
  const [attackQueuing, setAttackQueuing] = useState(false);
  const [attackError, setAttackError] = useState<string | null>(null);
  const [attackConfirming, setAttackConfirming] = useState(false);
  const searchParams = useSearchParams();
  const selectedFinding = searchParams.get("finding");
  const selectedTrialId = expandTrialParam(searchParams.get("trial"), taskId);
  const versionKnown = version !== undefined;
  // The fetch waits for the version so it can scope server-side — a task
  // carries trials across many versions and experiments, and every full row
  // ships its whole analysis payload.
  const trialsKey =
    taskId && versionKnown && !readOnly
      ? `${apiBaseUrl}/tasks/${taskId}/trials?include_adversarial=true${
          version !== null ? `&version=${version}` : ""
        }`
      : null;
  const { data: trials, error: trialsError, mutate: refreshTrials } = useSWR<Trial[]>(
    trialsKey,
    fetcher,
    {
      revalidateOnFocus: false,
      refreshInterval: (data) => {
        const anyAnalysisLive = (data ?? []).some((trial) =>
          isActivePipelineStatus(trial.analysis_status) ||
          (trial.kind === "adversarial" && isActiveTrialStatus(trial.status))
        );
        // A replay is queued, and its findings recorded, by hooks that run
        // after the trial settles; keep polling until both have run.
        return anyAnalysisLive || adversarialUnsettled(data ?? []) || qaActive
          ? 15000
          : 0;
      },
    }
  );

  // Import publishes findings after execution. Fetch once more when task QA
  // settles so stopping the polling timer cannot leave the last pending result.
  useEffect(() => {
    if (qaActive === false) void refreshTrials();
  }, [qaActive, refreshTrials]);

  // Host rows can include superseded and non-agent trials; filter them here too.
  const scoped = useMemo(() => {
    if (scopeTrials == null) return null;
    return scopeTrials.filter((trial) => isReviewableTrial(trial));
  }, [scopeTrials]);
  const fetchedById = useMemo(
    () => new Map((trials ?? []).map((trial) => [trial.id, trial])),
    [trials]
  );
  // Show every trial of the version. The verdict is computed over all of
  // them, so a shorter list can hide the evidence behind it.
  const displayTrials = useMemo(() => {
    if (scoped == null) return trials?.filter(isReviewableTrial) ?? null;
    const inScope = new Set(scoped.map((trial) => trial.id));
    // Until the host's rows have loaded, rows from elsewhere would render
    // without their mark -- hold them back.
    const elsewhere = scopeLoading
      ? []
      : (trials ?? []).filter(
          (trial) => !inScope.has(trial.id) && isReviewableTrial(trial)
        );
    return [
      ...scoped.map((trial) => fetchedById.get(trial.id) ?? trial),
      ...elsewhere,
    ];
  }, [scoped, scopeLoading, fetchedById, trials]);
  // Null until the host's rows have loaded, so nothing is marked too early.
  const foreignIds = useMemo(() => {
    if (scoped == null || scopeLoading) return null;
    const inScope = new Set(scoped.map((trial) => trial.id));
    return new Set(
      (trials ?? [])
        .filter((trial) => !inScope.has(trial.id) && isReviewableTrial(trial))
        .map((trial) => trial.id)
    );
  }, [scoped, scopeLoading, trials]);
  const versionTrials = useMemo(() => {
    if (version === undefined) return [];
    const all = displayTrials ?? [];
    if (version === null) return all;
    return all.filter((trial) => trial.task_version === version);
  }, [displayTrials, version]);

  const { classificationCounts, unanalyzedCount, mergedFindings, qaTrials } =
    useMemo(() => {
      const byKey = new Map<string, SourcedFinding>();
      const addTrial = (row: SourcedFinding, trial: Trial) => {
        if (!row.trials.some((t) => t.id === trial.id)) row.trials.push(trial);
      };
      for (const item of checksFindings ?? []) {
        const key = findingKey(item);
        byKey.set(key, {
          ...item,
          id: key,
          fromAudit: item.source !== "post_trial" && item.source !== "adversarial",
          trials: [],
        });
      }
      const counts = new Map<AnalysisClassification, number>();
      const withQa: Trial[] = [];
      let unanalyzed = 0;
      for (const trial of versionTrials) {
        if (!trial.analysis && !trial.analysis_status) {
          if (!foreignIds?.has(trial.id)) unanalyzed += 1;
          continue;
        }
        withQa.push(trial);
        const analysis = trial.analysis;
        if (!analysis || trial.analysis_status !== "success") continue;
        if (!foreignIds?.has(trial.id)) {
          counts.set(
            analysis.classification,
            (counts.get(analysis.classification) ?? 0) + 1
          );
        }
        // Exploitation assessments are the trial→audit-finding join: an
        // exploiting trial belongs on the audit row's "seen in" list. A
        // not-exploited assessment only says the classifier looked — skip it.
        for (const assessment of analysis.exploitation ?? []) {
          if (!assessment.exploited || !assessment.links_to) continue;
          const audited = byKey.get(assessment.links_to);
          if (!audited) continue;
          audited.exploited = true;
          addTrial(audited, trial);
        }
        for (const item of analysis.action_items ?? []) {
          // A post-trial item that links to an audit finding is the same
          // defect seen from the trial side — fold it into that row. Ids
          // can't make this join: the server hash includes the source.
          const linked = item.links_to ? byKey.get(item.links_to) : undefined;
          const existing = linked ?? byKey.get(findingKey(item));
          if (existing) {
            addTrial(existing, trial);
            // One exploiting trial marks the finding exploited.
            if (item.exploited) existing.exploited = true;
          } else {
            const key = findingKey(item);
            byKey.set(key, {
              ...item,
              id: key,
              fromAudit: false,
              trials: [trial],
            });
          }
        }
      }
      withQa.sort(
        (a, b) =>
          classificationRank(a) - classificationRank(b) ||
          Number(foreignIds?.has(a.id) ?? false) -
            Number(foreignIds?.has(b.id) ?? false) ||
          a.created_at.localeCompare(b.created_at)
      );
      return {
        classificationCounts: counts,
        unanalyzedCount: unanalyzed,
        mergedFindings: Array.from(byKey.values()),
        qaTrials: withQa,
      };
    }, [versionTrials, checksFindings, foreignIds]);

  // The rows handed to FindingList carry only copy-safe fields — its
  // per-item copy button serializes the row as-is, so the trial objects
  // stay behind in the lookup map and the copy gets ids.
  const findingItems = useMemo(
    () =>
      mergedFindings.map(({ fromAudit, trials: sources, ...item }) => ({
        ...item,
        from_audit: fromAudit,
        trial_ids: sources.map((t) => t.id),
      })),
    [mergedFindings]
  );
  const findingSourcesById = useMemo(
    () => new Map(mergedFindings.map((f) => [f.id ?? "", f])),
    [mergedFindings]
  );
  const experimentRuns = versionTrials.filter(
    (trial) => !foreignIds?.has(trial.id)
  );
  const additionalRuns = versionTrials.filter((trial) =>
    foreignIds?.has(trial.id)
  );
  const runsByExperiment = new Map<string | null, Trial[]>();
  for (const trial of additionalRuns) {
    const id = trial.experiment_id ?? null;
    const group = runsByExperiment.get(id);
    if (group) group.push(trial);
    else runsByExperiment.set(id, [trial]);
  }

  const taskTrialHref = (trial: Trial): string | null => {
    if (!taskId || readOnly) return null;
    const params = new URLSearchParams();
    if (trial.task_version_id) params.set("version", trial.task_version_id);
    params.set("trial", trial.id);
    return `/tasks/${taskId}?${params.toString()}`;
  };

  // Audit and QA runs live in a hidden analysis experiment, so link them here.
  const analysisTrialLink = (
    trialId: string | null | undefined,
    label: string
  ) =>
    trialId && taskId && !readOnly ? (
      <a
        href={`/tasks/${encodeURIComponent(taskId)}?trial=${encodeURIComponent(trialId)}`}
        target="_blank"
        rel="noopener noreferrer"
        className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1 font-mono text-[10px] underline decoration-dotted underline-offset-2"
      >
        {label}
        <ArrowUpRight className="h-3 w-3 shrink-0" aria-hidden="true" />
      </a>
    ) : null;

  const openTrial = (trial: Trial) => {
    if (onOpenTrial?.(trial)) return;
    const href = taskTrialHref(trial);
    if (href) router.push(href);
  };

  const renderFindingSources = (item: PreTrialFinding) => {
    const sourced = findingSourcesById.get(item.id ?? "");
    if (!sourced) return null;
    if (sourced.source === "adversarial") {
      return (
        <div className="mt-1 flex flex-wrap items-center gap-1.5">
          <span className="text-muted-foreground shrink-0 font-mono text-[10px] tracking-widest">
            SEEN IN
          </span>
          {analysisTrialLink(sourced.replay_trial_id, "Exploit replay")}
        </div>
      );
    }
    if (!sourced.fromAudit && !(sourced.trials?.length > 0)) return null;
    return (
      <div className="mt-1 flex flex-wrap items-center gap-1.5">
        <span className="text-muted-foreground shrink-0 font-mono text-[10px] tracking-widest">
          SEEN IN
        </span>
        {sourced.fromAudit ? (
          <span className="border-border text-muted-foreground inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px]">
            <SearchCode className="h-3 w-3 shrink-0" aria-hidden="true" />
            Pre-trial audit
          </span>
        ) : null}
        {(sourced.trials ?? []).map((trial) => {
          const foreign = foreignIds?.has(trial.id) ?? false;
          const evidence =
            trial.analysis?.exploitation?.find(
              (assessment) =>
                assessment.exploited && assessment.links_to === item.id
            )?.exploit_evidence ??
            trial.analysis?.action_items?.find(
              (finding) =>
                finding.id === item.id || finding.links_to === item.id
            )?.exploit_evidence;
          return (
            <div key={trial.id} className="min-w-0">
              <button
                type="button"
                onClick={() => openTrial(trial)}
                className={cn(
                  "border-border text-muted-foreground hover:text-foreground hover:border-foreground/40 inline-flex max-w-full min-w-0 items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px] transition-colors",
                  foreign && "border-dashed"
                )}
                title={
                  foreign
                    ? `Open trial ${trial.name} — ran outside this experiment`
                    : `Open trial ${trial.name}`
                }
              >
                <span className="min-w-0 truncate">{trialLabel(trial)}</span>
                <ArrowUpRight className="h-3 w-3 shrink-0" aria-hidden="true" />
              </button>
              {evidence && (
                <p className="text-muted-foreground mt-1 text-xs break-words whitespace-pre-wrap">
                  {evidence}
                </p>
              )}
            </div>
          );
        })}
      </div>
    );
  };

  const checkState = staticCheckState(
    checksStatus,
    checksFindings?.length ?? 0
  );
  const checksStateUnknown = Boolean(checksLoading || checksLoadError);
  // Only a live run blocks the button. A stale "queued" row must stay
  // re-queueable: re-queue is the backend's recovery path for queued jobs
  // that never got picked up.
  const auditRunning = (checksStatus ?? "").toLowerCase() === "running";
  // Audits run on the default version; a disabled button names the version
  // on screen instead of one this view never ran.
  // A finished audit is final for its version; only an unfinished or failed
  // one can be run again.
  const auditFinished =
    !checksStateUnknown &&
    (checkState === "clean" || checkState === "findings");
  // Trial findings count too, so a clean audit is not "Passed" until the
  // version's trials have loaded.
  const auditPassed =
    auditFinished &&
    versionKnown &&
    displayTrials != null &&
    !trialsError &&
    findingItems.length === 0;
  const auditVersion = checksDisabledReason
    ? version
    : verdictTask?.current_version;

  // Votes persist through the hosted API only; a public share pane has no
  // feedback route, so it renders no controls.
  const canVote = !readOnly && apiBaseUrl === "/api" && Boolean(taskId);
  const postFeedback = async (
    trialId: string | undefined,
    record: FeedbackRecord
  ) => {
    if (!trialId) throw new Error("No trial to record this vote against");
    await fetcher(
      `${apiBaseUrl}/tasks/${taskId}/feedback`,
      feedbackRequestInit(record, trialId)
    );
  };
  // An audit finding votes against the audit trial; a trial-only finding
  // against the first trial that reported it. Audits that predate the
  // recorded trial id have nothing to vote against.
  const findingVoteTrialId = (findingId: string) => {
    const sourced = findingSourcesById.get(findingId);
    return sourced?.fromAudit
      ? (checksTrialId ?? undefined)
      : sourced?.trials[0]?.id;
  };
  const handleFindingFeedback = (record: FeedbackRecord) =>
    postFeedback(
      record.target.kind === "action_item"
        ? findingVoteTrialId(record.target.id)
        : undefined,
      record
    );

  const verdictReview = verdictTask ? taskReviewStatus(verdictTask) : null;
  const verdictReportId = verdictTask?.verdict?._graded_by;

  const mustFixCount = findingItems.filter(
    (item) => (item.tier ?? item.severity) === "must_fix"
  ).length;
  const findingsSummary =
    mustFixCount > 0
      ? `${mustFixCount} Must fix`
      : findingItems.length > 0
        ? `${findingItems.length} finding${findingItems.length === 1 ? "" : "s"}`
        : checksLoading ||
            !versionKnown ||
            (displayTrials == null && !trialsError)
          ? "…"
          : checksLoadError || trialsError
            ? "Unavailable"
            : checkState === "queued"
              ? "Queued"
              : checkState === "running"
                ? "Running"
                : checkState === "failed"
                  ? "Couldn’t finish"
                  : checkState === "unaudited"
                    ? "Not checked"
                    : "No required fixes";

  const findingsBody = () => {
    if (checksLoading && findingItems.length === 0) {
      return (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-8 w-full rounded-lg" />
          <Skeleton className="h-8 w-full rounded-lg" />
          <Skeleton className="h-3 w-2/5" />
        </div>
      );
    }
    // The default tier copy narrates trial classification; these findings
    // speak to the task itself.
    const findingsList =
      findingItems.length > 0 ? (
        <FindingList
          items={findingItems}
          selectedFinding={selectedFinding}
          findingLink={
            !readOnly && taskId && version != null
              ? (item, file) => findingHref(taskId, version, item, file)
              : undefined
          }
          onOpenSource={onOpenSource}
          renderItemFooter={renderFindingSources}
          onFeedback={canVote ? handleFindingFeedback : undefined}
          canVoteOn={(item) => findingVoteTrialId(item.id ?? "") != null}
        />
      ) : null;
    if (checksLoadError) {
      // The audit state is unknown, but trial-side findings come from the
      // trials endpoint — show what survives under the error.
      return (
        <>
          <p className="font-mono text-[11px] break-all text-red-500">
            {checksLoadError}
          </p>
          {findingsList}
        </>
      );
    }
    return (
      <>
        {checkState === "failed" ? (
          <p className="font-mono text-[11px] break-all text-red-500">
            {checksError || "Pre-trial audit couldn’t finish."}
          </p>
        ) : checkState === "running" && findingItems.length === 0 ? (
          <div className="flex flex-col gap-2">
            <Skeleton className="h-8 w-full rounded-lg" />
            <Skeleton className="h-8 w-full rounded-lg" />
            <Skeleton className="h-3 w-2/5" />
          </div>
        ) : null}
        {findingsList}
      </>
    );
  };

  const trialQaBody = () => {
    if (!versionKnown) {
      // The scoping version hasn't resolved; aggregating now would span
      // every version. On a dead /detail it never will — say so.
      return checksLoadError ? (
        <p className="font-mono text-[11px] break-all text-red-500">
          Unable to resolve the task version to scope the trials.
        </p>
      ) : (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-8 w-full rounded-lg" />
          <Skeleton className="h-8 w-full rounded-lg" />
          <Skeleton className="h-3 w-2/5" />
        </div>
      );
    }
    // Without a host scope, the fetch defines the set — wait for it.
    // Scoped rows render immediately (or fail into the honest states below).
    if (displayTrials == null) {
      return trialsError ? (
        <p className="font-mono text-[11px] break-all text-red-500">
          Unable to load the task&apos;s trials.
        </p>
      ) : (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-8 w-full rounded-lg" />
          <Skeleton className="h-8 w-full rounded-lg" />
          <Skeleton className="h-3 w-2/5" />
        </div>
      );
    }
    if (versionTrials.length === 0) {
      // An empty scope while the host is still streaming its trial rows
      // is not an answer yet.
      if (scopeLoading) {
        return (
          <div className="flex flex-col gap-2">
            <Skeleton className="h-8 w-full rounded-lg" />
            <Skeleton className="h-8 w-full rounded-lg" />
            <Skeleton className="h-3 w-2/5" />
          </div>
        );
      }
      return null;
    }
    if (qaTrials.length === 0) return null;

    return (
      <>
        {qaActive ? (
          <p className="text-muted-foreground flex items-center gap-1.5 font-mono text-[11px]">
            Previous results
          </p>
        ) : null}
        <div className="flex flex-wrap items-center gap-1.5">
          {CLASSIFICATION_ORDER.map((classification) => {
            const count = classificationCounts.get(classification);
            if (!count) return null;
            const token = VERDICT_TOKENS[classification] ?? FALLBACK_TOKEN;
            const Icon = token.icon;
            return (
              <span
                key={classification}
                className={cn(
                  "inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px]",
                  token.chip,
                  token.accent
                )}
              >
                <Icon className="h-3 w-3" aria-hidden="true" />
                {count} {EXECUTION_LABELS[classification].toLowerCase()}
                {count === 1
                  ? ""
                  : classification === "GOOD_SUCCESS" ||
                      classification === "BAD_SUCCESS"
                    ? "es"
                    : "s"}
              </span>
            );
          })}
          {unanalyzedCount > 0 ? (
            <span className="text-muted-foreground font-mono text-[10px]">
              {unanalyzedCount} awaiting analysis
            </span>
          ) : null}
        </div>

        <div className="flex flex-col gap-1.5">
          {qaTrials
            .filter((trial) => !foreignIds?.has(trial.id))
            .map((trial) => (
              <TrialQaRow
                key={trial.id}
                trial={trial}
                onOpen={() => openTrial(trial)}
                onFeedback={
                  canVote
                    ? (record) => postFeedback(trial.id, record)
                    : undefined
                }
              />
            ))}
        </div>
      </>
    );
  };

  // Attacks never feed findings or the verdict, so they come straight from the fetch.
  const adversarialRows = (trials ?? []).filter(
    (trial) =>
      trial.kind === "adversarial" &&
      (version == null || trial.task_version === version)
  );
  const attacks = adversarialRows
    .filter(isAttackTrial)
    .sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? "") || b.id.localeCompare(a.id, undefined, { numeric: true }));
  const attackOutcomes = attacks.map((trial) => {
    const replay = attackReplay(trial, trials ?? []);
    const outcome = adversarialOutcome(
      trial,
      replay,
      mergedFindings.some(
        (f) => replay != null && f.source === "adversarial" && f.replay_trial_id === replay.id
      )
    );
    return { trial, replay, outcome };
  });
  // The newest launch, plus any older attack that proved a defect: a rerun
  // that finds nothing must not hide an earlier must-fix.
  const shownAttacks = attackOutcomes.filter(
    (row, index) => index < ATTACKS_PER_LAUNCH || row.outcome.tone === "defect"
  );
  const mustFixAttacks = attackOutcomes.filter((row) => row.outcome.tone === "defect").length;
  const incompleteAttacks = attackOutcomes.filter((row) => row.outcome.tone === "incomplete").length;
  // Replays run after their attack, so the batch is running until they and
  // their settle hooks finish; a launch before then is the same batch.
  const attackRunning = adversarialUnsettled(adversarialRows);
  // Leak findings arrive with the panel, not the trial list: refetch it when
  // a replay records its findings so the card does not wait for its timer.
  const recordedReplays = adversarialRows
    .filter(
      (trial) =>
        trial.agent === REPLAY_AGENT &&
        trial.harbor_config?.adversarial?.findings_recorded
    )
    .map((trial) => trial.id)
    .sort()
    .join(",");
  const seenRecordedReplays = useRef<{ key: string; ids: string } | null>(null);
  useEffect(() => {
    if (trials === undefined || trialsKey === null) return;
    const seen = seenRecordedReplays.current;
    if (seen?.key === trialsKey && seen.ids !== recordedReplays)
      onAdversarialFindings?.();
    seenRecordedReplays.current = { key: trialsKey, ids: recordedReplays };
  }, [trials, trialsKey, recordedReplays, onAdversarialFindings]);
  // The launch API only attacks the task's current version, so an older
  // version on screen would always get a 409.
  const currentVersion = verdictTask?.current_version;
  const attackDisabledReason =
    checksDisabledReason ||
    (currentVersion != null && version != null && version !== currentVersion
      ? `Adversarial QA runs on the current version, v${currentVersion}`
      : null);
  async function runAttack() {
    if (!taskId || !taskVersionId || attackQueuing || attackDisabledReason)
      return;
    if (!attackConfirming) {
      setAttackConfirming(true);
      return;
    }
    setAttackConfirming(false);
    setAttackQueuing(true);
    setAttackError(null);
    try {
      const response = await fetch(
        `${apiBaseUrl}/tasks/${taskId}/qa/adversarial`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            expected_version_id: taskVersionId,
            ...(experimentId ? { experiment_id: experimentId } : {}),
            request_id: crypto.randomUUID(),
            max_trial_attempts: 1,
          }),
        }
      );
      const result = await response.json();
      if (!response.ok)
        throw new Error(
          result.detail || result.error || "Unable to queue attack"
        );
      await refreshTrials();
    } catch (error) {
      setAttackError(
        error instanceof Error ? error.message : "Unable to queue attack"
      );
    } finally {
      setAttackQueuing(false);
    }
  }

  const auditButton = (
    <button
      type="button"
      disabled={
        checksRerunning ||
        auditRunning ||
        checksStateUnknown ||
        checksDisabledReason != null
      }
      onClick={onRerunChecks}
      className="text-muted-foreground hover:text-foreground border-border rounded border px-2 py-0.5 font-mono text-[10px] font-medium disabled:cursor-not-allowed disabled:opacity-50"
    >
      {checksRerunning
        ? "Queuing…"
        : `Run pre-trial audit${auditVersion != null ? ` v${auditVersion}` : ""}`}
    </button>
  );

  return (
    <div className={cn("flex flex-col", className)}>
      {verdictTask ? (
        <div className="border-border border-b p-4">
          <TaskVerdictBadge
            task={verdictTask}
            variant="inline"
            version={version}
            selectedTrialId={selectedTrialId}
            sourceLink={analysisTrialLink(
              verdictReportId,
              verdictTask.verdict?.is_good === false || verdictTask.verdict?.verdict === "reject"
                ? "View rejected review"
                : "View source review (new tab)"
            )}
            mustFixCount={mustFixCount}
            action={readOnly ? undefined : executionReviewAction}
            error={executionReviewError}
          />
          {canVote &&
          verdictReportId &&
          (verdictReview === "accepted" || verdictReview === "rejected") ? (
            <FeedbackControl
              key={verdictReportId}
              label="the task verdict"
              className="mt-3"
              onSubmit={(vote, note) =>
                postFeedback(verdictReportId, {
                  target: {
                    kind: "task_verdict",
                    verdict: verdictReview === "accepted" ? "accept" : "reject",
                    reportTrialId: verdictReportId,
                  },
                  vote,
                  note,
                })
              }
            />
          ) : null}
        </div>
      ) : null}

      {/* Shared views have no attack rows to show. */}
      {!readOnly && (
      <section
        aria-label="Adversarial QA"
        className="border-border flex flex-col gap-2 border-b p-4"
      >
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="font-mono text-[11px] font-semibold">
            Adversarial QA
            {attacks.length > 0 && (
              <span className="text-muted-foreground ml-2 font-normal">
                {attacks.length} attacks · {mustFixAttacks} must fix · {incompleteAttacks} incomplete
              </span>
            )}
          </h2>
          {!readOnly && taskVersionId && (
            <button
              type="button"
              onClick={() => void runAttack()}
              onBlur={() => setAttackConfirming(false)}
              disabled={
                attackQueuing ||
                attackRunning ||
                Boolean(attackDisabledReason) ||
                !trials ||
                Boolean(trialsError)
              }
              title={
                attackDisabledReason ||
                "Run adversarial QA on this task version"
              }
              className="border-border rounded border px-2 py-0.5 font-mono text-[10px] disabled:opacity-50"
            >
              {attackQueuing
                ? "Queuing…"
                : attackRunning
                  ? "Running"
                  : attackConfirming
                    ? `Confirm: run ${ATTACKS_PER_LAUNCH} attacks`
                    : attacks.length
                    ? `Rerun adversarial QA${version != null ? ` v${version}` : ""}`
                    : `Run adversarial QA${version != null ? ` v${version}` : ""}`}
            </button>
          )}
        </div>
        {trialsError ? (
          <p role="alert" className="text-xs text-red-500">
            Unable to load adversarial runs.
          </p>
        ) : !trials ? (
          <p className="text-muted-foreground text-xs">
            Loading adversarial runs…
          </p>
        ) : attacks.length === 0 ? (
          <p className="text-muted-foreground text-xs">
            Not requested for this version.
          </p>
        ) : (
          shownAttacks.map(({ trial, replay, outcome }) => {
            const canOpen = Boolean(onOpenTrial || taskTrialHref(trial));
            const token =
              outcome.tone === "defect"
                ? VERDICT_TOKENS.BAD_FAILURE
                : outcome.tone === "incomplete"
                  ? VERDICT_TOKENS.HARNESS_ERROR
                  : FALLBACK_TOKEN;
            return (
              <article
                key={trial.id}
                aria-label={`${trial.name}: ${outcome.label}`}
                className={cn("space-y-2 rounded-lg border p-3", token.card)}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span
                    className={cn(
                      "rounded px-2 py-0.5 text-xs font-medium",
                      outcome.tone === "defect"
                        ? TIER_BADGE.must_fix
                        : `border ${token.chip} ${token.accent}`
                    )}
                  >
                    {outcome.label}
                  </span>
                  {canOpen && (
                    <button
                      type="button"
                      onClick={() => openTrial(trial)}
                      title={trial.id}
                      className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1 text-xs"
                    >
                      View run{" "}
                      <ArrowUpRight className="h-3 w-3" aria-hidden="true" />
                    </button>
                  )}
                </div>
                <p className="text-muted-foreground font-mono text-xs" title={trial.task_version_id ?? undefined}>
                  Task version: {trial.task_version != null ? `v${trial.task_version}` : "Unknown"}
                  {trial.harbor_config?.adversarial?.white_box ? " · White box" : " · Black box"}
                </p>
                <AnalysisProse
                  text={outcome.detail}
                  className="text-foreground/90"
                />
                {outcome.tone === "defect" && (
                  <AttackReport trialId={trial.id} apiBaseUrl={apiBaseUrl} />
                )}
                <dl className="grid grid-cols-2 gap-3 text-xs">
                  <div>
                    <dt className="text-muted-foreground">Execution</dt>
                    <dd className="mt-0.5">{outcome.execution}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">Replay</dt>
                    <dd className="mt-0.5">
                      {replay && canOpen ? (
                        <button
                          type="button"
                          onClick={() => openTrial(replay)}
                          title={replay.id}
                          className="inline-flex items-center gap-1 underline-offset-2 hover:underline"
                        >
                          {outcome.replay}
                          <ArrowUpRight className="h-3 w-3" aria-hidden="true" />
                        </button>
                      ) : (
                        outcome.replay
                      )}
                    </dd>
                  </div>
                </dl>
                <div className="text-muted-foreground flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[10px]">
                  <span>
                    {trial.agent} ·{" "}
                    {trial.model?.split("/").pop() ?? "Model unavailable"}
                  </span>
                  {trial.reward != null && (
                    <span>Grader reward {trial.reward}</span>
                  )}
                  {trial.cost_usd != null && (
                    <span>${trial.cost_usd.toFixed(2)}</span>
                  )}
                </div>
                {trial.error_message && (
                  <details className="border-border border-t pt-2">
                    <summary className="text-muted-foreground cursor-pointer text-xs">
                      Technical details
                    </summary>
                    <pre className="text-muted-foreground mt-2 max-h-48 overflow-auto text-[11px] break-words whitespace-pre-wrap">
                      {errorOutput(trial.error_message)}
                    </pre>
                  </details>
                )}
              </article>
            );
          })
        )}
        {attackError && (
          <p role="alert" className="text-xs text-red-500">
            {attackError}
          </p>
        )}
      </section>
      )}

      <div className="border-border flex flex-col gap-3 border-b p-4">
        {selectedFinding &&
        !checksLoading &&
        !trialsError &&
        displayTrials &&
        !findingItems.some(
          (item) =>
            item.id === selectedFinding || item.links_to === selectedFinding
        ) ? (
          <p role="alert" className="text-sm text-amber-700">
            Finding {selectedFinding} is unavailable for v{version}.
          </p>
        ) : null}
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-muted-foreground font-mono text-[11px] font-semibold tracking-wider uppercase">
            {findingItems.length > 0 ? "Findings" : "Pre-trial audit"}
          </h2>
          {!auditPassed && !(verdictTask && mustFixCount > 0) && (
            mustFixCount > 0 ? (
              <span className={cn("rounded px-2 py-0.5 text-xs font-medium", TIER_BADGE.must_fix)}>{mustFixCount} Must fix</span>
            ) : (
              <span className="text-muted-foreground font-mono text-[11px]">{findingsSummary}</span>
            )
          )}
          {analysisTrialLink(checksTrialId, "View audit")}
          <div className="ml-auto flex items-center gap-2">
            {findingItems.length > 0 ? (
              <CopyJsonButton
                value={findingItems}
                label="the task's findings"
              />
            ) : null}
            {!readOnly && onRerunChecks && !auditFinished ? (
              // A disabled button gets no hover events, so a wrapper carries
              // the reason.
              checksDisabledReason ? (
                <span title={checksDisabledReason}>{auditButton}</span>
              ) : (
                auditButton
              )
            ) : null}
          </div>
        </div>

        {auditPassed ? (
          <div className="flex items-center gap-2.5 rounded-[10px] border border-emerald-500/40 bg-emerald-500/[0.04] px-3 py-2">
            <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-500" />
            <span className="font-mono text-[12px] font-semibold text-[color:var(--paper-ink)]">
              Passed
            </span>
          </div>
        ) : null}
        {checksQueueError ? (
          <p className="text-[11px] text-red-500">{checksQueueError}</p>
        ) : null}

        {canVote &&
        !checksStateUnknown &&
        checksTrialId &&
        (checkState === "clean" || checkState === "findings") ? (
          <FeedbackControl
            key={checksTrialId}
            label="the pre-trial audit"
            onSubmit={(vote, note) =>
              postFeedback(checksTrialId, {
                target: { kind: "pre_trial", reportTrialId: checksTrialId },
                vote,
                note,
              })
            }
          />
        ) : null}
        {findingsBody()}
      </div>

      {showRuns && (
      <div className="flex flex-col gap-3 p-4">
        <div className="flex flex-wrap items-center gap-2">
          {!readOnly && !verdictTask && (
            <div className="ml-auto">{executionReviewAction}</div>
          )}
          <span className="text-muted-foreground font-mono text-[11px]">
            {!versionKnown
              ? checksLoadError
                ? "Unavailable"
                : "…"
              : displayTrials == null
                ? trialsError
                  ? "Unavailable"
                  : "…"
                : versionTrials.length === 0 && scopeLoading
                  ? "…"
                  : `${scopeTrials != null ? "This experiment: " : ""}${runReviewSummary(experimentRuns)}${version != null ? ` · v${version}` : ""}`}
          </span>
        </div>
        {!verdictTask && executionReviewError && (
          <p role="alert" className="text-xs text-amber-700">
            {executionReviewError}
          </p>
        )}
        {trialQaBody()}
        {!readOnly && additionalRuns.length > 0 && (
          <section className="mt-3 space-y-2 border-t pt-3">
            <h3 className="text-sm font-medium">Other experiments</h3>
            <p className="text-muted-foreground text-xs">
              {runReviewSummary(additionalRuns)}
            </p>
            {Array.from(runsByExperiment, ([experimentId, runs]) => (
              <section
                key={experimentId ?? "unassigned"}
                className="border-border space-y-2 rounded-lg border p-3"
              >
                <h4 className="text-sm font-medium">
                  {experimentId ? (
                    <Link
                      className="inline-flex items-center gap-1 hover:underline"
                      href={`/experiments/${encodeExperimentRouteParam(experimentId)}`}
                    >
                      {experiments.find(
                        (experiment) => experiment.id === experimentId
                      )?.name ?? experimentId}
                      <ArrowUpRight
                        className="h-3.5 w-3.5"
                        aria-hidden="true"
                      />
                    </Link>
                  ) : (
                    "Unassigned runs"
                  )}
                </h4>
                {runs.map((trial) => (
                  <TrialQaRow
                    key={trial.id}
                    trial={trial}
                    onOpen={() => openTrial(trial)}
                    onFeedback={
                      canVote
                        ? (record) => postFeedback(trial.id, record)
                        : undefined
                    }
                  />
                ))}
              </section>
            ))}
          </section>
        )}
      </div>
      )}
    </div>
  );
}

function TrialQaRow({
  trial,
  onOpen,
  onFeedback,
}: {
  trial: Trial;
  onOpen: () => void;
  onFeedback?: (record: FeedbackRecord) => Promise<void>;
}) {
  const analysis = trial.analysis_status === "success" ? trial.analysis : null;
  const gradingError =
    analysis?.classification === "HARNESS_ERROR" &&
    analysis.subtype === "misgrade";
  const running = isActivePipelineStatus(trial.analysis_status);
  const failed = !analysis && trial.analysis_status === "failed";
  const token = analysis
    ? (VERDICT_TOKENS[analysis.classification] ?? FALLBACK_TOKEN)
    : FALLBACK_TOKEN;
  const Icon = token.icon;
  const voteControl =
    analysis?._graded_by && onFeedback ? (
      <FeedbackControl
        key={analysis._graded_by}
        label={`the ${EXECUTION_LABELS[analysis.classification]} analysis of ${trialLabel(trial)}`}
        className="mt-1"
        onSubmit={(vote, note) =>
          onFeedback({
            target: {
              kind: "trajectory",
              classification: analysis.classification,
              reportTrialId: analysis._graded_by!,
            },
            vote,
            note,
          })
        }
      />
    ) : null;
  const hasBody =
    Boolean(
      analysis?.evidence || analysis?.root_cause || analysis?.recommendation
    ) || voteControl != null;

  const header = (
    <>
      {running ? (
        <Loader2
          className="h-3.5 w-3.5 shrink-0 animate-spin text-blue-500"
          aria-hidden="true"
        />
      ) : (
        <Icon
          className={cn(
            "h-3.5 w-3.5 shrink-0",
            failed ? "text-amber-600" : token.accent
          )}
          aria-hidden="true"
        />
      )}
      <span
        className={cn(
          "shrink-0 font-mono text-[10px] font-semibold tracking-wider",
          running ? "text-blue-500" : failed ? "text-amber-600" : token.accent
        )}
      >
        {running
          ? "ANALYZING"
          : failed
            ? "ANALYSIS FAILED"
            : analysis
              ? gradingError
                ? "GRADING ERROR"
                : EXECUTION_LABELS[analysis.classification].toUpperCase()
              : "NOT ANALYZED"}
      </span>
      {analysis?.subtype && !gradingError ? (
        <span
          className="text-muted-foreground min-w-0 truncate font-mono text-[10px]"
          title={analysis.subtype}
        >
          {analysis.subtype}
        </span>
      ) : null}
      <span className="text-muted-foreground min-w-0 flex-1 truncate text-[11px]">
        {trialLabel(trial)}
      </span>
      <button
        type="button"
        onClick={(event) => {
          event.preventDefault();
          event.stopPropagation();
          onOpen();
        }}
        className="border-border text-muted-foreground hover:text-foreground hover:border-foreground/40 inline-flex shrink-0 items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px] transition-colors"
      >
        View trial
        <ArrowUpRight className="h-3 w-3" aria-hidden="true" />
      </button>
    </>
  );

  if (!hasBody) {
    return (
      <div className="border-border bg-background/40 flex items-center gap-2.5 rounded-lg border px-3 py-2">
        {header}
        {failed && trial.analysis_error ? (
          <span className="text-muted-foreground truncate font-mono text-[10px]">
            {trial.analysis_error}
          </span>
        ) : null}
      </div>
    );
  }

  return (
    <article className="border-border bg-background/40 rounded-lg border">
      <header className="flex items-center gap-2.5 px-3 py-2">{header}</header>
      <div className="border-border flex flex-col gap-2 border-t px-3 py-3">
        {analysis?.root_cause ? (
          <div className="flex items-baseline gap-2">
            <span className="text-muted-foreground shrink-0 font-mono text-[10px] tracking-widest">
              CAUSE
            </span>
            <AnalysisProse
              text={analysis.root_cause}
              className="text-foreground/90 min-w-0"
            />
          </div>
        ) : null}
        {analysis?.evidence && analysis.evidence !== analysis.root_cause ? (
          <div className="flex items-baseline gap-2">
            <span className="text-muted-foreground shrink-0 font-mono text-[10px] tracking-widest">
              EVIDENCE
            </span>
            <AnalysisProse
              text={analysis.evidence}
              className="text-foreground/90 min-w-0"
            />
          </div>
        ) : null}
        {analysis?.recommendation ? (
          <div className="flex items-baseline gap-2">
            <span className="text-muted-foreground shrink-0 font-mono text-[10px] tracking-widest">
              FIX
            </span>
            <AnalysisProse
              text={analysis.recommendation}
              className="text-foreground/90 min-w-0"
            />
          </div>
        ) : null}
        {voteControl}
      </div>
    </article>
  );
}
