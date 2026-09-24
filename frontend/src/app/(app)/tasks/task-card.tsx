"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { ExternalLink, GitPullRequest } from "lucide-react";
import { Badge, badgeVariants } from "@/components/ui/badge";
import { Card, CardHeader } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { ExperimentsList } from "@/components/experiments-list";
import {
  QaCostSuffix,
  VerifierCostSuffix,
} from "@/components/qa-cost-suffix";
import { TagChip } from "@/components/tag-chip";
import {
  getExperimentAgentKey, getExperimentAgentDisplay,
  experimentModelLabel, isBaselineAgentName,
} from "@/lib/experiment-agent-grouping";
import { TaskRunSummary } from "@/components/task-run-summary";
import { formatCostUsd, hasDisplayableCostUsd } from "@/lib/format";
import {
  formatPartialRewardBadgeValue,
  formatRewardPercent,
  formatRewardValue,
  getMatrixStatus,
  getRewardStyle,
  STATUS_CONFIG,
} from "@/lib/status-config";
import type { TaskBrowseItem } from "@/lib/types";

import { TaskBrowseLink } from "./task-browse-link";
import {
  cn,
  formatRelativeTime,
  formatShortDateTime,
  prBadge,
  taskPrUrl,
} from "@/lib/utils";
import { QA_OUTCOME_OPTIONS } from "@/lib/tasks-filters";
import { useSelection } from "./selection-context";

// "median 412 steps (p25 300 · p75 600) · 3 agents": the stored summary's
// trajectory-length percentiles and distinct-agent count, so a card shows
// what the "Median steps" / "Agents ≥" filters and sorts are reading.
function TrajectorySummary({ task }: { task: TaskBrowseItem }) {
  const parts: string[] = [];
  if (task.steps_p50 != null) {
    const spread =
      task.steps_p25 != null && task.steps_p75 != null
        ? ` (p25 ${task.steps_p25} · p75 ${task.steps_p75})`
        : "";
    parts.push(`median ${task.steps_p50} steps${spread}`);
  }
  if (task.agent_count) {
    parts.push(`${task.agent_count} runner${task.agent_count === 1 ? "" : "s"} incl. checks`);
  }
  if (parts.length === 0) return null;
  return (
    <div className="text-muted-foreground text-[11px] tabular-nums">
      {parts.join(" · ")}
    </div>
  );
}

// Show complete imported/finalized history and active membership even when
// the browse filter targets one lab. Missing records do not prove a task was
// never sent: imported history can be incomplete.
export function TaskDeliveryHistory({ task, showEmpty = false }: { task: TaskBrowseItem; showEmpty?: boolean }) {
  const params = useSearchParams();
  const records = [...(task.active_deliveries ?? []), ...(task.deliveries ?? [])];
  if (!records.length) return showEmpty ? <span className="text-muted-foreground text-xs">No delivery recorded</span> : null;
  return (
    <details className="text-xs">
      <summary className="cursor-pointer">
        {[...new Set(records.map(record => record.status === "active"
          ? record.delivery_id === params.get("delivery") ? "In this delivery" : `In active batch · ${record.customer}`
          : `${record.customer} · ${record.source === "history" ? "Recorded" : "Finalized"}`))].join(", ")}
      </summary>
      <ul className="mt-2 space-y-1">
        {records.map((record, index) => <li key={record.delivery_id ?? `${record.customer}-${index}`}>
          {record.customer} · {record.delivery_id
            ? <Link className="underline" href={`/deliveries/${encodeURIComponent(record.delivery_id)}`}>{record.batch}</Link>
            : record.batch ?? "Batch unknown"} · {record.status === "active" ? "Active" : record.date ?? "Date unknown"}
        </li>)}
      </ul>
    </details>
  );
}

type LatestTrial = TaskBrowseItem["latest_trials"][number];

function TrialIcon({ trial, taskId }: { trial: LatestTrial; taskId: string }) {
  const status = getMatrixStatus(
    trial.status,
    trial.reward,
    trial.error_message
  );
  const config = STATUS_CONFIG[status];
  const badgeLabel =
    status === "partial" ? formatPartialRewardBadgeValue(trial.reward) : null;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Link
          href={`/tasks/${encodeURIComponent(taskId)}?trial=${encodeURIComponent(trial.id)}`}
          className={`flex h-[18px] w-[18px] items-center justify-center rounded-[4px] border font-mono leading-none font-semibold ${config.matrixClass} ${status === "partial" ? "text-[7px] tracking-[-0.03em]" : ""}`}
          style={getRewardStyle(trial.reward)}
          aria-label={`${trial.name} ${config.shortLabel}`}
        >
          {badgeLabel}
        </Link>
      </TooltipTrigger>
      <TooltipContent>
        <div className="space-y-0.5">
          <div className="font-medium">{trial.name}</div>
          <div>{trial.agent} · {trial.model ?? "default"}</div>
          <div className="text-muted-foreground">{config.shortLabel}</div>
          {trial.reward !== null && (
            <div className="text-muted-foreground">
              Score {formatRewardValue(trial.reward)} (
              {formatRewardPercent(trial.reward)})
            </div>
          )}
        </div>
      </TooltipContent>
    </Tooltip>
  );
}

