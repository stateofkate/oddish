"use client";

import { Suspense } from "react";
import { ExperimentDetailView } from "@/components/experiment-detail-view";

export default function EarlyPlayback() {
  return <Suspense>
    <ExperimentDetailView experimentId="robot-experiment" tasksForExperiment={[]}
      isLoading isLoadingTrials pagesComplete={false} costTotals={{ status: "loading" }}
      onRetryCostTotals={() => {}} headerLeft="Loading experiment" allowRetry={false}
      showAnalysis={false} loadFullTrialOnOpen focusUrl="/api/experiments/robot-experiment/focus" />
  </Suspense>;
}
