import { runSummary } from "./run-summary-fixture";
import { expect, test } from "@playwright/test";

const storageKey = "oddish.tasks.selection.org-1";
const ids = Array.from({ length: 1201 }, (_, i) => `task-${i}`);

test.beforeEach(async ({ page }) => {
  await page.addInitScript(
    ({ storageKey, ids }) => {
      if (!localStorage.getItem(storageKey)) {
        localStorage.setItem(
          storageKey,
          JSON.stringify({
            v: 1,
            entries: ids.map((id) => [id, { cost: null, estimated: false }]),
          })
        );
      }
      const writes: string[] = [];
      Object.assign(window, { selectionWrites: writes });
      const remove = Storage.prototype.removeItem;
      const set = Storage.prototype.setItem;
      Storage.prototype.removeItem = function (key) {
        if (key === storageKey) writes.push("removed");
        return remove.call(this, key);
      };
      Storage.prototype.setItem = function (key, value) {
        if (key === storageKey) writes.push(value);
        return set.call(this, key, value);
      };
    },
    { storageKey, ids }
  );
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const data =
      path === "/api/customers"
        ? [{ id: "lab", name: "Lab" }]
        : path === "/api/deliveries"
          ? [
              {
                id: "existing",
                name: "Existing",
                status: "active",
                task_count: 0,
              },
            ]
          : path.endsWith("/facets")
            ? { delivery_customers: ["Mapped Lab", "Legacy Lab"], categories: ["coding"], agent_models: [{ agent: "test-agent", model: "test-model" }] }
            : path.endsWith("/count")
              ? { total: 0 }
              : { items: [], total: 0, has_more: false };
    await route.fulfill({ json: data });
  });
  await page.goto("/picker");
  await expect(page.getByText("1,201 selected", { exact: true })).toBeVisible({ timeout: 30_000 });
});

test("restoration never removes stored picks, including Strict Mode and remount", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Remount picker" }).click();
  await expect(page.getByText("1,201 selected", { exact: true })).toBeVisible();
  const writes = await page.evaluate(
    () => (window as unknown as { selectionWrites: string[] }).selectionWrites
  );
  expect(writes).not.toContain("removed");
  for (const value of writes)
    expect(JSON.parse(value).entries).toHaveLength(1201);
});

test("rapid author and sort changes preserve each other and pending search", async ({
  page,
}) => {
  await page
    .getByRole("textbox", { name: "Search tasks", exact: true })
    .fill("some task");
  await page.getByRole("button", { name: "Filter tasks by member" }).click();
  await page.evaluate(() => {
    const mine = Array.from(document.querySelectorAll("button")).find(button => button.textContent?.trim() === "Mine")!;
    const sort = document.querySelector<HTMLSelectElement>(
      'select[aria-label="Sort tasks"]'
    )!;
    mine.click();
    sort.value = "total_trials_desc";
    sort.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await expect
    .poll(() => new URL(page.url()).searchParams.get("q"))
    .toBe("some task");
  expect(new URL(page.url()).searchParams.get("mine")).toBe("only");
  expect(new URL(page.url()).searchParams.has("author")).toBe(false);
  expect(new URL(page.url()).searchParams.get("sort")).toBe(
    "total_trials_desc"
  );
  await expect(
    page.getByRole("button", { name: "Ready to ship", exact: true })
  ).toHaveCount(0);
});

test("creation sends the entire selection once and keeps it on failure", async ({
  page,
}) => {
  const requests: string[][] = [];
  await page.route("**/api/deliveries", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    requests.push(route.request().postDataJSON().task_ids);
    await route.fulfill({
      status: 404,
      json: { detail: "tasks not found: task-1200" },
    });
  });
  await page.getByRole("button", { name: "Add 1,201 to delivery" }).click();
  await page.getByRole("button", { name: "New delivery…" }).click();
  await page.getByLabel("Name", { exact: true }).fill("Batch");
  await page.getByRole("combobox", { name: "Customer", exact: true }).click();
  await page.getByRole("option", { name: "Lab", exact: true }).click();
  await page.getByRole("button", { name: "Create and add 1,201" }).click();
  await expect(page.getByRole("alert")).toHaveText(
    "tasks not found: task-1200"
  );
  expect(requests).toEqual([ids]);
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByText("1,201 selected", { exact: true })).toBeVisible();
});

