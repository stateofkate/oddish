import { test, expect } from "@playwright/test";

const empty = {
  task_version_id: "v2",
  current_user_id: "me",
  can_moderate: false,
  average_score: null,
  rating_count: 0,
  own_rating: null,
  own_rating_revision: 0,
  ratings: [],
  comments: [],
  next_before: null,
  next_before_id: null,
};
const rating = {
  id: "r1",
  reviewer: { id: "me", name: "Mark" },
  score: 4,
  revision: 1,
  updated_at: "2026-10-08T10:00:00Z",
};

test("day zero, rating, comment and version selection stay scoped", async ({
  page,
}, testInfo) => {
  const writes: Array<{ url: string; body: Record<string, unknown> }> = [];
  let saved = false;
  await page.route(
    "**/api/tasks/review-task/versions/*/reviews**",
    async (route) => {
      const req = route.request();
      if (req.method() !== "GET") {
        writes.push({ url: req.url(), body: req.postDataJSON() });
        if (req.method() === "PUT") saved = true;
        return route.fulfill({ json: { id: "saved", revision: 1 } });
      }
      const old = req.url().includes("/v1/");
      return route.fulfill({
        json: {
          ...empty,
          task_version_id: old ? "v1" : "v2",
          ...(saved && !old
            ? {
                own_rating: rating,
                own_rating_revision: 1,
                ratings: [rating],
                average_score: 4,
                rating_count: 1,
              }
            : {}),
        },
      });
    }
  );
  await page.goto("/task-reviews");
  await page.getByRole("button", { name: "Review task" }).click();
  await expect(page.getByText("No comments yet.")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Save rating" })
  ).toBeDisabled();
  await page.getByRole("button", { name: "Rate 4 out of 5" }).click();
  await page.getByRole("button", { name: "Save rating" }).click();
  await expect(page.getByText("Mark (you)")).toBeVisible();
  await expect(page.getByRole("button", { name: "Rate 4 out of 5" })).toBeEnabled();
  await page.screenshot({ path: testInfo.outputPath("desktop.png") });
  expect(writes[0].body).toMatchObject({
    score: 4,
    expected_revision: 0,
  });
  await page
    .getByLabel("Comment", { exact: true })
    .fill("The requirements are ambiguous.");
  await page.getByRole("button", { name: "Post comment" }).click();
  await expect(page.getByLabel("Comment", { exact: true })).toHaveValue("");
  expect(writes[1].body).toMatchObject({
    body: "The requirements are ambiguous.",
  });
  expect(writes[1].body.client_request_id).toMatch(/^[a-f0-9-]{36}$/);
  await page.getByLabel("Review version").selectOption("v1");
  await expect(page.getByText("Mark (you)")).toHaveCount(0);
  await page.getByRole("button", { name: "Rate 2 out of 5" }).click();
  await page.getByRole("button", { name: "Save rating" }).click();
  await expect.poll(() => writes.length).toBe(3);
  expect(writes[2].url).toContain("/versions/v1/reviews/rating");
});

test("others' reviews, older comments, conflict and mobile layout", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const comment = {
    id: "c1",
    reviewer: { id: "other", name: "Jesse" },
    body: "Needs clearer acceptance criteria.",
    source_trial_id: null,
    revision: 1,
    created_at: "2026-10-08T10:00:00Z",
    updated_at: "2026-10-08T10:00:00Z",
  };
  await page.route(
    "**/api/tasks/review-task/versions/*/reviews**",
    async (route) => {
      if (route.request().method() !== "GET")
        return route.fulfill({ status: 409, json: { detail: "stale" } });
      const older = route.request().url().includes("before=");
      return route.fulfill({
        json: {
          ...empty,
          ratings: [{ ...rating, reviewer: comment.reviewer }],
          rating_count: 1,
          average_score: 4,
          comments: [
            {
              ...comment,
              id: older ? "c0" : "c1",
              body: older ? "Earlier observation" : comment.body,
            },
          ],
          next_before: older ? null : comment.created_at,
          next_before_id: older ? null : "c1",
        },
      });
    }
  );
  await page.goto("/task-reviews");
  await page.getByRole("button", { name: "Review task" }).click();
  await expect(page.getByText(comment.body)).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Edit", exact: true })
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Older comments" }).click();
  await expect(page.getByText("Earlier observation")).toBeVisible();
  await page.getByLabel("Comment", { exact: true }).fill("My unsaved comment");
  await page.getByRole("button", { name: "Post comment" }).click();
  await expect(page.getByRole("alert")).toContainText("Couldn’t save review.");
  await expect(page.getByLabel("Comment", { exact: true })).toHaveValue(
    "My unsaved comment"
  );
  await page.screenshot({ path: testInfo.outputPath("mobile.png") });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth
    )
  ).toBe(true);
});

test("own comments can be edited and removed without changing another review", async ({
  page,
}) => {
  let comment = {
    id: "mine",
    reviewer: rating.reviewer,
    body: "Original",
    source_trial_id: null,
    revision: 1,
    created_at: "2026-10-08T10:00:00Z",
    updated_at: "2026-10-08T10:00:00.001Z",
  };
  let removed = false;
  await page.route(
    "**/api/tasks/review-task/versions/*/reviews**",
    async (route) => {
      if (["PATCH", "DELETE"].includes(route.request().method())) {
        const payload = route.request().postDataJSON();
        expect(payload.expected_revision).toBe(comment.revision);
        if (route.request().method() === "DELETE") removed = true;
        else
          comment = {
            ...comment,
            body: payload.body,
            revision: comment.revision + 1,
          };
        return route.fulfill({
          json: { id: comment.id, revision: comment.revision },
        });
      }
      return route.fulfill({
        json: { ...empty, comments: removed ? [] : [comment] },
      });
    }
  );
  await page.goto("/task-reviews");
  await page.getByRole("button", { name: "Review task" }).click();
  await expect(page.getByText("Original", { exact: true })).toBeVisible();
  await expect(page.getByText(/· Edited/)).toHaveCount(0);
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await page
    .getByLabel("Edit comment", { exact: true })
    .fill("Corrected feedback");
  await page.getByRole("button", { name: "Save comment" }).click();
  await expect(
    page.getByText("Corrected feedback", { exact: true })
  ).toBeVisible();
  await expect(page.getByText(/· Edited/)).toBeVisible();
  page.on("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(page.getByText("No comments yet.")).toBeVisible();
});

test("failed loads stay stable without preparation copy or retry controls", async ({ page }) => {
  let requests = 0;
  await page.route("**/api/tasks/review-task/versions/*/reviews**", async route => {
    requests++;
    await route.fulfill({ status: 500, json: { detail: "Internal error" } });
  });
  await page.goto("/task-reviews");
  await page.getByRole("button", { name: "Review task" }).click();
  await expect(page.getByRole("alert")).toHaveText("Couldn’t load reviews.");
  await expect(page.getByRole("button", { name: /Try again|Reload reviews/ })).toHaveCount(0);
  await page.clock.install();
  await page.clock.fastForward(60_000);
  expect(requests).toBe(1);
  await expect(page.getByRole("status")).toHaveCount(0);
});
