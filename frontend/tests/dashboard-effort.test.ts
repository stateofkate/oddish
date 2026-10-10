import assert from "node:assert/strict";
import test from "node:test";
import {
  buildDashboardApiPath,
  dashboardExperimentsRequest,
} from "../src/lib/dashboard-request.ts";
import { setTokenMetric } from "../src/lib/token-metrics.ts";

const request = (params: URLSearchParams) =>
  new URL(
    buildDashboardApiPath(dashboardExperimentsRequest(params)),
    "https://x",
  ).searchParams;

test("average ranges remain separate from per-trial matching, normalize and retain zero", () => {
  const params = request(
    new URLSearchParams(
      "min_steps=100&min_avg_steps=10&max_avg_steps=0&min_avg_cost=0&max_avg_cost=2.5&max_avg_tokens=Infinity&trial_metric_match=all&page=2&author=me",
    ),
  );
  assert.equal(params.get("experiments_min_steps"), "100");
  assert.equal(params.get("experiments_min_avg_steps"), "0");
  assert.equal(params.get("experiments_max_avg_steps"), "10");
  assert.equal(params.get("experiments_min_avg_cost"), "0");
  assert.equal(params.get("experiments_max_avg_cost"), "2.5");
  assert.equal(params.get("experiments_max_avg_tokens"), null);
  assert.equal(params.get("experiments_trial_metric_match"), "all");
  assert.equal(params.get("experiments_offset"), "25");
  assert.equal(params.get("experiments_author"), "me");
});

for (const metric of ["total", "cached_input", "uncached_input", "output"]) {
  test(`${metric} selector follows the displayed token sort and API filter`, () => {
    const params = new URLSearchParams("sort=avg_tokens_asc&min_avg_tokens=20");
    setTokenMetric(params, metric);
    const api = request(params);
    assert.equal(api.get("experiments_token_metric"), metric);
    assert.equal(api.get("experiments_sort"), "avg_tokens_asc");
    assert.equal(api.get("experiments_min_avg_tokens"), "20");
  });
}

test("cost sort is retained through token selection; unknown sort is recent", () => {
  const params = new URLSearchParams("sort=avg_cost_desc");
  setTokenMetric(params, "output");
  assert.equal(request(params).get("experiments_sort"), "avg_cost_desc");
  assert.equal(request(params).get("experiments_token_metric"), "output");
  assert.equal(
    request(new URLSearchParams("sort=invalid")).get("experiments_sort"),
    "recent",
  );
});

test("invalid token sort uses recent ordering", () => {
  const request = dashboardExperimentsRequest(
    new URLSearchParams("sort=avg_tokens_wrong"),
  );
  assert.equal(request.experiments_sort, "recent");
});

for (const direction of ["asc", "desc"]) {
  test(`score ${direction} ordering reaches the paginated API`, () => {
    const params = request(new URLSearchParams(`sort=avg_score_${direction}&page=2`));
    assert.equal(params.get("experiments_sort"), `avg_score_${direction}`);
    assert.equal(params.get("experiments_offset"), "25");
  });
}
