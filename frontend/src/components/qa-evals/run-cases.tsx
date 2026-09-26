"use client";

import {
  CheckCircle2,
  Clock,
  RotateCcw,
  TriangleAlert,
  XCircle,
} from "lucide-react";
import { GroupChips } from "@/components/qa-evals/group-chips";
import { TrialLink } from "@/components/qa-evals/trial-link";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { formatCostUsd } from "@/lib/format";
import {
  QA_EVAL_GROUPS,
  type QAEvalCase,
  type QAEvalGroup,
  type QAEvalOutcome,
  type QAEvalRunDetail,
} from "@/lib/qa-evals";
import { cn } from "@/lib/utils";

// Wrong answers first: those are what a prompt change has to fix.
const ORDER: Record<QAEvalOutcome, number> = {
  wrong: 0,
  invalid: 1,
  errored: 2,
  pending: 3,
  right: 4,
  off: 5,
};

const ICON = "h-3.5 w-3.5";
const OUTCOME_ICON: Record<QAEvalOutcome, React.ReactNode> = {
  right: <CheckCircle2 className={cn(ICON, "text-emerald-600")} />,
  wrong: <XCircle className={cn(ICON, "text-red-600")} />,
  invalid: <XCircle className={cn(ICON, "text-red-600")} />,
  errored: <TriangleAlert className={cn(ICON, "text-amber-600")} />,
  pending: <Clock className={cn(ICON, "text-[color:var(--paper-ink-3)]")} />,
  off: <span className={ICON} />,
};

/** What QA answered, or why there is no answer. */
const NO_ANSWER: Record<QAEvalOutcome, string> = {
  right: "—",
  wrong: "—",
  invalid: "invalid output",
  errored: "errored",
  pending: "running",
  off: "—",
};

const PICK_BOX = "mt-0.5 h-3.5 w-3.5";

/** One replay: the source trial (its drawer, ready to label), what the
 * golden expects and what QA said. With `pick`, a box picks the trial for a
 * rerun. */
function CaseRow({
  c,
  pick,
}: {
  c: QAEvalCase;
  pick?: { picked: boolean; onChange: (picked: boolean) => void };
}) {
  const copy = c.original_trial_id !== c.source_trial_id;
  return (
    <li
      className="grid grid-cols-[auto_auto_minmax(0,1fr)_auto] gap-x-2 px-3 py-1.5"
      title={c.error ?? undefined}
    >
      {pick ? (
        <Checkbox
          aria-label={`Pick ${c.original_trial_id} to rerun`}
          checked={pick.picked}
          onCheckedChange={(checked) => pick.onChange(checked === true)}
          className={PICK_BOX}
        />
      ) : (
        <span className="w-3.5" />
      )}
      <span className="pt-0.5">{OUTCOME_ICON[c.outcome]}</span>
      <span className="min-w-0">
        {/* Named by the trial its golden keys on; a copy opens as itself,
            the trial this deployment has. */}
        <TrialLink
          taskId={c.task_id}
          trialId={c.source_trial_id}
          className="block truncate font-mono text-xs hover:underline"
          title={
            copy
              ? `copy of ${c.original_trial_id}: ${c.source_trial_id}`
              : c.source_trial_id
          }
        >
          {c.original_trial_id}
        </TrialLink>
        <span className="block font-mono text-[11px] text-[color:var(--paper-ink-3)]">
          {c.expected ? (
            <>
              expected{" "}
              <span className="text-[color:var(--paper-ink)]">
                {c.expected}
              </span>{" "}
              · QA{" "}
            </>
          ) : (
            "no golden · QA "
          )}
          <span
            className={cn(
              c.group === "right" && "text-emerald-600",
              c.group === "wrong" && "text-red-600"
            )}
          >
            {c.got ?? NO_ANSWER[c.outcome]}
          </span>
        </span>
      </span>
      <span className="font-mono text-[11px] text-[color:var(--paper-ink-3)] tabular-nums">
        {c.cost_usd === null ? "" : formatCostUsd(c.cost_usd)}
      </span>
    </li>
  );
}

