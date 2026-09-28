"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { formatCostUsd, formatDate } from "@/lib/format";
import {
  formatScore,
  formatRunStatus,
  configurationLabel,
  shortSha,
  type QAEvalRun,
  type QAEvalStatus,
} from "@/lib/qa-evals";
import { cn } from "@/lib/utils";

function RunRow({
  run,
  production,
  selected,
  onSelect,
}: {
  run: QAEvalRun;
  production: boolean;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        title={run.experiment_name}
        className={cn(
          "w-full space-y-1 border-l-2 border-transparent px-3 py-2 text-left hover:bg-[color:var(--paper-line-2)]/40",
          selected &&
            "border-[color:var(--paper-ink)] bg-[color:var(--paper-line-2)]/60"
        )}
      >
        <span className="block truncate text-sm">{run.title}</span>
        <span className="flex items-center gap-1.5 text-xs text-[color:var(--paper-ink-3)]">
          {run.subset && (
            <Badge
              variant="outline"
              className="px-1 py-0 text-[10px]"
              title="Reran picked trials: scored on them alone, so not charted"
            >
              {run.replays} picked
            </Badge>
          )}
          <span className="truncate">
            {run.preflight && "Check · "}{configurationLabel(run)} · {formatDate(run.created_at)} ·{" "}
            <span
              className={cn("font-mono", production && "text-emerald-600")}
              title={production ? "the production prompt" : undefined}
            >
              {shortSha(run.prompt_sha256)}
            </span>
          </span>
          <span className="ml-auto font-mono tabular-nums">
            {formatCostUsd(run.cost_usd)}
          </span>
        </span>
        {run.accuracy !== null ? (
          <span className="flex items-center gap-2">
            <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-[color:var(--paper-line-2)]">
              <span
                className="block h-full rounded-full bg-[color:var(--paper-ink-2)]"
                style={{ width: `${Math.round(run.accuracy * 100)}%` }}
              />
            </span>
            <span className="w-24 text-right font-mono text-xs tabular-nums">
              {formatScore(run)}
            </span>
          </span>
        ) : null}
        {run.with_golden > 0 && (
          <span className="block text-right text-xs text-[color:var(--paper-ink-3)]">
            {formatRunStatus(run)}
          </span>
        )}
      </button>
    </li>
  );
}

/** Every run of the stage, newest first: the runs with golden trials, then
 * the runs with none folded away, and a footer of the spend per model. */
export function RunList({
  status,
  selected,
  onSelect,
}: {
  status: QAEvalStatus;
  selected: string | null;
  onSelect: (run: QAEvalRun) => void;
}) {
  const newestFirst = [...status.runs].reverse();
  const withGolden = newestFirst.filter((run) => run.with_golden > 0);
  const withoutGolden = newestFirst.filter((run) => run.with_golden === 0);
  // Opens by itself when the selected run is folded, and still closes.
  const [folded, setFolded] = useState(
    () => !withoutGolden.some((run) => run.experiment_id === selected)
  );
  const row = (run: QAEvalRun) => (
    <RunRow
      key={run.experiment_id}
      run={run}
      production={run.prompt_sha256 === status.production_prompt_sha256}
      selected={run.experiment_id === selected}
      onSelect={() => onSelect(run)}
    />
  );
  const { spend } = status;
  const Chevron = folded ? ChevronRight : ChevronDown;
  return (
    <>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <ol className="divide-y divide-[color:var(--paper-line-2)]">
          {withGolden.map(row)}
        </ol>
        {withoutGolden.length > 0 && (
          <>
            <button
              type="button"
              onClick={() => setFolded(!folded)}
              className="flex w-full items-center gap-1 border-t border-[color:var(--paper-line-2)] px-3 py-2 text-left text-xs text-[color:var(--paper-ink-3)] hover:text-[color:var(--paper-ink)]"
            >
              <Chevron className="h-3.5 w-3.5" />
              {withoutGolden.length}{" "}
              {withoutGolden.length === 1 ? "run" : "runs"} with no golden
              trials
            </button>
            {!folded && (
              <ol className="divide-y divide-[color:var(--paper-line-2)] opacity-70">
                {withoutGolden.map(row)}
              </ol>
            )}
          </>
        )}
      </div>
      <div className="space-y-0.5 border-t border-[color:var(--paper-line)] px-3 py-2 text-xs">
        <div className="flex justify-between font-medium">
          <span>
            {status.runs.length} {status.runs.length === 1 ? "run" : "runs"} ·{" "}
            {spend.trials} trials
          </span>
          <span className="font-mono tabular-nums">
            {formatCostUsd(spend.total_usd)}
          </span>
        </div>
        {spend.by_model.map(({ model_name, cost_usd }) => (
          <div
            key={model_name}
            className="flex justify-between text-[color:var(--paper-ink-3)]"
          >
            <span className="truncate">{model_name}</span>
            <span className="font-mono tabular-nums">
              {formatCostUsd(cost_usd)}
            </span>
          </div>
        ))}
        {spend.cost_missing > 0 && (
          <div className="text-[color:var(--paper-ink-3)]">
            {spend.cost_missing} finished trials have no recorded cost
          </div>
        )}
      </div>
    </>
  );
}
