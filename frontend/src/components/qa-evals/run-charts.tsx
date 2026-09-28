"use client";

import { useState } from "react";
import {
  CartesianGrid,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  type XAxisProps,
} from "recharts";
import { AgentLegend } from "@/components/agent-legend";
import {
  CHART_AXIS_STROKE,
  CHART_CURSOR,
  CHART_GRID,
  chartTick,
  type ChartTooltipProps,
} from "@/components/chart-style";
import { LeaderboardBars } from "@/components/leaderboard-bars";
import { AGENT_COLORS } from "@/components/pass-at-k-graph";
import { PaperToggle } from "@/components/paper-toggle";
import { formatCostUsd, formatDate } from "@/lib/format";
import {
  formatAccuracy,
  configurationKey,
  compareThinkingEffort,
  formatScore,
  formatRunStatus,
  shortSha,
  type QAEvalRun,
  type QAEvalStatus,
  type QAEvalsChart,
} from "@/lib/qa-evals";
import { stableSeriesColors } from "@/lib/series-colors";

type Point = { x: number; y: number; run: QAEvalRun };
type Series = { key: string; model: string; color: string; points: Point[] };

const DAY_MS = 24 * 60 * 60 * 1000;
const ACCURACY_TICKS = [0, 0.25, 0.5, 0.75, 1];
const AXIS_LABEL = { fontSize: 10, fill: "var(--paper-ink-3)" } as const;
const CHARTS: { value: QAEvalsChart; label: string }[] = [
  { value: "history", label: "Accuracy over time" },
  { value: "models", label: "Models: cost vs accuracy" },
];

function RunTooltip({ active, payload }: ChartTooltipProps) {
  const run = (payload?.[0]?.payload as Point | undefined)?.run;
  if (!active || !run) return null;
  return (
    <div className="max-w-72 rounded-md border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-3 py-2 text-xs shadow-sm">
      <div className="flex flex-wrap items-center gap-2 font-medium text-[color:var(--paper-ink)]">
        {run.model_name}
        <span className="rounded border border-[color:var(--paper-line)] px-1.5 py-0.5 text-[10px] font-normal text-[color:var(--paper-ink-3)]">
          {run.reasoning_effort
            ? `${run.reasoning_effort} thinking`
            : "effort not recorded"}
        </span>
      </div>
      <div className="mt-1 text-[color:var(--paper-ink-3)]">
        {formatDate(run.created_at)} · {run.agent ?? "claude-code"} · prompt{" "}
        {shortSha(run.prompt_sha256)}
      </div>
      <div className="text-[color:var(--paper-ink-3)]">{run.title}</div>
      <div className="mt-1 font-mono">
        {formatScore(run)} · {formatCostUsd(run.cost_usd)}
        {run.cost_per_trial !== null &&
          ` · ${formatCostUsd(run.cost_per_trial)}/trial`}
      </div>
      <div className="mt-1 text-[color:var(--paper-ink-3)]">{formatRunStatus(run)}</div>
    </div>
  );
}

/** Local midnights from the first run's day to the day after the last, at
 * most about seven, so the axis always has two date labels and they match
 * the dates the run list shows. */
function dayTicks(first: number, last: number): number[] {
  const day = new Date(first);
  day.setHours(0, 0, 0, 0);
  const end = new Date(last);
  end.setHours(24, 0, 0, 0);
  const step = Math.ceil(
    Math.round((end.getTime() - day.getTime()) / DAY_MS) / 6
  );
  const ticks = [];
  for (; day.getTime() < end.getTime(); day.setDate(day.getDate() + step)) {
    ticks.push(day.getTime());
  }
  ticks.push(end.getTime());
  return ticks;
}

function shortDay(ms: number): string {
  return new Date(ms).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}

type Dots = {
  hover: string | null;
  onHover: (model: string | null) => void;
  selected: string | null;
  productionSha: string;
  onSelect: (run: QAEvalRun) => void;
};

/** Runs by accuracy against `x`, a series of dots per model (joined by a line
 * with `lines`); `children` draws beneath the dots. The production prompt's
 * dots have a dark ring, and a click opens the run. */
