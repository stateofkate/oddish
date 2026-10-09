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
  name: "archil40-26-openbao-cross-namespace-lease-model-evaluation",
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
  experiments: [
    { id: "exp-one", name: "Baseline validation experiment — codex-gpt-5.6-terra-high-20260922" },
    { id: "exp-rerun", name: "Task rerun — verifier-checks-and-model-results-20260922" },
  ],
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

test("compact cards keep exact outcomes, direct filters and expandable evidence", async ({ page }) => {
  let summaryUnavailable = false;
  let release: (() => void) | undefined;
  let hold = false;
  await page.route("**/api/**", async route => {
    const url = new URL(route.request().url());
    const items = url.searchParams.get("run_coverage") === "in_progress" ? [base] : [base, validation, empty];
    let body: unknown = [];
    if (url.pathname === "/api/tasks/browse") {
      if (hold) await new Promise<void>(resolve => { release = resolve; });
      body = { items: items.map(item => ({ ...item, sort_value: url.searchParams.get("sort")?.startsWith("steps_p50") ? 184 : 1234567, run_summary: summaryUnavailable ? null : item.run_summary })), limit: 24, offset: 0, has_more: false };
    } else if (url.pathname.endsWith("/count")) body = { total: items.length };
    else if (url.pathname.endsWith("/ids")) body = { ids: items.map(item => item.id), truncated: false };
    else if (url.pathname.endsWith("/experiment-options")) body = { items: [] };
    else if (url.pathname.endsWith("/facets")) body = { delivery_customers: ["GDM"], models: ["known-model"], agents: ["known-agent"] };
    else if (url.pathname === "/api/people/search") body = { items: [] };
    await route.fulfill({ json: body });
  });
  await page.goto("/tasks?mine=off");
  const results = page.getByRole("region", { name: "Results", exact: true }).first();
  await expect(results.getByText("Pass 1", { exact: true })).toBeVisible();
  await expect(results.getByText("Fail 3", { exact: true })).toBeVisible();
  await expect(results.getByText("Error 0", { exact: true })).toBeVisible();
  await expect(results.getByText("Pending 1", { exact: true })).toBeVisible();
  await expect(page.getByText("Model score:", { exact: true })).toHaveCount(0);
  await expect(page.getByText("NOP: 1/1 as expected · 1 trial", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Live updates" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("link", { name: "High effort pass Pass" })).not.toBeVisible();
  await page.locator("summary").filter({ hasText: "Runs · 35 trials" }).click();
  await expect(page.getByText("test-model/high", { exact: true })).toBeVisible();
  await expect(page.getByText("test-model/low", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Recent oracle run Pass", exact: true })).toHaveAttribute("href", "/tasks/model-task?trial=oracle-run");
  await page.locator("summary").filter({ hasText: "Experiments · 2" }).first().click();
  await expect(page.getByRole("link", { name: /Baseline validation experiment/ }).first()).toHaveAttribute("href", "/experiments/exp-one?task=model-task");
  for (const width of [1280, 900, 390]) {
    await page.setViewportSize({ width, height: 1100 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
  }
  await page.setViewportSize({ width: 1280, height: 1100 });
  await page.getByRole("heading", { name: "Tasks", exact: true }).scrollIntoViewIfNeeded();
  const sidebar = await page.getByRole("complementary", { name: "Task filters" }).boundingBox();
  const cards = await page.getByRole("region", { name: "Results", exact: true }).first().boundingBox();
  expect(sidebar!.x + sidebar!.width).toBeLessThan(cards!.x);
  await page.screenshot({ path: "tasks-sidebar-preview.png", fullPage: true });
  await expect(page.getByRole("checkbox", { name: "Accepted", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Select model", exact: true }).click();
  const modelSearch = page.getByRole("textbox", { name: "Find model options", exact: true });
  await modelSearch.fill("invented-model");
  await modelSearch.press("Enter");
  expect(new URL(page.url()).searchParams.has("models")).toBe(false);
  await expect(page.getByText("No options", { exact: true })).toBeVisible();
  await modelSearch.fill("known");
  await page.getByRole("checkbox", { name: "known-model", exact: true }).check();
  await expect(page).toHaveURL(/models=known-model/);
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Select agent", exact: true }).click();
  await page.getByRole("checkbox", { name: "known-agent", exact: true }).check();
  await expect(page).toHaveURL(/agents=known-agent/);
  await page.keyboard.press("Escape");
  await page.getByRole("checkbox", { name: "Has execution errors", exact: true }).check();
  await expect(page).toHaveURL(/harness_count_min=1/);
  await page.getByRole("checkbox", { name: "Accepted", exact: true }).check();
  await expect(page).toHaveURL(/qa_outcomes=accepted/);
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Clear filters", exact: true }).click();
  await page.getByRole("combobox", { name: "Sort tasks" }).click();
  await page.getByRole("option", { name: "Total tokens", exact: true }).click();
  await expect(page.getByText("Total tokens: 1,234,567", { exact: true })).toHaveCount(3);
  await page.getByRole("button", { name: "Highest first", exact: true }).click();
  await expect(page).toHaveURL(/sort=total_tokens_asc/);
  await page.getByRole("combobox", { name: "Task view" }).selectOption("table");
  await expect(page.getByText("Pending 1", { exact: true })).toBeVisible();
  await expect(page.getByText("Error 0", { exact: true })).toHaveCount(3);
  await page.getByRole("combobox", { name: "Task view" }).selectOption("cards");
  hold = true;
  release = undefined;
  await page.getByRole("combobox", { name: "Sort tasks" }).click();
  await page.getByRole("option", { name: "Median steps", exact: true }).click();
  await expect(page.getByText("Median steps: 1,234,567", { exact: true })).toHaveCount(0);
  await expect.poll(() => Boolean(release)).toBe(true);
  hold = false;
  release!();
  await expect(page.getByText("Median steps: 184", { exact: true })).toHaveCount(3);
  hold = true;
  release = undefined;
  await page.getByRole("radio", { name: "In progress", exact: true }).check();
  await expect(page.getByRole("status").filter({ hasText: "Updating tasks…" })).toBeVisible();
  await expect(page.getByRole("checkbox", { name: `Select ${base.name}`, exact: true })).toBeDisabled();
  await expect.poll(() => Boolean(release)).toBe(true);
  hold = false;
  release!();
  await expect(page.getByRole("link", { name: validation.name, exact: true })).toHaveCount(0);
  const selection = page.waitForRequest(request => request.url().includes("/api/tasks/browse/ids?"));
  const selectMatching = page.getByRole("button", { name: /Select .*matching tasks/ });
  await page.evaluate(() => window.scrollTo(0, 0));
  await selectMatching.click();
  expect(new URL((await selection).url()).searchParams.get("run_coverage")).toBe("in_progress");
  await page.screenshot({ path: "tasks-mece-preview.png", fullPage: true });
  summaryUnavailable = true;
  await page.reload();
  await expect(page.getByText("Run summary unavailable", { exact: true })).toBeVisible();
  await expect(page.getByText("No model trials", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Pass 0", { exact: true })).toHaveCount(0);
  await page.route("**/api/tasks/browse/count?*", route => route.fulfill({ status: 500, json: { error: "Unavailable" } }));
  await page.reload();
  await expect(page.getByText("Task count unavailable", { exact: true })).toBeVisible();
});
