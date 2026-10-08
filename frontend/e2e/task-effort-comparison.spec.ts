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
      completed_trials: 2,
      steps_sum: 40,
      steps_trial_count: 2,
      tokens_sum: 1500000,
      tokens_trial_count: 1,
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
    "Avg tokens",
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
  await page.getByRole("button", { name: "Avg tokens", exact: true }).click();
  await expect(page).toHaveURL(/sort=avg_tokens_desc/);
  expect(new URL(page.url()).searchParams.get("offset")).toBeNull();
  expect(new URL(page.url()).searchParams.get("filter_search")).toBe("sample");
  await expect(
    page.getByRole("columnheader", { name: "Avg tokens" }),
  ).toHaveAttribute("aria-sort", "descending");
  await page.getByRole("button", { name: "Avg tokens", exact: true }).click();
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
