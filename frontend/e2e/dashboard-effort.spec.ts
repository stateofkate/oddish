import { expect, test } from "@playwright/test";

const effort = {
  completed_trials: 3,
  steps_sum: 21,
  steps_trial_count: 2,
  tokens_sum: 20000,
  tokens_trial_count: 2,
  cached_input_tokens_sum: 10000,
  cached_input_tokens_trial_count: 2,
  uncached_input_tokens_sum: 8000,
  uncached_input_tokens_trial_count: 2,
  output_tokens_sum: 2000,
  output_tokens_trial_count: 2,
  cost_sum: 5,
  cost_trial_count: 2,
};
const counts = {
  task_count: 1,
  total_trials: 3,
  completed_trials: 3,
  failed_trials: 0,
  skipped_trials: 0,
  retrying_trials: 0,
  active_trials: 0,
  avg_score: 1,
  author: null,
  last_runner: null,
  last_author: null,
  user_tags: [],
  summary_pending: false,
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/**", (route) =>
    route.fulfill({
      json: {
        capacity: [],
        queues: null,
        model_usage: [],
        job_usage: [],
        leaders: [],
        items: [],
        limit_usd: 100,
        used_usd: 0,
      },
    }),
  );
  await page.route("**/api/dashboard?**", (route) =>
    route.fulfill({
      json: {
        capacity: [],
        queues: null,
        model_usage: [],
        job_usage: [],
        experiments: [
          { ...counts, id: "measured", name: "Measured experiment", effort },
          { ...counts, id: "unknown", name: "Unknown experiment" },
        ],
        experiments_has_more: false,
      },
    }),
  );
});

test("compact averages, independent coverage, selector and persisted sort", async ({
  page,
}) => {
  await page.goto("/dashboard");
  const row = page.getByRole("row").filter({ hasText: "Measured experiment" });
  await expect(row).toContainText("11");
  await expect(row).toContainText("10K");
  await expect(row).toContainText("$2.50");
  await expect(
    page.getByRole("columnheader", { name: "Last run", exact: true }),
  ).toHaveCount(0);
  await page.getByLabel("Average token metric").selectOption("output");
  await expect(row).toContainText("1K");
  await page
    .getByRole("button", { name: "Sort by avg output tokens", exact: true })
    .click();
  await expect(page).toHaveURL(/sort=avg_output_tokens_desc/);
  await page.reload();
  await expect(page.getByLabel("Average token metric")).toHaveValue("output");
  await expect(
    page.getByRole("columnheader", { name: /Avg tokens/ }),
  ).toHaveAttribute("aria-sort", "descending");
  await row.getByText("$2.50", { exact: true }).focus();
  await expect(page.getByRole("tooltip")).toContainText(
    "2 of 3 completed model trials have recorded costs",
  );
  await page.getByRole("button", { name: "Columns", exact: true }).click();
  await page
    .getByRole("menuitemcheckbox", { name: "Last run, PR and last task" })
    .click();
  await expect(
    page.getByRole("columnheader", { name: "Last run", exact: true }),
  ).toBeVisible();
});

