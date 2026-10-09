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
  for (const label of ["Samples", "Tasks"])
    await expect(nav.getByRole("link", { name: label })).toBeVisible();
  await expect(nav.getByRole("link", { name: "Deliveries" })).toHaveCount(0);
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

test("samples list what each one holds", async ({ page }) => {
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
        skipped: 0,
        active: 0,
        reward_sum: 9,
        reward_total: 14,
        pass_count: 8,
        partial_count: 2,
        fail_count: 4,
        harness_error_count: 0,
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
  await expect(page.getByRole("heading", { name: "Samples" })).toBeVisible();
  const row = page.getByRole("link", { name: /gpu sample/ });
  for (const text of ["Tasks2", "Runs14", "Score64%", "1 / 1"])
    await expect(row).toContainText(text);
  await expect(row).toHaveAttribute("href", "/shared/exp-shared");
});

test("sharing confirms and then says every customer org has it", async ({
  page,
}) => {
  await clerk(page, { isSignedIn: true });
  let shares: OrgShare[] = [];
  await page.route("**/api/org-shares**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
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
          created_at: "2026-10-09T12:00:00Z",
        } as OrgShare,
      ];
    return route.fulfill({ json: shares });
  });
  await page.goto("/share-picker");
  await page.getByLabel("Share with orgs").selectOption("org-c");
  await page.getByRole("button", { name: "Share", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText("Shared with Cursive");
  await expect(page.getByText("Shared with every customer org.")).toBeVisible();
  await expect(page.getByText("No orgs left")).toHaveCount(0);
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
