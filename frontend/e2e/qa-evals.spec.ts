import { expect, test } from "@playwright/test";

const sha = "a".repeat(64);

test("QA checks each selected model and safely retries a partially submitted full run", async ({
  page,
}) => {
  const requests: { body: Record<string, any>; key: string }[] = [];
  const checks: Record<string, any>[] = [];
  let passed = false;
  let failLuna = true;
  await page.route("**/api/qa-evals**", async (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({
        json: {
          execution_configurable: true,
          replayable: true,
          production_prompt_sha256: sha,
          goldens: { counted: 25, waiting: 0, stale: 0, labels: [] },
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
  await page
    .getByLabel("Models (one per line)")
    .fill("openai/gpt-6-sol\nopenai/gpt-6-luna");
  await page.getByLabel("Reasoning effort").selectOption("high");
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
        body.agent === "claude-code" &&
        body.agent_config.kwargs.reasoning_effort === "high"
    )
  ).toBe(true);
  passed = true;
  await expect(
    page.getByRole("button", { name: "Replay 25 trials per model" })
  ).toBeEnabled();
  await page.screenshot({ path: "test-results/qa-evals-checks-passed.png" });
  await page
    .getByRole("button", { name: "Replay 25 trials per model" })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "Temporary submit failure"
  );
  await page
    .getByRole("button", { name: "Replay 25 trials per model" })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const full = requests.filter(({ body }) => !body.preflight);
  expect(full.map(({ body }) => body.model)).toEqual([
    "openai/gpt-6-sol",
    "openai/gpt-6-luna",
    "openai/gpt-6-luna",
  ]);
  expect(full[1].key).toBe(full[2].key);
  expect(
    full.every(({ body }) => body.preflight_run_id === `check-${body.model}`)
  ).toBe(true);
});