function RunScatter({
  xAxis,
  xLabel,
  series,
  lines = false,
  hover,
  onHover,
  selected,
  productionSha,
  onSelect,
  children,
}: Dots & {
  xAxis: XAxisProps;
  xLabel: string;
  series: Series[];
  lines?: boolean;
  children?: React.ReactNode;
}) {
  const dot =
    ({ model, color }: Series) =>
    ({ cx, cy, payload }: { cx?: number; cy?: number; payload?: Point }) => {
      if (cx === undefined || cy === undefined || !payload) return <g />;
      const { run } = payload;
      const isSelected = run.experiment_id === selected;
      const ringed = isSelected || run.prompt_sha256 === productionSha;
      return (
        <circle
          cx={cx}
          cy={cy}
          r={isSelected ? 7 : 5}
          fill={color}
          fillOpacity={hover && hover !== model ? 0.2 : 1}
          stroke={ringed ? "var(--paper-ink)" : "var(--paper-surface)"}
          strokeWidth={isSelected ? 2.5 : 1.5}
          strokeDasharray={run.pending > 0 ? "3 2" : undefined}
          aria-label={`${run.model_name}: ${formatScore(run)} · ${formatRunStatus(run)}`}
          className="cursor-pointer"
          onClick={() => onSelect(run)}
          onMouseEnter={() => onHover(model)}
          onMouseLeave={() => onHover(null)}
        />
      );
    };
  return (
    <div className="h-64">
      <ResponsiveContainer width="100%" height="100%">
        <ScatterChart margin={{ top: 8, right: 16, left: 0, bottom: 12 }}>
          <CartesianGrid {...CHART_GRID} />
          <XAxis
            type="number"
            dataKey="x"
            tick={chartTick(10)}
            stroke={CHART_AXIS_STROKE}
            label={{
              value: xLabel,
              position: "insideBottomRight",
              offset: -8,
              ...AXIS_LABEL,
            }}
            {...xAxis}
          />
          <YAxis
            type="number"
            dataKey="y"
            domain={[0, 1]}
            ticks={ACCURACY_TICKS}
            tickFormatter={formatAccuracy}
            tick={chartTick(9.5)}
            stroke={CHART_AXIS_STROKE}
            width={48}
            label={{
              value: "accuracy",
              angle: -90,
              position: "insideLeft",
              offset: 8,
              ...AXIS_LABEL,
            }}
          />
          <Tooltip
            content={(props) => (
              <RunTooltip {...(props as ChartTooltipProps)} />
            )}
            cursor={CHART_CURSOR}
            wrapperStyle={{ zIndex: 10, outline: "none" }}
          />
          {children}
          {series.map((s) => (
            <Scatter
              key={s.key}
              name={s.model}
              data={s.points}
              line={
                lines && {
                  stroke: s.color,
                  strokeWidth: 2,
                  strokeOpacity: hover && hover !== s.model ? 0.2 : 1,
                }
              }
              lineType="joint"
              shape={dot(s)}
              isAnimationActive={false}
            />
          ))}
        </ScatterChart>
      </ResponsiveContainer>
    </div>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <p className="self-center py-10 text-center text-sm text-[color:var(--paper-ink-3)]">
      {children}
    </p>
  );
}

/** The chart above the cards, one at a time: accuracy over time with a line
 * per model, or models by cost per trial and accuracy with the frontier and
 * a leaderboard of each model's latest run. Only charted runs (full and
 * scored) are plotted. The model legend serves both: a click hides a model
 * (`hidden`, kept in the URL), hover highlights it. */
