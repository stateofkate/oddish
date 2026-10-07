import type { Trial } from "../src/lib/types";
import type { ProgressHistory } from "../src/lib/progress-history";
const at = "2026-10-06T12:00:00Z";
export const progressTrial: Trial = {
  id: "progress-1",
  name: "progress fixture",
  task_id: "task-1",
  task_path: "fixture",
  agent: "nop",
  provider: "default",
  model: "nop",
  status: "running",
  attempts: 1,
  max_attempts: 2,
  harbor_stage: null,
  reward: null,
  created_at: at,
  started_at: at,
};
export const progressHistory: ProgressHistory = {
  trial_id: "progress-1",
  current_attempt: 1,
  history_available: true,
  history_truncated: false,
  events: [
    {
      attempt: 1,
      event_key: "phase-start",
      kind: "env_subphase",
      source_seq: 1,
      schema_version: 1,
      recorded_at: at,
      payload: {
        phase: "kubernetes.helm_install",
        status: "started",
        started_at: at,
        duration_seconds: null,
        error_type: null,
      },
    },
  ],
};
