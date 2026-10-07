import { expect, test } from "@playwright/test";
import { openFor, tasks } from "./review-app/records";

test("single group statistics only collapse when their trial scope matches", async ({
  page,
}) => {
  for (const scenario of [
    "single",
    "two-models",
    "different-scope",
    "historical",
  ] as const) {
    await page.route("**/api/tasks/task-a/open**", (route) => {
      const open = openFor(tasks[0], scenario === "historical" ? 6 : 7);
      const version = open.selected_version!;
      if (scenario === "two-models") {
        version.agent_models.push({
          ...version.agent_models[0],
          model: "openai/another-model",
        });
        version.trial_count = 2;
      }
      if (scenario === "different-scope") version.trial_count = 2;
      return route.fulfill({ json: open });
    });
    await page.goto(
      `/tasks/task-a${scenario === "historical" ? "?version=6" : ""}`
    );
    await expect(
      page.getByRole("heading", { name: "Agents", exact: true })
    ).toBeVisible();
    await expect(page.getByText(/^avg score$/i)).toHaveCount(
      scenario === "two-models" ? 2 : scenario === "different-scope" ? 1 : 0
    );
    await expect(
      page.getByText("Total cost (all versions)", { exact: true })
    ).toBeVisible();
    await expect(page.getByText("Billed spend", { exact: true })).toBeVisible();
    await expect(page.getByText(/0\/1 pass/)).toHaveCount(0);
    await page.unroute("**/api/tasks/task-a/open**");
  }
});

test("page keeps its verdict while the drawer shows its own verdict and control", async ({
  page,
}) => {
  await page.goto("/tasks/task-a?version=7&drawer=task&taskPane=overview");
  await expect(
    page.getByRole("heading", { name: "Findings", exact: true })
  ).toBeVisible();
  await expect(page.getByText(/^(Verdict rejected: )?1 Must fix$/)).toHaveCount(
    2
  );
  // A rejected verdict is final: neither the page nor the drawer offers a
  // rerun.
  await expect(
    page.getByRole("button", { name: /Generate QA verdict for v7/i })
  ).toHaveCount(0);
  const drawer = page.locator('[data-slot="resizable-drawer"]');
  await expect(
    drawer.getByText(/^(Verdict rejected: )?1 Must fix$/)
  ).toHaveCount(1);
  await drawer.getByRole("button", { name: "Close", exact: true }).click();
  await expect(page.getByText(/^(Verdict rejected: )?1 Must fix$/)).toHaveCount(
    1
  );
  await expect(
    page.getByRole("button", { name: /Generate QA verdict for v7/i })
  ).toHaveCount(0);
});

test("usage, action counts and experiment groups retain their meaning at desktop and narrow widths", async ({
  page,
}) => {
  await page.goto("/duplication");
  await expect(page.getByText("Input: 1.2M,", { exact: true })).toBeVisible();
  await expect(page.getByText("Output: 300.0K", { exact: true })).toBeVisible();
  await expect(
    page.getByText("(Total: 1.5M tokens)", { exact: true })
  ).toBeVisible();
  await expect(
    page.getByText(/Statuses include trial and task-QA/)
  ).toHaveCount(0);
  await expect(
    page.getByRole("heading", {
      name: "ACTION ITEMS: 2 MUST FIX TASK DEFECTS",
      exact: true,
    })
  ).toBeVisible();
  await page.getByLabel("Defect count").selectOption("1");
  await expect(
    page.getByRole("heading", {
      name: "ACTION ITEMS: 1 MUST FIX TASK DEFECT",
      exact: true,
    })
  ).toBeVisible();
  await page.getByLabel("Defect count").selectOption("0");
  await expect(
    page.getByRole("heading", { name: /ACTION ITEMS:/ })
  ).toHaveCount(0);
  const sourceA = page
    .getByRole("heading", { name: "Dependency baseline", exact: true })
    .locator("..");
  const sourceB = page
    .getByRole("heading", { name: "Verifier comparison", exact: true })
    .locator("..");
  await expect(
    sourceA.getByRole("button", { name: "View trial", exact: true })
  ).toHaveCount(2);
  await expect(
    sourceB.getByRole("button", { name: "View trial", exact: true })
  ).toHaveCount(1);
  await expect(sourceA.getByRole("link")).toHaveAttribute(
    "href",
    "/experiments/source-a"
  );
  const runs = sourceA.locator("article");
  await expect(runs.nth(0).getByText("CAUSE", { exact: true })).toBeVisible();
  await expect(runs.nth(0).getByText("EVIDENCE", { exact: true })).toHaveCount(
    0
  );
  await expect(
    runs.nth(1).getByText("EVIDENCE", { exact: true })
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByText("Output: 300.0K", { exact: true })).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth
    )
  ).toBe(true);
});

