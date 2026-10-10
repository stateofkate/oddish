import { test, expect } from "@playwright/test";

const empty = {
  task_version_id: "v2",
  current_user_id: "me",
  can_moderate: false,
  average_score: null,
  rating_count: 0,
  own_rating: null,
  own_comment: null,
  comment_count: 0,
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
  let releaseRefresh!: () => void;
  const refresh = new Promise<void>((resolve) => { releaseRefresh = resolve; });
  await page.route(
    "**/api/tasks/review-task/versions/*/reviews**",
    async (route) => {
      const req = route.request();
      if (req.method() !== "GET") {
        writes.push({ url: req.url(), body: req.postDataJSON() });
        saved = true;
        return route.fulfill({ json: { id: "saved", revision: 1 } });
      }
      const old = req.url().includes("/v1/");
      if (saved && !old) await refresh;
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
                comment_count: 1,
                own_comment: {
                  id: "saved",
                  reviewer: rating.reviewer,
                  body: "The requirements are ambiguous.",
                  revision: 1,
                  source_trial_id: null,
                  created_at: rating.updated_at,
                  updated_at: rating.updated_at,
                },
                comments: [
                  {
                    id: "saved",
                    reviewer: rating.reviewer,
                    body: "The requirements are ambiguous.",
                    revision: 1,
                    source_trial_id: null,
                    created_at: rating.updated_at,
                    updated_at: rating.updated_at,
                  },
                ],
              }
            : {}),
        },
      });
    }
  );
  await page.goto("/task-reviews");
  await page.getByRole("button", { name: "Review task" }).click();
  await page.getByRole("tab", { name: "Reviews (0)", exact: true }).click();
  await expect(page.getByText("No reviews yet.")).toBeVisible();
  await page.getByRole("tab", { name: "Your review", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Save review" })
  ).toBeDisabled();
  await page
    .getByLabel("Comment", { exact: true })
    .fill("The requirements are ambiguous.");
  await expect(
    page.getByRole("button", { name: "Save review" })
  ).toBeDisabled();
  await page.getByLabel("Comment", { exact: true }).fill("");
  await page.getByRole("button", { name: "Rate 4 out of 5" }).click();
  await expect(
    page.getByRole("button", { name: "Save review" })
  ).toBeDisabled();
  expect(writes).toHaveLength(0);
  await expect(
    page.getByRole("button", { name: "Rate 4 out of 5" })
  ).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByText(/Poor|Adequate|Excellent/)).toHaveCount(0);
  await page
    .getByLabel("Comment", { exact: true })
    .fill("The requirements are ambiguous.");
  const selectedRating = page.getByRole("button", { name: "Rate 4 out of 5" });
  const saveColor = await page
    .getByRole("button", { name: "Save review" })
    .evaluate((element) => getComputedStyle(element).backgroundColor);
  await expect
    .poll(() =>
      selectedRating.evaluate(
        (element) => getComputedStyle(element).backgroundColor
      )
    )
    .toBe(saveColor);
  await page.screenshot({ path: testInfo.outputPath("desktop.png") });
  await page.getByRole("button", { name: "Save review" }).click();
  await expect.poll(() => writes.length).toBe(1);
  await expect(page.getByRole("tab", { name: "Your review", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByLabel("Comment", { exact: true })).toHaveValue("The requirements are ambiguous.");
  releaseRefresh();
  await expect(page.getByText("Mark (you)")).toBeVisible();
  await expect(page.locator("article")).toHaveCSS("padding-bottom", "24px");
  await page.getByRole("tab", { name: "Your review", exact: true }).click();
  await expect(page.getByLabel("Comment", { exact: true })).toHaveValue(
    "The requirements are ambiguous."
  );
  await expect(
    page.getByRole("button", { name: "Save changes" })
  ).toBeDisabled();
  expect(writes).toHaveLength(1);
  expect(writes[0].body).toMatchObject({
    score: 4,
    expected_revision: 0,
    body: "The requirements are ambiguous.",
  });
  expect(writes[0].body.client_request_id).toMatch(/^[a-f0-9-]{36}$/);
  await page.getByRole("combobox", { name: "Review version" }).click();
  await page.getByRole("option", { name: "v1", exact: true }).click();
  await expect(page.getByText("Mark (you)")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Save review" })
  ).toBeDisabled();
  await page.getByRole("button", { name: "Rate 2 out of 5" }).click();
  await page
    .getByLabel("Comment", { exact: true })
    .fill("Earlier version feedback");
  await page.getByRole("button", { name: "Save review" }).click();
  await expect.poll(() => writes.length).toBe(2);
  expect(writes[1].url).toContain("/versions/v1/reviews");
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
          ratings: [
            { ...rating, reviewer: comment.reviewer },
            { ...rating, id: "r2", reviewer: { id: "older", name: "Kyle" } },
            { ...rating, id: "r3", reviewer: { id: "legacy", name: "Pat" } },
          ],
          rating_count: 3,
          comment_count: 2,
          average_score: 4,
          comments: [
            {
              ...comment,
              id: older ? "c0" : "c1",
              reviewer: older
                ? { id: "older", name: "Kyle" }
                : comment.reviewer,
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
  await page.getByRole("tab", { name: /^Reviews/ }).click();
  await expect(page.getByText(comment.body)).toBeVisible();
  await expect(page.getByText("Kyle", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Pat", { exact: true })).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Edit", exact: true })
  ).toHaveCount(0);
  await page.getByRole("button", { name: "More reviews" }).click();
  await expect(page.getByText("Earlier observation")).toBeVisible();
  await expect(page.getByText("Kyle", { exact: true })).toHaveCount(1);
  await expect(page.getByText("Pat", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "Your review", exact: true }).click();
  await page.getByRole("button", { name: "Rate 2 out of 5" }).click();
  await page.getByLabel("Comment", { exact: true }).fill("My unsaved comment");
  await page.getByRole("button", { name: "Save review" }).click();
  await expect(page.getByRole("alert")).toContainText("Couldn’t save review.");
  await expect(page.getByLabel("Comment", { exact: true })).toHaveValue(
    "My unsaved comment"
  );
  await expect(
    page.getByRole("button", { name: "Rate 2 out of 5" })
  ).toBeEnabled();
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
      if (["POST", "DELETE"].includes(route.request().method())) {
        const payload = route.request().postDataJSON();
        expect(
          route.request().method() === "DELETE"
            ? payload.expected_revision
            : payload.comment_revision
        ).toBe(comment.revision);
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
        json: {
          ...empty,
          own_rating: removed ? null : rating,
          own_rating_revision: 1,
          own_comment: removed ? null : comment,
          comment_count: removed ? 0 : 1,
          comments: removed ? [] : [comment],
        },
      });
    }
  );
  await page.goto("/task-reviews");
  await page.getByRole("button", { name: "Review task" }).click();
  await expect(page.getByLabel("Comment", { exact: true })).toHaveValue(
    "Original"
  );
  await expect(
    page.getByRole("button", { name: "Save changes" })
  ).toBeDisabled();
  await page.getByRole("tab", { name: /^Reviews/ }).click();
  await expect(page.getByText("Original", { exact: true })).toBeVisible();
  await expect(page.getByText(/· Edited/)).toHaveCount(0);
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await page.getByLabel("Comment", { exact: true }).fill("Corrected feedback");
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(
    page.getByText("Corrected feedback", { exact: true })
  ).toBeVisible();
  await expect(page.getByText(/· Edited/)).toBeVisible();
  page.on("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(page.getByText("No reviews yet.")).toBeVisible();
});

