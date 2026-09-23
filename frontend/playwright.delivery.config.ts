import { defineConfig, devices } from "@playwright/test";
const port = process.env.DELIVERY_TEST_PORT ?? "3109";
export default defineConfig({
  testDir: "e2e",
  projects: [
    {
      name: "delivery",
      testMatch: ["delivery-refresh.spec.ts", "tasks-picker.spec.ts"],
    },
    {
      name: "effort",
      testMatch: [
        "member-picker.spec.ts",
        "task-run-coverage.spec.ts",
        "reasoning-effort.spec.ts",
        "experiment-agent-grouping.spec.ts",
        "ec2-rerun-command.spec.ts",
      ],
      timeout: 90_000,
      retries: process.env.CI ? 1 : 0,
      use: { navigationTimeout: 60_000 },
    },
  ],
  workers: 1,
  reporter: "list",
  use: {
    baseURL: `http://localhost:${port}`,
    ...devices["Desktop Chrome"],
    trace: "retain-on-failure",
  },
  webServer: {
    command: `node node_modules/next/dist/bin/next build e2e/delivery-app --webpack && node node_modules/next/dist/bin/next start e2e/delivery-app -p ${port}`,
    url: `http://localhost:${port}`,
    reuseExistingServer: false,
    // CI's cold production build needs the same budget as the file fixtures.
    timeout: 300000,
  },
});
