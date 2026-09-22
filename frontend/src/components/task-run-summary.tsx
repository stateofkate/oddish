import type { TaskRunSummary as RunSummary } from "@/lib/types";
import { STATUS_CONFIG } from "@/lib/status-config";

/** Exact selected-version results. Preview strips never determine these totals. */
export function TaskRunSummary({ summary, compact = false }: {
  summary?: RunSummary | null;
  compact?: boolean;
}) {
  if (!summary) return <p className="text-muted-foreground text-xs">Run summary unavailable</p>;
  const model = summary.model;
  const score = model.scored ? model.reward_sum / model.scored : null;
  const tone = score === null ? "text-muted-foreground"
    : score >= .8 ? "text-emerald-600 dark:text-emerald-400"
    : score >= .35 ? "text-amber-600 dark:text-amber-400"
    : "text-rose-600 dark:text-rose-400";
  const outcomes = [
    ["pass", model.pass_count], ["partial", model.partial_count], ["fail", model.fail_count],
    ["harness-error", model.harness_count], ["skipped", model.skipped_count],
    ["scoreless", model.scoreless_count], ["pending", model.other_count],
  ] as const;
  return (
    <div className="space-y-2 text-xs" aria-label="Selected version run summary">
      <div className={compact ? "space-y-1" : "border-border/60 bg-muted/30 space-y-2 rounded-md border p-3"}>
        <div title="Mean reward across scored model trials on this version; excludes oracle/NOP checks.">
          <span className="text-muted-foreground">{model.total ? "Model score:" : "No model trials"}</span>{" "}
          {model.total > 0 ? (
            <strong className={`${compact ? "text-base" : "text-2xl"} font-mono tabular-nums ${tone}`}>
              {score === null ? "—" : `${Math.round(score * 100)}%`}
            </strong>
          ) : null}
        </div>
        {model.total > 0 ? (
          <div className="text-muted-foreground">
            {model.scored} scored / {model.total} model trials
            {model.scored > 0 ? ` · ${model.reward_sum.toFixed(2)} / ${model.scored} reward` : ""}
          </div>
        ) : null}
        {!compact && model.total > 0 ? (
          <div className="flex flex-wrap gap-x-3 gap-y-1">
            {outcomes.filter(([, count]) => count > 0).map(([status, count]) => (
              <span key={status} className="inline-flex items-center gap-1">
                <span className={`h-2 w-2 rounded-full ${STATUS_CONFIG[status].bracketClass}`} aria-hidden />
                {STATUS_CONFIG[status].shortLabel} <span className="font-mono">{count}</span>
              </span>
            ))}
          </div>
        ) : null}
      </div>
      <div className="flex flex-wrap gap-2" aria-label="Validation checks">
        {(["oracle", "nop"] as const).map(role => {
          const group = summary[role];
          const expected = role === "oracle" ? group.pass_count + group.partial_count : group.fail_count;
          const color = expected < group.scored ? "text-rose-600 dark:text-rose-400"
            : group.harness_count > 0 ? "text-amber-600 dark:text-amber-400"
            : group.scored ? "text-emerald-600 dark:text-emerald-400" : "text-muted-foreground";
          const result = group.total === 0 ? "No trials"
            : group.scored ? `${expected}/${group.scored} as expected`
            : group.other_count ? "Awaiting results" : "No scored results";
          return (
            <span key={role} className={`rounded-md border px-2 py-1 ${color}`}
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
