"use client";

import useSWR from "swr";
import {
  PANE_WINDOW,
  Pane,
  PaneGrid,
  PaneState,
} from "@/components/qa-evals/pane";
import { PromptCard } from "@/components/qa-evals/prompt-card";
import { RunCases } from "@/components/qa-evals/run-cases";
import { RunCharts } from "@/components/qa-evals/run-charts";
import { RunList } from "@/components/qa-evals/run-list";
import { useQAEvalsView } from "@/components/qa-evals/use-qa-evals-view";
import { fetcher } from "@/lib/api";
import {
  pollInterval,
  runUrl,
  selection,
  selectRun,
  type QAEvalRun,
  type QAEvalRunDetail,
  type QAEvalStatus,
} from "@/lib/qa-evals";
import { cn } from "@/lib/utils";

/** Runs of one stage: the charts, the run list, the shown prompt change and
 * the selected run's tasks. A stage that cannot replay shows only its
 * prompts. */
export function RunsView({
  status,
  onRerun,
}: {
  status: QAEvalStatus;
  onRerun: (sourceTrialIds: string[]) => void;
}) {
  const [view, show] = useQAEvalsView();
  const { runId, shown, base } = selection(status, view);
  const detail = useSWR<QAEvalRunDetail>(
    status.replayable && runId ? runUrl(runId) : null,
    fetcher,
    {
      revalidateOnFocus: true,
      refreshInterval: (data) => pollInterval(data && [data.summary]),
    }
  );
  const openRun = (run: QAEvalRun) =>
    show(selectRun(run.experiment_id));

  const promptCard = (
    <PromptCard
      stage={view.stage}
      versions={status.versions}
      shown={shown}
      base={base}
      onPick={show}
    />
  );
  if (!status.replayable) {
    return <div className={cn("flex flex-col", PANE_WINDOW)}>{promptCard}</div>;
  }
  return (
    <>
      <Pane className="shrink-0 px-4 py-3">
        <RunCharts
          status={status}
          chart={view.chart}
          onChart={(chart) => show({ chart })}
          hidden={view.hide}
          onHidden={(hide) => show({ hide })}
          selected={runId}
          onSelect={openRun}
        />
      </Pane>
      <PaneGrid>
        <Pane title="Runs">
          <PaneState empty={status.runs.length === 0 && "No runs yet."}>
            <RunList status={status} selected={runId} onSelect={openRun} />
          </PaneState>
        </Pane>
        <div className="grid min-h-0 flex-1 gap-3 xl:grid-cols-[minmax(0,1fr)_420px]">
          {promptCard}
          <Pane
            title={`Tasks${detail.data ? ` · ${detail.data.cases.length}` : ""}`}
          >
            <div className="min-h-0 flex-1 overflow-y-auto">
              <PaneState
                error={detail.error}
                loading={Boolean(runId) && !detail.data}
                empty={!runId && "No runs yet."}
              >
                {detail.data && (
                  <RunCases
                    run={detail.data}
                    filter={view.filter}
                    onFilter={(filter) => show({ filter })}
                    picks={view.trial}
                    onPicks={(trial) => show({ trial })}
                    onRerun={onRerun}
                  />
                )}
              </PaneState>
            </div>
          </Pane>
        </div>
      </PaneGrid>
    </>
  );
}
