import {
  effortMeasurement,
  TOKEN_METRICS,
  type EffortMetric,
} from "@/lib/token-metrics";
import type { TaskEffortSummary } from "@/lib/types";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";

export function EffortCell({
  effort,
  metric,
  scope = "Current version.",
}: {
  effort?: TaskEffortSummary & { cost_sum?: number; cost_trial_count?: number };
  metric: EffortMetric | "cost";
  scope?: string;
}) {
  const count =
    metric === "cost"
      ? (effort?.cost_trial_count ?? 0)
      : effortMeasurement(effort, metric).count;
  const average =
    metric === "cost"
      ? count > 0 && effort?.cost_sum != null
        ? effort.cost_sum / count
        : null
      : effortMeasurement(effort, metric).average;
  const tokenMetric = TOKEN_METRICS.find((option) => option.field === metric);
  const value =
    average === null
      ? "—"
      : metric === "cost"
        ? new Intl.NumberFormat("en", {
            style: "currency",
            currency: "USD",
          }).format(average)
        : metric === "steps"
          ? Math.round(average).toLocaleString()
          : new Intl.NumberFormat("en", {
              notation: "compact",
              maximumFractionDigits: 1,
            }).format(average);
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          tabIndex={0}
          className="focus-visible:outline-ring font-mono whitespace-nowrap tabular-nums focus-visible:outline"
        >
          {value}
        </span>
      </TooltipTrigger>
      <TooltipContent side="bottom" className="max-w-xs">
        <p>
          {count} of {effort?.completed_trials ?? 0} completed model trials have
          recorded{" "}
          {tokenMetric
            ? `${tokenMetric.label.toLowerCase()} tokens`
            : metric === "cost"
              ? "costs"
              : "steps"}
          .
        </p>
        <p>
          {scope} Includes reward passes and failures; excludes Oracle, NOP, QA,
          and infrastructure failures.
        </p>
        {tokenMetric && <p>{tokenMetric.description}</p>}
      </TooltipContent>
    </Tooltip>
  );
}
