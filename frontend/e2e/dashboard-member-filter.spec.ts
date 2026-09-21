import { expect, test } from "@playwright/test";

test("member picker shows emails and selects a stable account ID", async ({
  page,
}) => {
  const people = [
    {
      id: "email_only",
      email: "alpha@example.com",
      display_name: "alpha@example.com",
      github_username: null,
    },
    {
      id: "user_kyle",
      email: "kyle@example.com",
      display_name: "kyle@example.com",
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
  await page.route("**/api/people/search?*", (route) => {
    const query = new URL(route.request().url()).searchParams.get("q") ?? "";
    return route.fulfill({
      json: {
        items: people.filter(
          (person) => person.id === query || person.email.includes(query)
        ),
      },
    });
  });

  await page.goto("/dashboard");
  const picker = page.getByRole("combobox", {
    name: "Filter experiments by member",
  });
  await picker.click();
  await expect(page.getByRole("option")).toHaveText([
    "alpha@example.com",
    "kyle@example.com@kyle",
  ]);
  await page.getByPlaceholder("Search members…").fill("alpha@example.com");
  await expect(page.getByRole("option")).toHaveCount(1);
  await page.getByRole("option", { name: "alpha@example.com" }).click();
  await expect(page).toHaveURL(/author=email_only/);
  await expect(picker).toContainText("alpha@example.com");

  // A saved URL must also resolve its selected account ID to an email label.
  await page.reload();
  await expect(picker).toContainText("alpha@example.com");
  await picker.click();
  await page.getByPlaceholder("Search members…").fill("kyle@example.com");
  await page.getByRole("option", { name: /kyle@example.com/ }).click();
  await expect(page).toHaveURL(/author=user_kyle/);
  await expect(picker).toContainText("kyle@example.com");
});
