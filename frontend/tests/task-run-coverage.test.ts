import assert from "node:assert/strict";
import test from "node:test";
import {
  BROWSE_FORWARD_KEYS,
  FILTER_PARAM_KEYS,
  RUN_COVERAGE_OPTIONS,
  SORT_OPTIONS,
  filterParams,
  searchParamsToFilters,
} from "../src/lib/tasks-filters.ts";

test("coverage round trips through shared URL, saved view and forwarding registries", () => {
  for (const { value } of RUN_COVERAGE_OPTIONS) {
    const filters = searchParamsToFilters(
      new URLSearchParams(`run_coverage=${value}`)
    );
    assert.equal(filters.runCoverage, value);
    const encoded = new URLSearchParams(filterParams(filters));
    assert.equal(encoded.get("run_coverage"), value === "any" ? null : value);
    assert.deepEqual(searchParamsToFilters(encoded), filters);
  }
  assert.equal(
    searchParamsToFilters(new URLSearchParams("run_coverage=bad")).runCoverage,
    "any"
  );
  assert.ok(FILTER_PARAM_KEYS.includes("run_coverage"));
  assert.ok(BROWSE_FORWARD_KEYS.includes("run_coverage"));
  for (const token of [
    "model_score_asc",
    "model_score_desc",
    "last_model_activity_desc",
  ])
    assert.ok(SORT_OPTIONS.some((option) => option.value === token));
});