export function RunCharts({
  status,
  chart,
  onChart,
  effort,
  onEffort,
  sort,
  onSort,
  hidden,
  onHidden,
  selected,
  onSelect,
}: {
  status: QAEvalStatus;
  chart: QAEvalsChart;
  effort: string | null;
  onEffort: (effort: string | null) => void;
  sort: "accuracy" | "effort";
  onSort: (sort: "accuracy" | "effort") => void;
  onChart: (chart: QAEvalsChart) => void;
  hidden: string[];
  onHidden: (hidden: string[]) => void;
  selected: string | null;
  onSelect: (run: QAEvalRun) => void;
}) {
  const [hover, setHover] = useState<string | null>(null);
  const allCharted = status.runs.filter((run) => run.charted);
  const modelRuns = new Map(allCharted.map((run) => [run.model_name, run]));
  const models = [...modelRuns.keys()].sort();
  const colors = stableSeriesColors(models, AGENT_COLORS);
  // Keep old shared links that hid execution fingerprints working.
  const hiddenModels = new Set(
    allCharted
      .filter(
        (run) =>
          hidden.includes(run.model_name) ||
          hidden.includes(configurationKey(run)) ||
          (run.configuration_sha256 &&
            hidden.includes(run.configuration_sha256))
      )
      .map((run) => run.model_name)
  );
  const efforts = [
    ...new Set(allCharted.map((run) => run.reasoning_effort ?? "unrecorded")),
  ].sort((a, b) =>
    compareThinkingEffort(
      a === "unrecorded" ? null : a,
      b === "unrecorded" ? null : b
    )
  );
  const charted = allCharted.filter(
    (run) =>
      !hiddenModels.has(run.model_name) &&
      (effort === null || (run.reasoning_effort ?? "unrecorded") === effort)
  );
  const groups = new Map<string, QAEvalRun[]>();
  for (const run of charted) {
    const key = configurationKey(run);
    const group = groups.get(key);
    if (group) group.push(run);
    else groups.set(key, [run]);
  }
  const ranked = [...groups.values()].sort((a, b) => {
    const left = a.at(-1)!;
    const right = b.at(-1)!;
    return (
      (sort === "effort"
        ? compareThinkingEffort(
            left.reasoning_effort ?? null,
            right.reasoning_effort ?? null
          )
        : 0) ||
      right.accuracy! - left.accuracy! ||
      (left.cost_per_trial ?? Infinity) - (right.cost_per_trial ?? Infinity) ||
      left.model_name.localeCompare(right.model_name)
    );
  });
  const seriesBy = (x: (run: QAEvalRun) => number | null): Series[] =>
    [...groups].map(([key, runs]) => ({
      key,
      model: runs[0].model_name,
      color: colors[runs[0].model_name],
      points: runs.flatMap((run) => {
        const at = x(run);
        return at !== null ? [{ x: at, y: run.accuracy!, run }] : [];
      }),
    }));
  const dots: Dots = {
    hover,
    onHover: setHover,
    selected,
    productionSha: status.production_prompt_sha256,
    onSelect,
  };
  const costs = charted.flatMap((run) =>
    run.cost_per_trial === null ? [] : [run.cost_per_trial]
  );
  const uncosted = charted.length - costs.length;

  let body: React.ReactNode;
  if (allCharted.length === 0) {
    body = <Empty>No scored runs over all goldens yet.</Empty>;
  } else if (hiddenModels.size === models.length) {
    body = <Empty>All models are hidden.</Empty>;
  } else if (charted.length === 0) {
    body = <Empty>No visible runs match this thinking effort.</Empty>;
  } else if (chart === "history") {
    const ticks = dayTicks(
      Math.min(...charted.map((run) => Date.parse(run.created_at))),
      Math.max(...charted.map((run) => Date.parse(run.created_at)))
    );
    body = (
      <RunScatter
        {...dots}
        lines
        xLabel="run date"
        xAxis={{
          scale: "time",
          domain: [ticks[0], ticks.at(-1)!],
          ticks,
          tickFormatter: shortDay,
        }}
        series={seriesBy((run) => Date.parse(run.created_at))}
      />
    );
  } else {
    const domain: [number, number] = costs.length
      ? [Math.min(...costs) * 0.8, Math.max(...costs) * 1.1]
      : [0, 1];
    // Recompute the frontier from visible runs so hidden efforts/models cannot
    // set the comparison line. At a tied cost, take the most accurate first.
    let best = -1;
    const frontier = charted
      .filter((run) => run.cost_per_trial !== null)
      .sort(
        (a, b) =>
          a.cost_per_trial! - b.cost_per_trial! || b.accuracy! - a.accuracy!
      )
      .flatMap((run) => {
        if (run.accuracy! <= best) return [];
        best = run.accuracy!;
        return [{ x: run.cost_per_trial!, y: run.accuracy!, run }];
      });
    body = (
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_360px]">
        {costs.length === 0 ? (
          <Empty>No scored run recorded a cost.</Empty>
        ) : (
          <RunScatter
            {...dots}
            xLabel="cost per trial"
            xAxis={{
              domain,
              tickFormatter: (usd: number) => formatCostUsd(usd),
            }}
            series={seriesBy((run) => run.cost_per_trial)}
          >
            {/* A step through the best accuracy at or below each cost,
                carried to the right edge so a one-run frontier shows. */}
            <Scatter
              data={
                frontier.length
                  ? [...frontier, { ...frontier.at(-1)!, x: domain[1] }]
                  : []
              }
              line={{
                stroke: "var(--paper-ink-3)",
                strokeDasharray: "4 3",
                strokeWidth: 1.25,
              }}
              lineType="joint"
              lineJointType="stepAfter"
              shape={() => <g />}
              tooltipType="none"
              legendType="none"
              isAnimationActive={false}
            />
          </RunScatter>
        )}
        <LeaderboardBars
          title="Models"
          caption="latest scored run over all goldens"
          nameHeading="Model"
          scoreHeading="Accuracy"
          rows={ranked.map((runs) => {
            const run = runs.at(-1)!;
            return {
              key: configurationKey(run),
              hoverKey: run.model_name,
              label: run.model_name,
              badge: run.reasoning_effort
                ? `${run.reasoning_effort} thinking`
                : "effort not recorded",
              model: run.model,
              value: run.accuracy ?? 0,
              color: colors[run.model_name],
              detail: [
                formatRunStatus(run),
                run.agent ?? "claude-code",
                run.cost_per_trial !== null &&
                  `${formatCostUsd(run.cost_per_trial)}/trial`,
                `${runs.length} scored ${runs.length === 1 ? "run" : "runs"}`,
                formatDate(run.created_at),
              ]
                .filter(Boolean)
                .join(" · "),
            };
          })}
          hoverKey={hover}
          onHover={setHover}
          emptyText="All models are hidden."
        />
      </div>
    );
  }

  return (
    <>
      <p className="mb-2 text-xs text-[color:var(--paper-ink-3)]">
        Accuracy uses scored cases. Failed and pending cases are excluded; pending runs show provisional scores.
      </p>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        {CHARTS.map((c) => (
          <PaperToggle
            key={c.value}
            pressed={c.value === chart}
            onClick={() => onChart(c.value)}
            className="h-7 text-xs font-normal"
          >
            {c.label}
          </PaperToggle>
        ))}
        <label className="ml-auto flex items-center gap-2 text-xs text-[color:var(--paper-ink-3)]">
          Thinking effort
          <select
            aria-label="Thinking effort"
            value={effort ?? "all"}
            onChange={(event) =>
              onEffort(event.target.value === "all" ? null : event.target.value)
            }
            className="h-7 rounded-md border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-2 text-[color:var(--paper-ink)]"
          >
            <option value="all">All efforts</option>
            {effort && !efforts.includes(effort) && (
              <option value={effort}>{effort}</option>
            )}
            {efforts.map((value) => (
              <option key={value} value={value}>
                {value === "unrecorded" ? "Not recorded" : value}
              </option>
            ))}
          </select>
        </label>
        {chart === "models" && (
          <label className="flex items-center gap-2 text-xs text-[color:var(--paper-ink-3)]">
            Sort by
            <select
              aria-label="Sort models"
              value={sort}
              onChange={(event) =>
                onSort(event.target.value as "accuracy" | "effort")
              }
              className="h-7 rounded-md border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-2 text-[color:var(--paper-ink)]"
            >
              <option value="accuracy">Accuracy</option>
              <option value="effort">Thinking effort</option>
            </select>
          </label>
        )}
        {chart === "models" && uncosted > 0 && (
          <span className="ml-1 text-xs text-[color:var(--paper-ink-3)]">
            {uncosted} {uncosted === 1 ? "run has" : "runs have"} no recorded
            cost
          </span>
        )}
      </div>
      {body}
      {models.length > 0 && (
        <div className="flex flex-wrap items-center gap-x-3">
          <AgentLegend
            items={models.map((model) => ({
              key: model,
              label: model,
              color: colors[model],
              model: modelRuns.get(model)?.model,
            }))}
            hiddenKeys={hiddenModels}
            onToggle={(model) =>
              onHidden(
                hiddenModels.has(model)
                  ? [...hiddenModels].filter((m) => m !== model)
                  : [...hiddenModels, model]
              )
            }
            hoverKey={hover}
            onHover={setHover}
          />
          <span className="mt-2 inline-flex items-center gap-1.5 text-xs text-[color:var(--paper-ink-3)]">
            <span className="h-2.5 w-2.5 rounded-full border-2 border-[color:var(--paper-ink)]" />
            production prompt
          </span>
          {chart === "models" && (
            <span className="mt-2 inline-flex items-center gap-1.5 text-xs text-[color:var(--paper-ink-3)]">
              <span className="w-4 border-t-[1.5px] border-dashed border-[color:var(--paper-ink-3)]" />
              frontier
            </span>
          )}
        </div>
      )}
    </>
  );
}