export function TaskEvidence({ task, showSummary = false }: { task: TaskBrowseItem; showSummary?: boolean }) {
  const groups = new Map<string, LatestTrial[]>();
  for (const trial of task.latest_trials) {
    const key = getExperimentAgentKey({ ...trial, kind: "agent" });
    const group = groups.get(key);
    if (group) group.push(trial);
    else groups.set(key, [trial]);
  }
  const rows = [...groups].sort(([, a], [, b]) =>
    Number(isBaselineAgentName(a[0].agent)) - Number(isBaselineAgentName(b[0].agent))
  );
  return (
    <div className={showSummary ? "contents" : "space-y-3"}>
      {showSummary ? (
        <section aria-label="Results" className="mx-5 border-t border-border/60 py-3">
          <TaskRunSummary summary={task.run_summary} />
        </section>
      ) : null}
      <section aria-label="Recent trials" className={cn("space-y-3", showSummary && "mx-5 border-t border-border/60 py-3")}>
        <div className="flex flex-wrap items-baseline justify-between gap-2 text-xs">
          <span className="text-muted-foreground">Recent trials · current version</span>
          <TrajectorySummary task={task} />
        </div>
        {rows.length ? rows.map(([key, trials]) => {
          const display = getExperimentAgentDisplay(trials[0]);
          return <div key={key} className="space-y-1">
            <div className="flex flex-wrap items-center gap-2 text-[11px]">
              <span className="font-mono">{display.agent}</span>
              <Badge variant="outline" className="max-w-full whitespace-normal break-all font-mono text-[10px]">{experimentModelLabel(display.model, trials[0].reasoning_effort)}</Badge>
              <span className="text-muted-foreground ml-auto">{trials.length} shown</span>
            </div>
            <div className="flex flex-wrap gap-1">{trials.map(trial => <TrialIcon key={trial.id} trial={trial} taskId={task.id} />)}</div>
          </div>;
        }) : <p className="text-muted-foreground text-xs">{task.total_trials ? "Trial preview unavailable" : "No trials on this version"}</p>}
        <p className="text-muted-foreground text-xs">
          {task.total_trials > 0 ? `Showing ${task.latest_trials.length} of ${task.total_trials} trials. ` : null}
          <Link className="underline" href={`/tasks/${encodeURIComponent(task.id)}`}>
            View all results
          </Link>
        </p>
      </section>
      <section aria-label="Experiments" className={cn("space-y-1 border-t border-border/60 pt-3", showSummary && "mx-5 pb-5")}>
        <div className="text-muted-foreground text-xs">Experiments · all versions</div>
        {task.experiments.length ? <ExperimentsList
          experiments={task.experiments}
          taskId={task.id}
          maxVisible={3}
          layout="stacked"
          className="text-xs"
          linkClassName="text-[#5d77a5] dark:text-[#a8b8d2]"
        /> : <p className="text-muted-foreground text-xs">No linked experiments</p>}
      </section>
    </div>
  );
}

