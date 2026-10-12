import { COST_METRICS, type CostMetric } from "@/lib/cost-metrics";

export function CostMetricSelect({
  value,
  onChange,
  compact = false,
}: {
  value: CostMetric["value"];
  onChange: (value: string) => void;
  compact?: boolean;
}) {
  return (
    <span
      className={
        compact ? "inline-flex min-w-0" : "inline-flex flex-col items-end"
      }
    >
      {!compact && <span>Avg cost per trial</span>}
      <select
        aria-label="Average cost type"
        value={value}
        title="Average cost per completed model trial. Trial + QA includes both costs."
        className="hover:text-foreground focus-visible:outline-ring field-sizing-content max-w-full cursor-pointer rounded-sm bg-transparent py-1 text-inherit focus-visible:outline"
        onChange={(e) => onChange(e.target.value)}
      >
        {COST_METRICS.map((m) => (
          <option key={m.value} value={m.value}>
            {compact
              ? `Avg/trial · ${m.value === "total" ? "Trial + QA" : m.value === "trial" ? "Trial" : "QA"}`
              : m.label}
          </option>
        ))}
      </select>
    </span>
  );
}
