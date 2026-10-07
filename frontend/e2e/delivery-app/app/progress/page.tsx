"use client";
import { useState } from "react";
import { TrialDetailPanel } from "../../../../src/components/trial-detail-panel";
import { progressTrial } from "../../../progress-fixtures";
import type { Trial, Task } from "../../../../src/lib/types";
const task: Task = {
  id: "task-1",
  name: "progress fixture",
  task_path: "fixture",
  status: "running",
  priority: "low",
  experiment_id: "fixture",
  experiment_name: "fixture",
  experiment_is_public: false,
  total: 1,
  completed: 0,
  failed: 0,
  created_at: progressTrial.created_at,
  updated_at: progressTrial.created_at,
};
export default function Page() {
  const [readOnly, setReadOnly] = useState(false);
  const [trial, setTrial] = useState<Trial>(progressTrial);
  return (
    <div className="mx-auto h-screen max-w-3xl">
      <button
        onClick={() =>
          setTrial({
            ...progressTrial,
            status: "failed",
            harbor_stage: "cancelled",
            error_message: "Cancelled by user",
            finished_at: "2026-10-06T12:01:00Z",
          })
        }
      >
        Table reports cancelled
      </button>
      <button
        onClick={() =>
          setTrial({
            ...progressTrial,
            attempts: 2,
            status: "queued",
            started_at: null,
          })
        }
      >
        Table reports retry
      </button>
      <button onClick={() => { setReadOnly(true); setTrial({ ...progressTrial, harbor_stage: "environment_setup" }); }}>Public view</button>
      <button onClick={() => { setReadOnly(true); setTrial({ ...progressTrial, harbor_stage: null }); }}>Public without stage</button>
      <TrialDetailPanel
        readOnly={readOnly}
        isOpen
        task={task}
        onRetried={() => {}}
        onClose={() => {}}
        trial={trial}
        showAnalysis={false}
        allowRetry={false}
        contentOnly
      />
    </div>
  );
}
