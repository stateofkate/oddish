import { expect, test } from "@playwright/test";

const sha = "a".repeat(64);

for (const comparisonModel of ["openai/gpt-6-sol", "anthropic/claude-sonnet-5-5"]) {
  test(`QA checks ${comparisonModel} with Luna and safely retries a partially submitted full run`, async ({
    page,
  }) => {
    const requests: { body: Record<string, any>; key: string }[] = [];
    const checks: Record<string, any>[] = [];
    let passed = false;
    let failLuna = true;
    await page.route("**/api/qa-evals**", async (route) => {
      if (route.request().url().endsWith("/models")) {
        return route.fulfill({
          json: {
            production_model: "anthropic/claude-sonnet-5",
            models: [comparisonModel, "openai/gpt-6-luna", "xai/grok-4.6"].map(
              (model) => ({
                model,
                health: model.startsWith("xai") ? "failing" : "healthy",
                last_checked_at: "2026-10-09T00:00:00Z",
              })
            ),
          },
        });
      }
      if (route.request().method() === "GET") {
        return route.fulfill({
          json: {
            execution_configurable: true,
            replayable: true,
            production_prompt_sha256: sha,
            goldens: {
              counted: 25,
              waiting: 0,
              stale: 0,
              labels: [],
              sources: [
                { label: "original25", count: 19 },
                { label: "batch-b", count: 6 },
              ],
            },
            cost_per_trial: 0.5,
            frontier: [],
            models: [],
            spend: { total_usd: 0, trials: 0, cost_missing: 0, by_model: [] },
            runs: checks.map((run) => ({ ...run, preflight_passed: passed })),
            versions: [
              {
                sha256: sha,
                name: "Production",
                author: null,
                first_seen_at: "2026-09-27",
                git_commit: null,
                text_available: true,
                previous_sha256: null,
              },
            ],
          },
        });
      }
      const body = route.request().postDataJSON();
      requests.push({ body, key: route.request().headers()["idempotency-key"] });
      if (!body.preflight && body.model.endsWith("luna") && failLuna) {
        failLuna = false;
        return route.fulfill({
          status: 503,
          json: { detail: "Temporary submit failure" },
        });
      }
      const id = `${body.preflight ? "check" : "run"}-${body.model}`;
      if (body.preflight) checks.push({ experiment_id: id });
      return route.fulfill({ json: { experiment_id: id } });
    });
    await page.goto("/qa-evals");
    await page.getByLabel("Eval set").selectOption("batch-b");
    await page.getByLabel(/^Production/).uncheck();
    await expect(page.getByLabel("xai/grok-4.6")).toBeDisabled();
    for (const model of [comparisonModel, "openai/gpt-6-luna"]) {
      await page.getByLabel(model, { exact: true }).check();
      await page.getByLabel(`Reasoning effort for ${model}`).selectOption("max");
    }
    await page
      .getByRole("button", { name: "Check one golden per model" })
      .click();
    await expect(
      page.getByRole("button", { name: "Waiting for checks" })
    ).toBeDisabled();
    expect(requests).toHaveLength(2);
    expect(
      requests.every(
        ({ body }) =>
          body.preflight &&
          body.goldens &&
          body.golden_source === "batch-b" &&
          body.agent === "claude-code" &&
          body.agent_config.kwargs.reasoning_effort === "max"
      )
    ).toBe(true);
    passed = true;
    await expect(
      page.getByRole("button", { name: "Replay 6 trials per model" })
    ).toBeEnabled();
    await page.screenshot({ path: "test-results/qa-evals-checks-passed.png" });
    await page
      .getByRole("button", { name: "Replay 6 trials per model" })
      .click();
    await expect(page.getByRole("alert")).toContainText(
      "Temporary submit failure"
    );
    await page
      .getByRole("button", { name: "Replay 6 trials per model" })
      .click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    const full = requests.filter(({ body }) => !body.preflight);
    expect(full.map(({ body }) => body.model)).toEqual([
      comparisonModel,
      "openai/gpt-6-luna",
      "openai/gpt-6-luna",
    ]);
    expect(full[1].key).toBe(full[2].key);
    expect(
      full.every(({ body }) => body.preflight_run_id === `check-${body.model}`)
    ).toBe(true);
  });
}

