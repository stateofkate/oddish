// Recharts styling shared by the app's charts, in the paper theme.

import type { TooltipContentProps } from "recharts";

/** Props of a custom <Tooltip content> renderer. */
export type ChartTooltipProps = TooltipContentProps<
  number | string | ReadonlyArray<number | string>,
  number | string
>;

export const CHART_GRID = {
  strokeDasharray: "2 4",
  stroke: "var(--paper-line-2)",
  vertical: false,
} as const;

export const CHART_AXIS_STROKE = "var(--paper-line)";

export function chartTick(fontSize: number, fill = "var(--paper-ink-3)") {
  return {
    fontSize,
    fill,
    fontFamily: "var(--font-geist-mono), ui-monospace, monospace",
  };
}

export const CHART_CURSOR = {
  stroke: "var(--paper-ink-4)",
  strokeWidth: 1,
  strokeDasharray: "3 3",
} as const;
