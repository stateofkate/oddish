import { expect, test, type Page } from "@playwright/test";
import { progressHistory, progressTrial } from "./progress-fixtures";
import { formatDurationSec } from "../src/lib/format";
import { reconcileTrialLifecycle } from "../src/lib/trial-lifecycle";

const at = (seconds: number) =>
  new Date(Date.parse(progressTrial.created_at) + seconds * 1000).toISOString();
const timing = (a: number, b: number) => ({
  started_at: at(a),
  finished_at: at(b),
  duration_sec: b - a,
});
const ended = {
  ...progressTrial,
  status: "success",
  harbor_stage: "completed",
  finished_at: at(70),
  phase_timing: {
    environment_setup: timing(5, 35),
    agent_setup: timing(35, 35),
    agent_execution: timing(35, 55),
    verifier: timing(55, 65),
  },
};
function operation(
  seq: number,
  id: string,
  parent: string | null,
  phase: string,
  a: number,
  b: number,
  category = "build",
  status = "success"
) {
  return {
    ...progressHistory.events[0],
    event_key: `live:${seq}`,
    source_seq: seq,
    schema_version: 2,
    payload: {
      kind: "env_subphase",
      schema_version: 2,
      operation_id: id,
      parent_operation_id: parent,
      phase,
      category,
      status,
      provider: "docker",
      resource_layer: "task_environment",
      strategy: "compose",
      operation_attempt: 1,
      started_at: at(a),
      finished_at: at(b),
      duration_seconds: b - a,
      error_type: null,
    },
  };
}
const history = {
  ...progressHistory,
  events: [
    operation(1, "root", null, "environment.setup", 5, 35, "setup"),
    operation(2, "build", "root", "docker.compose_build", 5, 20),
    operation(3, "image", "build", "docker.image_build", 7, 15),
    operation(4, "stage", "root", "docker.stage_files", 10, 25, "stage"),
  ],
};
async function mock(
  page: Page,
  trial: object = ended,
  records: object = history
) {
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    return route.fulfill({
      json: path.endsWith("/progress-history")
        ? records
        : path.endsWith("/live")
          ? { attempt: 1, events: [], next_seq: 0, usage: {}, done: true }
          : trial,
    });
  });
}

test("terminal list wins stale detail; retry replaces the old attempt", () => {
  const terminal = {
    ...progressTrial,
    status: "failed" as const,
    finished_at: at(70),
  };
  expect(reconcileTrialLifecycle(progressTrial, terminal)?.status).toBe(
    "failed"
  );
  expect(reconcileTrialLifecycle(terminal, progressTrial)).toBe(terminal);
  const retry = { ...progressTrial, attempts: 2 };
  expect(reconcileTrialLifecycle(terminal, retry)).toBe(retry);
});

