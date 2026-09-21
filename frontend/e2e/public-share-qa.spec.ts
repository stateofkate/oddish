import { expect as baseExpect, test, type Page } from "@playwright/test";

import type { PreTrialFinding, Task, Trial } from "../src/lib/types";

// These browser tests use only local fixtures. Start the frontend and set
// E2E_BASE_URL to run them; the default test run needs no running app.
test.skip(!process.env.E2E_BASE_URL, "needs a frontend server at E2E_BASE_URL");
test.setTimeout(60_000);
const expect = baseExpect.configure({ timeout: 15_000 });

const token = "share-qa-browser-test";
const publicBase = `/api/public/experiments/${token}`;
const finding: PreTrialFinding = {
  id: "audit-finding",
  tier: "must_fix",
  title: "Add an empty input check",
  detail: "The test covers normal input but does not check empty input.",
  recommendation: "Add a test for empty input.",
};
const trial: Trial = {
  id: "trial-qa",
  name: "qa-example-trial",
  task_id: "task-qa",
  task_path: "tasks/qa-example",
  experiment_id: "exp-qa",
  agent: "codex",
  provider: "openai",
  model: "openai/gpt-5.2",
  status: "success",
  attempts: 1,
  max_attempts: 1,
  harbor_stage: null,
  reward: 1,
  task_version: 2,
  task_version_id: "version-2",
  analysis_status: "success",
  analysis: {
    classification: "GOOD_SUCCESS",
    subtype: "VALID_SOLUTION",
    root_cause: "The agent solved the task and passed all checks.",
    evidence: "The output matches the expected result.",
    recommendation: "Keep the checks for this result.",
  },
  created_at: "2026-09-21T12:00:00Z",
  started_at: "2026-09-21T12:00:00Z",
  finished_at: "2026-09-21T12:01:00Z",
};
const task: Task = {
  id: "task-qa",
  name: "QA example task",
  status: "completed",
  priority: "low",
  user: "test-user",
  task_path: "tasks/qa-example",
  experiment_id: "exp-qa",
  experiment_name: "Shared QA example",
  experiment_is_public: true,
  total: 1,
  completed: 1,
  failed: 0,
  reward_success: 1,
  reward_sum: 1,
  reward_total: 1,
  current_version: 4,
  current_version_id: "version-4",
  trial_version: 2,
  trial_version_id: "version-2",
  run_analysis: true,
  verdict_status: "success",
  verdict: {
    is_good: true,
    confidence: "high",
    reasoning: "The task checks the expected result.",
  },
  trials: [trial],
  created_at: "2026-09-21T12:00:00Z",
  updated_at: "2026-09-21T12:01:00Z",
};

async function mockShare(page: Page, showQa: boolean) {
  const requests: { path: string; method: string }[] = [];
  const unexpectedRequests: string[] = [];
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/api/client-traces") {
      await route.fulfill({ status: 204 });
      return;
    }
    requests.push({ path: `${path}${url.search}`, method: request.method() });
    let body: unknown;
    if (path === publicBase) {
      body = {
        id: "exp-qa",
        name: "Shared QA example",
        description: "An example of our QA process.",
        is_public: true,
        show_qa: showQa,
      };
    } else if (path === `${publicBase}/results`) {
      // Keep QA in both fixture modes to check the UI flag independently
      // from the server's response filters.
      const records = [
        {
          type: "experiment",
          experiment: {
            experiment_id: "exp-qa", name: "Shared QA example",
            created_at: task.created_at, revision: task.updated_at,
            has_active_trials: false, summary: null, tasks: [],
          },
        },
        { type: "task", task: { ...task, trials: undefined } },
        { type: "trial", trial: {
          ...trial, analysis: { ...trial.analysis, status: "success" },
        } },
        { type: "complete" },
      ];
      await route.fulfill({
        contentType: "application/x-ndjson",
        body: records.map((record) => JSON.stringify(record)).join("\n") + "\n",
      });
      return;
    } else if (path === `${publicBase}/cost-totals`) {
      body = {
        cost_usd: 0, cost_trial_count: 0, cost_has_estimated: false,
        cost_has_native: false, token_count: 0, token_trial_count: 0,
        owned_cost_usd: 0, owned_trial_count: 0, owned_has_estimated: false,
        owned_has_native: false, owned_token_count: 0, owned_token_trial_count: 0,
        billed_cost_usd: 0, billed_trial_count: 0, billed_has_estimated: false,
        billed_has_native: false, billed_token_count: 0, billed_token_trial_count: 0,
        total_trials: 1,
      };
    } else if (path === `${publicBase}/trials/${trial.id}`) {
      body = trial;
    } else if (path === `${publicBase}/tasks/task-qa`) {
      body = task;
    } else if (path === `${publicBase}/tasks/task-qa/qa`) {
      body = {
        version: 2,
        version_id: "version-2",
        pre_trial_findings: [finding],
        pre_trial_status: "success",
        pre_trial_error: null,
      };
    } else if (path === `${publicBase}/tasks/task-qa/files`) {
      body = {
        files: [
          {
            path: "instruction.md",
            name: "instruction.md",
            size: 35,
            content: "# Task\nReturn the expected result.",
          },
        ],
      };
    } else if (path === `${publicBase}/tasks/task-qa/files/instruction.md`) {
      body = { content: "# Task\nReturn the expected result." };
    } else if (path === `${publicBase}/trials/trial-qa/result`) {
      body = { result: null };
    } else if (path === `${publicBase}/trials/trial-qa/files`) {
      body = { files: [] };
    } else {
      unexpectedRequests.push(`${request.method()} ${path}${url.search}`);
      await route.fulfill({
        status: 404,
        json: { detail: "Unexpected request" },
      });
      return;
    }
    await route.fulfill({ json: body });
  });
  return { requests, unexpectedRequests, pageErrors };
}

