"use client";

import { Suspense, useState } from "react";
import { TaskOverviewPanel } from "@/components/task-overview-panel";
import { TrialDetailPanel } from "@/components/trial-detail-panel";
import { tasks } from "../../records";

export default function FeedbackPreview() {
  const [run, setRun] = useState(1);
  const task = {
    ...tasks[0],
    verdict_status: "success" as const,
    verdict: {
      is_good: true,
      confidence: "high",
      reasoning: "Source audit and baseline checks passed.",
      _graded_by: `qa-${run}`,
    },
    must_fix_count: 0,
  };
  const trial = {
    ...task.trials![0],
    analysis: { ...task.trials![0].analysis!, _graded_by: `qa-${run}` },
  };
  return (
    <Suspense>
      <button onClick={() => setRun(run + 1)}>Show replacement reports</button>
      <div className="mx-auto grid max-w-6xl grid-cols-2 gap-4 p-6">
        <TaskOverviewPanel
          taskId={task.id}
          version={task.current_version}
          verdictTask={task}
          scopeTrials={[]}
          checksFindings={[]}
          checksStatus="success"
          checksTrialId={`audit-${run}`}
          checksRerunning={false}
        />
        <TrialDetailPanel
          isOpen
          trial={trial}
          task={task}
          contentOnly
          requireTrialDetail={false}
          onClose={() => {}}
          onRetried={() => {}}
          allowRetry={false}
          allowDelete={false}
        />
      </div>
    </Suspense>
  );
}
