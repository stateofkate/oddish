import type { TaskCostSummary } from "@/lib/types";
import { averageCost, type CostMetric } from "@/lib/cost-metrics";
import { formatCostUsd } from "@/lib/format";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";

export function CostEffortCell({
  cost,
  metric,
}: {
  cost?: TaskCostSummary | null;
  metric: CostMetric["value"];
}) {
  const average = averageCost(cost, metric);
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          tabIndex={0}
          className="focus-visible:outline-ring font-mono whitespace-nowrap tabular-nums focus-visible:outline"
        >
          {average === null ? "—" : formatCostUsd(average)}
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">
        Selected version: {cost?.trial_count ?? 0} of{" "}
        {cost?.completed_trials ?? 0} completed model trials priced;{" "}
        {cost?.qa_jobs ?? 0} settled QA runs. QA spend is allocated over those completed
        model trials. The combined average is unavailable when prices or QA
        version links are missing.
      </TooltipContent>
    </Tooltip>
  );
}