async function expectNoWriteControls(page: Page) {
  await expect(
    page.getByRole("button", {
      name: /^(run pre-trial audit|run audit|re-run audit|run qa|rerun qa|re-run analysis|run analysis|retry|rerun|delete|generate qa verdict|re-run trajectory analysis|regenerate)( for)?( v\d+)?$/i,
    })
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: /agree|disagree/i })
  ).toHaveCount(0);
}

test("a share link hides QA when the share flag is off", async ({ page }) => {
  const state = await mockShare(page, false);
  await page.goto(`/share/${token}`);
  await expect(
    page.getByRole("heading", { name: "Shared QA example" })
  ).toBeVisible();
  await page.getByRole("button", { name: task.name, exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Overview", exact: true })
  ).toHaveCount(0);
  await expect(page.getByText(finding.title!, { exact: true })).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "View trial", exact: true })
  ).toHaveCount(0);
  await expectNoWriteControls(page);

  await page.goto(`/share/${token}?task=${task.id}&trial=${trial.id}`);
  await expect(
    page.getByRole("tab", { name: "Summary", exact: true })
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Good success", exact: true })
  ).toHaveCount(0);
  await expect(
    page.getByText(trial.analysis!.root_cause!, { exact: true })
  ).toHaveCount(0);
  await expectNoWriteControls(page);
  expect(state.requests.some(({ path }) => path.includes("/qa"))).toBe(false);
  expect(state.requests.every(({ method }) => method === "GET")).toBe(true);
  expect(state.unexpectedRequests).toEqual([]);
  expect(state.pageErrors).toEqual([]);
});

test("a share link shows read-only task and trial QA when the flag is on", async ({
  page,
}, testInfo) => {
  const state = await mockShare(page, true);
  await page.goto(`/share/${token}`);
  await expect(
    page.getByRole("heading", { name: "Shared QA example" })
  ).toBeVisible();
  await page.getByRole("button", { name: task.name, exact: true }).click();
  await page.getByRole("button", { name: "Overview", exact: true }).click();
  await expect(page.getByText(finding.title!, { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "View trial", exact: true })
  ).toBeVisible();
  await expectNoWriteControls(page);
  await page.getByRole("button", { name: "View trial", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Good success", exact: true })
  ).toBeVisible();
  await expect(
    page.getByText(trial.analysis!.root_cause!, { exact: true }).last()
  ).toBeVisible();
  await expectNoWriteControls(page);

  await page.screenshot({ path: testInfo.outputPath("public-share-qa-on.png"), fullPage: true });
  expect(state.requests).toContainEqual({
    path: `${publicBase}/tasks/task-qa/qa?version=2`,
    method: "GET",
  });
  expect(state.requests.every(({ method }) => method === "GET")).toBe(true);
  expect(state.unexpectedRequests).toEqual([]);
  expect(state.pageErrors).toEqual([]);
});
