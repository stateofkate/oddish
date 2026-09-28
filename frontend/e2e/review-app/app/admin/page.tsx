"use client";

import { Suspense } from "react";
import { RunList } from "@/components/qa-evals/run-list";
import { RunCharts } from "@/components/qa-evals/run-charts";
import { useQAEvalsView } from "@/components/qa-evals/use-qa-evals-view";
import type { QAEvalRun, QAEvalStatus } from "@/lib/qa-evals";

const run = (
  id: string,
  model: string,
  effort: string | null,
  accuracy: number,
  cost: number,
  date: string
): QAEvalRun => ({
  experiment_id: id,
  experiment_name: id,
  title: id,
  created_at: `2026-09-${date}T12:00:00Z`,
  model,
  model_name: model.split("/").at(-1)!,
  agent: "claude-code",
  reasoning_effort: effort,
  configuration_sha256: id,
  prompt_sha256: "production",
  subset: false,
  charted: true,
  replays: 25,
  with_golden: 25,
  judged: 25,
  correct: accuracy * 25,
  errored: 0,
  pending: 0,
  accuracy,
  cost_usd: cost * 25,
  cost_per_trial: cost,
});
const runs = [
  run("luna-old", "openai/gpt-6-luna", "high", 0.92, 0.15, "27"),
  run("sonnet-old", "anthropic/claude-sonnet-5", "high", 0.88, 0.65, "27"),
  run("luna-low", "gpt-6-luna", "low", 0.72, 0.05, "28"),
  run("sol-unknown", "openai/gpt-6-sol", null, 0.76, 1.86, "28"),
  run("luna-new", "gpt-6-luna", "high", 0.8, 0.18, "28"),
  { ...run("sonnet-new", "claude-sonnet-5", "high", 21 / 23, 0.62, "28"), judged: 23, correct: 21, errored: 1, pending: 1 },
];
const status: QAEvalStatus = {
  replayable: true,
  production_prompt_sha256: "production",
  goldens: { counted: 25, waiting: 0, stale: 0, labels: [] },
  cost_per_trial: null,
  spend: { total_usd: 0, trials: 0, cost_missing: 0, by_model: [] },
  runs,
  frontier: [],
  models: [],
  versions: [],
};
function Chart() {
  const [view, show] = useQAEvalsView();
  return (
    <div className="mx-auto max-w-[1500px] rounded-xl border p-4">
      <RunCharts
        status={status}
        chart={view.chart}
        onChart={(chart) => show({ chart })}
        effort={view.effort}
        onEffort={(effort) => show({ effort })}
        sort={view.sort}
        onSort={(sort) => show({ sort })}
        hidden={view.hide}
        onHidden={(hide) => show({ hide })}
        selected={view.run}
        onSelect={(run) => show({ run: run.experiment_id })}
      />
      <section aria-label="Runs" className="mt-4 max-w-sm">
        <RunList status={status} selected={view.run} onSelect={(run) => show({run: run.experiment_id})} />
      </section>
    </div>
  );
}
export default function Page() {
  return (
    <>
      <h1 className="mb-4 text-xl">QA Evals · chart regression fixture</h1>
      <Suspense>
        <Chart />
      </Suspense>
    </>
  );
}
