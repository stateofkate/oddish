import type { TaskCostSummary } from "./types";

export const COST_METRICS = [
  { value: "total", label: "Trial + QA", sort: "avg_total_cost" },
  { value: "trial", label: "Trial only", sort: "avg_trial_cost" },
  { value: "qa", label: "QA only", sort: "avg_qa_cost" },
] as const;
export type CostMetric = (typeof COST_METRICS)[number];
export function selectedCostMetric(
  params: Pick<URLSearchParams, "get">
): CostMetric {
  return (
    COST_METRICS.find(
      (m) => m.sort === params.get("sort")?.replace(/_(asc|desc)$/, "")
    ) ??
    COST_METRICS.find((m) => m.value === params.get("cost_metric")) ??
    COST_METRICS[0]
  );
}
export function setCostMetric(params: URLSearchParams, value: string) {
  const metric = COST_METRICS.find((m) => m.value === value) ?? COST_METRICS[0];
  if (metric.value === "total") params.delete("cost_metric");
  else params.set("cost_metric", metric.value);
  const sort = params.get("sort") ?? "";
  if (COST_METRICS.some((m) => m.sort === sort.replace(/_(asc|desc)$/, ""))) {
    params.set(
      "sort",
      `${metric.sort}_${sort.endsWith("_asc") ? "asc" : "desc"}`
    );
    params.delete("offset");
  }
}
export function averageCost(
  cost: TaskCostSummary | null | undefined,
  metric: CostMetric["value"]
) {
  if (!cost || !cost.completed_trials) return null;
  if (metric === "trial")
    return cost.trial_count ? cost.trial_sum / cost.trial_count : null;
  if (cost.qa_unpriced_jobs || cost.qa_unattributed_jobs) return null;
  if (metric === "qa") return cost.qa_sum / cost.completed_trials;
  return cost.trial_count === cost.completed_trials
    ? (cost.trial_sum + cost.qa_sum) / cost.completed_trials
    : null;
}
