import { effortMeasurement, TOKEN_METRICS, type EffortMetric } from "@/lib/token-metrics";
import type { TaskEffortSummary } from "@/lib/types";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";

export function TaskEffortCell({
  effort,
  metric,
}: {
  effort?: TaskEffortSummary;
  metric: EffortMetric;
}) {
  const { count, average } = effortMeasurement(effort, metric);
  const tokenMetric = TOKEN_METRICS.find(option => option.field === metric);
  const value =
    average === null
      ? "—"
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
          className="whitespace-nowrap font-mono tabular-nums focus-visible:outline focus-visible:outline-ring"
        >
          {value}
        </span>
      </TooltipTrigger>
      <TooltipContent side="bottom" className="max-w-xs">
        <p>
          {count} of {effort?.completed_trials ?? 0} completed model trials have
          recorded {tokenMetric ? `${tokenMetric.label.toLowerCase()} tokens` : "steps"}.
        </p>
        <p>
          Current version. Includes reward passes and failures; excludes Oracle,
          NOP, QA, and infrastructure failures.
        </p>
        {tokenMetric && <p>{tokenMetric.description}</p>}
      </TooltipContent>
    </Tooltip>
  );
}
