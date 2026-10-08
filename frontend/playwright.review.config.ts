import { defineConfig, devices } from "@playwright/test";
const port = process.env.REVIEW_TEST_PORT ?? "3207";
export default defineConfig({
  testDir: "e2e",
  testMatch: [
    "task-reviews.spec.ts",
    "robot-recording.spec.ts",
    "qa-evals.spec.ts",
    "qa-feedback.spec.ts",
    "dashboard-member-filter.spec.ts",
    "review-meaning.spec.ts",
    "ui-duplication.spec.ts",
    "task-effort-comparison.spec.ts",
    "qa-rejection-state.spec.ts",
    "user-ui-layout.spec.ts",
    "public-experiment-tasks.spec.ts",
    "public-share-qa.spec.ts",
  ],
  workers: 1,
  reporter: "list",
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
  },
  webServer: {
    command:
      `cd e2e/review-app && node ../../node_modules/next/dist/bin/next build --webpack && node ../../node_modules/next/dist/bin/next start --hostname 127.0.0.1 --port ${port}`,
    url: `http://127.0.0.1:${port}`,
    reuseExistingServer: false,
    timeout: 120000,
  },
});
