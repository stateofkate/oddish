import { expect, test } from "@playwright/test";

test("three reports submit independent votes and replacement reports reset controls", async ({
  page,
}) => {
  const requests: Record<string, unknown>[] = [];
  await page.route("**/api/**/feedback", async (route) => {
    requests.push(route.request().postDataJSON());
    await route.fulfill({ json: { id: `vote-${requests.length}` } });
  });
  await page.goto("/feedback");
  await expect(page.getByText("IS THIS RIGHT?", { exact: true })).toHaveCount(
    3
  );
  await expect(page.getByText("Correct label", { exact: true })).toHaveCount(0);
  await expect(
    page.getByRole("tabpanel", { name: "Summary" })
      .getByText("The verifier recorded the result.", { exact: true })
  ).toBeVisible();
  // QA evidence is never collapsed; only the trial's execution timeline is.
  await expect(
    page.locator("details").filter({ hasNotText: "Execution Timeline" })
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Agree with the task verdict", exact: true })
    .click();
  await page
    .getByRole("button", {
      name: "Agree with the pre-trial audit",
      exact: true,
    })
    .click();
  await page
    .getByRole("button", {
      name: "Disagree with the Good failure analysis",
      exact: true,
    })
    .click();
  await page
    .getByRole("textbox", { name: "WHY? (NOT REQUIRED)" })
    .fill("The verifier missed the correct result.");
  await page.getByRole("button", { name: "Submit", exact: true }).click();
  await expect(
    page.getByText("FEEDBACK RECORDED", { exact: true })
  ).toHaveCount(3);
  expect(requests).toEqual([
    {
      body: "",
      target: "qa_task_verdict",
      target_key: "accept",
      vote: "agree",
      trial_id: "qa-1",
      report_trial_id: "qa-1",
    },
    {
      body: "",
      target: "qa_pre_trial",
      target_key: "report",
      vote: "agree",
      trial_id: "audit-1",
      report_trial_id: "audit-1",
    },
    {
      body: "The verifier missed the correct result.",
      target: "qa_trajectory",
      target_key: "GOOD_FAILURE",
      vote: "disagree",
      trial_id: "task-a-trial",
      report_trial_id: "qa-1",
    },
  ]);
  await page.getByRole("button", { name: "Show replacement reports" }).click();
  await expect(page.getByText("IS THIS RIGHT?", { exact: true })).toHaveCount(
    3
  );
  await page.screenshot({
    path: "test-results/qa-report-feedback.png",
    fullPage: true,
  });
});
