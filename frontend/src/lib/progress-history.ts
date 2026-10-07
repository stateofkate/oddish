import type { EnvInfo, EnvSubphasePayload } from "./setup-events";

export interface ProgressHistory {
  trial_id: string;
  current_attempt: number | null;
  history_available: boolean;
  history_truncated: boolean;
  events: Array<{
    attempt: number;
    event_key: string;
    kind: "env_subphase" | "env_info";
    source_seq: number | null;
    schema_version: 1 | 2;
    recorded_at: string;
    payload: EnvSubphasePayload | EnvInfo;
  }>;
}
