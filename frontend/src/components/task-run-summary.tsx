import type { TaskRunSummary as RunSummary, TaskEffortSummary } from "@/lib/types";
import { STATUS_CONFIG } from "@/lib/status-config";

/** Exact selected-version results. Preview strips never determine these totals. */
export function TaskRunSummary({ summary, compact = false, outcomesOnly = false, effort }: {
  summary?: RunSummary | null;
  effort?: TaskEffortSummary;
  compact?: boolean;
  outcomesOnly?: boolean;
}) {
  const model = summary?.model;
  const score = model?.scored ? model.reward_sum / model.scored : null;
  const tone = score === null ? "text-muted-foreground"
    : score >= .8 ? "text-emerald-600 dark:text-emerald-400"
    : score >= .35 ? "text-amber-600 dark:text-amber-400"
    : "text-rose-600 dark:text-rose-400";
  const outcomes = [
    ["pass", model?.pass_count ?? 0], ["partial", model?.partial_count ?? 0], ["fail", model?.fail_count ?? 0],
    ["harness-error", model?.harness_count ?? 0], ["skipped", model?.skipped_count ?? 0],
    ["scoreless", model?.scoreless_count ?? 0], ["pending", model?.other_count ?? 0],
  ] as const;
  return (
    <div className="space-y-2 text-xs" aria-label="Selected version run summary">
      <div className="space-y-2">
        <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
          {!outcomesOnly && <div title="Mean reward across scored model trials on this version; excludes oracle/NOP checks.">
            <span className="text-muted-foreground">Model score:</span>{" "}
            <strong className={`${compact ? "text-base" : "text-xl"} font-mono tabular-nums ${tone}`}>
              {score === null ? "—" : `${Math.round(score * 100)}%`}
            </strong>
          </div>}
          <span className="text-muted-foreground" title={model ? `${model.reward_sum.toFixed(2)} reward across ${model.scored} scored trials` : undefined}>
            {!model ? "Run summary unavailable" : model.total ? `${model.scored} scored / ${model.total} model trials` : "No model trials"}
          </span>
        </div>
        {effort && (
          <div
            className="text-muted-foreground font-mono text-xs"
            title={`Completed model trials on this version; excludes oracle/NOP, QA and infrastructure failures. Steps recorded on ${effort.steps_trial_count}/${effort.completed_trials}; input and output tokens on ${effort.tokens_trial_count}/${effort.completed_trials}.`}
          >
            Avg steps{" "}
            {effort.steps_trial_count
              ? Math.round(
                  effort.steps_sum / effort.steps_trial_count
                ).toLocaleString()
              : "—"}
            {" · "}Avg tokens{" "}
            {effort.tokens_trial_count
              ? new Intl.NumberFormat("en", {
                  notation: "compact",
                  maximumFractionDigits: 1,
                }).format(effort.tokens_sum / effort.tokens_trial_count)
              : "—"}
          </div>
        )}
        {(!compact || outcomesOnly) && model ? (
          <div className="flex min-h-4 flex-wrap gap-x-3 gap-y-1">
            {outcomes.filter(([status, count]) => count > 0 || (outcomesOnly && ["pass", "fail", "harness-error", "pending"].includes(status))).map(([status, count]) => (
              <span key={status} className={outcomesOnly ? "inline-flex items-center gap-1.5 rounded-md border px-2 py-1.5 text-sm" : "inline-flex items-center gap-1"}>
                <span className={`h-2 w-2 rounded-full ${STATUS_CONFIG[status].bracketClass}`} aria-hidden />
                {STATUS_CONFIG[status].shortLabel} <span className="font-mono">{count}</span>
              </span>
            ))}
          </div>
        ) : null}
      </div>
      <div className="grid gap-1" aria-label="Validation checks">
        {(["oracle", "nop"] as const).map(role => {
          const group = summary?.[role];
          if (!group) return <span key={role} className="text-muted-foreground">{role === "oracle" ? "Oracle" : "NOP"}: Unavailable</span>;
          const expected = role === "oracle" ? group.pass_count + group.partial_count : group.fail_count;
          const color = expected < group.scored ? "text-rose-600 dark:text-rose-400"
            : group.harness_count > 0 ? "text-amber-600 dark:text-amber-400"
            : group.scored ? "text-emerald-600 dark:text-emerald-400" : "text-muted-foreground";
          const result = group.total === 0 ? "No trials"
            : group.scored ? `${expected}/${group.scored} as expected`
            : group.other_count ? "Awaiting results" : "No scored results";
          return (
            <span key={role} className={color}
              title={role === "oracle" ? "The known solution should earn positive reward." : "Doing nothing should earn zero reward."}>
              {role === "oracle" ? "Oracle" : "NOP"}: {result}
              {group.total > 0 ? ` · ${group.total} ${group.total === 1 ? "trial" : "trials"}` : ""}
              {group.harness_count > 0 ? ` · ${group.harness_count} errors` : ""}
            </span>
          );
        })}
      </div>
    </div>
  );
}
