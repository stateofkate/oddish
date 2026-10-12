import { expect, test } from "@playwright/test";
import type { TaskBrowseItem } from "@/lib/types";
const base: Omit<TaskBrowseItem, "id" | "name"> = {
  version_count: 2, pass_count: 1, partial_count: 0, fail_count: 1, harness_count: 0, skipped_count: 0, pending_count: 0,
  cost_trial_count: 0, cost_has_estimated: false, cost_has_native: false, billed_cost_usd: 0, billed_trial_count: 0, billed_has_estimated: false, billed_has_native: false, latest_trials_truncated: false,
  total_trials: 2,
  completed_trials: 2,
  failed_trials: 0,
  reward_success: 1,
  reward_sum: 1,
  reward_total: 2,
  cost_usd: 0,
  user_tags: [],
  experiments: [],
  latest_trials: [],
  current_version: 2,
  current_version_id: "v2",
};
const rows: TaskBrowseItem[] = [
  {
    ...base,
    id: "a",
    name: "Measured task",
    effort: {
      cost: { completed_trials: 2, trial_sum: 10, trial_count: 2, qa_sum: .6, qa_jobs: 2, qa_unpriced_jobs: 0, qa_unattributed_jobs: 0 },
      completed_trials: 2,
      steps_sum: 40,
      steps_trial_count: 2,
      tokens_sum: 1500000,
      tokens_trial_count: 1,
      cached_input_tokens_sum: 600000,
      cached_input_tokens_trial_count: 2,
      uncached_input_tokens_sum: 400000,
      uncached_input_tokens_trial_count: 1,
      output_tokens_sum: 0,
      output_tokens_trial_count: 1,
    },
  },
  { ...base, id: "b", name: "Unmeasured task" },
];
test.beforeEach(async ({ page }) => {
  await page.route("**/api/tasks/browse**", (route) => {
    return route.fulfill({
      json: new URL(route.request().url()).pathname.endsWith("/count")
        ? { total: 2 }
        : { items: rows, offset: 0, limit: 24, has_more: false },
    });
  });
});
test("averages follow results; sorting preserves filters and resets pagination", async ({
  page,
}, testInfo) => {
  await page.goto("/comparison?view=table&offset=24&filter_search=sample");
  await expect(page.getByRole("columnheader")).toHaveText([
    "Select",
    "Task",
    "QA outcome",
    "Trials",
    "Avg steps",
    "Avg tokens · TotalAvg tokens · Uncached inputAvg tokens · Cached inputAvg tokens · Output",
    "Avg cost per trialTrial + QATrial onlyQA only",
    "Delivery history",
    "Details",
  ]);
  const measured = page.getByRole("row").filter({ hasText: "Measured task" });
  await expect(measured.getByText("20", { exact: true })).toBeVisible();
  await expect(measured.getByText("1.5M", { exact: true })).toBeVisible();
  await measured.getByText("1.5M", { exact: true }).focus();
  await expect(page.getByRole("tooltip")).toContainText(
    "1 of 2 completed model trials",
  );
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Sort by average total tokens", exact: true }).click();
  await expect(page).toHaveURL(/sort=avg_tokens_desc/);
  expect(new URL(page.url()).searchParams.get("offset")).toBeNull();
  expect(new URL(page.url()).searchParams.get("filter_search")).toBe("sample");
  await expect(
    page.getByRole("columnheader", { name: "Avg tokens" }),
  ).toHaveAttribute("aria-sort", "descending");
  await page.getByRole("button", { name: "Sort by average total tokens", exact: true }).click();
  await expect(page).toHaveURL(/sort=avg_tokens_asc/);
  await expect(measured.getByText(/Avg tokens:/)).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("comparison-desktop.png"), fullPage: true });
});
test("saved column choices and missing values remain explicit", async ({
  page,
}) => {
  await page.goto(
    "/comparison?view=table&columns=Trials,Avg%20steps,Avg%20tokens",
  );
  await expect(
    page
      .getByRole("row")
      .filter({ hasText: "Unmeasured task" })
      .getByText("—", { exact: true }),
  ).toHaveCount(2);
  await page.getByText("Columns", { exact: true }).click();
  await page.getByLabel("Avg tokens", { exact: true }).uncheck();
  await expect(
    page.getByRole("columnheader", { name: "Avg tokens" }),
  ).toHaveCount(0);
  await page.reload();
  await expect(
    page.getByRole("columnheader", { name: "Avg tokens" }),
  ).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});

