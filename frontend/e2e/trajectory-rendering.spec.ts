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

test("reasoning and agent messages are visible while tool details stay closed", async ({ page }) => {
  await page.goto("/trajectory");
  const panel = page.getByTestId("trajectory-panel");
  const reasoning = panel.getByText(
    "Confirm the exact rule and its tests before writing a change.",
    { exact: true }
  );
  await expect(reasoning).toBeVisible();
  await expect(panel.getByText("I'll look at the repository state first.", { exact: true })).toBeVisible();
  await expect(panel.getByRole("img", { name: "Reasoning", exact: true }).first()).toHaveText("💭:");
  await expect(panel.getByRole("img", { name: "Agent message", exact: true }).first()).toHaveText("🤖:");
  await expect(panel.getByText(
    "The public function can keep the same inputs and return type.",
    { exact: true }
  )).toBeVisible();
  await expect(panel.getByText(
    "The change belongs inside the existing rule check.",
    { exact: true }
  )).toBeVisible();
  await expect(panel.getByText(
    "The response format needs a valid JSON string.",
    { exact: true }
  )).toBeVisible();
  const repeated = panel.getByText(
    "The test must cover a disabled rule that would otherwise match.",
    { exact: true }
  );
  await expect(repeated).toHaveCount(2);
  // Tool-only and synthetic-message steps must not reserve prose placeholder space.
  const emptyProse = await panel.locator('[class*="contain-intrinsic-size:auto_80px"]').evaluateAll(
    (elements) => elements.filter((element) => !element.textContent?.trim()).length
  );
  expect(emptyProse).toBe(0);
  await expect(panel.getByText("Raw step JSON", { exact: true })).toHaveCount(0);
  await expect(panel.getByText("On branch main", { exact: false })).toHaveCount(0);
  await expect(panel.getByText("Agent", { exact: true })).toHaveCount(0);
  await expect(panel.getByText("example-model", { exact: true })).toHaveCount(0);
  await expect(panel.getByText("user", { exact: true })).toHaveCount(2);

  await panel.getByRole("button", { name: /^#3\b/ }).click();
  await expect(panel.getByText("I'll look at the repository state first.", { exact: true })).toBeVisible();
  await expect(reasoning).toHaveCount(1);
  await expect(reasoning).toBeVisible();
  await expect(panel.getByText("On branch main", { exact: false })).toBeVisible();
  await expect(panel.getByText("Raw step JSON", { exact: true })).toHaveCount(1);
  await panel.getByRole("button", { name: /^#14\b/ }).click();
  await expect(repeated).toHaveCount(2);
});

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
  await expect(live.getByText("description: Run tests")).toBeVisible();
});


test("agent elapsed time ticks between polls and stops at the recorded end", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-09-30T12:00:05Z") });
  let ended = false;
  let requests = 0;
  await page.route("**/api/trials/trajectory-fixture/live?**", route => {
    requests++;
    return route.fulfill({ json: {
      attempt: 1, events: [], next_seq: 0, usage: {},
      harbor_stage: ended ? "verification" : "agent_running", done: false,
      agent_timing: {
        started_at: "2026-09-30T12:00:00Z",
        ...(ended ? { finished_at: "2026-09-30T12:00:08Z", duration_sec: 8 } : {}),
      },
    } });
  });
  await page.goto("/trajectory");
  const live = page.getByTestId("live-panel");
  const timer = live.getByRole("timer", { name: "Agent elapsed time" });
  await expect(timer).toHaveText("Agent time 00:05");
  await page.clock.fastForward(2000);
  await expect(timer).toHaveText("Agent time 00:07");
  expect(requests).toBe(1);
  ended = true;
  await page.clock.fastForward(10000);
  await expect(timer).toHaveText("Agent time 00:08");
  await page.clock.fastForward(5000);
  await expect(timer).toHaveText("Agent time 00:08");
});

test("missing agent timing is unavailable rather than time since opening the page", async ({ page }) => {
  await page.goto("/trajectory");
  await expect(page.getByTestId("live-panel").getByRole("timer")).toHaveText("Agent time —");

});


test("completed trace keeps the recorded agent duration beside its step count", async ({ page }) => {
  await page.clock.install();
  await page.goto("/trajectory");
  const timer = page.getByTestId("trajectory-panel").getByRole("timer");
  await expect(timer).toHaveText("Agent time 02:30");
  await page.clock.fastForward(5000);
  await expect(timer).toHaveText("Agent time 02:30");
});