test("organization changes never overwrite either organization's picks", async ({
  page,
}) => {
  await page.evaluate(() =>
    localStorage.setItem(
      "oddish.tasks.selection.org-2",
      JSON.stringify({
        v: 1,
        entries: [["other-task", { cost: null, estimated: false }]],
      })
    )
  );
  await page.getByRole("button", { name: "Switch organization" }).click();
  await expect(page.getByText("1 selected", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Switch organization" }).click();
  await expect(page.getByText("1,201 selected", { exact: true })).toBeVisible();
  expect(
    await page.evaluate(
      () =>
        JSON.parse(localStorage.getItem("oddish.tasks.selection.org-2")!)
          .entries
    )
  ).toEqual([["other-task", { cost: null, estimated: false }]]);
});

test("adding to an existing delivery uses one request for all picks", async ({
  page,
}) => {
  const requests: string[][] = [];
  await page.route("**/api/deliveries/existing/tasks", async (route) => {
    requests.push(route.request().postDataJSON().task_ids);
    await route.fulfill({ json: { added: ids.length } });
  });
  await page.getByRole("button", { name: "Add 1,201 to delivery" }).click();
  await page
    .getByRole("combobox", { name: "Delivery batch" })
    .click();
  await page.getByRole("option", { name: "Existing · No lab · 0 tasks", exact: true }).click();
  expect(requests).toEqual([]);
  await page
    .getByRole("button", { name: "Add 1,201 tasks", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Clear selection", exact: true })
  ).toHaveCount(0);
  expect(requests).toEqual([ids]);
});

test("filter presets and delivery context survive clearing filters", async ({
  page,
}) => {
  await page.goto("/picker?delivery=existing&not_delivered_to=Lab");
  await expect(
    page.getByRole("heading", { name: "Add tasks to Existing" })
  ).toBeVisible();
  await page.getByRole("button", { name: /^More criteria/ }).click();
  await page.getByRole("textbox", { name: "Find a filter" }).fill("trial");
  await page
    .getByRole("combobox", { name: "Trial count", exact: true })
    .selectOption("5");
  await expect
    .poll(() => new URL(page.url()).searchParams.get("total_trials_min"))
    .toBe("5");
  await page.keyboard.press("Escape");
  await page
    .getByRole("button", { name: "Clear filters", exact: true })
    .first()
    .click();
  await expect
    .poll(() => new URL(page.url()).searchParams.get("total_trials_min"))
    .toBe(null);
  expect(new URL(page.url()).searchParams.get("delivery")).toBe("existing");
});

test("reviewing selection exposes picks outside the current empty results", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Review selection" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(
    page.getByRole("link", { name: "task-1200", exact: true })
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Remove", exact: true })
    .first()
    .click();
  await expect(
    page.getByRole("heading", { name: "1,200 selected tasks" })
  ).toBeVisible();
});

const task = {
  id: "visible-task",
  name: "Queue recovery",
  run_summary: runSummary({ total: 40, scored: 30, reward_sum: 29, pass_count: 29, fail_count: 1, other_count: 10 }),
  current_version: 3,
  current_version_id: "version-3",
  version_count: 3,
  qa_outcome: "accepted",
  total_trials: 40,
  completed_trials: 29,
  failed_trials: 1,
  reward_success: 29,
  reward_sum: 29,
  reward_total: 30,
  pass_count: 29,
  partial_count: 0,
  fail_count: 1,
  harness_count: 0,
  pending_count: 10,
  agent_count: 3,
  cost_usd: 4.5,
  cost_trial_count: 30,
  latest_trials: [],
  latest_trials_truncated: true,
  experiments: [],
  user_tags: [],
  deliveries: [],
};

test("delivery picker preserves trial counts and only adds to its named destination", async ({
  page,
}) => {
  await page.route("**/api/tasks/browse?**", (route) =>
    route.fulfill({
      json: { items: [task], offset: 0, limit: 24, has_more: false },
    })
  );
  await page.route("**/api/tasks/browse/count?**", (route) =>
    route.fulfill({ json: { total: 1 } })
  );
  let submitted: string[] = [];
  await page.route("**/api/deliveries/existing/tasks", (route) => {
    submitted = route.request().postDataJSON().task_ids;
    return route.fulfill({ json: { added: 1 } });
  });
  await page.goto("/picker?delivery=existing&view=table");
  await expect(
    page.getByRole("heading", { name: "Add tasks to Existing" })
  ).toBeVisible();
  await expect(page.getByText(/30 scored \/ 40 model trials/)).toBeVisible();
  await page
    .getByRole("button", { name: "Trial details", exact: true })
    .click();
  await expect(page.getByText("Model score: 97%", { exact: true })).toBeVisible();
  await page
    .getByRole("checkbox", { name: "Select Queue recovery", exact: true })
    .first()
    .check();
  await page
    .getByRole("button", { name: "Add 1 to Existing", exact: true })
    .click();
  await expect(page).toHaveURL(/\/deliveries\/existing$/, { timeout: 30_000 });
  expect(submitted).toEqual(["visible-task"]);
});

test("numeric custom ranges apply once and reject inverted bounds", async ({
  page,
}) => {
  await page.getByRole("button", { name: /^More criteria/ }).click();
  await page.getByRole("textbox", { name: "Find a filter" }).fill("Median steps");
  await page
    .getByRole("combobox", { name: "Median steps", exact: true })
    .selectOption("custom");
  await page
    .getByRole("spinbutton", { name: "Median steps minimum" })
    .fill("100");
  await page
    .getByRole("spinbutton", { name: "Median steps maximum" })
    .fill("50");
  await expect(page.getByText("Minimum cannot exceed maximum")).toBeVisible();
  expect(new URL(page.url()).searchParams.get("steps_p50_min")).toBe(null);
  await page
    .getByRole("spinbutton", { name: "Median steps maximum" })
    .fill("200");
  await page.getByRole("button", { name: "Apply", exact: true }).click();
  await expect
    .poll(() => new URL(page.url()).searchParams.get("steps_p50_min"))
    .toBe("100");
  expect(new URL(page.url()).searchParams.get("steps_p50_max")).toBe("200");
});



test("sorting only-mine links keeps the author filter", async ({page}) => {
  await page.goto("/picker?mine=only");
  await expect(page.getByText("1,201 selected", { exact: true })).toBeVisible();
  await page.getByRole("combobox", {name: "Sort tasks"}).selectOption("model_score_desc");
  await expect.poll(() => new URL(page.url()).searchParams.get("mine")).toBe("only");
  await page.evaluate(() => {
    const sort = document.querySelector<HTMLSelectElement>('select[aria-label="Sort tasks"]')!;
    const clear = document.querySelector<HTMLButtonElement>('button[aria-label="Clear member filter"]')!;
    sort.value = "recent"; sort.dispatchEvent(new Event("change", {bubbles: true}));
    clear.click();
  });
  await expect.poll(() => new URL(page.url()).searchParams.get("mine")).toBe("off");
  expect(new URL(page.url()).searchParams.has("author")).toBe(false);
});

for (const clear of ["Clear filters", "Author: Me ×"]) {
  test(`${clear} restores everyone after selecting Only mine`, async ({ page }) => {
    await page.goto("/picker?delivery=existing&mine=off&sort=model_score_desc&run_coverage=model_trials");
    await page.getByRole("button", { name: "Filter tasks by member" }).click();
    await page.getByRole("button", { name: "Mine", exact: true }).click();
    await expect.poll(() => new URL(page.url()).searchParams.get("mine")).toBe("only");
    expect(new URL(page.url()).searchParams.has("author")).toBe(false);
    await page.getByRole("button", { name: clear, exact: true }).first().click();
    await expect(page.getByRole("button", { name: "Filter tasks by member" })).toHaveText("Members");
    const params = new URL(page.url()).searchParams;
    expect(params.has("mine")).toBe(false);
    expect(params.has("author")).toBe(false);
    expect(params.get("sort")).toBe("model_score_desc");
    expect(params.get("delivery")).toBe("existing");
    expect(params.get("run_coverage")).toBe(clear === "Clear filters" ? null : "model_trials");
  });
}

test("clearing other filters preserves explicitly unpinned Everyone", async ({ page }) => {
  await page.goto("/picker?mine=off&sort=model_score_desc&run_coverage=model_trials");
  await page.getByRole("button", { name: "Clear filters", exact: true }).first().click();
  await expect(page.getByRole("button", { name: "Filter tasks by member" })).toHaveText("Members");
  expect(new URL(page.url()).searchParams.get("mine")).toBe("off");
  expect(new URL(page.url()).searchParams.get("sort")).toBe("model_score_desc");
});

for (const [filter, option, key, value] of [
  ["QA outcome", "Accepted", "qa_outcomes", "accepted"],
  ["Category", "coding", "categories", "coding"],
  ["Agent · Model", "test-model", "agent_models", "test-agent:test-model"],
]) {
  test(`nested ${filter} accepts pointer clicks above its parent`, async ({ page }) => {
    if (filter === "QA outcome") await page.getByRole("button", { name: "Review: Any", exact: true }).click();
    else {
      await page.getByRole("button", { name: /^More criteria/ }).click();
      await page.getByRole("textbox", { name: "Find a filter" }).fill(filter);
    }
    await page
      .getByRole("group", { name: filter, exact: true })
      .getByRole("button", { name: "Any", exact: true })
      .click();
    await page.getByRole("checkbox", { name: option, exact: true }).click();
    await expect.poll(() => new URL(page.url()).searchParams.get(key)).toBe(value);
  });
}

test("advanced conditions accept pointer clicks above the filters panel", async ({ page }) => {
  await page.getByRole("button", { name: /^More criteria/ }).click();
  await page.getByRole("textbox", { name: "Find a filter" }).fill("Match any");
  await page.getByRole("button", { name: "Add condition", exact: true }).click();
  await page.getByRole("menuitem", { name: "Trial count", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Task filters" }).getByText("Trial count", { exact: true })).toBeVisible();
});

test("empty customer list supports creation without losing delivery name or picks", async ({ page }) => {
  let customerCreated = false;
  await page.route("**/api/customers", async (route) => {
    if (route.request().method() === "POST") {
      expect(route.request().postDataJSON()).toEqual({ name: "New Lab" });
      customerCreated = true;
      return route.fulfill({ json: { id: "new-lab", name: "New Lab" } });
    }
    await route.fulfill({ json: [] });
  });
  let submitted: unknown;
  await page.route("**/api/deliveries", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    submitted = route.request().postDataJSON();
    await route.fulfill({ status: 400, json: { detail: "Keep dialog open for inspection" } });
  });
  await page.getByRole("button", { name: "Add 1,201 to delivery" }).click();
  await page.getByRole("button", { name: "New delivery…" }).click();
  await page.getByLabel("Name", { exact: true }).fill("Preserved batch");
  await expect(page.getByText("No customers yet.")).toBeVisible();
  await page.getByRole("combobox", { name: "Customer", exact: true }).click();
  await page.getByRole("option", { name: "New customer…", exact: true }).click();
  const customerDialog = page.getByRole("dialog", { name: "New customer", exact: true });
  await customerDialog.getByLabel("Name", { exact: true }).fill("New Lab");
  await customerDialog.getByRole("button", { name: "Create", exact: true }).click();
  await expect(customerDialog).toBeHidden();
  expect(customerCreated).toBe(true);
  await expect(page.getByLabel("Name", { exact: true })).toHaveValue("Preserved batch");
  await expect(page.getByRole("combobox", { name: "Customer", exact: true })).toHaveText("New Lab");
  await page.getByRole("button", { name: "Create and add 1,201" }).click();
  await expect(page.getByRole("alert")).toHaveText("Keep dialog open for inspection");
  expect(submitted).toEqual({ name: "Preserved batch", customer: "new-lab", task_ids: ids });
});

test("customer loading and failure offer a working retry", async ({ page }) => {
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => { release = resolve; });
  let fail = true;
  await page.route("**/api/customers", async (route) => {
    await held;
    await route.fulfill(fail ? { status: 500, json: { detail: "Unavailable" } } : { json: [{ id: "lab", name: "Lab" }] });
  });
  await page.getByRole("button", { name: "Add 1,201 to delivery" }).click();
  await page.getByRole("button", { name: "New delivery…" }).click();
  await expect(
    page.getByRole("combobox", { name: "Customer", exact: true })
  ).toHaveText("Loading customers…");
  release();
  await expect(page.getByRole("alert")).toContainText("Could not load customers.");
  fail = false;
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(page.getByRole("alert")).toBeHidden();
  await page.getByRole("combobox", { name: "Customer", exact: true }).click();
  await page.getByRole("option", { name: "Lab", exact: true }).click();
  await expect(page.getByRole("combobox", { name: "Customer", exact: true })).toHaveText("Lab");
});


test("tags load only when their filter is revealed and reuse the result on reopen", async ({ page }) => {
  let tagRequests = 0;
  await page.route("**/api/tags", async (route) => {
    tagRequests++;
    await route.fulfill({ json: { items: [] } });
  });
  await page.getByRole("button", { name: /^More criteria/ }).click();
  await expect(page.getByRole("textbox", { name: "Find a filter" })).toBeVisible();
  expect(tagRequests).toBe(0);
  await page.getByRole("textbox", { name: "Find a filter" }).fill("Tags");
  await expect.poll(() => tagRequests).toBe(1);
  await expect(page.getByRole("button", { name: "Has all", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: /^More criteria/ }).click();
  await expect(page.getByRole("button", { name: "Has all", exact: true })).toBeVisible();
  expect(tagRequests).toBe(1);
});

for (const searchParam of [null, "q", "query"]) {
  for (const source of ["toolbar", "empty results"]) {
    test(`${source} clear cancels pending search with ${searchParam ?? "empty"} URL search`, async ({ page }) => {
      const params = new URLSearchParams({
        delivery: "existing",
        qa_outcomes: "rejected",
        sort: "total_trials_desc",
        mine: "off",
      });
      if (searchParam) params.set(searchParam, "old search");
      // Install before navigation so the initial search debounce uses the same
      // clock as later edits and clearTimeout. Installing after mount leaves a
      // real timer alive that can restore the old search after Clear filters.
      await page.clock.install({ time: new Date("2026-09-22T12:00:00Z") });
      await page.clock.pauseAt(new Date("2026-09-22T12:01:00Z"));
      await page.goto(`/picker?${params}`);
      await expect(page.getByRole("heading", { name: "Add tasks to Existing" })).toBeVisible();
      await expect(page.getByText("No tasks match the current filters.")).toBeVisible();
      const search = page.getByRole("textbox", { name: "Search tasks", exact: true });
      await search.fill("pending search");
      const clears = page.getByRole("button", { name: "Clear filters", exact: true });
      await clears.nth(source === "toolbar" ? 0 : 1).click();
      await expect(search).toHaveValue("");
      // The input updates before Playwright necessarily observes the history
      // change. Wait for the clear, then advance beyond the search debounce.
      await expect(page).toHaveURL(url =>
        !url.searchParams.has("q") &&
        !url.searchParams.has("query") &&
        !url.searchParams.has("qa_outcomes")
      );
      await page.clock.runFor(1000);
      await expect(search).toHaveValue("");
      const cleared = new URL(page.url()).searchParams;
      expect(cleared.has("q")).toBe(false);
      expect(cleared.has("query")).toBe(false);
      expect(cleared.has("qa_outcomes")).toBe(false);
      expect(cleared.get("delivery")).toBe("existing");
      expect(cleared.get("sort")).toBe("total_trials_desc");
      expect(cleared.get("mine")).toBe("off");
      // A reset cancels the old edit, but must not disable subsequent searches.
      await search.fill("new search");
      await page.clock.runFor(300);
      await expect.poll(() => new URL(page.url()).searchParams.get("q")).toBe("new search");
    });
  }
}


for (const destination of ["", "?delivery=existing"]) {
  test(`lab filters are visible and recorded destinations appear in ${destination ? "delivery picking" : "task cards"}`, async ({ page }) => {
    await page.route("**/api/tasks/browse?*", (route) => route.fulfill({
      json: { items: [{ ...task, deliveries: [
        { customer: "Mapped Lab", batch: "September batch", date: "2026-09-01", source: "history" },
        { customer: "Legacy Lab", batch: "August batch", date: "2026-08-01", source: "history" },
      ] }], total: 1, has_more: false },
    }));
    await page.goto(`/picker${destination}`);
    await expect(
      page.locator("summary").filter({ hasText: "Mapped Lab" })
    ).toBeVisible();
    await page.locator("summary").filter({ hasText: "Mapped Lab" }).click();
    await expect(page.getByText(/August batch · 2026-08-01/)).toBeVisible();
    await page
      .getByRole("combobox", { name: "Lab", exact: true })
      .selectOption("Mapped Lab");
    expect(new URL(page.url()).searchParams.get("delivered_to")).toBe("Mapped Lab");
    expect(new URL(page.url()).searchParams.has("not_delivered_to")).toBe(
      false
    );
    await page
      .getByRole("combobox", { name: "Delivery status" })
      .selectOption("delivered");
    await expect
      .poll(() => new URL(page.url()).searchParams.get("delivered_to"))
      .toBe("Mapped Lab");
    await page
      .getByRole("combobox", { name: "Delivery status" })
      .selectOption("none");
    await expect
      .poll(() => new URL(page.url()).searchParams.get("not_delivered_to"))
      .toBe("Mapped Lab");
    expect(new URL(page.url()).searchParams.has("delivered_to")).toBe(false);
  });
}

for (const view of ["table", "cards"]) {
  for (const filter of ["lab=Mapped+Lab", "lab=Mapped+Lab&not_delivered_to=Mapped+Lab", "not_delivered_to=Mapped+Lab"]) {
    test(`${view} preserves other labs' recorded, finalized, and active history with ${filter}`, async ({ page }) => {
      await page.route("**/api/tasks/browse?*", route => route.fulfill({ json: {
        items: [{ ...task,
          deliveries: [
            { customer: "Legacy Lab", batch: "Imported batch", date: "2026-08-01", source: "history" },
            { customer: "Legacy Lab", batch: "Final batch", date: "2026-09-01", source: "delivery", status: "finalized", delivery_id: "final-batch" },
          ],
          active_deliveries: [{ customer: "Legacy Lab", batch: "Active batch", source: "delivery", status: "active", delivery_id: "active-batch" }],
        }], offset: 0, has_more: false,
      } }));
      await page.goto(`/picker?view=${view}&${filter}`);
      const history = page.locator("summary").filter({ hasText: "Legacy Lab" });
      await expect(history).toContainText("Legacy Lab · Recorded");
      await expect(history).toContainText("Legacy Lab · Finalized");
      await expect(history).toContainText("In active batch · Legacy Lab");
      await expect(page.getByText("No delivery recorded", { exact: true })).toHaveCount(0);
      await history.click();
      await expect(page.getByText("Legacy Lab · Imported batch · 2026-08-01", { exact: true })).toBeVisible();
      await expect(page.getByRole("link", { name: "Final batch", exact: true })).toHaveAttribute("href", "/deliveries/final-batch");
      await expect(page.getByRole("link", { name: "Active batch", exact: true })).toHaveAttribute("href", "/deliveries/active-batch");
    });
  }
}

test("QA search suggestion cancels pending text and keeps step filters", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-09-22T12:00:00Z") });
  await page.clock.pauseAt(new Date("2026-09-22T12:01:00Z"));
  await page.goto("/picker?steps_p50_min=100");
  const search = page.getByRole("textbox", { name: "Search tasks", exact: true });
  await search.fill("needs work");
  await page.getByRole("button", { name: "Filter QA: Needs work" }).click();
  await expect(search).toHaveValue("");
  await expect
    .poll(() => new URL(page.url()).searchParams.get("qa_outcomes"))
    .toBe("rejected");
  await page.clock.runFor(1000);
  expect(new URL(page.url()).searchParams.has("q")).toBe(false);
  expect(new URL(page.url()).searchParams.get("steps_p50_min")).toBe("100");
});

test("exact selection sharing stores IDs independently of current filters", async ({ page }) => {
  let saved: Record<string, unknown> | undefined;
  await page.route("**/api/tag-filters", async route => {
    saved = route.request().postDataJSON();
    await route.fulfill({ json: { ...saved, id: "saved-selection" } });
  });
  await page
    .getByRole("button", { name: "Share selection", exact: true })
    .click();
  await page
    .getByRole("textbox", { name: "Selection name" })
    .fill("Long horizon review");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Shared selection link" })).toHaveValue(/\?selection_id=saved-selection$/);
  expect(saved).toEqual({ name: "Long horizon review", visibility: "ORG", filter_ast: { v: 2, task_ids: ids } });
  await expect(page.getByText("1,201 selected", { exact: true })).toBeVisible();
});

test("table exposes steps and existing batch members without extra selection", async ({ page }) => {
  await page.route("**/api/tasks/browse/count?*", route => route.fulfill({ json: { total: 1 } }));
  await page.route("**/api/tasks/browse?*", route => route.fulfill({ json: { items: [{ ...task, steps_p50: 184, steps_present: 12, active_deliveries: [{ delivery_id: "existing", customer: "Lab", batch: "Existing", status: "active", source: "delivery" }] }], offset: 0, has_more: false } }));
  await page.goto("/picker?delivery=existing&lab=Lab&view=table&columns=QA+outcome,Median+steps,Trials,Delivery+history");
  await expect(
    page.getByRole("columnheader", { name: "Trials", exact: true })
  ).toBeVisible();
  await expect(page.getByText("184", { exact: true })).toBeVisible();
  await expect(page.getByText("12 measured trials", { exact: true })).toBeVisible();
  await expect(page.getByRole("checkbox", { name: "Select Queue recovery", exact: true })).toBeDisabled();
  await expect(page.getByText("In this delivery", { exact: true })).toBeVisible();
  await page.screenshot({ path: "/tmp/oddish-task-browser.png", fullPage: true });
  await page.getByRole("combobox", { name: "Task view" }).selectOption("cards");
  await expect(
    page.getByRole("checkbox", { name: "Select Queue recovery", exact: true })
  ).toBeDisabled();
});

for (const view of ["table", "cards"]) {
  test(`${view} keeps select-all available when the current page contains only delivery members`, async ({ page }) => {
    await page.route("**/api/tasks/browse/count?*", route =>
      route.fulfill({ json: { total: 25 } })
    );
    await page.route("**/api/tasks/browse?*", route =>
      route.fulfill({ json: {
        items: [{ ...task, active_deliveries: [{
          delivery_id: "existing", customer: "Lab", batch: "Existing",
          status: "active", source: "delivery",
        }] }],
        offset: 0, has_more: true,
      } })
    );
    const selectionRequests: URL[] = [];
    await page.route("**/api/tasks/browse/ids?*", route => {
      selectionRequests.push(new URL(route.request().url()));
      return route.fulfill({ json: { ids: ["later-page-task"], truncated: false } });
    });
    let submitted: string[] = [];
    await page.route("**/api/deliveries/existing/tasks", route => {
      submitted = route.request().postDataJSON().task_ids;
      return route.fulfill({ json: { added: 1 } });
    });
    await page.goto(`/picker?delivery=existing&view=${view}&steps_p50_min=100`);
    await expect(page.getByRole("checkbox", { name: "Select Queue recovery", exact: true })).toBeDisabled();
    await expect(page.getByRole("checkbox", { name: "Select this page", exact: true })).toBeDisabled();
    const selectAll = page.getByRole("button", { name: "Select all 25 matching tasks", exact: true });
    await expect(selectAll).toBeEnabled();
    expect(selectionRequests).toHaveLength(0);
    await selectAll.click();
    await expect(page.getByText("1 selected", { exact: true })).toBeVisible();
    expect(selectionRequests).toHaveLength(1);
    expect(selectionRequests[0].searchParams.get("delivery")).toBe("existing");
    expect(selectionRequests[0].searchParams.get("steps_p50_min")).toBe("100");
    expect(selectionRequests[0].searchParams.has("offset")).toBe(false);
    await page.getByRole("button", { name: "Add 1 to Existing", exact: true }).click();
    await expect(page).toHaveURL(/\/deliveries\/existing$/);
    expect(submitted).toEqual(["later-page-task"]);
  });
}

test("presentation changes preserve the page without refetching", async ({ page }) => {
  let requests = 0;
  await page.route("**/api/tasks/browse?*", route => {
    requests++;
    return route.fulfill({ json: { items: [task], offset: 24, has_more: false } });
  });
  await page.goto("/picker?offset=24&view=table");
  await expect(page.getByRole("columnheader", { name: "Trials", exact: true })).toBeVisible();
  const before = requests;
  await page.locator("summary").filter({ hasText: /^Columns$/ }).click();
  await page.getByRole("checkbox", { name: "Cost", exact: true }).check();
  await expect(page.getByRole("columnheader", { name: "Cost", exact: true })).toBeVisible();
  await page.getByRole("combobox", { name: "Task view" }).selectOption("cards");
  await expect(page.getByText("Model score: 97%", { exact: true })).toBeVisible();
  expect(new URL(page.url()).searchParams.get("offset")).toBe("24");
  expect(requests).toBe(before);
});

test("copy view includes pending search and omits the delivery destination", async ({ page }) => {
  await page.clock.install({ time: new Date("2026-09-22T12:00:00Z") });
  await page.clock.pauseAt(new Date("2026-09-22T12:01:00Z"));
  await page.goto("/picker?delivery=existing&lab=Mapped+Lab");
  await page.evaluate(() => {
    Object.defineProperty(navigator, "clipboard", { value: { writeText: async (text: string) => { Object.assign(window, { copiedView: text }); } } });
  });
  await page.getByRole("textbox", { name: "Search tasks", exact: true }).fill("pending terms");
  await page.getByRole("button", { name: "Copy view link", exact: true }).click();
  const copied = new URL(await page.evaluate(() => (window as unknown as { copiedView: string }).copiedView));
  expect(copied.searchParams.get("q")).toBe("pending terms");
  expect(copied.searchParams.get("lab")).toBe("Mapped Lab");
  expect(copied.searchParams.has("delivery")).toBe(false);
});

for (const width of [1280, 375]) {
  test(`delivery dialog contains long batch names and actions at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 });
    const batchName = "September long-horizon evaluation delivery with extended customer requirements and replacement tasks";
    await page.route("**/api/deliveries", route => route.fulfill({ json: [
      { id: "long-batch", name: batchName, customer_name: "Long customer name", status: "active", task_count: 1200 },
    ] }));
    const submissions: unknown[] = [];
    await page.route("**/api/deliveries/long-batch/tasks", route => {
      submissions.push(route.request().postDataJSON());
      return route.fulfill({ json: { added: ids.length } });
    });
    await page.getByRole("button", { name: "Add 1,201 to delivery", exact: true }).click();
    const dialog = page.getByRole("dialog");
    const selector = dialog.getByRole("combobox", { name: "Delivery batch", exact: true });
    const add = dialog.getByRole("button", { name: "Add 1,201 tasks", exact: true });
    await expect(add).toBeDisabled();
    await selector.click();
    const option = page.getByRole("option", { name: `${batchName} · Long customer name · 1200 tasks`, exact: true });
    await expect(option).toBeVisible();
    const menu = await page.getByRole("listbox").boundingBox();
    expect(menu!.x).toBeGreaterThanOrEqual(0);
    expect(menu!.x + menu!.width).toBeLessThanOrEqual(width);
    await option.click();
    await expect(selector).toContainText(batchName);
    expect(submissions).toEqual([]);
    const bounds = await dialog.boundingBox();
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
    for (const control of [selector, add, dialog.getByRole("button", { name: "New delivery…", exact: true })]) {
      const box = await control.boundingBox();
      expect(box!.x).toBeGreaterThanOrEqual(bounds!.x);
      expect(box!.x + box!.width).toBeLessThanOrEqual(bounds!.x + bounds!.width);
      expect(box!.y + box!.height).toBeLessThanOrEqual(bounds!.y + bounds!.height);
    }
    expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    await page.screenshot({ path: `/tmp/oddish-delivery-dialog-${width}.png` });
    await add.click();
    await expect(dialog).toBeHidden();
    expect(submissions).toEqual([{ task_ids: ids }]);
  });
}



for (const columnsQuery of ["", "&columns="]) {
  test(`rapid column toggles preserve each other from ${columnsQuery ? "empty" : "default"} columns`, async ({ page }) => {
    let requests = 0;
    await page.route("**/api/tasks/browse?*", route => {
      requests++;
      return route.fulfill({ json: { items: [task], offset: 24, has_more: false } });
    });
    await page.goto(`/picker?view=table&offset=24&steps_p50_min=100${columnsQuery}`);
    await expect(page.getByRole("link", { name: task.name, exact: true })).toBeVisible();
    await page.locator("summary").filter({ hasText: /^Columns$/ }).click();
    const before = requests;
    const initialColumns = columnsQuery ? [] : ["QA outcome", "Trials", "Delivery history"];
    // Both DOM clicks run in the same browser task, before React can commit
    // the URL-driven render between them.
    for (const checked of [true, false]) {
      const committed = await page.evaluate(() => {
        for (const name of ["Cost", "Tags"]) {
          const label = Array.from(document.querySelectorAll("label"))
            .find(element => element.textContent?.trim() === name)!;
          label.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
        }
        return new URL(location.href).searchParams.get("columns");
      });
      const expected = checked ? [...initialColumns, "Cost", "Tags"] : initialColumns;
      expect(committed).toBe(expected.join(","));
      for (const name of ["Cost", "Tags"]) {
        await expect(page.getByRole("checkbox", { name, exact: true })).toBeChecked({ checked });
        await expect(page.getByRole("columnheader", { name, exact: true })).toHaveCount(checked ? 1 : 0);
      }
      expect(new URL(page.url()).searchParams.get("offset")).toBe("24");
      expect(new URL(page.url()).searchParams.get("steps_p50_min")).toBe("100");
      expect(requests).toBe(before);
    }
  });
}

test("URL restores expanded evidence and criteria without changing the task query", async ({ page }) => {
  await page.route("**/api/tasks/browse/count?*", route => route.fulfill({json: {total: 25}}));
  let requests = 0;
  await page.route("**/api/tasks/browse?*", route => {
    requests++;
    const params = new URL(route.request().url()).searchParams;
    for (const key of ["expanded", "filters", "filter_groups", "filter_search"]) expect(params.has(key)).toBe(false);
    return route.fulfill({ json: { items: [task], offset: 24, has_more: false } });
  });
  await page.goto("/picker?view=table&offset=24&expanded=visible-task");
  await expect(page.getByRole("button", {name: "Trial details"})).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByRole("link", {name: "View all results"})).toBeVisible();
  await page.reload();
  await expect(page.getByRole("button", {name: "Trial details"})).toHaveAttribute("aria-expanded", "true");
  const before = requests;
  await page.getByRole("button", {name: /^More criteria/}).click();
  await page.getByRole("textbox", {name: "Find a filter"}).fill("Median steps");
  await expect.poll(() => new URL(page.url()).searchParams.get("filter_search")).toBe("Median steps");
  expect(new URL(page.url()).searchParams.get("filters")).toBe("1");
  expect(new URL(page.url()).searchParams.get("offset")).toBe("24");
  expect(requests).toBe(before);
  await page.reload();
  await expect(page.getByRole("textbox", {name: "Find a filter"})).toHaveValue("Median steps");
  await expect(page.getByRole("combobox", {name: "Median steps", exact: true})).toBeVisible();
  await page.screenshot({path: "task-url-navigation.png", fullPage: true, animations: "disabled"});
  await page.keyboard.press("Escape");
  await page.evaluate(() => {
    Object.defineProperty(navigator, "clipboard", {value: {writeText: async (text: string) => Object.assign(window, {copiedView: text})}});
  });
  await page.getByRole("button", {name: "Copy view link", exact: true}).click();
  const copied = new URL(await page.evaluate(() => (window as unknown as {copiedView: string}).copiedView));
  expect(copied.searchParams.get("expanded")).toBe("visible-task");
  expect(copied.searchParams.get("offset")).toBe("24");
  await page.goto("/picker?expanded=off-page-task");
  await expect(page.getByText("The linked task is outside this page or filter.", {exact: false})).toBeVisible();
  await expect(page.getByRole("link", {name: "Open task", exact: true})).toHaveAttribute("href", "/tasks/off-page-task");
  await page.goto("/picker?expanded=visible-task");
  await expect(page.getByRole("combobox", {name: "Task view"})).toHaveValue("table");
  await page.getByRole("button", {name: "Trial details"}).click();
  await expect(page.getByRole("combobox", {name: "Task view"})).toHaveValue("table");
  expect(new URL(page.url()).searchParams.has("expanded")).toBe(false);
  await page.goto("/picker?filters=1&filter_groups=Trial");
  await expect(page.getByRole("button", {name: "Trial filters", exact: true})).toHaveAttribute("aria-expanded", "true");
});

test("criteria search preserves caret edits and follows URL navigation", async ({ page }) => {
  await page.goto("/picker?filters=1&offset=24");
  const input = page.getByRole("textbox", { name: "Find a filter" });
  await input.pressSequentially("steps");
  await expect(input).toHaveValue("steps");
  await input.press("Home");
  await input.pressSequentially("Median ", { delay: 20 });
  await expect(input).toHaveValue("Median steps");
  await expect.poll(() => new URL(page.url()).searchParams.get("filter_search")).toBe("Median steps");
  expect(new URL(page.url()).searchParams.get("offset")).toBe("24");
  await expect(page.getByRole("combobox", { name: "Median steps", exact: true })).toBeVisible();
  await input.press("Backspace");
  await expect(input).toHaveValue("Mediansteps");
  await input.press(" ");
  await expect(input).toHaveValue("Median steps");
  await page.evaluate(() => {
    const url = new URL(window.location.href);
    url.searchParams.set("filter_search", "Tags");
    window.history.pushState(null, "", url);
  });
  await expect(input).toHaveValue("Tags");
  await page.goBack();
  await expect(input).toHaveValue("Median steps");
  await page.goForward();
  await expect(input).toHaveValue("Tags");
  await input.fill("");
  await expect.poll(() => new URL(page.url()).searchParams.has("filter_search")).toBe(false);
  await page.reload();
  await expect(input).toHaveValue("");
});


test("delivery picker searches members and restores selection on reload", async ({ page }) => {
  await page.route("**/api/people/search?*", route => route.fulfill({ json: {
    items: [{ id: "user-alice", email: "alice@example.com", github_username: "alice-dev" }],
  } }));
  await page.goto("/picker?delivery=existing&lab=Lab&mine=only&offset=24");
  const members = page.getByRole("button", { name: "Filter tasks by member" });
  await members.click();
  await page.getByRole("textbox", { name: "Search members" }).fill("@alice-dev");
  await page.getByRole("button", { name: "alice@example.com @alice-dev" }).click();
  await expect(members).toContainText("alice@example.com");
  let params = new URL(page.url()).searchParams;
  expect(params.get("author")).toBe("user-alice");
  expect(params.get("mine")).toBe("off");
  expect(params.has("offset")).toBe(false);
  expect(params.get("delivery")).toBe("existing");
  expect(params.get("lab")).toBe("Lab");
  await expect.poll(() => page.evaluate(() => performance.getEntriesByType("resource").some(entry => {
    const url = new URL(entry.name);
    return url.pathname === "/api/tasks/browse" && url.searchParams.get("author") === "user-alice";
  }))).toBe(true);
  await page.reload();
  await expect(members).toContainText("alice@example.com");
  await expect(page.getByRole("button", { name: "Author: alice@example.com ×" })).toBeVisible();
  await page.getByRole("button", { name: "Clear member filter" }).click();
  params = new URL(page.url()).searchParams;
  expect(params.has("author")).toBe(false);
  expect(params.get("delivery")).toBe("existing");
});
