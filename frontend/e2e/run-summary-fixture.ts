import type { TaskRunSummary, TaskRunGroup } from "../src/lib/types";
export const emptyRunGroup: TaskRunGroup = {
  total: 0,
  scored: 0,
  reward_sum: 0,
  pass_count: 0,
  partial_count: 0,
  fail_count: 0,
  harness_count: 0,
  skipped_count: 0,
  scoreless_count: 0,
  other_count: 0,
  last_activity: null,
};
export function runSummary(model: Partial<TaskRunGroup>): TaskRunSummary {
  return {
    model: { ...emptyRunGroup, ...model },
    oracle: { ...emptyRunGroup },
    nop: { ...emptyRunGroup },
  };
}
