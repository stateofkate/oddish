import { expect as baseExpect, test, type Page } from "@playwright/test";

import type { OrgShare, SharedExperiment, SharedTask } from "../src/lib/types";

// Customer orgs (org_type "customer") see only what Abundant shared with them.
// The review config starts the isolated app; no Clerk credentials needed.
const expect = baseExpect.configure({ timeout: 15_000 });

type ClerkFlags = { isSignedIn?: boolean; isLoaded?: boolean };

async function clerk(page: Page, flags: ClerkFlags) {
  await page.addInitScript((value) => {
    (window as unknown as { __fixtureClerk: ClerkFlags }).__fixtureClerk =
      value;
  }, flags);
}

async function api(
  page: Page,
  org: { org_type: string; access: string },
  extra: Record<string, unknown> = {}
) {
  const reads: string[] = [];
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    reads.push(url.pathname + url.search);
    if (url.pathname === "/api/org")
      return route.fulfill({ json: { id: "fixture", name: "Org", ...org } });
    if (url.pathname in extra)
      return route.fulfill({ json: extra[url.pathname] });
    if (url.pathname.startsWith("/api/org-shares"))
      return route.fulfill({ json: [] });
    return route.fulfill({ status: 404, json: { detail: "Not found" } });
  });
  return reads;
}

test("a customer org is sent to /shared and its nav lists only shared views", async ({
  page,
}) => {
  await clerk(page, { isSignedIn: true });
  await api(page, { org_type: "customer", access: "viewer" });
  await page.goto("/leaderboard");
  await expect(page).toHaveURL(/\/shared$/);
  const nav = page.locator("nav[data-page-sticky-header]");
  // Docs leads the nav for every org, view-only ones included.
  await expect(nav.getByRole("link", { name: "Docs" })).toBeVisible();
  for (const label of ["Deliveries", "Tasks"])
    await expect(nav.getByRole("link", { name: label })).toBeVisible();
  // Its Deliveries are what was shared, never Abundant's own /deliveries.
  await expect(nav.getByRole("link", { name: "Deliveries" })).toHaveAttribute(
    "href",
    "/shared"
  );
  await expect(nav.getByRole("link", { name: "Leaderboard" })).toHaveCount(0);
  await expect(nav.getByRole("link", { name: "Dashboard" })).toHaveCount(0);
});

test("an internal org stays on its page with the full nav", async ({
  page,
}) => {
  await clerk(page, { isSignedIn: true });
  const reads = await api(page, { org_type: "internal", access: "execution" });
  await page.goto("/leaderboard");
  await expect
    .poll(() => reads.some((r) => r.startsWith("/api/org?")))
    .toBe(true);
  await expect(page.getByText("Leaderboard fixture")).toBeVisible();
  await expect(page).toHaveURL(/\/leaderboard$/);
  const nav = page.locator("nav[data-page-sticky-header]");
  await expect(nav.getByRole("link", { name: "Leaderboard" })).toBeVisible();
});

test("a customer org that runs jobs never sees Abundant's own pages", async ({
  page,
}) => {
  await clerk(page, { isSignedIn: true });
  await api(page, { org_type: "customer", access: "execution" });
  await page.goto("/leaderboard");
  await expect(page).toHaveURL(/\/shared$/);
  const nav = page.locator("nav[data-page-sticky-header]");
  for (const label of ["Dashboard", "Tasks", "Shared"])
    await expect(nav.getByRole("link", { name: label })).toBeVisible();
  for (const label of ["Deliveries", "Leaderboard"])
    await expect(nav.getByRole("link", { name: label })).toHaveCount(0);
});

test("shared tasks link to the version that shipped", async ({ page }) => {
  await clerk(page, { isSignedIn: true });
  const tasks: SharedTask[] = [
    {
      id: "task-shipped",
      name: "Shipped task",
      version: 3,
      delivery_id: "del-1",
      delivery_name: "Batch 1",
      shared_at: "2026-10-08T12:00:00Z",
    },
  ];
  await api(
    page,
    { org_type: "customer", access: "viewer" },
    { "/api/org-shares/incoming/tasks": tasks }
  );
  await page.goto("/shared/tasks");
  const link = page.getByRole("link", { name: /Shipped task/ });
  await expect(link).toHaveAttribute(
    "href",
    "/shared/tasks/task-shipped?version=3"
  );
  await expect(link).toContainText("Batch 1 · v3");
});

