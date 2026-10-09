import { expect as baseExpect, test, type Page } from "@playwright/test";

// Empty-dashboard setup: create a key, save it, install, hand off to an agent.
const expect = baseExpect.configure({ timeout: 15_000 });
const KEY = "ok_test1234567890abcdef";

async function keyApi(page: Page, existing = false) {
  const state = {
    created: 0,
    used: false,
    body: null as unknown,
    mine: existing ? ["old-key"] : ([] as string[]),
  };
  await page.route("**/api/settings/api-keys**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/permissions"))
      return route.fulfill({
        json: {
          can_create: true,
          can_manage: true,
          allowed_scopes: ["full", "tasks", "read"],
        },
      });
    if (request.method() === "POST") {
      state.created += 1;
      state.body = request.postDataJSON();
      state.mine.push("key-1");
      return route.fulfill({ json: { id: "key-1", key: KEY, scope: "full" } });
    }
    return route.fulfill({
      json: [
        // A teammate's key never counts as yours.
        {
          id: "other",
          is_active: true,
          mine: false,
          last_used_at: "2026-10-01T00:00:00Z",
          created_at: "2026-10-01T00:00:00Z",
        },
        ...state.mine.map((id) => ({
          id,
          is_active: true,
          mine: true,
          last_used_at: state.used ? "2026-10-09T10:00:00Z" : null,
          created_at: "2026-10-08T12:00:00Z",
        })),
      ],
    });
  });
  return state;
}

test("the first key is made unasked, then the steps follow the CLI", async ({
  page,
  context,
}) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const state = await keyApi(page);
  await page.goto("/onboarding");

  const step1 = page.getByRole("region", { name: "Your API key" });
  await expect(step1).toContainText("We made your first key for you");
  await expect(page.getByText("1 / 4")).toBeVisible();
  // Exactly one key, at the widest scope the user may mint.
  await page.waitForTimeout(500);
  expect(state.created).toBe(1);
  expect(state.body).toMatchObject({ scope: "full" });

  // The key is masked on screen and copied whole.
  await expect(step1).toContainText('export ODDISH_API_KEY="ok_test…cdef"');
  await expect(step1).not.toContainText(KEY);
  await step1.getByRole("button", { name: "Copy command" }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
    `echo 'export ODDISH_API_KEY="${KEY}"' >> ~/.zshrc && source ~/.zshrc`
  );
  await step1.getByRole("radio", { name: "bash" }).click();
  await expect(step1).toContainText("~/.bashrc");

  const step2 = page.getByRole("region", { name: "Install the CLI" });
  await expect(step2).toContainText("/cli/install.sh | sh");
  await expect(step2).toContainText("Waiting");
  state.used = true;
  await expect(step2).toContainText("Connected", { timeout: 15_000 });
  await expect(page.getByText("2 / 4")).toBeVisible();

  const step3 = page.getByRole("region", { name: "Set up your agent" });
  await step3.getByRole("button", { name: "Copy instructions" }).click();
  const instructions = await page.evaluate(() =>
    navigator.clipboard.readText()
  );
  expect(instructions).toContain("ODDISH_API_KEY");
  expect(instructions).not.toContain(KEY);
  await expect(page.getByText("3 / 4")).toBeVisible();
});

test("someone who already has a key gets no new one", async ({ page }) => {
  const state = await keyApi(page, true);
  await page.goto("/onboarding");
  const step1 = page.getByRole("region", { name: "Your API key" });
  await expect(
    step1.getByRole("button", { name: "Make another" })
  ).toBeVisible();
  await page.waitForTimeout(500);
  expect(state.created).toBe(0);
  await expect(step1).not.toContainText("We made your first key");
});

test("loading never claims a key is being made", async ({ page }) => {
  await keyApi(page, true);
  // Slow responses: the step must wait quietly, not announce a new key.
  await page.route("**/api/settings/api-keys**", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    await route.fallback();
  });
  const seen: string[] = [];
  await page.goto("/onboarding");
  for (let i = 0; i < 6; i += 1) {
    seen.push(await page.locator("body").innerText());
    await page.waitForTimeout(250);
  }
  expect(seen.some((text) => text.includes("Making your key"))).toBe(false);
  await expect(
    page.getByRole("button", { name: "Make another" })
  ).toBeVisible();
});

test("a failed copy leaves the agent step unfinished", async ({ page }) => {
  await keyApi(page, true);
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: () => Promise.reject(new Error("denied")) },
    });
  });
  await page.goto("/onboarding");
  await expect(page.getByText("1 / 4")).toBeVisible();
  await page.getByRole("button", { name: "Copy instructions" }).click();
  await page.waitForTimeout(300);
  await expect(page.getByText("1 / 4")).toBeVisible();
});

test("a reload or second tab never makes a second key", async ({ page }) => {
  const state = await keyApi(page);
  // The list keeps saying "no key of yours", as a racing tab would see it.
  await page.route("**/api/settings/api-keys", async (route) => {
    if (route.request().method() === "GET") return route.fulfill({ json: [] });
    return route.fallback();
  });
  await page.goto("/onboarding");
  await expect(page.getByText("We made your first key for you")).toBeVisible();
  await page.reload();
  await page.waitForTimeout(800);
  expect(state.created).toBe(1);
});

test("keys from a server without `mine` count as yours", async ({ page }) => {
  const state = await keyApi(page);
  await page.route("**/api/settings/api-keys", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    return route.fulfill({
      json: [
        {
          id: "legacy",
          is_active: true,
          last_used_at: null,
          created_at: "2026-10-01T00:00:00Z",
        },
      ],
    });
  });
  await page.goto("/onboarding");
  await expect(
    page.getByRole("button", { name: "Make another" })
  ).toBeVisible();
  await page.waitForTimeout(500);
  expect(state.created).toBe(0);
});