test("token selector switches values, coverage, sort and URL without extra columns", async ({ page }) => {
  await page.goto("/comparison?view=table&sort=avg_tokens_desc&models=grok&offset=24");
  const selector = page.getByRole("combobox", { name: "Average token metric" });
  await selector.selectOption("cached_input");
  await expect(page).toHaveURL(/sort=avg_cached_input_tokens_desc/);
  expect(new URL(page.url()).searchParams.get("models")).toBe("grok");
  expect(new URL(page.url()).searchParams.has("offset")).toBe(false);
  const measured = page.getByRole("row").filter({ hasText: "Measured task" });
  await expect(measured.getByText("300K", { exact: true })).toBeVisible();
  await measured.getByText("300K", { exact: true }).focus();
  await expect(page.getByRole("tooltip")).toContainText("2 of 2 completed model trials");
  await page.keyboard.press("Escape");
  await page.reload();
  await expect(selector).toHaveValue("cached_input");
  await selector.selectOption("uncached_input");
  await expect(measured.getByText("400K", { exact: true })).toBeVisible();
  await selector.selectOption("output");
  await expect(measured.getByText("0", { exact: true })).toBeVisible();
  await expect(page.getByRole("row").filter({ hasText: "Unmeasured task" }).getByText("—", { exact: true })).toHaveCount(3);
  await expect(page.getByRole("columnheader")).toHaveCount(9);
});

test("narrow table keeps selector accessible without widening the page", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/comparison?view=table&token_metric=output");
  const selector = page.getByRole("combobox", { name: "Average token metric" });
  await selector.selectOption("cached_input");
  await expect(selector).toHaveValue("cached_input");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({path: testInfo.outputPath("token-selector-mobile.png")});
});

test("saved views persist and restore token metric with cohort filters", async ({ page }) => {
  let saved: Record<string, unknown> | null = null;
  await page.route("**/api/tags", route => route.fulfill({ json: { items: [] } }));
  await page.route("**/api/tag-filters", async route => {
    if (route.request().method() === "POST") {
      saved = { ...route.request().postDataJSON(), id: "saved-token-view" };
      return route.fulfill({ json: saved });
    }
    return route.fulfill({ json: { items: saved ? [saved] : [] } });
  });
  await page.goto("/comparison?view=table&token_metric=cached_input&models=grok");
  await page.getByRole("button", { name: "Saved views", exact: true }).click();
  await page.getByRole("textbox", { name: "View name" }).fill("Cache comparison");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect.poll(() => saved).not.toBeNull();
  expect(saved).toMatchObject({ filter_ast: { params: { token_metric: "cached_input", models: "grok" } } });
  await page.goto("/comparison?view=table");
  await page.getByRole("button", { name: "Saved views", exact: true }).click();
  await page.getByRole("button", { name: "Cache comparison", exact: true }).click();
  await expect(page.getByRole("combobox", { name: "Average token metric" })).toHaveValue("cached_input");
  expect(new URL(page.url()).searchParams.get("models")).toBe("grok");
});


test("cost selector switches components and sorting, persists URL and excludes missing data", async ({page}) => {
  await page.goto("/comparison?view=table&sort=avg_total_cost_desc&offset=24&models=grok");
  const row=page.getByRole("row").filter({hasText:"Measured task"});
  const selector=page.getByRole("combobox",{name:"Average cost type"});
  await expect(selector).toHaveValue("total");
  await expect(row.getByText("$5.30",{exact:true})).toBeVisible();
  await selector.selectOption("qa");
  await expect(row.getByText("$0.30",{exact:true})).toBeVisible();
  await expect(page).toHaveURL(/sort=avg_qa_cost_desc/);
  expect(new URL(page.url()).searchParams.has("offset")).toBe(false);
  expect(new URL(page.url()).searchParams.get("models")).toBe("grok");
  await page.reload();
  await expect(selector).toHaveValue("qa");
  await page.getByRole("button",{name:"Sort by average cost",exact:true}).click();
  await expect(page).toHaveURL(/sort=avg_qa_cost_asc/);
  await selector.selectOption("trial");
  await expect(row.getByText("$5.00",{exact:true})).toBeVisible();
  await selector.selectOption("total");
  await expect(row.getByText("$5.30",{exact:true})).toBeVisible();
  expect(new URL(page.url()).searchParams.has("cost_metric")).toBe(false);
});