for (const id of ["unreviewed", "review-failed"]) {
  test(`verdict action stays with the ${id} result and explanation`, async ({
    page,
  }) => {
    await page.goto(`/tasks/${id}?drawer=task&taskPane=overview`);
    const action = page.getByRole("button", {
      name: /Generate QA verdict for v/i,
    });
    await expect(action).toHaveCount(1);
    const card = action.locator("..");
    await expect(card).toContainText(
      id === "unreviewed" ? "No verdict yet" : "Verdict failed"
    );
    if (id === "review-failed")
      await expect(card).toContainText("Evidence could not be read.");
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(action).toBeVisible();
  });
}

test("opening a trial drawer preserves acceptance before and after the panel response", async ({
  page,
}) => {
  const reasoning = "The recorded runs support acceptance.";
  await page.route("**/api/tasks/task-a/open**", (route) => {
    const open = openFor(tasks[0], 7);
    open.task.verdict = {
      is_good: true,
      recommendations: [],
      confidence: "high",
      reasoning,
      _graded_by: "task-a-published-qa",
    };
    open.selected_version!.must_fix_count = 0;
    open.selected_version!.pre_trial_must_fix_count = 0;
    return route.fulfill({ json: open });
  });
  let releasePanel!: () => void;
  const gate = new Promise<void>((resolve) => {
    releasePanel = resolve;
  });
  await page.route("**/api/tasks/task-a/panel**", async (route) => {
    const panel = await (await route.fetch()).json();
    panel.qa_active = true;
    panel.can_run_qa = false;
    panel.task.verdict_status = "queued";
    panel.task.verdict = { ...panel.task.verdict, _graded_by: "task-a-other-qa" };
    panel.version.pre_trial_findings = [];
    panel.version.retained_findings = [];
    await gate;
    await route.fulfill({ json: panel });
  });
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/tasks/task-a");
  const accepted = page.getByText("Verdict accepted", { exact: true });
  const explanation = page.getByText(reasoning, { exact: true });
  await expect(accepted).toHaveCount(1);
  await expect(explanation).toHaveCount(1);
  await page.getByRole("button", { name: /^task-a-trial / }).click();
  const drawer = page.locator('[data-slot="resizable-drawer"]');
  const panelResponse = page.waitForResponse((response) =>
    response.url().includes("/api/tasks/task-a/panel")
  );
  try {
    await expect(drawer).toBeVisible();
    await expect(accepted).toHaveCount(2);
    await expect(explanation).toHaveCount(2);
    await expect(
      drawer.getByRole("link", { name: "View source review (new tab)", exact: true })
    ).toHaveAttribute("href", "/tasks/task-a?trial=task-a-published-qa");
  } finally {
    releasePanel();
  }
  await panelResponse;
  await expect(
    drawer.getByRole("button", { name: /Generate QA verdict/ })
  ).toHaveCount(0);
  await expect(accepted).toHaveCount(2);
  await expect(explanation).toHaveCount(2);
  await expect(
    drawer.getByRole("link", { name: "View source review (new tab)", exact: true })
  ).toHaveAttribute("href", "/tasks/task-a?trial=task-a-published-qa");
  await expect(page.getByText("Verdict queued", { exact: true })).toHaveCount(
    0
  );
  await drawer.getByRole("button", { name: "Close", exact: true }).click();
  await expect(drawer).toHaveCount(0);
  await expect(accepted).toHaveCount(1);
  await expect(explanation).toHaveCount(1);
});

