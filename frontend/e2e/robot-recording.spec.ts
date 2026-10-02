import { expect, test, type Page } from "@playwright/test";
import path from "node:path";
import episode from "./fixtures/robot-embedded-playback.json";
import playbackIndex from "./fixtures/robot-playback-index.json";

const root = "verifier/artifacts/";
const collected = "artifacts/episode/";
const indexPath = "_oddish/playback-index/fixture-revision.json";
const ready = { manifest_path: root + "playback.json", index_status: "ready", index_path: indexPath, index_revision: "fixture-revision", index_error: null };
const trialRecord = { id: "robot-0", name: "Robot pick-and-place", task_id: "robot", task_path: "robot", agent: "oracle", provider: "local", model: null, status: "success", attempts: 1, max_attempts: 1, harbor_stage: null, reward: 1, created_at: "2026-09-29T00:00:00Z", has_trajectory: false, playback: ready };
const command = (id: "o1" | "o2") => playbackIndex.commands[id]!.arguments.command;
// Without viewer_layout the recording keeps the layout saved in the .rrd.
const ownLayout = { ...episode, viewer_layout: undefined };

/** Serve one stored trial: its index, the referenced files and a listing. */
async function mockRun(page: Page, options: { missing?: boolean; manifest?: object } = {}) {
  await page.route("**/api/trials/robot-0", route => route.fulfill({ json: trialRecord }));
  await page.route("**/api/trials/robot-0/files**", route => {
    const url = new URL(route.request().url());
    const file = decodeURIComponent(url.pathname.split("/files/")[1] ?? "");
    if (url.pathname.endsWith("/files"))
      return route.fulfill({ json: { files: [
        "playback.json", "recording.rrd", "outcome.json", ...episode.observations.map(o => o.image),
      ].map(name => ({ path: root + name, size: 1000 })) } });
    if (file === indexPath)
      return route.fulfill({ json: {
        ...playbackIndex,
        revision: "fixture-revision",
        ...(options.manifest ? { manifest: options.manifest } : {}),
      } });
    if (file === root + "outcome.json")
      return route.fulfill({ json: { checks: Object.fromEntries(episode.checks.map(c => [c.key, true])) } });
    if (file === root + "recording.rrd")
      return options.missing
        ? route.fulfill({ status: 404 })
        : route.fulfill({ path: path.resolve("e2e/fixtures/robot-embedded-recording.rrd"), contentType: "application/octet-stream" });
    // Only stored objects exist; a shortened path is not one of them.
    if ((file.startsWith(root) || file.startsWith(collected)) && file.endsWith(".jpg"))
      return route.fulfill({ path: path.resolve("e2e/fixtures", path.basename(file)), contentType: "image/jpeg" });
    return route.fulfill({ status: 404 });
  });
}

const slider = (page: Page) => page.getByRole("slider", { name: "Simulation time" });
const play = (page: Page) => page.getByRole("button", { name: "Play replay", exact: true });
const caption = (page: Page) => page.getByRole("region", { name: "Command for recorded frame" });

/** Serve the trial record with another descriptor than the ready fixture. */
const serveTrial = (page: Page, playback: object | null) =>
  page.route("**/api/trials/robot-0", route => route.fulfill({ json: { ...trialRecord, playback } }));

const tabNames = async (page: Page) =>
  (await page.getByRole("tab").allTextContents()).map(name => name.trim());

test("a trial with a playback manifest adds a Scene tab beside the usual tabs", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  // The fixture's viewer_layout shows the agent's camera images beside the scene.
  await mockRun(page);
  await page.goto("/robot-recording");
  await expect(page.getByRole("tab", { name: "Summary", exact: true })).toHaveAttribute("data-state", "active");
  // Collect errors once the drawer has hydrated: this review app server-renders
  // the drawer, which intermittently reports a text hydration mismatch (React
  // #418) for coding-agent trials too.
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  expect(await tabNames(page)).toEqual(["Summary", "Scene", "Files", "Trajectory", "Artifacts"]);
  // Summary leads with the episode goal and its final checks.
  await page.getByText("Final checks", { exact: true }).click();
  await expect(page.getByText("Passed: ", { exact: true })).toHaveCount(episode.checks.length);

  await page.getByRole("tab", { name: "Scene", exact: true }).click();
  await expect(play(page)).toBeEnabled({ timeout: 60000 });
  await expect.poll(async () => Number(await slider(page).inputValue())).toBeCloseTo(episode.start_time, 1);
  const canvas = await page.locator("canvas").boundingBox();
  expect(canvas!.height).toBeGreaterThan(350);
  await page.getByRole("button", { name: "Next observation", exact: true }).click();
  await expect.poll(async () => Number(await slider(page).inputValue()))
    .toBeCloseTo(episode.observations[1].time, 1);
  await expect(caption(page).getByText(command("o1"), { exact: true })).toBeVisible();
  await page.screenshot({ path: "test-results/robot-scene-tab.png" });

  // The caption opens the trajectory step that returned this observation.
  await expect(caption(page).getByRole("link")).toHaveAttribute("href", /tab=trajectory.*#step-8$/);
  await caption(page).getByRole("link").click();
  await expect(page.getByRole("tab", { name: "Trajectory", exact: true })).toHaveAttribute("data-state", "active");
  await expect(page).toHaveURL(/tab=trajectory/);
  expect(errors).toEqual([]);
});

test("a ?tab=scene link opens the scene directly", async ({ page }) => {
  await mockRun(page, { manifest: ownLayout });
  await page.goto("/robot-recording?tab=scene");
  await expect(page.getByRole("tab", { name: "Scene", exact: true })).toHaveAttribute("data-state", "active");
  await expect(play(page)).toBeEnabled({ timeout: 60000 });
});

test("an invalid manifest keeps the Scene tab and explains the index error", async ({ page }) => {
  await mockRun(page);
  await serveTrial(page, { ...ready, index_status: "invalid", index_path: null, index_revision: null, index_error: "missing_reference" });
  await page.goto("/robot-recording/invalid?tab=scene");
  await expect(page.getByRole("tab", { name: "Scene", exact: true })).toHaveAttribute("data-state", "active");
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.getByRole("button", { name: "Prepare playback" })).toHaveCount(0);
});

const absent = { manifest_path: null, index_status: "absent", index_path: null, index_revision: null, index_error: null };
for (const [kind, route, playback] of [["without a manifest", "absent", absent], ["recorded before indexing", "legacy", null]] as const) {
  test(`a trial ${kind} keeps the coding-agent drawer`, async ({ page }) => {
    let indexReads = 0;
    page.on("request", request => { if (request.url().includes("playback-index")) indexReads++; });
    await mockRun(page);
    await serveTrial(page, playback);
    // A stale ?tab=scene link falls back to Summary instead of an empty tab.
    await page.goto(`/robot-recording/${route}?tab=scene`);
    await expect(page.getByRole("tab", { name: "Summary", exact: true })).toHaveAttribute("data-state", "active");
    expect(await tabNames(page)).toEqual(["Summary", "Files", "Trajectory", "Artifacts"]);
    await expect(page.getByText("Playback has not been prepared", { exact: false })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Prepare playback" })).toHaveCount(0);
    await expect(page.getByText("Final checks", { exact: true })).toHaveCount(0);
    expect(indexReads).toBe(0);
  });
}
