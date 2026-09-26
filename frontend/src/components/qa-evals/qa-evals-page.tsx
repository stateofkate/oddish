"use client";

import { useState } from "react";
import useSWR from "swr";
import { ArrowLeft, Play, Plus } from "lucide-react";
import { LabelView } from "@/components/qa-evals/label-view";
import { Pane, PaneState } from "@/components/qa-evals/pane";
import { RunDialog } from "@/components/qa-evals/run-dialog";
import { RunsView } from "@/components/qa-evals/runs-view";
import { useQAEvalsView } from "@/components/qa-evals/use-qa-evals-view";
import { Button } from "@/components/ui/button";
import { fetcher } from "@/lib/api";
import {
  RUNS_VIEW,
  pollInterval,
  selection,
  selectRun,
  statusUrl,
  type QAEvalStatus,
  type QAGoldenReview,
  type QAGoldensSummary,
} from "@/lib/qa-evals";
import { EXECUTION_LABELS } from "@/lib/review";
import type { AnalysisClassification } from "@/lib/types";

/** What scores count against: the counted goldens by label, then the ones
 * that do not count yet, each opening its review queue. */
function GoldensLine({
  goldens,
  onReview,
}: {
  goldens: QAGoldensSummary;
  onReview: (review: QAGoldenReview) => void;
}) {
  const byLabel = goldens.labels
    .map(
      ({ label, count }) =>
        `${count} ${EXECUTION_LABELS[label as AnalysisClassification] ?? label}`
    )
    .join(" · ");
  const review = (key: QAGoldenReview, count: number, text: string) =>
    count > 0 && (
      <>
        {" · "}
        <button
          type="button"
          className="hover:text-[color:var(--paper-ink)] hover:underline"
          onClick={() => onReview(key)}
        >
          {count} {text}
        </button>
      </>
    );
  return (
    <span className="text-xs text-[color:var(--paper-ink-3)]">
      <span className="text-[color:var(--paper-ink)]">
        {goldens.counted} goldens
      </span>
      {byLabel && ` (${byLabel})`}
      {review("waiting", goldens.waiting, "waiting for review")}
      {review("stale", goldens.stale, "stale")}
    </span>
  );
}

/** The admin panel's QA Evals tab: the goldens line and view switch, then
 * the runs or the labeling view, and the dialog starting a run. */
export function QaEvalsPage() {
  const [view, show] = useQAEvalsView();
  // Polls while a run is still replaying, and stops once none is.
  const status = useSWR<QAEvalStatus>(statusUrl(view.stage), fetcher, {
    revalidateOnFocus: true,
    refreshInterval: (data) => pollInterval(data?.runs),
  });
  const data = status.data;
  const [dialog, setDialog] = useState({
    open: false,
    trials: null as string[] | null,
  });
  const openDialog = (trials: string[] | null) =>
    setDialog({ open: true, trials });

  return (
    <div className="mx-auto flex max-w-[1800px] flex-col gap-3">
      {data?.replayable !== false && (
        <header className="flex flex-wrap items-center gap-x-4 gap-y-2">
          {data && (
            <GoldensLine
              goldens={data.goldens}
              onReview={(review) => show({ review, experiment: null })}
            />
          )}
          <div className="ml-auto flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              className="h-7 gap-1.5"
              onClick={() => show(view.label ? RUNS_VIEW : { label: true })}
            >
              {view.label ? (
                <>
                  <ArrowLeft className="h-3.5 w-3.5" /> Runs
                </>
              ) : (
                <>
                  <Plus className="h-3.5 w-3.5" /> Add to goldens
                </>
              )}
            </Button>
            <Button
              size="sm"
              className="h-7 gap-1.5"
              disabled={!data?.goldens.counted}
              onClick={() => openDialog(null)}
            >
              <Play className="h-3.5 w-3.5" /> New run
            </Button>
          </div>
        </header>
      )}

      {!data ? (
        <Pane>
          <PaneState error={status.error} loading />
        </Pane>
      ) : view.label && data.replayable ? (
        <LabelView />
      ) : (
        <RunsView status={data} onRerun={openDialog} />
      )}

      {data && (
        <RunDialog
          open={dialog.open}
          stage={view.stage}
          status={data}
          trials={dialog.trials}
          defaultVersion={selection(data, view).shown}
          onClose={() => setDialog((d) => ({ ...d, open: false }))}
          onStarted={(experimentId) => {
            void status.mutate();
            show(selectRun(experimentId));
          }}
        />
      )}
    </div>
  );
}
