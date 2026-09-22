import { test, expect } from "@playwright/test";
import type { TaskBrowseItem, TaskRunGroup } from "../src/lib/types";

const zero: TaskRunGroup = {
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
const base: TaskBrowseItem = {
  id: "model-task",
  name: "Model evaluation",
  current_version: 1,
  current_version_id: "v1",
  version_count: 1,
  total_trials: 35,
  completed_trials: 34,
  failed_trials: 0,
  reward_success: 31,
  reward_sum: 31,
  reward_total: 34,
  pass_count: 31,
  partial_count: 0,
  fail_count: 3,
  harness_count: 0,
  skipped_count: 0,
  pending_count: 0,
  cost_usd: 1,
  cost_trial_count: 4,
  cost_has_estimated: false,
  cost_has_native: true,
  billed_cost_usd: 1,
  billed_trial_count: 4,
  billed_has_estimated: false,
  billed_has_native: true,
  qa_cost_usd: 0,
  verifier_cost_usd: 0,
  latest_trials: [
    { id: "model-pass", name: "High effort pass", agent: "codex", model: "test-model", reasoning_effort: "high", status: "success", reward: 1, error_message: null },
    { id: "model-fail", name: "High effort fail", agent: "codex", model: "test-model", reasoning_effort: "high", status: "success", reward: 0, error_message: null },
    { id: "model-running", name: "Low effort running", agent: "codex", model: "test-model", reasoning_effort: "low", status: "running", reward: null, error_message: null },
    {
      id: "oracle-run",
      name: "Recent oracle run",
      status: "success",
      reward: 1,
      error_message: null,
      agent: "oracle",
      model: null,
    },
  ],
  latest_trials_truncated: true,
  experiments: [{ id: "exp-one", name: "Baseline validation experiment" }],
  user_tags: [],
  run_summary: {
    model: {
      ...zero,
      total: 5,
      other_count: 1,
      scored: 4,
      reward_sum: 1,
      pass_count: 1,
      fail_count: 3,
    },
    oracle: { ...zero, total: 30, scored: 30, reward_sum: 30, pass_count: 30 },
    nop: zero,
  },
};
const validation: TaskBrowseItem = {
  ...base,
  id: "validation-task",
  name: "Oracle and NOP checks only",
  latest_trials: [
    { id: "validation-oracle", name: "Oracle check", agent: "oracle", model: "nop_oracle", status: "success", reward: 1, error_message: null },
    { id: "validation-nop", name: "NOP check", agent: "nop", model: "nop_oracle", status: "success", reward: 0, error_message: null },
  ],
  total_trials: 2,
  cost_usd: 0,
  cost_trial_count: 0,
  billed_cost_usd: 0,
  billed_trial_count: 0,
  run_summary: {
    model: zero,
    oracle: { ...zero, total: 1, scored: 1, reward_sum: 1, pass_count: 1 },
    nop: { ...zero, total: 1, scored: 1, fail_count: 1 },
  },
};
const empty: TaskBrowseItem = {
  ...base,
  id: "empty-task",
  name: "New task without trials",
  total_trials: 0,
  cost_usd: 0,
  cost_trial_count: 0,
  billed_cost_usd: 0,
  billed_trial_count: 0,
  latest_trials: [],
  experiments: [],
  run_summary: { model: zero, oracle: zero, nop: zero },
};

test("coverage and author scope reach selection while result links preserve context", async ({
  page,
}) => {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const coverage = url.searchParams.get("run_coverage");
    const items =
      coverage === "model_trials"
        ? [base]
        : coverage === "validation_only"
          ? [validation]
          : [base, validation, empty];
    let body: unknown = [];
    if (url.pathname === "/api/tasks/browse") {
      body = { items, limit: 24, offset: 0, has_more: false };
    } else if (url.pathname.endsWith("/count")) body = { total: items.length };
    else if (url.pathname.endsWith("/ids"))
      body = { ids: items.map((item) => item.id), truncated: false };
    else if (url.pathname.endsWith("/facets")) body = {};
    await route.fulfill({ json: body });
  });
  await page.goto("/tasks?mine=off");
  await expect(
    page.getByText("Model score: 25%", { exact: true })
  ).toBeVisible();
  await expect(page.getByText("No model trials", { exact: true })).toHaveCount(
    2
  );
  await expect(
    page.getByText("NOP: 1/1 as expected · 1 trial", {
      exact: true,
    })
  ).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Task view" })).toHaveValue("cards");
  await expect(page.getByText("test-model/high", { exact: true })).toBeVisible();
  await expect(page.getByText("test-model/low", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Baseline validation experiment" }).first()
  ).toHaveAttribute("href", "/experiments/exp-one?task=model-task");
  await expect(
    page.getByRole("link", { name: "Recent oracle run Pass" }).first()
  ).toHaveAttribute("href", "/tasks/model-task?trial=oracle-run");
  await page.screenshot({ path: "task-run-coverage.png", fullPage: true });
  await page
    .getByRole("combobox", { name: "Author", exact: true })
    .selectOption("first");
  await page
    .getByRole("combobox", { name: "Sort tasks" })
    .selectOption("model_score_desc");
  await expect(page).toHaveURL(/mine=first/);
  await expect(page).toHaveURL(/sort=model_score_desc/);
  await page
    .getByRole("combobox", { name: "Runs on current version" })
    .selectOption("model_trials");
  await expect(page).toHaveURL(/run_coverage=model_trials/);
  await expect(
    page.getByText("Oracle and NOP checks only", { exact: true })
  ).toHaveCount(0);
  const selection = page.waitForRequest((request) =>
    request.url().includes("/api/tasks/browse/ids?")
  );
  await page.getByRole("button", { name: /Select .*matching tasks/ }).click();
  expect(
    new URL((await selection).url()).searchParams.get("run_coverage")
  ).toBe("model_trials");
  await page
    .getByRole("button", { name: "Clear filters", exact: true })
    .click();
  await expect(
    page.getByRole("combobox", { name: "Runs on current version" })
  ).toHaveValue("any");
});
