import { TOKEN_METRICS, type TokenMetric } from "@/lib/token-metrics";

export function TokenMetricSelect({
  value,
  onChange,
}: {
  value: TokenMetric["value"];
  onChange: (value: string) => void;
}) {
  return (
    <select
      aria-label="Average token metric"
      className="hover:text-foreground focus-visible:outline-ring max-w-full cursor-pointer rounded-sm bg-transparent py-2 text-xs focus-visible:outline"
      value={value}
      onChange={(event) => onChange(event.target.value)}
    >
      {TOKEN_METRICS.map((metric) => (
        <option key={metric.value} value={metric.value}>
          Avg tokens · {metric.label}
        </option>
      ))}
    </select>
  );
}
