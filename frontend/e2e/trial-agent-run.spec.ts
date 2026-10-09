import { tasks } from "./review-app/records";
import { expect, test } from "@playwright/test";

for (const share of [false, true]) {
  test(`agent run header survives tabs and narrow width (${share ? "share" : "internal"})`, async ({
    page,
  }) => {
    await page.route("**/api/**", (route) => route.fulfill({ json: {} }));
    await page.goto(`/agent-run${share ? "?share=1" : ""}`);
    const card = page.getByRole("region", { name: "Agent run", exact: true });
    await expect(card).toContainText("3h 49m");
    await expect(card).toContainText("960 steps");
    await expect(page.getByText("960 steps", { exact: true })).toHaveCount(1);
    for (const tab of ["Files", "Trajectory", "Artifacts", "Summary"]) {
      await page.getByRole("tab", { name: tab, exact: true }).click();
      await expect(card).toBeVisible();
    }
    await page.screenshot({
      path: `test-results/agent-run-${share ? "share" : "internal"}-desktop.png`,
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(card).toBeInViewport();
    const box = await card.boundingBox();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(390);
    await page.screenshot({
      path: `test-results/agent-run-${share ? "share" : "internal"}-mobile.png`,
    });
  });
}
test("missing measurements show dashes and active execution ticks", async ({
  page,
}) => {
  await page.route("**/api/**", (route) => route.fulfill({ json: {} }));
  await page.goto("/agent-run?state=missing");
  const card = page.getByRole("region", { name: "Agent run", exact: true });
  await expect(card.getByLabel("Agent execution time")).toHaveText("—");
  await expect(card).toContainText("— steps");
  await page.goto("/agent-run?state=active");
  await expect(card).toContainText("· live");
  const timer = card.getByRole("timer");
  const before = await timer.textContent();
  await expect(timer).not.toHaveText(before!);
  await page
    .getByRole("button", { name: "Start verification", exact: true })
    .click();
  await expect(card).not.toContainText("· live");
  await expect(card.getByRole("timer")).toHaveCount(0);
  await page
    .getByRole("button", { name: "Complete trial", exact: true })
    .click();
  await expect(card).not.toContainText("· live");
  await expect(card.getByRole("timer")).toHaveCount(0);
});

for (const state of ["completed", "tokens-missing", "tokens-zero"]) {
  test(`token breakdown preserves cache semantics (${state})`, async ({
    page,
  }) => {
    await page.route("**/api/**", (route) => route.fulfill({ json: {} }));
    await page.goto(`/agent-run?state=${state}`);
    const total = page.getByText(
      state === "tokens-missing" ? "— tokens" : "1.2k tokens",
      { exact: true }
    );
    await expect(total).toBeVisible();
    await total.focus();
    const tip = page.getByRole("tooltip");
    await expect(tip).toBeVisible();
    const values = tip.locator("dd");
    await expect(values).toHaveText(
      state === "tokens-missing"
        ? ["—", "—", "—", "200"]
        : state === "tokens-zero"
          ? ["1,000", "0", "1,000", "200"]
          : ["1,000", "800", "200", "200"]
    );
    await expect(tip).toContainText("Cached input is already included");
  });
}

test("retry setup hides stale measurements and a new agent ignores the old verifier", async ({
  page,
}) => {
  await page.route("**/api/**", (route) => route.fulfill({ json: {} }));
  const card = page.getByRole("region", { name: "Agent run", exact: true });
  await page.goto("/agent-run?state=retry-setup");
  await expect(card.getByLabel("Agent execution time")).toHaveText("—");
  await expect(card).toContainText("— steps");
  await expect(card).not.toContainText("· live");
  await page.goto("/agent-run?state=retry-active");
  await expect(card.getByRole("timer")).toHaveText(/1m [0-9]+s/);
  await expect(card).toContainText("— steps");
  await expect(card).toContainText("· live");
});
test("duration display does not roll fractional seconds into malformed minutes", async ({
  page,
}) => {
  await page.route("**/api/**", (route) => route.fulfill({ json: {} }));
  for (const [duration, label] of [
    [59.99, "59s"],
    [60, "1m"],
    [119.9, "1m 59s"],
    [3599.99, "59m 59s"],
    [3600, "1h"],
  ]) {
    await page.goto(`/agent-run?duration=${duration}`);
    await expect(
      page
        .getByRole("region", { name: "Agent run", exact: true })
        .getByLabel("Agent execution time")
    ).toHaveText(String(label));
  }
});

test("panel stops the card from newer list verification while detail remains running", async ({
  page,
}) => {
  const detail = {
    ...tasks[0].trials![0],
    status: "running",
    harbor_stage: "agent_running",
    total_steps: 321,
    phase_timing: {
      agent_execution: {
        started_at: new Date(Date.now() - 65000).toISOString(),
      },
    },
  };
  await page.route("**/api/**", (route) =>
    route.fulfill({
      json: new URL(route.request().url()).pathname.endsWith(
        `/trials/${detail.id}`
      )
        ? detail
        : {},
    })
  );
  await page.goto("/agent-run?state=active&fetch-detail=1");
  const card = page.getByRole("region", { name: "Agent run", exact: true });
  await expect(card).toContainText("321 steps");
  await expect(card).toContainText("· live");
  await page
    .getByRole("button", { name: "Start verification", exact: true })
    .click();
  await expect(card).not.toContainText("· live");
  await expect(card.getByRole("timer")).toHaveCount(0);
  await expect(card).toContainText("321 steps");
  const value = card.getByLabel("Agent execution time");
  await expect(value).not.toHaveText("—");
  const frozen = await value.textContent();
  await page.waitForTimeout(1100);
  await expect(value).toHaveText(frozen!);
});

for (const { completed, share, result } of [
  { completed: false, share: false, result: "recorded" },
  { completed: true, share: false, result: "recorded" },
  { completed: false, share: true, result: "recorded" },
  { completed: true, share: true, result: "recorded" },
  { completed: false, share: false, result: "missing" },
  { completed: false, share: true, result: "error" },
  { completed: false, share: false, result: "stale" },
]) {
  test(`stage-only detail refresh (${completed ? "completed" : "verification"}, ${share ? "share" : "internal"}, ${result})`, async ({
    page,
  }) => {
    const start = new Date(Date.now() - 65000).toISOString();
    const finish = new Date(Date.parse(start) + 60000).toISOString();
    const detail = {
      ...tasks[0].trials![0],
      status: "running",
      harbor_stage: "agent_running",
      total_steps: 321,
      phase_timing: { agent_execution: { started_at: start } },
    };
    const payload = (value: unknown) =>
      share ? { ...tasks[0], trials: [value] } : value;
    const endpoint = share ? `/tasks/${tasks[0].id}` : `/trials/${detail.id}`;
    let requests = 0;
    let releasePoll!: () => void;
    const pollGate = new Promise<void>(resolve => { releasePoll = resolve; });
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    await page.route("**/api/**", async (route) => {
      if (!new URL(route.request().url()).pathname.endsWith(endpoint))
        return route.fulfill({ json: {} });
      requests++;
      if (requests === 1) return route.fulfill({ json: payload(detail) });
      await gate;
      if (requests > 2 && result === "missing") await pollGate;
      if (result === "error")
        return route.fulfill({ status: 500, json: { error: "unavailable" } });
      if (result === "stale") return route.fulfill({ json: payload(detail) });
      return route.fulfill({
        json: payload({
          ...detail,
          status: completed ? "success" : "running",
          harbor_stage: completed ? "completed" : "verification",
          phase_timing:
            result === "missing"
              ? null
              : {
                  agent_execution: {
                    started_at: start,
                    finished_at: finish,
                    duration_sec: 60,
                  },
                },
        }),
      });
    });
    await page.goto(
      `/agent-run?state=active&fetch-detail=1&slim=1${share ? "&share=1" : ""}`
    );
    const card = page.getByRole("region", { name: "Agent run", exact: true });
    await expect(card).toContainText("321 steps");
    await expect(card).toContainText("· live");
    await page
      .getByRole("button", {
        name: completed ? "Complete trial" : "Start verification",
        exact: true,
      })
      .click();
    await expect.poll(() => requests, { timeout: 2000 }).toBe(2);
    await expect(card.getByLabel("Agent execution time")).toHaveText(
      "Updating…"
    );
    await expect(card.getByRole("timer")).toHaveCount(0);
    await expect(card).not.toContainText("· live");
    release();
    await expect(card.getByLabel("Agent execution time")).toHaveText(
      result === "recorded" ? "1m" : "—"
    );
    await expect(card).toContainText("321 steps");
    if (result === "missing") {
      await expect.poll(() => requests, { timeout: 10000 }).toBeGreaterThan(2);
      await expect(card.getByLabel("Agent execution time")).toHaveText("—");
      releasePoll();
    }
  });
}

for (const status of ["paused", "retrying", "cancelled", "cancel-stage"] as const) {
  for (const share of [false, true]) {
    test(`list ${status} stops the live clock (${share ? "share" : "internal"})`, async ({ page }) => {
      const detail = { ...tasks[0].trials![0], status: "running", harbor_stage: "agent_running",
        phase_timing: { agent_execution: { started_at: new Date(Date.now() - 65000).toISOString() } } };
      const endpoint = share ? `/tasks/${tasks[0].id}` : `/trials/${detail.id}`;
      let requests = 0;
      await page.route("**/api/**", route => {
        if (!new URL(route.request().url()).pathname.endsWith(endpoint)) return route.fulfill({ json: {} });
        requests++;
        return route.fulfill({ json: share ? { ...tasks[0], trials: [detail] } : detail });
      });
      await page.goto(`/agent-run?state=active&fetch-detail=1&slim=1${share ? "&share=1" : ""}`);
      const card = page.getByRole("region", { name: "Agent run", exact: true });
      await expect(card).toContainText("· live");
      await expect(card.getByRole("timer")).toHaveText(/1m [0-9]+s/);
      await page.getByRole("button", { name: status, exact: true }).click();
      await expect(card.getByRole("timer")).toHaveCount(0);
      await expect(card).not.toContainText("· live");
      await expect.poll(() => requests, { timeout: 2000 }).toBe(2);
      const time = card.getByLabel("Agent execution time");
      if (status === "paused") {
        await expect(time).toHaveText(/1m [0-9]+s/);
        await expect(time).toHaveAttribute("title", "Last observed agent execution time before pause.");
        const frozen = await time.textContent();
        await page.waitForTimeout(1500);
        await expect(time).toHaveText(frozen!);
        await page.getByRole("button", { name: "retrying", exact: true }).click();
        await expect(time).toHaveText("—");
        await page.getByRole("button", { name: "paused", exact: true }).click();
        await expect(time).toHaveText("—");
      } else await expect(time).toHaveText("—");
    });
  }
}

test("a newly opened pause needs measured timing; recorded timing wins", async ({ page }) => {
  await page.route("**/api/**", route => route.fulfill({ json: {} }));
  const time = page.getByRole("region", { name: "Agent run", exact: true }).getByLabel("Agent execution time");
  await page.goto("/agent-run?state=paused");
  await expect(time).toHaveText("—");
  await page.goto("/agent-run?state=paused&duration=125");
  await expect(time).toHaveText("2m 5s");
  await expect(time).not.toHaveAttribute("title", /Last observed/);
});