for (const mobile of [false, true])
  test(`compact measured progress (${mobile ? "mobile" : "desktop"})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: mobile ? 390 : 1280, height: 1000 });
    await mock(page);
    await page.goto("/progress");
    const progress = page.getByRole("region", { name: "Trial progress" });
    const environment = progress.getByRole("group", {
      name: "Environment Setup",
      exact: true,
    });
    await expect(environment.getByText("30.0s", { exact: true })).toHaveCount(
      1
    );
    await expect(
      environment.getByText(/no recorded substep timing|Measurements may overlap/)
    ).toHaveCount(0);
    await expect(
      environment.getByTitle("docker.image_build")
    ).not.toBeVisible();
    await expect(environment.getByTitle("docker.image_build")).toHaveCount(0);
    await expect(environment.getByLabel("Expand all steps")).toHaveCount(0);
    await expect(environment.getByRole("list", { name: "Environment setup steps" }).getByRole("button")).toHaveCount(0);
    for (const name of ["Initializing", "Agent Running", "Verification"]) {
      const phase = progress.getByRole("group", { name, exact: true });
      await expect(phase.locator("summary")).toHaveCount(0);
    }
    await expect(
      progress.getByText("Trial Started", { exact: true })
    ).toBeVisible();
    await expect(
      progress.getByRole("group", { name: "Completed", exact: true }).getByText("Completed", { exact: true })
    ).toBeVisible();
    await expect(
      progress.getByText("Worker pickup", { exact: true })
    ).not.toBeVisible();
    await expect(
      progress.getByText("Agent setup", { exact: true })
    ).not.toBeVisible();
    await expect(
      progress.getByRole("region", { name: "Agent output" })
    ).toHaveCount(0);
    await progress.screenshot({
      path: `delivery-test-results/progress-compact-${mobile ? "mobile" : "desktop"}.png`,
    });
    await environment
      .getByRole("list", { name: "Environment setup steps", exact: true })
      .screenshot({
        path: `delivery-test-results/setup-operation-tree-${mobile ? "mobile" : "desktop"}.png`,
      });
    await progress
      .getByText("Execution Timeline", { exact: true })
      .click();
    await expect(environment).not.toBeVisible();
  });

for (const status of ["success", "error", "cancelled"] as const)
  test(`retained ${status} survives reload without a running timer`, async ({
    page,
  }) => {
    await mock(
      page,
      {
        ...ended,
        status: status === "success" ? "success" : "failed",
        harbor_stage: status === "cancelled" ? "cancelled" : "completed",
      },
      {
        ...history,
        events: [
          operation(
            1,
            "step",
            null,
            "docker.compose_build",
            5,
            20,
            "build",
            status
          ),
        ],
      }
    );
    await page.goto("/progress");
    await page.reload();
    const progress = page.getByRole("region", { name: "Trial progress" });
    await expect(progress.getByTitle("docker.compose_build")).toBeVisible();
    await expect(
      progress
        .getByLabel(
          status === "success"
            ? "done"
            : status === "error"
              ? "failed"
              : "cancelled",
          { exact: true }
        )
        .first()
    ).toBeVisible();
    await expect(progress.getByRole("timer")).toHaveCount(0);
  });

test("legacy history omits timing diagnostics while incomplete steps stay explicit", async ({
  page,
}) => {
  await mock(
    page,
    { ...ended },
    { ...progressHistory, history_truncated: true }
  );
  await page.goto("/progress");
  const progress = page.getByRole("region", { name: "Trial progress" });
  await expect(progress.getByTitle("kubernetes.helm_install")).toBeVisible();
  await expect(
    progress.getByText("End not recorded", { exact: true })
  ).toBeVisible();
  await expect(progress.getByText(/history display limit/)).toBeVisible();
  await expect(progress.getByText(/no recorded substep timing|Measurements may overlap/)).toHaveCount(0);
  await expect(progress.getByText("Run metadata & timing sources")).toHaveCount(0);
});

test("missing history does not create an expandable environment phase", async ({
  page,
}) => {
  await mock(page, ended, {
    ...progressHistory,
    events: [],
    history_available: false,
  });
  await page.goto("/progress");
  const progress = page.getByRole("region", { name: "Trial progress" });
  await expect(
    progress.getByText("No setup history recorded for this attempt.")
  ).toBeVisible();
  await expect(
    progress
      .getByRole("group", { name: "Environment Setup", exact: true })
      .locator("summary")
  ).toHaveCount(0);
});

test("live response advancing before detail cannot relabel the next attempt", async ({
  page,
}) => {
  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    return route.fulfill({
      json: url.pathname.endsWith("/progress-history")
        ? progressHistory
        : url.pathname.endsWith("/live")
          ? {
              attempt: 2,
              next_seq: 1,
              usage: {},
              done: false,
              events:
                url.searchParams.get("after_seq") === "0"
                  ? [
                      {
                        seq: 1,
                        kind: "env_subphase",
                        payload: {
                          phase: "next_attempt_only",
                          status: "started",
                          started_at: at(0),
                        },
                      },
                    ]
                  : [],
            }
          : progressTrial,
    });
  });
  await page.goto("/progress");
  const progress = page.getByRole("region", { name: "Trial progress" });
  await expect(progress.getByTitle("kubernetes.helm_install")).toBeVisible();
  await expect(progress.getByTitle("next_attempt_only")).toHaveCount(0);
  await page.getByRole("tab", { name: "Live", exact: true }).click();
  await expect(progress.getByTitle("next_attempt_only")).toHaveCount(0);
  await page.getByRole("button", { name: "Table reports retry" }).click();
  await page.getByRole("tab", { name: "Summary", exact: true }).click();
  await expect(progress).toHaveAttribute("data-attempt", "2");
  await expect(progress.getByTitle("next_attempt_only")).toBeVisible();
  await expect(
    progress.getByTitle("kubernetes.helm_install")
  ).not.toBeVisible();
});

test("live keeps its header, timer and output alongside environment details", async ({
  page,
}) => {
  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    const live = {
      attempt: 1,
      next_seq: 1,
      done: false,
      harbor_stage: "agent_running",
      agent_timing: { started_at: at(0) },
      usage: { input_tokens: 7, output_tokens: 3 },
      events:
        url.searchParams.get("after_seq") === "0"
          ? [
              {
                seq: 1,
                kind: "message",
                payload: {
                  role: "assistant",
                  text: "Measured output",
                  turn_id: "1",
                },
              },
            ]
          : [],
    };
    return route.fulfill({
      json: url.pathname.endsWith("/progress-history")
        ? progressHistory
        : url.pathname.endsWith("/live")
          ? live
          : { ...progressTrial, agent: "claude-code", harbor_stage: "agent_running" },
    });
  });
  await page.goto("/progress");
  await page.getByRole("tab", { name: "Live", exact: true }).click();
  await expect(page.getByText("10 tok", { exact: true })).toBeVisible();
  const output = page.getByRole("region", { name: "Agent output" });
  await expect(output.locator("p").filter({ hasText: /^Measured output$/ })).toBeVisible();
  await expect(page.getByRole("timer")).toHaveCount(1);
  await expect(page.getByRole("region", { name: "Trial progress" })).toHaveCount(0);
  const disclosure = page.locator("summary").filter({ hasText: /^Environment Setup$/ });
  await expect(disclosure).toHaveClass(/list-none/);
  await expect(disclosure.locator("svg")).toHaveClass(/lucide-chevron-right/);
  await expect(disclosure).toHaveCSS("display", "flex");
  await disclosure.click();
  await expect(output.locator("p").filter({ hasText: /^Measured output$/ })).toBeVisible();
  await expect(page.getByText("10 tok", { exact: true })).toBeVisible();
});
test("shared timeline preserves legacy failure flags and hides missing stages", async ({ page }) => {
  await page.goto("/progress-timeline");
  await expect(page.getByRole("region", { name: "No stage", exact: true }).getByRole("group")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Before provisioning failure" }).getByRole("img", { name: /: failed$/ })).toHaveCount(6);
  for (const name of ["Failure after setup", "Failure after agent", "Successful trial"]) {
    await expect(page.getByRole("region", { name, exact: true }).getByRole("img", { name: /: completed$/ })).toHaveCount(6);
  }
});

test("shared duration formatter preserves its unavailable-zero convention", () => {
  expect(formatDurationSec(0)).toBe("—");
  for (const invalid of [-1, NaN, Infinity])
    expect(formatDurationSec(invalid)).toBe("—");
  expect(formatDurationSec(0.25)).toBe("250ms");
  expect(formatDurationSec(3.7)).toBe("3.7s");
});

test("Live follows the output anchor and preserves scrolling away", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1000, height: 700 });
  await page.clock.install();
  let append = false;
  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    const after = Number(url.searchParams.get("after_seq"));
    const seq = after === 0 ? 1 : append && after === 1 ? 2 : null;
    const live = {
      attempt: 1,
      next_seq: seq ?? after,
      done: false,
      harbor_stage: "agent_running",
      usage: {},
      agent_timing: { started_at: progressTrial.created_at },
      events: seq
        ? [
            {
              seq,
              kind: "message",
              payload: {
                role: "assistant",
                text:
                  seq === 1
                    ? `Output 1\n\n${"Long streamed line.\n\n".repeat(20)}`
                    : "Output 2",
                turn_id: `turn-${seq}`,
              },
            },
          ]
        : [],
    };
    return route.fulfill({
      json: url.pathname.endsWith("/progress-history")
        ? progressHistory
        : url.pathname.endsWith("/live")
          ? live
          : { ...progressTrial, harbor_stage: "agent_running" },
    });
  });
  await page.goto("/progress");
  await page.getByRole("tab", { name: "Live", exact: true }).click();
  const scroller = page.getByRole("region", {
    name: "Live trial",
    exact: true,
  });
  const output = page.getByRole("region", {
    name: "Agent output",
    exact: true,
  });
  await expect(
    output.locator("p").filter({ hasText: /^Output 1$/ })
  ).toBeVisible();
  await expect
    .poll(async () => {
      const a = await output.boundingBox();
      const b = await scroller.boundingBox();
      return a && b ? Math.abs(a.y + a.height - b.y - b.height) : Infinity;
    })
    .toBeLessThan(40);
  await expect
    .poll(() => scroller.evaluate((el) => el.scrollTop))
    .toBeGreaterThan(0);
  await scroller.evaluate((el) => {
    el.scrollTop = 0;
    el.dispatchEvent(new Event("scroll"));
  });
  append = true;
  await page.clock.fastForward(10000);
  await expect(
    output.getByRole("button", { name: /#2 Agent Output 2/ })
  ).toHaveCount(1);
  await expect(scroller).toHaveJSProperty("scrollTop", 0);
});

for (const outcome of ["success", "cancelled", "active-completed"]) {
  test(`environment root outcome ${outcome}`, async ({ page }) => {
    const records = { ...history, events: [
      operation(1, "root", null, "environment.setup", 5, 35, "setup", outcome === "cancelled" ? "cancelled" : "success"),
      operation(2, "failed-build", "root", "docker.image_build", 5, 10, "build", "error"),
      operation(3, "retry-build", "root", "docker.image_build", 10, 20),
      { ...operation(4, "incomplete", "root", "docker.stage_files", 20, 25, "stage", "started"), payload: { ...operation(4, "incomplete", "root", "docker.stage_files", 20, 25, "stage", "started").payload, finished_at: null, duration_seconds: null } },
    ] };
    const trial = outcome === "active-completed"
      ? { ...ended, status: "running", harbor_stage: "agent_setup", finished_at: null, phase_timing: { environment_setup: timing(5, 35) } }
      : ended;
    await mock(page, trial, records);
    await page.goto("/progress");
    const phase = page.getByRole("group", { name: "Environment Setup", exact: true });
    await expect(phase.getByTitle("docker.stage_files")).toBeVisible();
    await expect(phase.getByRole("img", { name: /^Environment Setup:/ })).toHaveAttribute("aria-label", `Environment Setup: ${outcome === "cancelled" ? "cancelled" : "completed"}`);
    await expect(phase.getByRole("timer")).toHaveCount(0);
    await expect(phase.getByText("End not recorded", { exact: true })).toBeVisible();
    await expect(phase.locator(".animate-spin")).toHaveCount(0);
  });
}

for (const mobile of [false, true]) {
  test(`chronological supporting activities (${mobile ? "mobile" : "desktop"})`, async ({ page }) => {
    await page.setViewportSize({ width: mobile ? 390 : 1280, height: 1000 });
    const create = operation(2, "create", "root", "thunder.create_sandbox", 6, 12, "provision");
    const body = operation(3, "body", "create", "thunder.create_sandbox.operation", 7, 11, "provision");
    const saved = operation(4, "save", "create", "progress.delivery.started", 6, 7, "other");
    const records = { ...history, events: [
      operation(1, "ready", "root", "thunder.sandbox_ready", 15, 25, "ready"),
      { ...create, payload: { ...create.payload, duration_seconds: 4 } },
      { ...operation(5, "root", null, "environment.setup", 5, 35, "setup"), payload: {
        ...operation(5, "root", null, "environment.setup", 5, 35, "setup").payload,
        completed_activities: [
          { ...operation(8, "root-report", "root", "progress.delivery.finished", 5, 6, "other").payload, activity_kind: "supporting" },
          { ...operation(7, "nested", "body", "thunder.resolve_image", 8, 10, "resolve").payload, activity_kind: "operation" },
          { ...body.payload, activity_kind: "operation" },
          { ...saved.payload, activity_kind: "supporting" },
          { ...operation(6, "record", "root", "environment.record_sandbox", 12, 15, "other").payload, activity_kind: "supporting" },
          create.payload,
        ],
      } },
    ] };
    await mock(page, { ...ended, phase_timing: { ...ended.phase_timing, environment_setup: timing(5, 45) } }, records);
    await page.goto("/progress");
    const progress = page.getByRole("region", { name: "Trial progress" });
    const environment = progress.getByRole("group", { name: "Environment Setup", exact: true });
    const rows = environment.getByRole("list", { name: "Environment setup steps", exact: true }).locator(":scope > li");
    await expect(rows).toHaveCount(3);
    await expect(environment.getByTitle("progress.delivery.finished")).toHaveCount(0);
    await expect(progress.getByTitle("progress.delivery.finished")).not.toBeVisible();
    await expect(rows.nth(0)).toContainText("Create sandbox");
    await expect(rows.nth(1)).toContainText("Record sandbox details");
    await expect(rows.nth(2)).toContainText("Wait for sandbox");
    await expect(environment.getByLabel("Expand all steps")).toHaveCount(0);
    await expect(rows.locator("details")).toHaveCount(0);
    await expect(rows.nth(0)).toContainText("6.0s");
    for (const phase of ["progress.delivery.started", "progress.delivery.finished", "thunder.create_sandbox.operation", "thunder.resolve_image"]) {
      await expect(progress.getByTitle(phase)).toHaveCount(0);
    }
    await expect(environment.getByText("30.0s", { exact: true })).toHaveCount(1);
    await expect(environment.getByText("40.0s", { exact: true })).toHaveCount(0);
    await expect(environment.getByText(/no recorded substep timing|Measurements may overlap/)).toHaveCount(0);
    await progress.screenshot({ path: `delivery-test-results/chronological-${mobile ? "mobile" : "desktop"}.png` });
    await expect(progress.getByText("Environment reporting · Attempt 1")).toHaveCount(0);
    await expect(progress.getByText("Save progress", { exact: true })).toHaveCount(0);
    await expect(progress.getByTitle("thunder.create_sandbox.operation")).toHaveCount(0);
    await expect(page.locator("body")).toHaveJSProperty("scrollWidth", await page.locator("body").evaluate(el => el.clientWidth));
  });
}

test("initializing remains active and unknown phase durations stay blank", async ({ page }) => {
  await mock(page, { ...progressTrial, harbor_stage: "starting", phase_timing: null }, { ...history, events: [] });
  await page.goto("/progress");
  const progress = page.getByRole("region", { name: "Trial progress" });
  await expect(progress.getByRole("img", { name: "Initializing: active", exact: true })).toBeVisible();
  await expect(progress.getByRole("img", { name: "Trial Started: not recorded", exact: true })).toBeVisible();
  await expect(progress.getByRole("group", { name: "Initializing", exact: true }).getByRole("timer")).toBeVisible();
  await expect(progress.getByRole("group", { name: "Environment Setup", exact: true }).getByText("Not recorded")).toHaveCount(0);
});

test("failed trial header and final node preserve the failure", async ({ page }) => {
  await mock(page, { ...ended, status: "failed" });
  await page.goto("/progress");
  const progress = page.getByRole("region", { name: "Trial progress" });
  await expect(progress.getByText("Failed", { exact: true })).toBeVisible();
  await expect(progress.getByRole("img", { name: "Completed: failed", exact: true })).toBeVisible();
  await expect(progress.getByRole("img", { name: "Environment Setup: completed", exact: true })).toBeVisible();
});

for (const [stage, label, phaseName] of [
  ["environment_setup", "Environment Setup", "environment_setup"],
  ["agent_running", "Agent Running", "agent_execution"],
  ["verification", "Verification", "verifier"],
] as const) {
  for (const recordedTiming of ["missing", "started", "finished"]) {
    test(`failure during ${stage} identifies the stopped phase (${recordedTiming} timing)`, async ({ page }) => {
      await mock(page, {
        ...ended, status: "failed", harbor_stage: stage,
        phase_timing: {
          environment_setup: stage === "environment_setup" ? null : timing(5, 35),
          agent_execution: stage === "verification" ? timing(35, 55) : null,
          verifier: null,
          [phaseName]: recordedTiming === "finished" ? timing(55, 65) : recordedTiming === "started" ? { started_at: at(55) } : null,
        },
      }, { ...history, events: [] });
      await page.goto("/progress");
      const progress = page.getByRole("region", { name: "Trial progress" });
      const phase = progress.getByRole("group", { name: label, exact: true });
      await expect(phase.getByRole("img")).toHaveAttribute("aria-label", `${label}: failed`);
      if (recordedTiming === "finished") {
        await expect(phase.getByText("10.0s", { exact: true })).toBeVisible();
      } else {
        await expect(phase.locator(".tabular-nums").filter({ hasText: /\S/ })).toHaveCount(0);
      }
      await expect(progress.getByRole("timer")).toHaveCount(0);
      await expect(progress.getByText("Failed", { exact: true })).toBeVisible();
      await expect(progress.getByRole("img", { name: "Completed: failed", exact: true })).toBeVisible();
      if (stage !== "environment_setup") {
        await expect(progress.getByRole("img", { name: "Environment Setup: completed", exact: true })).toBeVisible();
      }
      if (stage === "verification") {
        await expect(progress.getByRole("img", { name: "Agent Running: completed", exact: true })).toBeVisible();
      } else {
        await expect(progress.getByRole("img", { name: "Verification: not recorded", exact: true })).toBeVisible();
      }
      if (stage === "environment_setup") {
        await expect(progress.getByRole("img", { name: "Agent Running: not recorded", exact: true })).toBeVisible();
      }
    });
  }
}

test("failure before environment does not establish the Trial Started milestone", async ({ page }) => {
  await mock(page, { ...ended, status: "failed", phase_timing: null }, { ...history, events: [] });
  await page.goto("/progress");
  const progress = page.getByRole("region", { name: "Trial progress" });
  await expect(progress.getByRole("img", { name: "Trial Started: not recorded", exact: true })).toBeVisible();
  await expect(progress.getByRole("img", { name: "Initializing: not recorded", exact: true })).toBeVisible();
  await expect(progress.getByRole("img", { name: "Environment Setup: not recorded", exact: true })).toBeVisible();
  await expect(progress.getByRole("img", { name: "Completed: failed", exact: true })).toBeVisible();
});

test("timeline stage navigation still opens matching files", async ({ page }) => {
  await mock(page);
  await page.goto("/progress");
  const progress = page.getByRole("region", { name: "Trial progress" });
  await progress.getByRole("button", { name: "Verification", exact: true }).click();
  await expect(page.getByRole("tab", { name: "Files", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect.poll(() => new URL(page.url()).searchParams.get("file")).toBe("verifier/test-stdout.txt");
});

test("timeline and phase disclosures preserve toggles across clock and history updates", async ({ page }) => {
  await page.clock.install();
  let historyReads = 0;
  await page.route("**/api/**", route => {
    const path = new URL(route.request().url()).pathname;
    return route.fulfill({ json: path.endsWith("/progress-history")
      ? { ...history, events: history.events.map(event => ({ ...event, recorded_at: at(++historyReads) })) }
      : path.endsWith("/live") ? { attempt: 1, events: [], next_seq: 0, usage: {}, done: false }
      : { ...progressTrial, harbor_stage: "agent_running", input_tokens: 1, phase_timing: { environment_setup: timing(5, 35), agent_execution: { started_at: at(35) } } }
    });
  });
  await page.goto("/progress");
  const progress = page.getByRole("region", { name: "Trial progress" });
  const environment = progress.getByRole("group", { name: "Environment Setup", exact: true }).locator("details");
  await expect(progress.getByRole("group", { name: "Agent Running", exact: true }).locator("details")).toHaveCount(0);
  await environment.locator("summary").click();
  const reads = historyReads;
  await page.clock.runFor(11_000);
  await expect.poll(() => historyReads).toBeGreaterThan(reads);
  await expect(environment).not.toHaveAttribute("open", "");
  await progress.getByText("Execution Timeline", { exact: true }).click();
  await page.clock.runFor(11_000);
  await expect(progress.locator(":scope > details").first()).not.toHaveAttribute("open", "");
  await progress.getByText("Execution Timeline", { exact: true }).click();
  await environment.locator("summary").click();
  await page.clock.runFor(6_000);
  await expect(environment).toHaveAttribute("open", "");
  await page.getByRole("button", { name: "Table reports cancelled", exact: true }).click();
  await expect(progress.getByRole("img", { name: "Completed: cancelled", exact: true })).toBeVisible();
});

test("Live reports stream failure before any agent output", async ({ page }) => {
  await mock(page, progressTrial);
  await page.route("**/api/trials/*/live?*", route => route.fulfill({ status: 503, json: { detail: "Live backend unavailable" } }));
  await page.goto("/progress");
  await page.getByRole("tab", { name: "Live", exact: true }).click();
  await expect(page.getByRole("region", { name: "Live trial", exact: true }).getByRole("alert")).toContainText("Live stream error: Live backend unavailable");
  await expect(page.getByRole("region", { name: "Agent output" }).getByRole("alert")).toBeVisible();
});

for (const stage of [null, "environment_setup"]) {
  test(`initialization follows phase progress without timing (${stage ?? "null stage"})`, async ({ page }) => {
    await mock(page, { ...progressTrial, harbor_stage: stage, phase_timing: null }, { ...history, events: [] });
    await page.goto("/progress");
    const progress = page.getByRole("region", { name: "Trial progress" });
    const phase = progress.getByRole("group", { name: "Initializing", exact: true });
    await expect(phase.getByRole("img")).toHaveAttribute("aria-label", `Initializing: ${stage ? "completed" : "active"}`);
    if (stage) {
      await expect(phase.getByRole("timer")).toHaveCount(0);
      await expect(phase.locator(".tabular-nums")).toHaveText("");
    } else {
      await expect(phase.getByRole("timer")).toContainText("elapsed");
      await expect(progress.getByRole("img", { name: "Trial Started: not recorded", exact: true })).toBeVisible();
    }
  });
}

for (const status of ["pending", "queued", "retrying"]) {
  for (const stage of ["environment_setup", "agent_running", "verification"]) {
    test(`waiting ${status} ignores stale ${stage} timing`, async ({ page }) => {
      await mock(page, {
        ...ended, attempts: 2, status, harbor_stage: stage,
        phase_timing: {
          ...ended.phase_timing,
          // Mix finished phases with an unfinished previous-attempt timer.
          verifier: { started_at: at(55) },
        },
      }, { ...history, events: [] });
      await page.goto("/progress");
      const progress = page.getByRole("region", { name: "Trial progress" });
      for (const label of ["Initializing", "Trial Started", "Environment Setup", "Agent Running", "Verification", "Completed"]) {
        const phase = progress.getByRole("group", { name: label, exact: true });
        await expect(phase.getByRole("img")).toHaveAttribute("aria-label", `${label}: not recorded`);
        await expect(phase.getByRole("timer")).toHaveCount(0);
        await expect(phase.locator(".tabular-nums").filter({ hasText: /\S/ })).toHaveCount(0);
      }
    });
  }
}

for (const phaseName of ["agent_execution", "verifier"]) {
  test(`initialization recognizes downstream ${phaseName} timing without a stage`, async ({ page }) => {
    await mock(page, { ...progressTrial, harbor_stage: null, phase_timing: { [phaseName]: { started_at: at(35) } } }, { ...history, events: [] });
    await page.goto("/progress");
    const progress = page.getByRole("region", { name: "Trial progress" });
    const phase = progress.getByRole("group", { name: "Initializing", exact: true });
    await expect(phase.getByRole("img")).toHaveAttribute("aria-label", "Initializing: completed");
    await expect(phase.getByRole("timer")).toHaveCount(0);
    await expect(phase.locator(".tabular-nums")).toHaveText("");
    await expect(progress.getByRole("img", { name: "Trial Started: completed", exact: true })).toBeVisible();
  });
}


test("Summary agent usage preserves agent-file navigation", async ({ page }) => {
  await mock(page, { ...ended, input_tokens: 7, output_tokens: 3 });
  await page.goto("/progress");
  await page.getByRole("button", { name: "Agent Running", exact: true }).click();
  await expect(page.getByRole("tab", { name: "Files", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect.poll(() => new URL(page.url()).searchParams.get("file")).toBe("agent");
});

test("public detail keeps its original conditional timeline without runtime additions", async ({ page }) => {
  await mock(page);
  await page.goto("/progress");
  await page.getByRole("button", { name: "Public view", exact: true }).click();
  await expect(page.getByText("Execution Timeline", { exact: true })).toBeVisible();
  await expect(page.getByText("Run metadata & timing sources", { exact: true })).toHaveCount(0);
  await expect(page.getByText(/No setup history recorded/)).toHaveCount(0);
  await page.getByRole("button", { name: "Public without stage", exact: true }).click();
  await expect(page.getByText("Execution Timeline", { exact: true })).toHaveCount(0);
});

test("Live drains completed transcript pages before stopping", async ({ page }) => {
  const cursors: number[] = [];
  await page.route("**/api/**", route => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/progress-history")) return route.fulfill({ json: progressHistory });
    if (!url.pathname.endsWith("/live")) return route.fulfill({ json: { ...progressTrial, agent: "claude-code", harbor_stage: "agent_running" } });
    const cursor = Number(url.searchParams.get("after_seq"));
    cursors.push(cursor);
    const events = cursor === 0 ? Array.from({ length: 500 }, (_, index) => ({
      seq: index + 1, kind: "message", payload: { role: "assistant", text: `First page ${index}`, turn_id: "1" },
    })) : cursor === 500 ? [{ seq: 501, kind: "message", payload: { role: "assistant", text: "Final page retained", turn_id: "2" } }] : [];
    return route.fulfill({ json: { attempt: 1, events, next_seq: events.at(-1)?.seq ?? cursor, usage: {}, harbor_stage: "completed", done: true } });
  });
  await page.goto("/progress");
  await page.getByRole("tab", { name: "Live", exact: true }).click();
  await expect(page.getByRole("region", { name: "Agent output" }).getByText("Final page retained", { exact: true }).last()).toBeVisible();
  await expect.poll(() => cursors.includes(501)).toBe(true);
});

for (const mobile of [false, true])
  for (const view of ["Summary", "Live"])
    test(`EC2 health updates one existing row in ${view} (${mobile ? "mobile" : "desktop"})`, async ({ page }) => {
      await page.setViewportSize({ width: mobile ? 390 : 1280, height: 1000 });
      await page.clock.install({ time: new Date(at(15)) });
      let healthStatus = "initializing";
      let complete = false;
      const setupStart = operation(1, "root", null, "environment.setup", 5, 35, "setup", "started");
      const healthStart = operation(2, "health", "root", "ec2.instance_status_ok", 10, 30, "ready", "started");
      await page.route("**/api/**", route => {
        const path = new URL(route.request().url()).pathname;
        const ec2_health = {
          observed_at: at(complete ? 30 : 15), instance_state: "running", instance_status: healthStatus,
          system_status: "ok", instance_reachability: healthStatus === "ok" ? "passed" : "initializing", system_reachability: "passed",
          instance_impaired_since: null, system_impaired_since: null,
        };
        const events = [setupStart, healthStart, { ...healthStart, source_seq: 3, event_key: "live:3", payload: {
          ...healthStart.payload, status: complete ? "success" : "started", duration_seconds: complete ? 20 : null,
          finished_at: complete ? at(30) : null, ec2_health,
        } }];
        if (complete) events.push(operation(4, "root", null, "environment.setup", 5, 35, "setup"));
        return route.fulfill({ json: path.endsWith("/progress-history") ? { ...progressHistory, events }
          : path.endsWith("/live") ? { attempt: 1, events: [], next_seq: 0, usage: {}, done: false }
          : { ...progressTrial, agent: "claude-code", harbor_stage: complete ? "agent_running" : "environment_setup", phase_timing: {
            environment_setup: complete ? timing(5, 35) : { started_at: at(5) },
          } }
        });
      });
      await page.goto("/progress");
      if (view === "Live") await page.getByRole("tab", { name: "Live", exact: true }).click();
      const label = page.getByTitle("ec2.instance_status_ok", { exact: true });
      const disclosure = page.locator("summary").filter({ hasText: /^Environment Setup/ }).first();
      await expect(label).toBeAttached();
      if (await disclosure.locator("..").getAttribute("open") === null) await disclosure.click();
      await expect(label).toBeVisible();
      await expect(label).toHaveCount(1);
      await expect(label).toContainText("Wait for instance health check");
      const healthToggle = label.getByRole("button", { name: "Wait for instance health check", exact: true });
      const healthDetails = page.getByTitle("AWS status reported " + at(15), { exact: true });
      await expect(healthToggle).toHaveAttribute("aria-expanded", "false");
      await expect(healthDetails).toBeHidden();
      await label.locator("../..").screenshot({ path: `delivery-test-results/ec2-health-closed-${view.toLowerCase()}-${mobile ? "mobile" : "desktop"}.png` });
      await healthToggle.focus();
      await page.keyboard.press("Enter");
      await expect(healthToggle).toHaveAttribute("aria-expanded", "true");
      await expect(healthDetails).toBeVisible();
      await expect(healthDetails).toContainText("Instance running · Instance health initializing · System health passed");
      const row = label.locator("..");
      await expect(row.getByLabel("running", { exact: true })).toBeVisible();
      const initialSeconds = Number.parseFloat(await row.locator(":scope > span").last().innerText());
      expect(initialSeconds).toBeGreaterThanOrEqual(5);
      expect(initialSeconds).toBeLessThan(10);
      await row.locator("..").screenshot({ path: `delivery-test-results/ec2-health-initializing-${view.toLowerCase()}-${mobile ? "mobile" : "desktop"}.png` });
      healthStatus = "ok";
      await page.clock.runFor(6000);
      await expect(healthDetails).toContainText("Instance health passed");
      await expect(healthToggle).toHaveAttribute("aria-expanded", "true");
      await expect(label).toHaveCount(1);
      const updatedSeconds = Number.parseFloat(await row.locator(":scope > span").last().innerText());
      expect(updatedSeconds).toBeGreaterThan(initialSeconds + 5);
      complete = true;
      await page.clock.runFor(6000);
      await expect(row.getByLabel("done", { exact: true })).toBeVisible();
      await expect(row).toContainText("20.0s");
      const finalDetails = page.getByTitle("AWS status reported " + at(30), { exact: true });
      await expect(finalDetails).toContainText("System health passed");
      await expect(healthToggle).toHaveAttribute("aria-expanded", "true");
      await healthToggle.click();
      await expect(finalDetails).toBeHidden();
      await expect(row).toContainText("20.0s");
      await healthToggle.click();
      await expect(finalDetails).toBeVisible();
      const bounds = await row.boundingBox();
      expect(bounds).not.toBeNull();
      expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(mobile ? 390 : 1280);
      await row.locator("..").screenshot({ path: `delivery-test-results/ec2-health-${view.toLowerCase()}-${mobile ? "mobile" : "desktop"}.png` });
      await page.reload();
      if (view === "Live") await page.getByRole("tab", { name: "Live", exact: true }).click();
      await expect(label).toBeVisible();
      await expect(healthToggle).toHaveAttribute("aria-expanded", "false");
      await expect(finalDetails).toBeHidden();
      await expect(row).toContainText("20.0s");
    });