export function TaskCard({ task }: { task: TaskBrowseItem }) {
  const { isSelected, toggle, deliveryId } = useSelection();
  const alreadyAdded = task.active_deliveries?.some(record => record.delivery_id === deliveryId) ?? false;
  const selected = isSelected(task.id);

  return (
    <Card
      className={cn(
        "row-span-5 grid min-w-0 grid-rows-subgrid gap-y-0 bg-card/95 border-[#6f88b4]/20 shadow-xs transition-colors hover:border-[#6f88b4]/40",
        selected && "border-[#6f88b4]/70 ring-1 ring-[#6f88b4]/40"
      )}
    >
      <CardHeader className="space-y-3 px-5 pt-5 pb-3">
        <div className="grid grid-cols-[auto_minmax(0,1fr)] items-start gap-3">
          <Checkbox
            checked={selected || alreadyAdded}
            disabled={alreadyAdded}
            onCheckedChange={() => toggle(task)}
            aria-label={`Select ${task.name}`}
            className="mt-0.5 shrink-0"
          />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <TaskBrowseLink
                task={task}
                className="text-foreground break-words font-mono text-sm font-semibold transition-colors hover:text-[#5d77a5] dark:hover:text-[#a8b8d2]"
              />
              <Badge variant="outline" className="w-fit font-mono text-[11px]">
                v{task.current_version ?? "—"}
              </Badge>
              {task.qa_outcome ? <Badge variant="outline">{QA_OUTCOME_OPTIONS.find(option => option.value === task.qa_outcome)?.label}</Badge> : null}
              {task.author_pinned ? (
                <Badge
                  className="w-fit border-transparent bg-[#6f88b4]/20 text-[11px] text-[#3f5a8a] dark:text-[#a8b8d2]"
                  title="One of your tasks"
                >
                  mine
                </Badge>
              ) : null}
              {(() => {
                const meta = task.github_meta;
                const prUrl = taskPrUrl(task.link, meta);
                if (!prUrl) return null;
                const { label, number } = prBadge(prUrl, meta?.pr_number);
                const title = meta?.pr_title;
                return (
                  <a
                    href={prUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    title={
                      title
                        ? `${title} — view on GitHub`
                        : "View pull request on GitHub"
                    }
                    onClick={(e) => e.stopPropagation()}
                    className={cn(
                      badgeVariants({ variant: "outline" }),
                      "hover:bg-accent w-fit gap-1.5 font-mono text-[11px] transition-colors"
                    )}
                  >
                    <GitPullRequest className="h-3 w-3 shrink-0" aria-hidden />
                    <span className="max-w-[140px] min-w-0 truncate">
                      {label}
                      {number && (
                        <span className="text-muted-foreground">
                          {" "}
                          #{number}
                        </span>
                      )}
                    </span>
                    <ExternalLink
                      className="h-3 w-3 shrink-0 opacity-50"
                      aria-hidden
                    />
                  </a>
                );
              })()}
            </div>
          </div>
        </div>
        {(task.user_tags ?? []).length > 0 ? (
          <div className="flex flex-wrap gap-1">
            {(task.user_tags ?? []).map((t) => (
              <TagChip key={t.tag_id} tag={t} />
            ))}
          </div>
        ) : null}
        <TaskDeliveryHistory task={task} />
      </CardHeader>
      <section aria-label="Activity and cost" className="mx-5 grid grid-cols-2 gap-3 pb-3">
        <div className="min-w-0">
          <div className="text-muted-foreground text-[11px] tracking-wide uppercase">
            Last run
          </div>
          <div className="mt-1 text-xs">
            {task.last_run_at ? formatRelativeTime(task.last_run_at) : "—"}
          </div>
          {task.last_run_at ? (
            <div className="text-muted-foreground text-[11px]">
              {formatShortDateTime(task.last_run_at)}
            </div>
          ) : null}
        </div>
        <div className="min-w-0 text-right">
          <div className="text-muted-foreground text-[11px] tracking-wide uppercase">
            Cost
          </div>
          <div className="mt-1 flex flex-wrap justify-end items-baseline gap-1.5 text-sm font-semibold tabular-nums">
            <span>
              {task.cost_trial_count > 0 &&
              hasDisplayableCostUsd(task.cost_usd)
                ? `${task.cost_has_estimated && !task.cost_has_native ? "~" : ""}${formatCostUsd(task.cost_usd)}`
                : "—"}
            </span>
            <QaCostSuffix
              costUsd={task.qa_cost_usd}
              title="QA/analysis spend for this task's trials. Not included in the cost figure."
            />
            <VerifierCostSuffix
              costUsd={task.verifier_cost_usd}
              title="CUA/verifier LLM spend for this task's trials. Not included in the cost figure."
            />
          </div>
          {task.cost_trial_count > 0 ? (
            <div className="text-muted-foreground text-[11px]">
              {task.cost_trial_count} of {task.total_trials} priced
            </div>
          ) : null}
          {task.cost_trial_count > 0 ? (
            <div className="text-muted-foreground text-[11px]">
              spent{" "}
              {task.billed_trial_count === 0
                ? formatCostUsd(0)
                : `${task.billed_has_estimated && !task.billed_has_native ? "~" : ""}${formatCostUsd(task.billed_cost_usd)}`}
            </div>
          ) : null}
        </div>
      </section>
      <TaskEvidence task={task} showSummary />
    </Card>
  );
}
