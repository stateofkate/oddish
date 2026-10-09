import { defineConfig, devices } from "@playwright/test";

// No `webServer` block on purpose: Playwright would boot it before it knows the
// suite is going to skip, so a credential-less run (the CI/local skip path)
// would still try to spin up `pnpm dev`. The authenticated spec instead assumes
// an already-running dev stack (see docs/e2e-test-plan.md P4 for the env it
// needs). Point it at a different origin with E2E_BASE_URL.
export default defineConfig({
  testDir: "e2e",
  // Uses its own local app, with controlled responses and no Clerk credentials.
  testIgnore: [
    "task-reviews.spec.ts",
    "trial-agent-run.spec.ts",
    "robot-recording.spec.ts",
    "dashboard-member-filter.spec.ts",
    "member-picker.spec.ts",
    "delivery-refresh.spec.ts",
    "tasks-picker.spec.ts",
    "task-run-coverage.spec.ts",
    "task-effort-comparison.spec.ts",
    "file-loading.spec.ts",
    "prepared-reads.spec.ts",
    "member-filter-pagination.spec.ts",
    "reasoning-effort.spec.ts",
    "user-ui-layout.spec.ts",
    "ui-duplication.spec.ts",
    "public-experiment-tasks.spec.ts",
    "public-share-qa.spec.ts",
    "qa-evals.spec.ts",
    "qa-feedback.spec.ts",
    "trajectory-rendering.spec.ts",
    "trial-progress.spec.ts",
    "customer-view.spec.ts",
  ],
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: "list",
  globalSetup: "./e2e/global-setup.ts",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
