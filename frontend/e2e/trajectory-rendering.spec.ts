import { expect, test, type Locator, type Page } from "@playwright/test";
import { longCommand, trajectory } from "./review-app/trajectory";

async function openAll(panel: Locator) {
  const closed = panel.locator("h3 button[data-state=closed]");
  while ((await closed.count()) > 0) await closed.first().click();
}

async function openAllSteps(page: Page) {
  const panel = page.getByTestId("trajectory-panel");
  await expect(
    panel.getByText(`${trajectory.steps.length} steps`)
  ).toBeVisible();
  await openAll(panel);
  return panel;
}

test("tool calls show each command once, with unwrapped results", async ({
  page,
}) => {
  await page.goto("/trajectory");
  const panel = await openAllSteps(page);

  // Codex code mode: settled promises and process records become outputs.
  await expect(panel.getByText("call 2", { exact: true })).toBeVisible();
  await expect(panel.getByText('"count": 8,')).toBeVisible();
  await expect(panel.getByText("exit 1", { exact: true })).toBeVisible();
  await expect(panel.getByText("step 60/60")).toHaveCount(0);
  await panel.getByRole("button", { name: "Show all 60 lines" }).click();
  await expect(panel.getByText("step 60/60 loss=0.525")).toBeVisible();

  // The echoed "Executed command:" message is not repeated above the command.
  await expect(panel.getByText(`Executed command: ${longCommand}`)).toHaveCount(
    0
  );
  await expect(panel.locator("pre", { hasText: longCommand })).toHaveCount(1);

  // One rejection, one raw disclosure per step.
  await expect(panel.getByText("Rejected", { exact: true })).toHaveCount(1);
  await expect(panel.getByText("Raw step JSON")).toHaveCount(
    trajectory.steps.length
  );
  await expect(panel.getByText("approval policy")).toBeVisible();
});

test("a narrow trial panel never scrolls sideways", async ({ page }) => {
  await page.goto("/trajectory");
  await page.getByRole("button", { name: "420px panel" }).click();
  const panel = await openAllSteps(page);
  const overflowing = await panel.evaluate((root) => {
    const right = root.getBoundingClientRect().right;
    return [...root.querySelectorAll<HTMLElement>("*")].filter(
      (el) => el.offsetParent && el.getBoundingClientRect().right > right + 1
    ).length;
  });
  expect(overflowing).toBe(0);
});

test("live tool calls use the same command and output rendering", async ({
  page,
}) => {
  await page.goto("/trajectory");
  const live = page.getByTestId("live-panel");
  await expect(live.getByText("Ended")).toBeVisible();
  await openAll(live);
  await expect(live.getByText("pytest -q tests/")).toBeVisible();
  await expect(live.getByText("2 passed in 0.31s")).toBeVisible();
  await expect(live.getByText("description Run tests")).toBeVisible();
});
