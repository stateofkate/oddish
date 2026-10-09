import { expect as baseExpect, test, type Page } from "@playwright/test";

import type { SharedDeliveryBoard, SharedTask } from "../src/lib/types";

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
  for (const label of ["Experiments", "Tasks", "Deliveries"])
    await expect(nav.getByRole("link", { name: label })).toBeVisible();
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

test("a shared delivery board is read for the active org", async ({ page }) => {
  await clerk(page, { isSignedIn: true });
  const board: SharedDeliveryBoard = {
    id: "del-1",
    name: "Batch 1",
    finalized_at: "2026-10-08T12:00:00Z",
    task_count: 1,
    tasks: [
      {
        task_id: "task-shipped",
        task_name: "Shipped task",
        version: 3,
        customer_note: "Hard one",
        checks: [{ label: "QA", status: "pass" }],
        findings: ["Typo fixed"],
      },
    ],
  } as SharedDeliveryBoard;
  const reads = await api(
    page,
    { org_type: "customer", access: "viewer" },
    { "/api/org-shares/incoming/deliveries/del-1": board }
  );
  await page.goto("/shared/deliveries/del-1");
  await expect(page.getByText("Typo fixed")).toBeVisible();
  expect(reads).toContain(
    "/api/org-shares/incoming/deliveries/del-1?org=fixture"
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