for (const mode of ["task", "trial"]) {
  for (const review of ["verdict", "audit"]) {
    test(`queuing ${review} from the ${mode} pane refreshes the shared page verdict`, async ({
      page,
    }) => {
      let queued = false;
      await page.route("**/api/tasks/task-a/open**", (route) => {
        const open = openFor(tasks[0], 7);
        // Only a failed verdict or audit can be run again; an accepted or
        // rejected verdict is final.
        open.task.verdict_status = queued
          ? review === "verdict"
            ? "queued"
            : null
          : "failed";
        open.task.verdict = null;
        open.task.verdict_error = queued ? null : "QA run failed.";
        open.selected_version!.must_fix_count = 0;
        open.selected_version!.pre_trial_must_fix_count = 0;
        return route.fulfill({ json: open });
      });
      await page.route("**/api/tasks/task-a/panel**", async (route) => {
        const panel = await (await route.fetch()).json();
        panel.qa_active = queued;
        if (review === "audit")
          panel.version.pre_trial_status = queued ? "queued" : "failed";
        panel.can_run_qa = !queued;
        panel.version.pre_trial_findings = [];
        panel.version.retained_findings = [];
        await route.fulfill({ json: panel });
      });
      await page.route(
        `**/api/tasks/task-a/qa/${review === "verdict" ? "retry" : "pre-trial"}`,
        (route) => {
          queued = true;
          return route.fulfill({ json: { status: "queued" } });
        }
      );
      await page.setViewportSize({ width: 1600, height: 900 });
      await page.goto(
        mode === "task"
          ? "/tasks/task-a?drawer=task&taskPane=overview"
          : "/tasks/task-a?trial=task-a-trial&taskPane=overview"
      );
      const drawer = page.locator('[data-slot="resizable-drawer"]');
      await expect(
        page.getByText("Verdict failed", { exact: true })
      ).toHaveCount(2);
      await drawer
        .getByRole("button", {
          name:
            review === "verdict"
              ? "Generate QA verdict for v7"
              : "Run pre-trial audit v7",
          exact: true,
        })
        .click();
      await expect(
        page.getByText(
          review === "verdict" ? "Verdict queued" : "No verdict yet",
          { exact: true }
        )
      ).toHaveCount(2);
      await expect(
        page.getByText("Verdict failed", { exact: true })
      ).toHaveCount(0);
    });
  }
}

test("a newer report fetched by the drawer replaces the page verdict and keeps its full text", async ({
  page,
}) => {
  const fixes = Array.from({ length: 9 }, (_, i) => `Fix ${i + 1}.`);
  let rerun = false;
  await page.route("**/api/tasks/task-a/open**", (route) => {
    const open = openFor(tasks[0], 7);
    open.task.verdict_status = "success";
    // The compact page response caps recommendations at eight.
    open.task.verdict = rerun
      ? {
          is_good: false,
          confidence: "high",
          reasoning: "The verifier is broken.",
          recommendations: fixes.slice(0, 8),
          _graded_by: "task-a-new-qa",
        }
      : {
          is_good: true,
          confidence: "high",
          reasoning: "The recorded runs support acceptance.",
          recommendations: [],
          _graded_by: "task-a-old-qa",
        };
    open.selected_version!.must_fix_count = 0;
    open.selected_version!.pre_trial_must_fix_count = 0;
    return route.fulfill({ json: open });
  });
  await page.route("**/api/tasks/task-a/panel**", async (route) => {
    const panel = await (await route.fetch()).json();
    // Another reviewer reran QA after the page loaded.
    rerun = true;
    panel.qa_active = false;
    panel.task.verdict_status = "success";
    panel.task.verdict = {
      is_good: false,
      confidence: "high",
      reasoning: "The verifier is broken.",
      recommendations: fixes,
      _graded_by: "task-a-new-qa",
    };
    panel.version.pre_trial_findings = [];
    panel.version.retained_findings = [];
    await route.fulfill({ json: panel });
  });
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/tasks/task-a");
  await expect(
    page.getByText("Verdict accepted", { exact: true })
  ).toHaveCount(1);
  await page.getByRole("button", { name: /^task-a-trial / }).click();
  const drawer = page.locator('[data-slot="resizable-drawer"]');
  await expect(
    page.getByText("Verdict accepted", { exact: true })
  ).toHaveCount(0);
  await expect(
    drawer.getByText("Previous task review: Rejected", { exact: true })
  ).toBeVisible();
  await expect(drawer.getByText("Fix 9.", { exact: true })).toBeVisible();
  await expect(
    drawer.getByRole("link", { name: "View rejected review", exact: true })
  ).toHaveAttribute("href", "/tasks/task-a?trial=task-a-new-qa");
});