test("average filters coexist with per-trial rules and clear together", async ({
  page,
}) => {
  const requests: URLSearchParams[] = [];
  page.on("request", (req) => {
    if (req.url().includes("/api/dashboard?"))
      requests.push(new URL(req.url()).searchParams);
  });
  await page.goto("/dashboard?min_steps=80&trial_metric_match=all");
  await page.getByRole("button", { name: /^Filters/ }).click();
  await page
    .getByRole("spinbutton", { name: "Maximum avg trial cost ($)" })
    .fill("4.5");
  await page
    .getByRole("spinbutton", { name: "Maximum avg trial cost ($)" })
    .press("Enter");
  await expect(page).toHaveURL(/max_avg_cost=4.5/);
  await expect
    .poll(() =>
      requests.some(
        (params) =>
          params.get("experiments_max_avg_cost") === "4.5" &&
          params.get("experiments_min_steps") === "80" &&
          params.get("experiments_trial_metric_match") === "all",
      ),
    )
    .toBeTruthy();
  await page.getByRole("tab", { name: "Trial metrics" }).click();
  await expect(
    page.getByText("Trajectory length", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Clear all", exact: true }).click();
  await expect
    .poll(() => new URL(page.url()).searchParams.has("min_steps"))
    .toBeFalsy();
  await expect
    .poll(() => new URL(page.url()).searchParams.has("max_avg_cost"))
    .toBeFalsy();
});

test("unknown measurements remain unknown and the narrow table scrolls within its card", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/dashboard");
  const row = page.getByRole("row").filter({ hasText: "Unknown experiment" });
  await expect(row).toContainText("—");
  await expect(row).not.toContainText("$0.00");
  expect(
    await page.evaluate(() => document.body.scrollWidth <= window.innerWidth),
  ).toBeTruthy();
  await page.getByRole("button", { name: /^Filters/ }).click();
  await expect(
    page.getByRole("spinbutton", { name: "Minimum avg steps" }),
  ).toBeVisible();
});

test("loading a new metric retains the selected column layout", async ({
  page,
}) => {
  await page.goto("/dashboard");
  await expect(
    page.getByText("Measured experiment", { exact: true }),
  ).toBeVisible();
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(
    "**/api/dashboard?**experiments_token_metric=output**",
    async (route) => {
      await held;
      await route.fulfill({
        json: {
          experiments: [
            { ...counts, id: "measured", name: "Measured experiment", effort },
          ],
          experiments_has_more: false,
        },
      });
    },
  );
  try {
    await page.getByLabel("Average token metric").selectOption("output");
    const loading = page.getByLabel("Loading experiments");
    await expect(loading).toBeVisible();
    await expect(loading.getByLabel("Average token metric")).toHaveValue(
      "output",
    );
    await expect(
      loading.getByRole("columnheader", { name: "Last run", exact: true }),
    ).toHaveCount(0);
    await expect(loading.getByRole("columnheader")).toHaveCount(9);
  } finally {
    release();
  }
  await expect(
    page.getByText("Measured experiment", { exact: true }),
  ).toBeVisible();
});

test("same-tick control changes retain the live URL's sibling filters", async ({
  page,
}) => {
  await page.goto(
    "/dashboard?sort=avg_steps_desc&experiment_details=true&min_avg_cost=1",
  );
  await expect(
    page.getByText("Measured experiment", { exact: true }),
  ).toBeVisible();
  // Next's URL snapshot has not rerendered between these two events.
  await page.evaluate(() => {
    const url = new URL(window.location.href);
    url.searchParams.set("max_avg_cost", "5");
    window.history.pushState(null, "", url);
    const selector = document.querySelector<HTMLSelectElement>(
      '[aria-label="Average token metric"]',
    )!;
    selector.value = "output";
    selector.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await expect(page).toHaveURL(/max_avg_cost=5/);
  await expect(page.getByLabel("Average token metric")).toHaveValue("output");
  await page.getByRole("button", { name: "Columns", exact: true }).click();
  await expect(page.getByRole("menuitemcheckbox")).toBeVisible();
  await page.evaluate(() => {
    const url = new URL(window.location.href);
    url.searchParams.set("sort", "avg_cost_asc");
    window.history.pushState(null, "", url);
    document.querySelector<HTMLElement>('[role="menuitemcheckbox"]')!.click();
  });
  await expect(page).toHaveURL(/sort=avg_cost_asc/);
  await expect(page).not.toHaveURL(/experiment_details/);
  await page.getByRole("button", { name: "Filters (1)", exact: true }).click();
  await page.getByLabel("Minimum avg steps", { exact: true }).fill("77");
  await page.evaluate(() => {
    const url = new URL(window.location.href);
    url.searchParams.set("max_avg_tokens", "500");
    window.history.pushState(null, "", url);
    document
      .querySelector<HTMLInputElement>('[aria-label="Minimum avg steps"]')!
      .blur();
  });
  await expect(page).toHaveURL(/min_avg_steps=77/);
  await expect(page).toHaveURL(/max_avg_tokens=500/);
});