test("tasks shared with an org that runs jobs show on its Tasks page", async ({
  page,
}) => {
  await clerk(page, { isSignedIn: true });
  const tasks: SharedTask[] = [
    {
      id: "task-shipped",
      name: "Shipped task",
      version: 3,
      delivery_id: "del-1",
      delivery_name: "Batch 1",
      shared_at: "2026-10-08T12:00:00Z",
    },
  ];
  await api(
    page,
    { org_type: "customer", access: "execution" },
    { "/api/org-shares/incoming/tasks": tasks }
  );
  await page.goto("/tasks");
  const section = page.getByTestId("shared-tasks");
  await expect(section).toContainText("Shared with you 1");
  await expect(
    section.getByRole("link", { name: /Shipped task/ })
  ).toHaveAttribute("href", "/shared/tasks/task-shipped?version=3");
});

test("the Tasks page shows no shared section when nothing is shared", async ({
  page,
}) => {
  await clerk(page, { isSignedIn: true });
  const reads = await api(page, { org_type: "internal", access: "execution" });
  await page.goto("/tasks");
  await expect
    .poll(() => reads.some((r) => r.startsWith("/api/org-shares/incoming/tasks")))
    .toBe(true);
  await expect(page.getByTestId("shared-tasks")).toHaveCount(0);
});

test("shared deliveries list what each one holds", async ({ page }) => {
  await clerk(page, { isSignedIn: true });
  const samples: SharedExperiment[] = [
    {
      id: "exp-shared",
      name: "gpu sample",
      description: "Kernel tasks",
      shared_at: "2026-10-08T12:00:00Z",
      task_count: 2,
      run_count: 14,
      summary: {
        task_count: 2,
        trial_count: 14,
        completed: 14,
        failed: 0,
        skipped: 3,
        active: 0,
        reward_sum: 9,
        reward_total: 14,
        pass_count: 8,
        partial_count: 2,
        fail_count: 4,
        harness_error_count: 1,
        average_score: 0.64,
        qa_accepted: 1,
        qa_rejected: 1,
        qa_running: 0,
        qa_failed: 0,
      },
    },
  ];
  await api(
    page,
    { org_type: "customer", access: "viewer" },
    { "/api/org-shares/incoming": samples }
  );
  await page.goto("/shared");
  await expect(
    page.getByRole("heading", { name: "Deliveries" })
  ).toBeVisible();
  const row = page.getByRole("link", { name: /gpu sample/ });
  for (const text of ["Tasks2", "Runs14", "Score64%", "1 / 1"])
    await expect(row).toContainText(text);
  // Skipped runs and errors are their own parts of the bar, as on the page.
  await expect(
    row.locator(
      '[title="8 passed · 2 partial · 4 failed · 1 errors · 3 skipped"]'
    )
  ).toHaveCount(1);
  await expect(row).toHaveAttribute("href", "/shared/exp-shared");
});

test("a delivery card leaves QA out when it is not opened to the org", async ({
  page,
}) => {
  await clerk(page, { isSignedIn: true });
  const sample: SharedExperiment = {
    id: "exp-no-qa",
    name: "no qa sample",
    description: null,
    shared_at: "2026-10-08T12:00:00Z",
    task_count: 1,
    run_count: 3,
    show_qa: false,
  };
  await api(
    page,
    { org_type: "customer", access: "viewer" },
    { "/api/org-shares/incoming": [sample] }
  );
  await page.goto("/shared");
  const card = page.getByRole("link", { name: /no qa sample/ });
  await expect(card).toContainText("Runs3");
  await expect(card).not.toContainText("QA");
});

