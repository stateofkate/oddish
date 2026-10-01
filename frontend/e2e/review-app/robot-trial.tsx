"use client";

import { useState } from "react";
import { TrialDetailPanel } from "@/components/trial-detail-panel";
import type { Task, Trial } from "@/lib/types";

/** The descriptor of a trial whose index the browser tests serve. */
export const READY_PLAYBACK: Trial["playback"] = {
  manifest_path: "verifier/artifacts/playback.json",
  index_status: "ready",
  index_path: "_oddish/playback-index/fixture-revision.json",
  index_revision: "fixture-revision",
  index_error: null,
};

const trial: Trial = {
  id: "robot-0",
  name: "Robot pick-and-place",
  task_id: "robot",
  task_path: "robot",
  agent: "oracle",
  provider: "local",
  model: null,
  status: "success",
  attempts: 1,
  max_attempts: 1,
  harbor_stage: null,
  reward: 1,
  created_at: "2026-09-29T00:00:00Z",
};

export function RobotTrialPreview({ playback }: { playback?: Trial["playback"] }) {
  const [visible, setVisible] = useState(true);
  const [analysisVisible, setAnalysisVisible] = useState(false);
  const [attempt, setAttempt] = useState(1);
  const shown = { ...trial, attempts: attempt, playback };
  const task = { id: "robot", name: "Robot pick-and-place", trials: [shown] } as Task;
  return (
    <>
      <button onClick={() => setVisible(!visible)}>Toggle viewer</button>
      <button onClick={() => setAnalysisVisible(true)}>Show analysis</button>
      <button onClick={() => setAttempt(attempt + 1)}>Next attempt</button>
      <div className="border-border mt-3 h-[calc(100vh-150px)] min-h-[480px] overflow-hidden rounded-xl border">
        {visible && (
          <TrialDetailPanel
            isOpen
            onClose={() => {}}
            trial={shown}
            task={task}
            onRetried={() => {}}
            contentOnly
            requireTrialDetail={false}
            allowRetry={false}
            showAnalysis={analysisVisible}
          />
        )}
      </div>
    </>
  );
}
