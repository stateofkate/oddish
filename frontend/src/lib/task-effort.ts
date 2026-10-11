import type { Trial } from "@/lib/types";
import { isReviewableTrial } from "@/lib/review";
import { getMatrixStatus } from "@/lib/status-config";

/** Same population as the server's task effort: completed agent runs. */
export function avgEffort(trials: Trial[] | null | undefined) {
  let steps = 0;
  let stepRuns = 0;
  let tokens = 0;
  let tokenRuns = 0;
  for (const trial of trials ?? []) {
    if (!isReviewableTrial(trial)) continue;
    const status =
      trial.outcome ??
      getMatrixStatus(trial.status, trial.reward, trial.error_message);
    if (!["pass", "partial", "fail", "scoreless"].includes(status)) continue;
    if (trial.total_steps != null && trial.total_steps >= 0) {
      steps += trial.total_steps;
      stepRuns++;
    }
    if (
      trial.input_tokens != null &&
      trial.input_tokens >= 0 &&
      trial.output_tokens != null &&
      trial.output_tokens >= 0
    ) {
      tokens += trial.input_tokens + trial.output_tokens;
      tokenRuns++;
    }
  }
  return {
    steps: stepRuns ? steps / stepRuns : null,
    tokens: tokenRuns ? tokens / tokenRuns : null,
  };
}
