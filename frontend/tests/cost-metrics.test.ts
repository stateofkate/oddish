import { test } from "node:test";
import assert from "node:assert/strict";
import {
  averageCost,
  selectedCostMetric,
  setCostMetric,
} from "../src/lib/cost-metrics.ts";
const cost = {
  completed_trials: 2,
  trial_sum: 10,
  trial_count: 2,
  qa_sum: 0.6,
  qa_jobs: 2,
  qa_unpriced_jobs: 0,
  qa_unattributed_jobs: 0,
};
test("cost components share the completed-trial denominator, including free trials", () => {
  assert.equal(averageCost(cost, "total"), 5.3);
  assert.equal(averageCost(cost, "trial"), 5);
  assert.equal(averageCost(cost, "qa"), 0.3);
  assert.equal(averageCost({ ...cost, qa_sum: 0, qa_jobs: 0 }, "total"), 5);
});
test("unknown prices do not become free QA or combined spend", () => {
  for (const field of ["qa_unpriced_jobs", "qa_unattributed_jobs"]) {
    assert.equal(averageCost({ ...cost, [field]: 1 }, "total"), null);
    assert.equal(averageCost({ ...cost, [field]: 1 }, "qa"), null);
    assert.equal(averageCost({ ...cost, [field]: 1 }, "trial"), 5);
  }
  assert.equal(averageCost({ ...cost, trial_count: 1 }, "total"), null);
  assert.equal(averageCost(undefined, "total"), null);
  assert.equal(averageCost({ ...cost, completed_trials: 0 }, "qa"), null);
});
test("default combined cost, URL selection and matching sort keep their direction", () => {
  const p = new URLSearchParams(
    "sort=avg_total_cost_asc&offset=24&models=grok"
  );
  assert.equal(selectedCostMetric(new URLSearchParams()).value, "total");
  setCostMetric(p, "qa");
  assert.equal(p.get("sort"), "avg_qa_cost_asc");
  assert.equal(p.get("offset"), null);
  assert.equal(p.get("models"), "grok");
  setCostMetric(p, "total");
  assert.equal(p.has("cost_metric"), false);
});