test("QA charts unify model aliases, retain model identity across efforts and share filters", async ({
  page,
}) => {
  await page.goto("/admin?tab=qa-evals");
  const luna = page.getByRole("button", {
    name: "OpenAI gpt-6-luna",
    exact: true,
  });
  await expect(luna).toHaveCount(1);
  await expect(
    page.getByRole("button", { name: "Anthropic claude-sonnet-5", exact: true })
  ).toHaveCount(1);
  await expect(page.locator(".recharts-scatter-symbol")).toHaveCount(6);
  const color = await luna.locator("span").first().getAttribute("style");
  // High-effort Luna has two historical points in the same series.
  const seriesSizes = await page
    .locator(".recharts-scatter")
    .evaluateAll((series) =>
      series.map((s) => s.querySelectorAll(".recharts-scatter-symbol").length)
    );
  expect(seriesSizes.sort()).toEqual([1, 1, 2, 2]);
  await page
    .getByLabel("Thinking effort", { exact: true })
    .selectOption("high");
  await expect(page).toHaveURL(/effort=high/);
  await expect(page.locator(".recharts-scatter-symbol")).toHaveCount(4);
  await expect(luna.locator("span").first()).toHaveAttribute("style", color!);
  await page.getByRole("button", { name: "Models: cost vs accuracy" }).click();
  const board = page.getByRole("region", { name: "Models", exact: true });
  await expect(board.getByText("gpt-6-luna", { exact: true })).toHaveCount(1);
  await expect(board.getByText("80.0%", { exact: true })).toBeVisible();
  await expect(board.getByText("92.0%", { exact: true })).toHaveCount(0);
  await expect(board.getByText("high thinking", { exact: true })).toHaveCount(
    2
  );
  await page.getByLabel("Thinking effort", { exact: true }).selectOption("all");
  await page.getByLabel("Sort models").selectOption("effort");
  const badges = board.getByText(
    /^(low thinking|high thinking|effort not recorded)$/
  );
  await expect(badges).toHaveText([
    "low thinking",
    "high thinking",
    "high thinking",
    "effort not recorded",
  ]);
  await page.reload();
  await expect(page.getByLabel("Sort models")).toHaveValue("effort");
  await luna.click();
  await expect(board.getByText("gpt-6-luna", { exact: true })).toHaveCount(0);
  await luna.click();
  await expect(board.getByText("gpt-6-luna", { exact: true })).toHaveCount(2);
  await page.screenshot({
    path: "test-results/qa-model-thinking-effort.png",
    fullPage: true,
  });
  await page
    .getByLabel("Thinking effort", { exact: true })
    .selectOption("unrecorded");
  await expect(board.getByText("gpt-6-sol", { exact: true })).toBeVisible();
  await expect(board.getByText("gpt-6-luna", { exact: true })).toHaveCount(0);
  // Links created before the fix hid a raw configuration hash.
  await page.goto("/admin?tab=qa-evals&hide=luna-old");
  await expect(luna).toHaveAttribute("title", "Click to show");
  await expect(page.locator(".recharts-scatter-symbol")).toHaveCount(3);
});


for (const chart of ["history", "models"]) {
  test(`QA ${chart} chart distinguishes hidden models from an effort with no matching runs`, async ({
    page,
  }) => {
    await page.goto(`/admin?tab=qa-evals&chart=${chart}`);
    for (const name of [
      "OpenAI gpt-6-luna",
      "OpenAI gpt-6-sol",
      "Anthropic claude-sonnet-5",
    ]) {
      await page.getByRole("button", { name, exact: true }).click();
    }
    await expect(
      page.getByLabel("Thinking effort", { exact: true })
    ).toHaveValue("all");
    await expect(
      page.getByText("All models are hidden.", { exact: true })
    ).toBeVisible();
    await expect(
      page.getByText("No visible runs match this thinking effort.", {
        exact: true,
      })
    ).toHaveCount(0);
    await page
      .getByLabel("Thinking effort", { exact: true })
      .selectOption("high");
    await expect(
      page.getByText("All models are hidden.", { exact: true })
    ).toBeVisible();
    // Sol has no recorded effort: revealing it leaves the high-effort filter empty.
    await page
      .getByRole("button", { name: "OpenAI gpt-6-sol", exact: true })
      .click();
    await expect(
      page.getByText("All models are hidden.", { exact: true })
    ).toHaveCount(0);
    await expect(
      page.getByText("No visible runs match this thinking effort.", {
        exact: true,
      })
    ).toBeVisible();
    await page
      .getByLabel("Thinking effort", { exact: true })
      .selectOption("all");
    await expect(
      page.getByText("No visible runs match this thinking effort.", {
        exact: true,
      })
    ).toHaveCount(0);
    await expect(page.locator(".recharts-scatter-symbol circle")).toHaveCount(1);
  });
}

for (const chart of ["history", "models"]) {
  test(`QA ${chart} retains partial scores and shows execution failures`, async ({ page }) => {
    await page.goto(`/admin?tab=qa-evals&chart=${chart}`);
    await expect(page.getByLabel("claude-sonnet-5: 91% (21/23) · 23/25 scored · 1 failed · 1 pending", { exact: true })).toBeVisible();
    const runs = page.getByRole("region", { name: "Runs", exact: true });
    await expect(runs.getByText("23/25 scored · 1 failed · 1 pending", { exact: true })).toBeVisible();
    await expect(runs.getByText("91% (21/23)", { exact: true })).toBeVisible();
    if (chart === "models") {
      await expect(page.getByRole("region", { name: "Models", exact: true }).getByText(/23\/25 scored · 1 failed · 1 pending/)).toBeVisible();
      await page.screenshot({ path: "test-results/qa-partial-run.png", fullPage: true });
    }
  });
}
