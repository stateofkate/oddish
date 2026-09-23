import { expect, test } from "@playwright/test";

test("member picker filters its loaded list and restores the selected account from the URL", async ({
  page,
}) => {
  const people = [
    {
      id: "email_only",
      email: "alpha@example.com",
      github_username: null,
    },
    {
      id: "user_kyle",
      email: "kyle@example.com",
      github_username: "kyle",
    },
  ];
  await page.route("**/api/**", (route) =>
    route.fulfill({
      json: {
        queues: null,
        model_usage: [],
        job_usage: [],
        leaders: [],
        items: [],
        limit_usd: 100,
        used_usd: 0,
      },
    })
  );
  const memberRequests: string[] = [];
  await page.route("**/api/people/search?*", (route) => {
    const url = new URL(route.request().url());
    memberRequests.push(url.search);
    expect([...url.searchParams.entries()]).toEqual([["all_members", "true"]]);
    return route.fulfill({
      json: { items: people },
    });
  });

  await page.goto("/dashboard");
  const picker = page.getByRole("button", {
    name: "Filter experiments by member",
  });
  await picker.click();
  await expect(
    page.getByLabel("Member results").getByRole("button")
  ).toHaveText(["alpha@example.com", "kyle@example.com@kyle"]);
  await page
    .getByRole("textbox", { name: "Search members" })
    .fill("ALPHA@EXAMPLE.COM");
  await expect(
    page.getByLabel("Member results").getByRole("button")
  ).toHaveCount(1);
  await page
    .getByLabel("Member results")
    .getByRole("button", { name: "alpha@example.com" })
    .click();
  await expect(page).toHaveURL(/author=email_only/);
  await expect(picker).toContainText("alpha@example.com");
  expect(memberRequests).toEqual(["?all_members=true"]);

  // A saved URL must also resolve its selected account ID to an email label.
  await page.reload();
  await expect(picker).toContainText("alpha@example.com");
  await picker.click();
  await expect(
    page.getByLabel("Member results").getByRole("button", { pressed: true })
  ).toHaveText("alpha@example.com");
  await page.getByRole("textbox", { name: "Search members" }).fill("@KYLE");
  await expect(
    page.getByLabel("Member results").getByRole("button")
  ).toHaveCount(1);
  await page
    .getByLabel("Member results")
    .getByRole("button", { name: /kyle@example.com/ })
    .click();
  await expect(page).toHaveURL(/author=user_kyle/);
  await expect(picker).toContainText("kyle@example.com");
  await page.getByRole("button", { name: "Clear member filter" }).click();
  await expect(page).toHaveURL(/author=all/);
  await expect(picker).toContainText("Members");
  await page.goBack();
  await expect(page).toHaveURL(/author=user_kyle/);
  await expect(picker).toContainText("kyle@example.com");
  await page.goForward();
  await expect(page).toHaveURL(/author=all/);
  await expect(picker).toContainText("Members");
  expect(memberRequests).toEqual(["?all_members=true", "?all_members=true"]);
});
