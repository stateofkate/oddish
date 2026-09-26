"use client";

import { ExperimentPicker } from "@/components/qa-evals/experiment-picker";
import { LabelQueue } from "@/components/qa-evals/label-queue";
import { PaneGrid } from "@/components/qa-evals/pane";
import { useQAEvalsView } from "@/components/qa-evals/use-qa-evals-view";
import { queueUrl } from "@/lib/qa-evals";

/** Adding goldens: pick an experiment (or a review queue) and label its
 * trials. */
export function LabelView() {
  const [view, show] = useQAEvalsView();
  return (
    <PaneGrid>
      <ExperimentPicker view={view} show={show} />
      {/* A new queue starts with its cursor on the first trial. */}
      <LabelQueue key={queueUrl(view)} view={view} show={show} />
    </PaneGrid>
  );
}