test("sharing confirms and then says every customer org has it", async ({
  page,
}) => {
  await clerk(page, { isSignedIn: true });
  let shares: OrgShare[] = [];
  let patched: unknown = null;
  await page.route("**/api/org-shares**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "PATCH") {
      patched = request.postDataJSON();
      shares = [{ ...shares[0], show_qa: false }];
      return route.fulfill({ json: shares[0] });
    }
    if (url.pathname === "/api/org-shares/orgs")
      return route.fulfill({
        json: [{ id: "org-c", name: "Cursive", slug: "cursive" }],
      });
    if (request.method() === "POST")
      shares = [
        {
          id: "share-1",
          grantee_org_id: "org-c",
          grantee_org_name: "Cursive",
          resource_type: "experiment",
          resource_id: "exp-shared",
          show_costs: false,
          show_qa: true,
          created_at: "2026-10-09T12:00:00Z",
        },
      ];
    return route.fulfill({ json: shares });
  });
  await page.goto("/share-picker");
  await page.getByLabel("Share with orgs").selectOption("org-c");
  await page.getByRole("button", { name: "Share", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText("Shared with Cursive");
  await expect(page.getByText("Shared with every customer org.")).toBeVisible();
  await expect(page.getByText("No orgs left")).toHaveCount(0);
  // A new share shows QA and hides costs; each switch saves on its own.
  const qa = page.getByRole("checkbox", { name: "Show QA" });
  await expect(qa).toBeChecked();
  await expect(page.getByRole("checkbox", { name: "Show costs" })).not.toBeChecked();
  await qa.click();
  await expect.poll(() => patched).toEqual({ show_qa: false });
  await expect(qa).not.toBeChecked();
});

test("with no customer orgs there is nothing to share with", async ({
  page,
}) => {
  await clerk(page, { isSignedIn: true });
  let orgsRead = false;
  await page.route("**/api/org-shares**", async (route) => {
    if (new URL(route.request().url()).pathname === "/api/org-shares/orgs")
      orgsRead = true;
    return route.fulfill({ json: [] });
  });
  await page.goto("/share-picker");
  await expect.poll(() => orgsRead).toBe(true);
  await expect(page.getByText("Share with orgs")).toHaveCount(0);
  await expect(page.getByText("Shared with every customer org.")).toHaveCount(
    0
  );
});

test("a public share link loads even when Clerk never does", async ({
  page,
}) => {
  await clerk(page, { isLoaded: false });
  const token = "share-without-clerk";
  const read = page.waitForRequest((request) =>
    request.url().includes(`/api/public/experiments/${token}`)
  );
  await page.route("**/api/**", (route) =>
    route.fulfill({ status: 404, json: { detail: "Not found" } })
  );
  await page.goto(`/share/${token}`);
  await read;
});

test("docs list the guides each org can use and link between them", async ({
  page,
}) => {
  await clerk(page, { isSignedIn: true });
  await api(page, { org_type: "customer", access: "execution" });
  await page.goto("/docs");
  const side = page.locator("article").locator("..").locator("nav");
  await expect(page.getByRole("heading", { name: "Getting started" })).toBeVisible();
  await expect(side.getByRole("link", { name: "Run a task" })).toBeVisible();
  // Abundant-only features have no guide for a customer org.
  await expect(side.getByRole("link", { name: "Review a delivery" })).toHaveCount(0);
  await page.locator("article").getByRole("link", { name: "Troubleshooting" }).click();
  await expect(page).toHaveURL(/\/docs\/troubleshooting$/);
  await expect(page.getByRole("heading", { name: "Troubleshooting" })).toBeVisible();
});

test("a view-only org's docs are setup, terms and shared data", async ({
  page,
}) => {
  await clerk(page, { isSignedIn: true });
  await api(page, { org_type: "customer", access: "viewer" });
  await page.goto("/docs/shared/use-shared-data");
  await expect(page).toHaveURL(/\/docs\/shared\/use-shared-data$/);
  const side = page.locator("article").locator("..").locator("nav");
  await expect(side.getByRole("link")).toHaveText([
    "Getting started",
    "Terms used in these guides",
    "Use data Abundant shares with you",
  ]);
  await page.goto("/docs/guides/first-run");
  await expect(page.getByText("Page not found.")).toBeVisible();
});