test("failed loads stay stable without preparation copy or retry controls", async ({
  page,
}) => {
  let requests = 0;
  await page.route(
    "**/api/tasks/review-task/versions/*/reviews**",
    async (route) => {
      requests++;
      if (route.request().url().includes("/v1/"))
        return route.fulfill({ json: { ...empty, task_version_id: "v1" } });
      await route.fulfill({ status: 500, json: { detail: "Internal error" } });
    }
  );
  await page.goto("/task-reviews");
  await page.getByRole("button", { name: "Review task" }).click();
  await expect(page.getByRole("alert")).toHaveText("Couldn’t load reviews.");
  await expect(
    page.getByRole("button", { name: /Try again|Reload reviews/ })
  ).toHaveCount(0);
  await page.clock.install();
  await page.clock.fastForward(60_000);
  expect(requests).toBe(1);
  await expect(page.getByRole("status")).toHaveCount(0);
  await page.getByRole("combobox", { name: "Review version" }).click();
  await page.getByRole("option", { name: "v1", exact: true }).click();
  await expect(page.getByLabel("Comment", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("combobox", { name: "Review version" })
  ).toHaveText("v1");
});

test("a pending review load leaves the version selector available", async ({
  page,
}) => {
  let release = () => {};
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(
    "**/api/tasks/review-task/versions/*/reviews**",
    async (route) => {
      if (route.request().url().includes("/v2/")) await pending;
      await route.fulfill({ json: empty });
    }
  );
  await page.goto("/task-reviews");
  await page.getByRole("button", { name: "Review task" }).click();
  await expect(page.getByRole("status")).toHaveText("Loading reviews…");
  await page.getByRole("combobox", { name: "Review version" }).click();
  await page.getByRole("option", { name: "v1", exact: true }).click();
  await expect(page.getByLabel("Comment", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("combobox", { name: "Review version" })
  ).toHaveText("v1");
  release();
});

test("saved review edits, rating baseline and per-version drafts remain compact", async ({
  page,
}, testInfo) => {
  let score = 3;
  let body = "Saved review";
  const own = () => ({
    id: "own",
    reviewer: rating.reviewer,
    body,
    revision: 1,
    source_trial_id: null,
    created_at: rating.updated_at,
    updated_at: rating.updated_at,
  });
  const writes: Record<string, unknown>[] = [];
  await page.route(
    "**/api/tasks/review-task/versions/*/reviews**",
    async (route) => {
      if (route.request().method() === "POST") {
        const data = route.request().postDataJSON();
        writes.push(data);
        score = data.score;
        body = data.body;
        return route.fulfill({ json: { id: "own", revision: 2 } });
      }
      const old = route.request().url().includes("/v1/");
      const older = route.request().url().includes("before=");
      return route.fulfill({
        json: {
          ...empty,
          ...(old
            ? {}
            : {
                own_rating: { ...rating, score },
                own_rating_revision: 1,
                own_comment: own(),
                comment_count: 100,
                comments: older
                  ? [own()]
                  : Array.from({ length: 50 }, (_, i) => ({
                      ...own(),
                      id: `other-${i}`,
                      reviewer: { id: `other-${i}`, name: `Reviewer ${i}` },
                    })),
                next_before: older ? null : rating.updated_at,
                next_before_id: older ? null : "cursor",
              }),
        },
      });
    }
  );
  await page.goto("/task-reviews");
  await page.getByRole("button", { name: "Review task" }).click();
  await expect(page.getByLabel("Comment", { exact: true })).toHaveValue(
    "Saved review"
  );
  await page.getByRole("button", { name: "Rate 4 out of 5" }).click();
  await expect(page.getByText("Previously 3 / 5")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Save changes" })
  ).toBeEnabled();
  const filled = await page
    .getByRole("button", { name: "Save changes" })
    .evaluate((el) => getComputedStyle(el).backgroundColor);
  await expect
    .poll(() =>
      page
        .getByRole("button", { name: "Rate 4 out of 5" })
        .evaluate((el) => getComputedStyle(el).backgroundColor)
    )
    .toBe(filled);
  await page.screenshot({ path: testInfo.outputPath("rating-change.png") });
  await page.getByRole("button", { name: "Rate 3 out of 5" }).click();
  await expect(page.getByText("Previously 3 / 5")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Save changes" })
  ).toBeDisabled();
  await page.getByLabel("Comment", { exact: true }).fill("Edited review");
  await page.getByRole("tab", { name: "Reviews (100)" }).click();
  const height = await page
    .getByRole("dialog")
    .evaluate((el) => el.getBoundingClientRect().height);
  expect(height).toBeLessThanOrEqual(448);
  expect(
    await page
      .getByRole("region", { name: "Review history" })
      .evaluate((el) => el.scrollHeight > el.clientHeight)
  ).toBe(true);
  await page.getByRole("tab", { name: "Your review", exact: true }).click();
  await expect(page.getByLabel("Comment", { exact: true })).toHaveValue(
    "Edited review"
  );
  await page.getByRole("combobox", { name: "Review version" }).click();
  await page.getByRole("option", { name: "v1", exact: true }).click();
  await expect(page.getByLabel("Comment", { exact: true })).toHaveValue("");
  await page.getByRole("combobox", { name: "Review version" }).click();
  await page.getByRole("option", { name: "v2 · Default", exact: true }).click();
  await expect(page.getByLabel("Comment", { exact: true })).toHaveValue(
    "Edited review"
  );
  await page.getByRole("button", { name: "Save changes" }).click();
  expect(writes).toHaveLength(1);
  expect(writes[0]).toMatchObject({
    comment_id: "own",
    comment_revision: 1,
    body: "Edited review",
    score: 3,
  });
  const savedReview = page
    .getByRole("article")
    .filter({ hasText: "Mark (you)" });
  await expect(savedReview).toHaveCount(1);
  await expect(savedReview.getByText("3 / 5", { exact: true })).toBeVisible();
  await expect(
    savedReview.getByText("Edited review", { exact: true })
  ).toBeInViewport();
  await page.getByRole("button", { name: "More reviews", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "More reviews", exact: true })
  ).toHaveCount(0);
  await expect(savedReview).toHaveCount(1);
  await page.getByRole("tab", { name: "Your review", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Save changes" })
  ).toBeDisabled();
});

test("a conflict preserves the draft and a subsequent manual save uses refreshed revisions", async ({
  page,
}) => {
  let revision = 1;
  let attempts = 0;
  let body = "Saved comment";
  await page.route(
    "**/api/tasks/review-task/versions/*/reviews**",
    async (route) => {
      if (route.request().method() === "POST") {
        const data = route.request().postDataJSON();
        if (++attempts === 1) {
          expect(data.comment_revision).toBe(1);
          revision = 2;
          return route.fulfill({
            status: 409,
            json: { detail: "Comment changed" },
          });
        }
        expect(data.comment_revision).toBe(2);
        expect(data.expected_revision).toBe(2);
        body = data.body;
        return route.fulfill({ json: { id: "mine", revision: 3 } });
      }
      const comment = {
        id: "mine",
        reviewer: rating.reviewer,
        body,
        revision,
        source_trial_id: null,
        created_at: rating.updated_at,
        updated_at: rating.updated_at,
      };
      return route.fulfill({
        json: {
          ...empty,
          own_rating: { ...rating, revision },
          own_rating_revision: revision,
          own_comment: comment,
          comments: [comment],
          comment_count: 1,
        },
      });
    }
  );
  await page.goto("/task-reviews");
  await page.getByRole("button", { name: "Review task" }).click();
  await page.getByLabel("Comment", { exact: true }).fill("My retained edit");
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByRole("alert")).toContainText("Couldn’t save review.");
  await expect(
    page.getByRole("button", { name: "Save changes" })
  ).toBeEnabled();
  await expect(page.getByLabel("Comment", { exact: true })).toHaveValue(
    "My retained edit"
  );
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(
    page.getByText("My retained edit", { exact: true })
  ).toBeVisible();
  expect(attempts).toBe(2);
});