/** Every case of the run under its one group, a chip per group narrowing
 * the list to it, and the errored replays' messages under "not judged".
 * Cases with a golden can be picked, one by one or a group at a time, and
 * rerun; picks are source trial ids, the ids a replay runs on. */
export function RunCases({
  run,
  filter,
  onFilter,
  picks,
  onPicks,
  onRerun,
}: {
  run: QAEvalRunDetail;
  filter: QAEvalGroup | "all";
  onFilter: (filter: QAEvalGroup | "all") => void;
  picks: string[];
  onPicks: (picks: string[]) => void;
  onRerun: (sourceTrialIds: string[]) => void;
}) {
  // Picks the URL carries for trials not in this run are ignored.
  const pickable = new Set(
    run.cases.filter((c) => c.golden_id).map((c) => c.source_trial_id)
  );
  const picked = new Set(picks.filter((id) => pickable.has(id)));
  const setPicks = (ids: string[], on: boolean) => {
    const next = new Set(picked);
    for (const id of ids) {
      if (on) next.add(id);
      else next.delete(id);
    }
    onPicks([...next]);
  };
  const groups = QA_EVAL_GROUPS.map((meta) => ({
    ...meta,
    cases: run.cases
      .filter((c) => c.group === meta.group)
      .sort((a, b) => ORDER[a.outcome] - ORDER[b.outcome]),
  })).filter((g) => g.cases.length > 0);
  // A filter naming a group this run lacks shows every group.
  const shown = groups.some((g) => g.group === filter) ? filter : "all";
  return (
    <>
      <div className="flex flex-wrap items-center gap-1.5 px-3 py-2">
        <GroupChips
          groups={groups.map((g) => ({
            value: g.group,
            label: `${g.name} ${g.cases.length}`,
          }))}
          total={run.cases.length}
          value={shown}
          onChange={onFilter}
        />
        <Button
          size="sm"
          variant="outline"
          className="ml-auto h-6 gap-1 px-2 text-xs"
          disabled={picked.size === 0}
          title={
            picked.size === 0
              ? "Tick tasks below to rerun only them with a prompt version"
              : undefined
          }
          onClick={() => onRerun([...picked])}
        >
          <RotateCcw className="h-3 w-3" />
          Rerun {picked.size || ""} picked
        </Button>
      </div>
      {groups
        .filter((g) => shown === "all" || g.group === shown)
        .map((g) => {
          const groupPicks = g.cases
            .map((c) => c.source_trial_id)
            .filter((id) => pickable.has(id));
          const pickedHere = groupPicks.filter((id) => picked.has(id)).length;
          return (
            <section key={g.group}>
              <h3
                className={cn(
                  "flex items-center gap-2 border-t border-[color:var(--paper-line-2)] px-3 pt-2 pb-1 text-xs font-semibold",
                  g.tone
                )}
              >
                {groupPicks.length > 0 && (
                  <Checkbox
                    aria-label={`Pick every task in ${g.title}`}
                    checked={
                      pickedHere === groupPicks.length ||
                      (pickedHere > 0 && "indeterminate")
                    }
                    onCheckedChange={(checked) =>
                      setPicks(groupPicks, checked === true)
                    }
                    className={PICK_BOX}
                  />
                )}
                {g.title} · {g.cases.length}
              </h3>
              {g.group === "not_judged" && run.error_groups.length > 0 && (
                <div className="mx-3 mb-1 space-y-0.5 rounded-md border border-amber-500/30 bg-amber-500/5 px-2 py-1.5 text-[11px]">
                  {run.error_groups.map((e) => (
                    <div key={e.message} className="break-words">
                      <span className="font-medium">{e.count} ×</span>{" "}
                      <span className="font-mono">{e.message}</span>
                    </div>
                  ))}
                </div>
              )}
              <ul className="divide-y divide-[color:var(--paper-line-2)]">
                {g.cases.map((c) => (
                  <CaseRow
                    key={c.qa_eval_trial_id}
                    c={c}
                    pick={
                      pickable.has(c.source_trial_id)
                        ? {
                            picked: picked.has(c.source_trial_id),
                            onChange: (on) => setPicks([c.source_trial_id], on),
                          }
                        : undefined
                    }
                  />
                ))}
              </ul>
            </section>
          );
        })}
    </>
  );
}
