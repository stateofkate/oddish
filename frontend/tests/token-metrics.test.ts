import { test } from "node:test";
import assert from "node:assert/strict";
import { selectedTokenMetric, setTokenMetric, effortMeasurement } from "../src/lib/token-metrics.ts";

const effort = { completed_trials: 3, steps_sum: 10, steps_trial_count: 2, tokens_sum: 6_000_000_000, tokens_trial_count: 2, cached_input_tokens_sum: 0, cached_input_tokens_trial_count: 1 };
test("token averages retain separate denominators, exact large sums, recorded zero and missing", () => {
  assert.deepEqual(effortMeasurement(effort, "tokens"), {count: 2, average: 3_000_000_000});
  assert.deepEqual(effortMeasurement(effort, "cached_input_tokens"), {count: 1, average: 0});
  assert.deepEqual(effortMeasurement(effort, "output_tokens"), {count: 0, average: null});
  assert.equal(effortMeasurement(undefined, "tokens").average, null);
});
test("metric selection preserves cohort and other sorts without resetting a page", () => {
  const params = new URLSearchParams("sort=name_asc&offset=24&models=grok&columns=Avg+tokens&view=table");
  assert.equal(setTokenMetric(params, "cached_input"), false);
  assert.equal(params.get("sort"), "name_asc");
  assert.equal(params.get("offset"), "24");
  assert.equal(params.get("models"), "grok");
  assert.equal(selectedTokenMetric(params).field, "cached_input_tokens");
});
test("token sorting switches metric and resets pagination while preserving direction", () => {
  const params = new URLSearchParams("sort=avg_tokens_asc&offset=24&agents=codex");
  assert.equal(setTokenMetric(params, "output"), true);
  assert.equal(params.get("sort"), "avg_output_tokens_asc");
  assert.equal(params.get("offset"), null);
  assert.equal(params.get("agents"), "codex");
  assert.equal(selectedTokenMetric(params).value, "output");
});
test("sidebar sort wins over stale selection; invalid values and legacy links default total", () => {
  assert.equal(selectedTokenMetric(new URLSearchParams("sort=avg_cached_input_tokens_desc&token_metric=output")).value, "cached_input");
  assert.equal(selectedTokenMetric(new URLSearchParams("token_metric=invalid")).value, "total");
  assert.equal(selectedTokenMetric(new URLSearchParams("sort=avg_tokens_desc")).value, "total");
});
