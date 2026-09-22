import { test, expect } from "@playwright/test";

test("browse all pages, reset search, and retry a failed next page", async ({
  page,
}) => {
  const members = Array.from({ length: 61 }, (_, i) => ({
    id: `user_${i}`,
    email: `member${String(i).padStart(2, "0")}@example.com`,
    display_name: `member${i}`,
    github_username: null,
  }));
  const requests: string[] = [];
  let failNextPage = true;
  await page.route("**/api/people/search?*", async (route) => {
    const params = new URL(route.request().url()).searchParams;
    const query = params.get("q") ?? "";
    const offset = Number(params.get("offset") ?? 0);
    const limit = Number(params.get("limit"));
    requests.push(`${query}:${offset}`);
    if (!query && offset === 25 && failNextPage) {
      failNextPage = false;
      await route.fulfill({ status: 503, body: "unavailable" });
      return;
    }
    const matches = members.filter(
      (m) => m.email.includes(query) || m.id === query
    );
    await route.fulfill({
      json: {
        items: matches.slice(offset, offset + limit),
        next_offset: offset + limit < matches.length ? offset + limit : null,
      },
    });
  });
  await page.goto("/members");
  await page
    .getByRole("combobox", { name: "Filter experiments by member" })
    .click();
  const options = page.getByRole("option");
  await expect(options).toHaveCount(25);
  const list = page.getByRole("listbox");
  await list.hover();
  await page.mouse.wheel(0, 1500);
  await expect(list.getByRole("alert")).toContainText("Could not load members");
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(options).toHaveCount(50);
  await list.hover();
  await page.mouse.wheel(0, 2500);
  await expect(options).toHaveCount(61);
  await expect(
    page.getByRole("button", { name: "Load more members" })
  ).toHaveCount(0);
  await page.getByPlaceholder("Search members…").fill("member");
  await expect(options).toHaveCount(25);
  await list.hover();
  await page.mouse.wheel(0, 1500);
  await expect(options).toHaveCount(50);
  expect(requests).toContain("member:25");
  await page.getByPlaceholder("Search members…").fill("member60");
  await expect(options).toHaveCount(1);
  expect(requests).toContain("member60:0");
  expect(requests).not.toContain("member60:25");
  await options.click();
  await expect(page.locator("output")).toHaveText("user_60");
});