test("queued trial links its earlier rejection without replacing the current run", async ({ page, context }) => {
  const sourceId = "task-a-42";
  const queued = { ...tasks[0].trials![0], status: "queued", reward: null,
    started_at: null, finished_at: null, analysis: null, analysis_status: null };
  await context.route("**/api/tasks/task-a/open**", (route) => {
    const open = openFor(tasks[0], 7);
    open.task.verdict!._graded_by = sourceId;
    open.trials = [queued as typeof open.trials[number]];
    return route.fulfill({ json: open });
  });
  await context.route("**/api/tasks/task-a/panel**", async (route) => {
    const panel = await (await route.fetch()).json();
    panel.task.verdict._graded_by = sourceId;
    return route.fulfill({ json: panel });
  });
  await context.route("**/api/trials/task-a-trial", (route) => route.fulfill({ json: queued }));
  await context.route(`**/api/trials/${sourceId}`, (route) => route.fulfill({ json: {
    ...tasks[0].trials![0], id: sourceId, name: sourceId,
  } }));
  await page.goto("/tasks/task-a?version=7&trial=task-a-trial&tab=summary");
  const drawer = page.locator('[data-slot="resizable-drawer"]');
  await expect(drawer.getByText("Previous task review: Rejected · 1 Must fix", { exact: true })).toBeVisible();
  await expect(drawer.getByText("This rejection is from an earlier review, not this trial.")).toBeVisible();
  await expect(drawer.getByText("Queued", { exact: true })).toBeVisible();
  const source = drawer.getByRole("link", { name: "View rejected review" });
  await expect(source).toHaveAttribute("target", "_blank");
  await expect(source).toHaveAttribute("rel", "noopener noreferrer");
  await expect(source).toHaveAttribute("href", /trial=task-a-42/);
  await expect(drawer.getByRole("tab", { name: "Summary", exact: true })).toBeVisible();
  await expect(drawer.getByRole("heading", { name: "Findings", exact: true })).toBeVisible();
  const popupPromise = page.waitForEvent("popup");
  await source.click();
  const popup = await popupPromise;
  await expect(popup).toHaveURL(/trial=task-a-42/);
  await expect(popup.locator('[data-slot="resizable-drawer"]').getByText("Task review: Rejected · 1 Must fix", { exact: true })).toBeVisible();
  await expect(popup.getByText("This rejection is from an earlier review, not this trial.")).toHaveCount(0);
  await expect(page).toHaveURL(/trial=task-a-trial/);
  await expect(drawer.getByText("Queued", { exact: true })).toBeVisible();
  await popup.goto("/tasks/task-a?trial=42&tab=summary");
  await expect(popup.locator('[data-slot="resizable-drawer"]').getByText("Task review: Rejected · 1 Must fix", { exact: true })).toBeVisible();
  await popup.close();
});
