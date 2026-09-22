import { test, expect } from "@playwright/test";

const members = Array.from({ length: 61 }, (_, i) => ({
  id: `user_${i}`,
  email: `member${String(i).padStart(2, "0")}@example.com`,
  github_username: i === 60 ? "last-member" : null,
}));

test("load once, browse and search locally, select and clear without automatic selection", async ({
  page,
}) => {
  const requests: string[] = [];
  await page.route("**/api/people/search?*", async (route) => {
    requests.push(route.request().url());
    expect(new URL(route.request().url()).searchParams.get("all_members")).toBe(
      "true"
    );
    await route.fulfill({ json: { items: members, next_offset: null } });
  });
  await page.goto("/members");
  const trigger = page.getByRole("button", {
    name: "Filter experiments by member",
  });
  await trigger.click();
  const results = page.getByLabel("Member results");
  await expect(results.getByRole("button")).toHaveCount(61);
  await expect(results.getByRole("button", { pressed: true })).toHaveCount(0);
  await expect(page.locator("output")).toBeEmpty();
  const search = page.getByRole("textbox", { name: "Search members" });
  await expect(search).toBeFocused();
  await search.press("Enter");
  await expect(page.locator("output")).toBeEmpty();
  await results.hover();
  await page.mouse.wheel(0, 3000);
  await expect(
    results.getByRole("button", { name: /member60/ })
  ).toBeInViewport();
  await search.fill("MEMBER60");
  await expect(results.getByRole("button")).toHaveCount(1);
  await search.fill("@last-member");
  await expect(results.getByRole("button")).toHaveCount(1);
  await search.press("Tab");
  await page.keyboard.press("Enter");
  await expect(page.locator("output")).toHaveText("user_60");
  await expect(trigger).toContainText("member60@example.com");
  await page.getByRole("button", { name: "Clear member filter" }).click();
  await expect(page.locator("output")).toBeEmpty();
  await expect(trigger).toContainText("Members");
  await trigger.click();
  await expect(results.getByRole("button")).toHaveCount(61);
  await expect(results.getByRole("button", { pressed: true })).toHaveCount(0);
  await results.getByRole("button", { name: /member60/ }).click();
  await trigger.click();
  await results.getByRole("button", { name: /member60/ }).click();
  await expect(page.locator("output")).toBeEmpty();
  expect(requests).toHaveLength(1);
});

test("retry a failed list request", async ({ page }) => {
  let count = 0;
  await page.route("**/api/people/search?*", async (route) => {
    count++;
    await route.fulfill(
      count === 1
        ? { status: 503, body: "unavailable" }
        : { json: { items: members } }
    );
  });
  await page.goto("/members");
  await page
    .getByRole("button", { name: "Filter experiments by member" })
    .click();
  await expect(
    page.getByLabel("Member results").getByRole("alert")
  ).toContainText("Could not load members");
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(
    page.getByLabel("Member results").getByRole("button")
  ).toHaveCount(61);
});
