import type { Trial } from "@/lib/types";
import { avgEffort } from "@/lib/task-effort";

const CHIP =
  "inline-flex shrink-0 items-center gap-1 rounded-[3px] bg-[color:var(--paper-bg-2)] px-1 py-px font-mono text-[9.5px] leading-[14px] font-medium whitespace-nowrap tabular-nums";

const compact = new Intl.NumberFormat("en", {
  notation: "compact",
  maximumFractionDigits: 1,
});

/** Same wording and rounding as the task page's run summary. */
export function EffortChip({ trials }: { trials: Trial[] | null | undefined }) {
  const { steps, tokens } = avgEffort(trials);
  if (steps == null && tokens == null) return null;
  return (
    <span className={`${CHIP} text-[color:var(--paper-ink-3)]`}>
      Avg steps {steps == null ? "—" : Math.round(steps).toLocaleString()}
      {" · "}Avg tokens {tokens == null ? "—" : compact.format(tokens)}
    </span>
  );
}
