import { expect, test, type Page } from "@playwright/test";
import path from "node:path";
import episode from "./fixtures/robot-embedded-playback.json";
import playbackIndex from "./fixtures/robot-playback-index.json";

const root = "verifier/artifacts/";
const collected = "artifacts/episode/";
const indexPath = "_oddish/playback-index/fixture-revision.json";
const ready = { manifest_path: root + "playback.json", index_status: "ready", index_path: indexPath, index_revision: "fixture-revision", index_error: null };
const trialRecord = { id: "robot-0", name: "Robot pick-and-place", task_id: "robot", task_path: "robot", agent: "oracle", provider: "local", model: null, status: "success", attempts: 1, max_attempts: 1, harbor_stage: null, reward: 1, created_at: "2026-09-29T00:00:00Z", has_trajectory: false, playback: ready };
const focus = (trial: object) => ({
  task: { id: "robot", name: "Robot pick-and-place", status: "completed", total: 1, completed: 1, failed: 0, created_at: "2026-09-29T00:00:00Z", updated_at: "2026-09-29T00:00:00Z" },
  trial: { ...trial, analysis: { status: null } },
});
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

test("playback is the default, plays and seeks, and keeps one viewer across views", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  let recordings = 0;
  page.on("request", request => { if (new URL(request.url()).pathname.endsWith("recording.rrd")) recordings++; });
  await mockRun(page, { manifest: ownLayout });
  await page.goto("/robot-recording");
  await expect(page.getByRole("tab", { name: "Run", exact: true })).toHaveAttribute("data-state", "active");
  await expect(play(page)).toBeEnabled({ timeout: 60000 });
  await expect(page.getByRole("button", { name: "Data", exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "Overview", exact: true }).click();
  await page.getByText("Final checks", { exact: true }).click();
  await expect(page.getByText("Passed: ", { exact: true })).toHaveCount(episode.checks.length);
  await page.getByRole("tab", { name: "Run", exact: true }).click();
  await expect(play(page)).toBeEnabled();
  await expect.poll(async () => Number(await slider(page).inputValue())).toBeCloseTo(episode.start_time, 1);
  const canvas = await page.locator("canvas").boundingBox();
  expect(canvas!.height).toBeGreaterThan(350);
  expect(canvas!.y + canvas!.height).toBeLessThanOrEqual((await slider(page).boundingBox())!.y);
  await play(page).click();
  await expect.poll(async () => Number(await slider(page).inputValue())).toBeGreaterThan(0.1);
  await page.getByRole("button", { name: "Pause replay", exact: true }).click();
  await page.getByRole("button", { name: "Restart replay", exact: true }).click();
  await page.getByRole("button", { name: "Next observation", exact: true }).click();
  await expect.poll(async () => Number(await slider(page).inputValue()))
    .toBeCloseTo(episode.observations[1].time, 1);
  await page.getByRole("button", { name: "Restart replay", exact: true }).click();
  await expect.poll(async () => Number(await slider(page).inputValue())).toBeCloseTo(episode.start_time, 1);
  await page.screenshot({ path: "test-results/robot-playback-desktop.png" });
  await page.getByRole("tab", { name: "Files", exact: true }).click();
  await page.getByRole("combobox", { name: "File filter" }).selectOption("artifacts");
  await expect(page).toHaveURL(/tab=artifacts/);
  // Hidden and paused, not destroyed.
  await expect(page.locator("canvas")).toHaveCount(1);
  await expect(play(page)).toHaveCount(0);
  await page.getByRole("tab", { name: "Run", exact: true }).click();
  await expect(play(page)).toBeEnabled();
  expect(recordings).toBe(1);
  await page.getByRole("button", { name: "Toggle viewer", exact: true }).click();
  await expect(page.locator("canvas")).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("narrow player fits its allocated height and checks remain reachable", async ({ page }) => {
  await page.setViewportSize({ width: 650, height: 850 });
  await mockRun(page, { manifest: ownLayout });
  await page.goto("/robot-recording?tab=playback");
  await expect(play(page)).toBeEnabled({ timeout: 60000 });
  const canvas = await page.locator("canvas").boundingBox();
  const region = await page.getByRole("region", { name: "Simulation playback" }).boundingBox();
  expect(canvas!.y + canvas!.height).toBeLessThanOrEqual(region!.y + region!.height);
  await page.getByRole("tab", { name: "Overview", exact: true }).click();
  await page.getByText("Final checks", { exact: true }).scrollIntoViewIfNeeded();
  await expect(page.getByText("Final checks", { exact: true })).toBeVisible();
  await page.screenshot({ path: "test-results/robot-playback-narrow.png" });
});

test("missing recordings and invalid descriptions are reported, not played", async ({ page }) => {
  await mockRun(page, { missing: true });
  await page.goto("/robot-recording?tab=playback");
  await expect(page.getByRole("status")).toContainText("HTTP 404");
  await page.unrouteAll({ behavior: "wait" });
  await mockRun(page, { manifest: { ...episode, recording: "../../outside.rrd" } });
  await page.reload();
  await expect(page.getByRole("alert").filter({ hasText: "Unsupported" })).toHaveText("Unsupported or invalid playback description");
  await page.unrouteAll({ behavior: "wait" });
  await mockRun(page, { manifest: { ...episode, actions: [{ ...episode.actions[0], observation_id: "missing" }] } });
  await page.reload();
  await expect(page.getByRole("alert").filter({ hasText: "Invalid action" })).toBeVisible();
  await expect(page.locator("canvas")).toHaveCount(0);
});

test("an invalid index shows its error in Run instead of another view", async ({ page }) => {
  await mockRun(page);
  let requests = 0;
  page.on("request", request => { if (request.url().includes("/robot-0/files")) requests++; });
  await page.goto("/robot-recording/invalid");
  await expect(page.getByRole("tab", { name: "Run", exact: true })).toHaveAttribute("data-state", "active");
  await expect(page.getByRole("alert").filter({ hasText: "playback" }))
    .toHaveText("This run's playback references a missing recording, result, or image.");
  await expect(page).toHaveURL(/tab=playback/);
  await page.getByRole("button", { name: "Full agent trace", exact: true }).click();
  await expect(page).toHaveURL(/tab=trajectory/);
  expect(requests).toBe(0);
});

test("ordinary trials open Overview, and their images show no robot caption", async ({ page }) => {
  await mockRun(page);
  await page.goto("/robot-recording/absent");
  await expect(page.getByRole("tab", { name: "Overview", exact: true })).toHaveAttribute("data-state", "active");
  await expect(page.getByRole("tab")).toHaveCount(3);
  await page.goto(`/robot-recording/absent?tab=artifacts&file=${root}o1.jpg`);
  await expect(page.getByRole("img", { name: "o1.jpg", exact: true })).toBeVisible();
  await expect(caption(page)).toHaveCount(0);
});

test("linked playback opens while the experiment table is still loading", async ({ page }) => {
  await mockRun(page);
  await page.route("**/api/experiments/robot-experiment/focus?**", route => route.fulfill({ json: focus(trialRecord) }));
  await page.goto("/robot-recording/early?task=robot&trial=robot-0&tab=playback");
  await expect(page.getByLabel("Loading experiment", { exact: true })).toBeVisible();
  await expect(play(page)).toBeEnabled({ timeout: 60000 });
  await page.getByRole("button", { name: "Full agent trace", exact: true }).click();
  await expect(page.getByText("Oracle runs have no agent trace.", { exact: true })).toBeVisible();
});

test("a stored descriptor starts the index before trial details and keeps a tab chosen meanwhile", async ({ page }) => {
  await mockRun(page);
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  let indexes = 0;
  let listings = 0;
  page.on("request", req => { if (req.url().includes("/robot-0/files?")) listings++; });
  await page.route("**/api/trials/robot-0", async route => { await held; await route.fulfill({ json: trialRecord }); });
  await page.route(`**/${indexPath}?*`, async route => { indexes++; await held; await route.fallback(); });
  await page.route("**/api/experiments/robot-experiment/focus?**", route => route.fulfill({ json: focus(trialRecord) }));
  try {
    await page.goto("/robot-recording/early?task=robot&trial=robot-0");
    await expect(page.getByRole("tab", { name: "Run", exact: true })).toHaveAttribute("data-state", "active");
    await expect(page.getByRole("status").filter({ hasText: "Loading playback" })).toBeVisible();
    await expect.poll(() => indexes).toBe(1);
    await page.getByRole("tab", { name: "Overview", exact: true }).click();
  } finally { release(); }
  await expect(page.getByRole("tab", { name: "Overview", exact: true })).toHaveAttribute("data-state", "active");
  await expect(page).toHaveURL(/tab=summary/);
  expect(listings).toBe(0);
});

test("a compact row without a descriptor waits for the trial record", async ({ page }) => {
  await mockRun(page);
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  let indexes = 0;
  page.on("request", req => { if (new URL(req.url()).pathname.endsWith(indexPath)) indexes++; });
  await page.route("**/api/trials/robot-0", async route => { await held; await route.fulfill({ json: trialRecord }); });
  await page.route("**/api/experiments/robot-experiment/focus?**", route =>
    route.fulfill({ json: focus({ ...trialRecord, playback: undefined }) }));
  try {
    await page.goto("/robot-recording/early?task=robot&trial=robot-0");
    await expect(page.getByText("Loading run views…")).toBeVisible();
    await expect(page.getByRole("tab", { name: "Overview", exact: true })).toHaveCount(0);
    await page.waitForTimeout(300);
    expect(indexes).toBe(0);
  } finally { release(); }
  await expect(page.getByRole("tab", { name: "Run", exact: true })).toHaveAttribute("data-state", "active");
  await expect.poll(() => indexes).toBe(1);
});

test("viewer initialization starts before a held recording download finishes", async ({ page }) => {
  await mockRun(page);
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  let viewerRequested = false;
  page.on("request", request => { if (request.url().includes(".wasm")) viewerRequested = true; });
  await page.route("**/recording.rrd{,?*}", async route => {
    await held;
    await route.fulfill({ path: path.resolve("e2e/fixtures/robot-embedded-recording.rrd"), contentType: "application/octet-stream" });
  });
  try {
    await page.goto("/robot-recording?tab=playback");
    await expect.poll(() => viewerRequested, { timeout: 15000 }).toBe(true);
  } finally { release(); }
  await expect(play(page)).toBeEnabled({ timeout: 60000 });
});

test("actions seek to the observation before and after control", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await mockRun(page);
  await page.goto("/robot-recording?tab=playback");
  await expect(play(page)).toBeEnabled({ timeout: 60000 });
  await expect(page.getByRole("list", { name: "Agent actions" })).toHaveCount(0);
  await page.getByRole("button", { name: "Actions", exact: true }).click();
  await page.getByRole("button", { name: "Close gripper", exact: false }).click();
  await expect.poll(async () => Number(await slider(page).inputValue())).toBeCloseTo(1.1, 1);
  const current = page.getByRole("list", { name: "Agent actions" }).locator('[aria-current="step"]');
  await expect(current).toContainText("Close gripper");
  await page.getByRole("button", { name: "After", exact: true }).click();
  await expect.poll(async () => Number(await slider(page).inputValue())).toBeCloseTo(1.9, 1);
  await expect(current).toContainText("Close gripper");
  await page.getByRole("button", { name: "Before", exact: true }).click();
  await expect.poll(async () => Number(await slider(page).inputValue())).toBeCloseTo(1.1, 1);
  await page.getByRole("button", { name: "Next observation", exact: true }).click();
  await expect.poll(async () => Number(await slider(page).inputValue())).toBeCloseTo(1.9, 1);
  await page.getByRole("button", { name: "Previous observation", exact: true }).click();
  await expect.poll(async () => Number(await slider(page).inputValue())).toBeCloseTo(1.1, 1);
  await page.screenshot({ path: "test-results/robot-interactive-desktop.png" });
  await page.getByRole("tab", { name: "Overview", exact: true }).click();
  await page.getByText("Final checks", { exact: true }).click();
  await expect(page.getByText("Passed: ", { exact: true })).toHaveCount(episode.checks.length);
});

test("an unreadable first image leaves the scene usable and says so", async ({ page }) => {
  await mockRun(page);
  await page.route("**/o0.jpg{,?*}", route => route.fulfill({ status: 404 }));
  await page.goto("/robot-recording?tab=playback");
  await expect(play(page)).toBeEnabled({ timeout: 60000 });
  await expect(page.getByRole("alert").filter({ hasText: "Frame unavailable" })).toBeVisible();
  await page.getByRole("button", { name: "Next observation", exact: true }).click();
  await expect(page.getByRole("img", { name: "Agent view", exact: true })).toHaveAttribute("data-frame-index", "1");
});

test("frame captions come from the index and link to their trace step", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await mockRun(page);
  let laterPages = 0;
  let traces = 0;
  page.on("request", request => { if (request.url().endsWith("/trajectory")) traces++; });
  await page.route("**/api/trials/robot-0/files?**", route => {
    if (new URL(route.request().url()).searchParams.has("cursor")) laterPages++;
    return route.fulfill({ json: { files: [{ path: root + "o1.jpg" }], cursor: "later" } });
  });
  await page.goto(`/robot-recording?tab=artifacts&file=${root}o1.jpg`);
  // The command the indexer copied, shown through the trace formatter.
  await expect(caption(page).getByText(command("o1"), { exact: true })).toBeVisible();
  await expect(caption(page)).toContainText("o0 → o1");
  await expect(caption(page)).toContainText("Observation 2 of 4");
  await expect(caption(page).getByRole("link")).toHaveAttribute("href", /tab=trajectory.*observation=o1#step-8$/);
  const image = await page.getByRole("img", { name: "o1.jpg", exact: true }).boundingBox();
  const details = await caption(page).boundingBox();
  expect(details!.y).toBeGreaterThanOrEqual(image!.y + image!.height);
  expect(details!.y + details!.height).toBeLessThan(1000);
  await page.screenshot({ path: "test-results/robot-frame-command.png" });
  await page.getByRole("button", { name: "Next observation image" }).click();
  await expect(caption(page)).toContainText("Observation 3 of 4");
  await expect(caption(page).getByText(command("o2"), { exact: true })).toBeVisible();
  await expect(page.getByRole("img", { name: "o2.jpg", exact: true })).toBeVisible();
  await page.getByRole("treeitem", { name: "o1.jpg", exact: true }).click();
  await expect(caption(page).getByText(command("o1"), { exact: true })).toBeVisible();
  // A collected copy of the same image carries the same caption.
  await page.goto(`/robot-recording?tab=artifacts&file=${collected}o1.jpg`);
  await expect(caption(page).getByText(command("o1"), { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Next observation image" }).click();
  await expect(page).toHaveURL(/file=artifacts%2Fepisode%2Fo2.jpg/);
  expect(laterPages).toBe(0);
  expect(traces).toBe(0);
  await caption(page).getByRole("link").click();
  await expect(page.getByRole("tab", { name: "Run", exact: true })).toHaveAttribute("data-state", "active");
  await expect(page).toHaveURL(/tab=trajectory/);
});

test("Run returns from the trace with Back and reuses its viewer until the attempt changes", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await mockRun(page);
  let recordings = 0;
  page.on("request", request => { if (new URL(request.url()).pathname.endsWith("recording.rrd")) recordings++; });
  await page.route("**/api/trials/robot-0/trajectory", route => route.fulfill({ json: {
    schema_version: "ATIF-v1.7", session_id: "robot-session",
    agent: { name: "codex", version: "test", model_name: "test-model" },
    steps: [{ step_id: 8, source: "agent", message: "Move gripper", tool_calls: [] }], final_metrics: null,
  } }));
  await page.goto("/robot-recording");
  await expect(play(page)).toBeEnabled({ timeout: 60000 });
  await page.getByRole("button", { name: "Next observation", exact: true }).click();
  await expect(caption(page)).toContainText(command("o1"));
  await caption(page).getByRole("link").click();
  await expect(page).toHaveURL(/tab=trajectory.*observation=o1#step-8$/);
  await expect(page.getByText("Move gripper", { exact: true }).first()).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(/tab=playback/);
  await expect(page.getByTestId("observation-time")).toContainText("Frame 2 / 4");
  await page.goForward();
  await expect(page.getByText("Move gripper", { exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: "Scene and observations", exact: true }).click();
  await expect(page.getByTestId("observation-time")).toContainText("Frame 2 / 4");
  await expect(page.locator("canvas")).toHaveCount(1);
  expect(recordings).toBe(1);
  await page.getByRole("button", { name: "Next attempt", exact: true }).click();
  await expect(play(page)).toBeEnabled({ timeout: 60000 });
  await expect(page.locator("canvas")).toHaveCount(1);
  expect(recordings).toBe(2);
});

test("slow replay, seeking while playing, and completion share one clock", async ({ page }) => {
  await mockRun(page);
  await page.goto("/robot-recording?tab=playback");
  await expect(play(page)).toBeEnabled({ timeout: 60000 });
  await page.getByRole("combobox", { name: "Playback speed" }).selectOption("0.25");
  await play(page).click();
  await page.waitForTimeout(1000);
  await page.getByRole("button", { name: "Pause replay", exact: true }).click();
  const slowTime = Number(await slider(page).inputValue());
  expect(slowTime).toBeGreaterThan(0.4);
  expect(slowTime).toBeLessThan(0.9);
  await page.locator("canvas").press("Space");
  await expect(page.getByRole("button", { name: "Pause replay", exact: true })).toBeVisible();
  await page.locator("canvas").press("Space");
  await expect(play(page)).toBeVisible();
  await page.getByRole("combobox", { name: "Playback speed" }).selectOption("2");
  await play(page).click();
  await page.getByRole("button", { name: "Restart replay", exact: true }).click();
  await expect(play(page)).toBeVisible();
  await expect.poll(async () => Number(await slider(page).inputValue())).toBeCloseTo(0.3, 1);
  await page.waitForTimeout(200);
  await expect.poll(async () => Number(await slider(page).inputValue())).toBeCloseTo(0.3, 1);
  await play(page).click();
  await expect(play(page)).toBeVisible({ timeout: 10000 });
  await expect.poll(async () => Number(await slider(page).inputValue())).toBeCloseTo(2.7, 1);
  await play(page).click();
  await expect(page.getByRole("button", { name: "Pause replay", exact: true })).toBeVisible();
  await expect.poll(async () => Number(await slider(page).inputValue())).toBeLessThan(2.7);
});

test("agent observations are fixed images and the reviewer keeps camera navigation", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await mockRun(page);
  await page.goto("/robot-recording?tab=playback");
  await expect(play(page)).toBeEnabled({ timeout: 60000 });
  const image = page.getByRole("img", { name: "Agent view", exact: true });
  await expect(image).toHaveJSProperty("naturalWidth", 64);
  await expect(image).toHaveAttribute("draggable", "false");
  await expect(page.getByTestId("observation-time")).toHaveText("Frame 1 / 4 · Captured 0.3 s");
  await expect(page.getByRole("button", { name: "Data", exact: true })).toHaveCount(0);
  const reviewer = page.getByTestId("reviewer-scene");
  const camera = page.getByTestId("agent-observations");
  const beforeLayout = [await reviewer.boundingBox(), await camera.boundingBox()];
  await page.getByTestId("reviewer-heading").dragTo(camera);
  expect([await reviewer.boundingBox(), await camera.boundingBox()]).toEqual(beforeLayout);
  await page.getByRole("button", { name: "Next observation", exact: true }).click();
  await expect(page.getByTestId("observation-time")).toHaveText("Frame 2 / 4 · Captured 1.1 s");
  // Between observations the image and its timestamp stay on the recorded frame.
  await slider(page).fill("1.5");
  await expect(page.getByTestId("observation-time")).toHaveText("Frame 2 / 4 · Captured 1.1 s");
  const scene = beforeLayout[0]!;
  await page.mouse.move(0, 0);
  const beforeOrbit = await reviewer.screenshot();
  await page.mouse.move(scene.x + scene.width / 2, scene.y + scene.height / 2);
  await page.mouse.down();
  await page.mouse.move(scene.x + scene.width / 2 + 100, scene.y + scene.height / 2 + 40, { steps: 10 });
  await page.mouse.up();
  await page.mouse.move(0, 0);
  expect(await reviewer.screenshot()).not.toEqual(beforeOrbit);
  expect([await reviewer.boundingBox(), await camera.boundingBox()]).toEqual(beforeLayout);
  await page.locator("canvas").press("Delete");
  await page.locator("canvas").press("Control+p");
  await expect(page.getByTestId("reviewer-heading")).toBeVisible();
  await page.screenshot({ path: "test-results/robot-fixed-observations.png" });
  // Trial feedback stays in Overview; golden labeling lives in the admin queue.
  await page.getByRole("button", { name: "Show analysis", exact: true }).click();
  await page.getByRole("tab", { name: "Overview", exact: true }).click();
  await expect(page.getByText("Correct label", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "Label note", exact: true })).toHaveCount(0);
});

test("delayed observation decoding holds playback and preserves the visible image", async ({ page }) => {
  await mockRun(page);
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/o1.jpg{,?*}", async route => {
    await held;
    await route.fulfill({ path: path.resolve("e2e/fixtures/o1.jpg"), contentType: "image/jpeg" });
  });
  try {
    await page.goto("/robot-recording?tab=playback");
    await expect(play(page)).toBeEnabled({ timeout: 60000 });
    const image = page.getByRole("img", { name: "Agent view", exact: true });
    await expect(image).toHaveJSProperty("naturalWidth", 64);
    const original = await image.elementHandle();
    const source = await image.getAttribute("src");
    await page.getByRole("combobox", { name: "Playback speed" }).selectOption("0.5");
    await play(page).click();
    await expect(page.getByRole("status", { name: "", exact: true })).toHaveText("Buffering");
    const stoppedAt = Number(await slider(page).inputValue());
    expect(stoppedAt).toBeLessThanOrEqual(1.1); // Range input rounds to its 0.01 s step.
    await page.waitForTimeout(250);
    expect(Number(await slider(page).inputValue())).toBeCloseTo(stoppedAt, 2);
    await expect(image).toHaveAttribute("src", source!);
    await expect(page.getByTestId("observation-time")).toHaveText("Frame 1 / 4 · Captured 0.3 s");
    release();
    await expect(image).toHaveAttribute("data-frame-index", "1");
    await page.getByRole("button", { name: "Pause replay", exact: true }).click();
    await expect(page.getByTestId("observation-time")).toHaveText("Frame 2 / 4 · Captured 1.1 s");
    expect(await original!.evaluate(element => element.isConnected)).toBe(true);
  } finally { release(); }
});

test("late and failed image loads cannot replace the selected frame or advance its caption", async ({ page }) => {
  await mockRun(page);
  let releaseFirst!: () => void;
  let releaseSecond!: () => void;
  const first = new Promise<void>(resolve => { releaseFirst = resolve; });
  const second = new Promise<void>(resolve => { releaseSecond = resolve; });
  await page.route("**/o1.jpg{,?*}", async route => {
    await first;
    await route.fulfill({ path: path.resolve("e2e/fixtures/o1.jpg"), contentType: "image/jpeg" });
  });
  await page.route("**/o2.jpg{,?*}", async route => {
    await second;
    await route.fulfill({ path: path.resolve("e2e/fixtures/o2.jpg"), contentType: "image/jpeg" });
  });
  await page.route("**/o3.jpg{,?*}", route => route.fulfill({ status: 503 }));
  try {
    await page.goto("/robot-recording?tab=playback");
    await expect(play(page)).toBeEnabled({ timeout: 60000 });
    const image = page.getByRole("img", { name: "Agent view", exact: true });
    await page.getByRole("button", { name: "Actions", exact: true }).click();
    await page.getByRole("button", { name: "Close gripper", exact: false }).click();
    await expect(page.getByText("Buffering", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Move up; close", exact: false }).click();
    await expect(image).toHaveAttribute("data-frame-index", "0");
    releaseSecond();
    await expect(image).toHaveAttribute("data-frame-index", "2");
    await expect(page.getByTestId("observation-time")).toHaveText("Frame 3 / 4 · Captured 1.9 s");
    releaseFirst();
    await page.waitForTimeout(250);
    await expect(image).toHaveAttribute("data-frame-index", "2");
    await page.getByRole("button", { name: "After", exact: true }).click();
    await expect(page.getByRole("alert").filter({ hasText: "Frame unavailable" })).toBeVisible();
    await expect(image).toHaveAttribute("data-frame-index", "2");
    await expect(page.getByTestId("observation-time")).toHaveText("Frame 3 / 4 · Captured 1.9 s");
    await expect.poll(async () => Number(await slider(page).inputValue())).toBeCloseTo(1.9, 1);
  } finally { releaseFirst(); releaseSecond(); }
});

test("file inspection fits the image and command in a short viewport and supports keyboard navigation", async ({ page }) => {
  await page.setViewportSize({ width: 1100, height: 760 });
  await mockRun(page);
  await page.goto(`/robot-recording?tab=artifacts&file=${root}o1.jpg`);
  await expect(caption(page)).toContainText("Observation 2 of 4");
  await page.getByRole("button", { name: "Hide file list" }).click();
  await expect(page.getByRole("tree")).toHaveCount(0);
  await caption(page).focus();
  await page.keyboard.press("ArrowRight");
  await expect(caption(page)).toContainText("Observation 3 of 4");
  await expect(page).toHaveURL(/file=.*o2.jpg/);
  expect((await caption(page).boundingBox())!.y + (await caption(page).boundingBox())!.height).toBeLessThan(760);
  await page.getByRole("button", { name: "100%", exact: true }).click();
  await expect(page.getByRole("img", { name: "o2.jpg", exact: true })).toHaveCSS("width", "64px");
  await page.getByRole("button", { name: "Fit", exact: true }).click();
  await page.getByRole("combobox", { name: "File filter" }).selectOption("all");
  await expect(page.getByRole("img", { name: "o2.jpg", exact: true })).toBeVisible();
  await page.getByRole("combobox", { name: "File filter" }).selectOption("artifacts");
  await expect(caption(page)).toContainText("Observation 3 of 4");
  // A filter change does not remount the browser; the list stays hidden.
  await expect(page.getByRole("button", { name: "Show file list" })).toBeVisible();
  await page.screenshot({ path: "test-results/inspection-fit.png" });
});

test("legacy run links and filter changes keep their meaning", async ({ page }) => {
  await mockRun(page);
  await page.goto("/robot-recording?tab=run");
  await expect(page.getByRole("button", { name: "Scene and observations", exact: true })).toHaveClass(/secondary/);
  await expect(page).toHaveURL(/tab=playback/);
  await page.goto("/robot-recording?tab=files&file=agent/notes.txt");
  await page.getByRole("combobox", { name: "File filter" }).selectOption("artifacts");
  // An excluded selection is cleared, not replaced by another file.
  await expect(page.getByText("Select a file", { exact: true })).toBeVisible();
  await expect(page).not.toHaveURL(/file=/);
  await page.getByRole("treeitem", { name: "o2.jpg", exact: true }).click();
  await page.getByRole("combobox", { name: "File filter" }).selectOption("all");
  await expect(page.getByRole("img", { name: "o2.jpg", exact: true })).toBeVisible();
  await expect(page).toHaveURL(/tab=files&.*file=verifier%2Fartifacts%2Fo2.jpg/);
});

async function listResults(page: Page, pages: string[][]) {
  await page.route("**/api/trials/robot-0/files?**", route => {
    const cursor = Number(new URL(route.request().url()).searchParams.get("cursor") ?? 0);
    return route.fulfill({ json: {
      files: pages[cursor].map(p => ({ path: p })),
      cursor: cursor + 1 < pages.length ? String(cursor + 1) : null,
    } });
  });
  await page.route("**/api/trials/robot-0/files/**/*.txt", route => route.fulfill({
    body: `${route.request().url().split("/files/")[1].split("/")[0]} content`,
  }));
  await page.route("**/api/trials/robot-0/files/*.txt", route => route.fulfill({ status: 404 }));
}

test("colliding artifact names remain separate files", async ({ page }) => {
  await mockRun(page);
  await listResults(page, [["agent/artifacts/result.txt", "verifier/artifacts/result.txt"]]);
  await page.goto("/robot-recording?tab=artifacts&file=agent/artifacts/result.txt");
  await expect(page.getByText("agent content", { exact: true })).toBeVisible();
  await expect(page.getByText("2 files", { exact: true })).toBeVisible();
  await page.getByRole("treeitem", { name: "result.txt", exact: true }).last().click();
  await expect(page.getByText("verifier content", { exact: true })).toBeVisible();
});

test("a short link matching files on several listing pages asks which one", async ({ page }) => {
  await mockRun(page);
  await listResults(page, [["verifier/artifacts/result.txt"], ["agent/artifacts/result.txt"]]);
  await page.goto("/robot-recording?tab=artifacts&file=result.txt");
  await expect(page.getByText("Several files match this link. Choose one:")).toBeVisible();
  await page.getByRole("button", { name: "verifier/artifacts/result.txt", exact: true }).click();
  await expect(page.getByText("verifier content", { exact: true })).toBeVisible();
  await expect(page.getByRole("treeitem", { name: "result.txt", exact: true }).last()).toHaveAttribute("aria-selected", "true");
});

test("a unique short link resolves to its full path across listing pages", async ({ page }) => {
  await mockRun(page);
  await listResults(page, [["agent/trajectory.json"], ["verifier/artifacts/result.txt"]]);
  await page.goto("/robot-recording?tab=files&file=result.txt&lines=L1");
  await expect(page.getByText("verifier content", { exact: true })).toBeVisible();
  await expect(page).toHaveURL(/file=verifier%2Fartifacts%2Fresult.txt/);
  await expect(page).toHaveURL(/lines=L1/);
});

test("Files keeps earlier pages while loading the rest of a large listing", async ({ page }) => {
  await mockRun(page);
  await listResults(page, [["first/a.txt"], ["second/b.txt"]]);
  await page.goto("/robot-recording?tab=files&file=first/a.txt");
  await expect(page.getByText("first content", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Load more files" }).click();
  await expect(page.getByRole("treeitem", { name: "a.txt", exact: true })).toBeVisible();
  await page.getByRole("treeitem", { name: "b.txt", exact: true }).click();
  await expect(page.getByText("second content", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Load more files" })).toHaveCount(0);
});

test("raw recording files retain a usable viewer height in Files", async ({ page }) => {
  await page.setViewportSize({ width: 1200, height: 850 });
  await mockRun(page);
  await page.goto(`/robot-recording?tab=artifacts&file=${root}recording.rrd`);
  await expect(page.locator("canvas")).toBeVisible({ timeout: 60000 });
  await expect.poll(async () => (await page.locator("canvas").boundingBox())?.height ?? 0).toBeGreaterThan(250);
});

for (const change of [
  { button: "Change resume frame", index: "2", time: 1.9, paused: true },
  { button: "Refresh observations", index: null, time: null, paused: false },
]) {
  test(`a playing viewer is not recreated when ${change.button.toLowerCase()}`, async ({ page }) => {
    await mockRun(page);
    let recordings = 0;
    page.on("request", request => { if (new URL(request.url()).pathname.endsWith("recording.rrd")) recordings++; });
    await page.goto("/robot-recording/reset");
    const image = page.getByRole("img", { name: "Agent view", exact: true });
    await expect(play(page)).toBeEnabled({ timeout: 60000 });
    await page.getByRole("combobox", { name: "Playback speed" }).selectOption("0.5");
    await play(page).click();
    await expect.poll(async () => Number(await slider(page).inputValue())).toBeGreaterThan(0.3);
    await page.getByRole("button", { name: change.button, exact: true }).click();
    await expect(page.getByText("Loading recording…", { exact: true })).toHaveCount(0);
    if (change.paused) {
      await expect(play(page)).toBeVisible();
      await expect(image).toHaveAttribute("data-frame-index", change.index!);
      await expect.poll(async () => Number(await slider(page).inputValue())).toBeCloseTo(change.time!, 1);
    } else {
      const before = Number(await slider(page).inputValue());
      await expect.poll(async () => Number(await slider(page).inputValue())).toBeGreaterThan(before);
      await page.getByRole("button", { name: "Pause replay", exact: true }).click();
    }
    await expect(page.locator("canvas")).toHaveCount(1);
    expect(recordings).toBe(1);
  });
}

test("a replaced recording starts a fresh paused viewer and ignores the old one", async ({ page }) => {
  await mockRun(page);
  await page.goto("/robot-recording/reset");
  const image = page.getByRole("img", { name: "Agent view", exact: true });
  await expect(play(page)).toBeEnabled({ timeout: 60000 });
  const oldSource = await image.getAttribute("src");
  await page.getByRole("combobox", { name: "Playback speed" }).selectOption("0.5");
  await play(page).click();
  await expect.poll(async () => Number(await slider(page).inputValue())).toBeGreaterThan(0.3);
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/recording.rrd{,?*}", async route => {
    await held;
    await route.fulfill({ path: path.resolve("e2e/fixtures/robot-embedded-recording.rrd"), contentType: "application/octet-stream" });
  });
  try {
    await page.getByRole("button", { name: "Replace recording", exact: true }).click();
    await expect(page.getByText("Loading recording…", { exact: true })).toBeVisible();
    await expect(play(page)).toBeDisabled();
    await expect(image).toHaveCount(0);
    await expect.poll(async () => Number(await slider(page).inputValue())).toBeCloseTo(0.3, 1);
    await expect(page.locator("canvas")).toHaveCount(1);
  } finally { release(); }
  await expect(play(page)).toBeEnabled({ timeout: 60000 });
  await expect(image).toHaveAttribute("data-frame-index", "0");
  await expect(image).not.toHaveAttribute("src", oldSource!);
  await play(page).click();
  await expect.poll(async () => Number(await slider(page).inputValue())).toBeGreaterThan(0.4);
});

test("Escape closes the action menu before closing its drawer", async ({ page }) => {
  await page.goto("/robot-recording/actions");
  await page.getByRole("button", { name: "Trial actions" }).click();
  await expect(page.getByRole("menuitem", { name: "Retry trial" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menuitem", { name: "Retry trial" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Robot pick-and-place" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("heading", { name: "Robot pick-and-place" })).toHaveCount(0);
});

test("an unprepared hosted trial can prepare its saved recording and open playback", async ({ page }) => {
  await mockRun(page);
  let prepared = false;
  let writes = 0;
  await page.route("**/api/trials/robot-0", route => route.fulfill({ json: { ...trialRecord, playback: prepared ? ready : null } }));
  await page.route("**/api/experiments/robot-experiment/focus?**", route => route.fulfill({ json: focus({ ...trialRecord, playback: null }) }));
  await page.route("**/api/trials/robot-0/playback/rebuild", route => {
    expect(route.request().method()).toBe("POST");
    writes++;
    prepared = true;
    return route.fulfill({ json: ready });
  });
  await page.goto("/robot-recording/early?task=robot&trial=robot-0&tab=playback");
  await expect(page.getByRole("button", { name: "Prepare playback", exact: true })).toBeVisible();
  expect(writes).toBe(0);
  await page.getByRole("button", { name: "Prepare playback", exact: true }).click();
  await expect(play(page)).toBeEnabled({ timeout: 60000 });
  await expect(page.getByRole("img", { name: "Agent view", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Prepare playback", exact: true })).toHaveCount(0);
  expect(writes).toBe(1);
});

test("Files opens an embedded recording with native controls and links to its scene", async ({ page }) => {
  await mockRun(page);
  await page.goto(`/robot-recording?tab=files&file=${root}recording.rrd`);
  await expect(page.getByRole("button", { name: "Scene and observations", exact: true })).toBeVisible();
  await expect(page.getByLabel("Robot recording: recording.rrd").locator("canvas")).toBeVisible({ timeout: 60000 });
  await expect(page.getByText("Loading recording…", { exact: true })).toHaveCount(0);
  await page.screenshot({ path: "test-results/robot-raw-recording-controls.png" });
  await page.getByRole("button", { name: "Scene and observations", exact: true }).click();
  await expect(play(page)).toBeEnabled({ timeout: 60000 });
  await expect(page.getByRole("img", { name: "Agent view", exact: true })).toBeVisible();
});

for (const fails of [false, true]) {
  test(`a delayed initial image ${fails ? "failure" : "completion"} cannot overwrite a later selection`, async ({ page }) => {
    await mockRun(page);
    let release!: () => void;
    const held = new Promise<void>(resolve => { release = resolve; });
    await page.route("**/o0.jpg?*", async route => {
      await held;
      await route.fulfill(fails ? { status: 503 } : {
        path: path.resolve("e2e/fixtures/o0.jpg"), contentType: "image/jpeg",
      });
    });
    try {
      await page.goto("/robot-recording?tab=playback");
      await expect(play(page)).toBeEnabled({ timeout: 60000 });
      await page.getByRole("button", { name: "Next observation", exact: true }).click();
      const image = page.getByRole("img", { name: "Agent view", exact: true });
      await expect(image).toHaveAttribute("data-frame-index", "1");
      const response = page.waitForResponse(response => new URL(response.url()).pathname.endsWith("o0.jpg"));
      release();
      await response;
      await page.waitForTimeout(250);
      await expect(image).toHaveAttribute("data-frame-index", "1");
      await expect(page.getByTestId("observation-time")).toHaveText("Frame 2 / 4 · Captured 1.1 s");
      await expect(slider(page)).toHaveValue("1.1");
      await expect(caption(page)).toContainText(command("o1"));
      await expect(page.getByText("Frame unavailable", { exact: true })).toHaveCount(0);
    } finally { release(); }
  });
}

test("an empty artifact filter keeps the toolbar and can return to all files", async ({ page }) => {
  await mockRun(page);
  await listResults(page, [["agent/log.txt"]]);
  await page.goto("/robot-recording?tab=files");
  await expect(page.getByText("agent content", { exact: true })).toBeVisible();
  const filter = page.getByRole("combobox", { name: "File filter" });
  await filter.selectOption("artifacts");
  await expect(page.getByText("No artifacts", { exact: true })).toBeVisible();
  await expect(page).not.toHaveURL(/file=|lines=/);
  await expect(page.getByRole("button", { name: "Hide file list" })).toBeVisible();
  await filter.selectOption("all");
  await page.getByRole("treeitem", { name: "log.txt", exact: true }).click();
  await expect(page.getByText("agent content", { exact: true })).toBeVisible();
});

for (const ambiguous of [false, true]) {
  test(`${ambiguous ? "An ambiguous" : "A unique"} legacy step link resolves across listing pages and preserves lines`, async ({ page }) => {
    await mockRun(page);
    const first = "trial/steps/setup/artifacts/result.txt";
    const second = "other/steps/setup/artifacts/result.txt";
    await listResults(page, [["agent/log.txt"], [first], ambiguous ? [first, second] : [first]]);
    await page.route("**/files/setup/result.txt", route => route.fulfill({ status: 404 }));
    await page.goto("/robot-recording?tab=artifacts&file=setup/result.txt&lines=L1");
    if (ambiguous) {
      await expect(page.getByText("Several files match this link. Choose one:")).toBeVisible();
      expect(new URL(page.url()).searchParams.get("file")).toBe("setup/result.txt");
      await expect(page.getByRole("button", { name: first, exact: true })).toHaveCount(1);
      await page.getByRole("button", { name: second, exact: true }).click();
    }
    await expect.poll(() => new URL(page.url()).searchParams.get("file")).toBe(ambiguous ? second : first);
    await expect(page).toHaveURL(/lines=L1/);
    await expect(page.getByText(`${ambiguous ? "other" : "trial"} content`, { exact: true })).toBeVisible();
  });
}

test("file history can clear selection and restore it without extra unlisted-file downloads", async ({ page }) => {
  await mockRun(page);
  await listResults(page, [["agent/log.txt"]]);
  let downloads = 0;
  await page.route("**/files/unlisted/note.txt", route => {
    downloads++;
    return route.fulfill({ body: "unlisted content" });
  });
  await page.goto("/robot-recording?tab=files&file=unlisted/note.txt&lines=L1");
  await expect(page.getByText("unlisted content", { exact: true })).toBeVisible();
  const loaded = downloads; // Includes the existence probe and content read.
  await page.getByRole("button", { name: "Hide file list" }).click();
  await page.getByRole("button", { name: "Show file list" }).click();
  await page.waitForTimeout(250);
  expect(downloads).toBe(loaded);
  await page.evaluate(() => {
    const cleared = new URL(window.location.href);
    cleared.searchParams.delete("file");
    cleared.searchParams.delete("lines");
    window.history.pushState(window.history.state, "", cleared);
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await expect(page.getByText("Select a file", { exact: true })).toBeVisible();
  await expect(page.getByText("unlisted content", { exact: true })).toHaveCount(0);
  await page.goBack();
  await expect(page.getByText("unlisted content", { exact: true })).toBeVisible();
  await expect(page).toHaveURL(/lines=L1/);
  await page.goForward();
  await expect(page.getByText("Select a file", { exact: true })).toBeVisible();
  await expect(page).not.toHaveURL(/file=/);
  await page.getByRole("treeitem", { name: "log.txt", exact: true }).click();
  await expect(page.getByText("agent content", { exact: true })).toBeVisible();
  await page.evaluate(() => {
    const cleared = new URL(window.location.href);
    cleared.searchParams.delete("file");
    window.history.pushState(window.history.state, "", cleared);
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await expect(page.getByText("Select a file", { exact: true })).toBeVisible();
  await expect(page.getByRole("treeitem", { name: "log.txt", exact: true })).not.toHaveAttribute("aria-selected", "true");
  await page.getByRole("treeitem", { name: "log.txt", exact: true }).click();
  await expect(page.getByText("agent content", { exact: true })).toBeVisible();
});

test("Back from a trace returns to the departure frame even when the initial link named another frame", async ({ page }) => {
  await mockRun(page);
  await page.route("**/api/trials/robot-0/trajectory", route => route.fulfill({ json: {
    schema_version: "ATIF-v1.7", session_id: "robot-session",
    agent: { name: "codex", version: "test", model_name: "test-model" },
    steps: [{ step_id: 12, source: "agent", message: "Departure frame command", tool_calls: [] }], final_metrics: null,
  } }));
  await page.goto("/robot-recording?tab=playback&observation=o1&keep=me");
  await expect(page.getByTestId("observation-time")).toContainText("Frame 2 / 4");
  await page.evaluate(() => window.history.replaceState({ ...window.history.state, fixture: "preserved" }, ""));
  await page.getByRole("button", { name: "Next observation", exact: true }).click();
  await expect(page.getByTestId("observation-time")).toContainText("Frame 3 / 4");
  await caption(page).getByRole("link").click();
  await expect(page).toHaveURL(/tab=trajectory.*observation=o2.*#step-/);
  await expect(page.getByText("Departure frame command", { exact: true }).first()).toBeVisible();
  const traceUrl = new URL(page.url());
  traceUrl.hash = ""; // TrajectoryViewer consumes step anchors when it opens them.
  await page.goBack();
  await expect(page).toHaveURL(/tab=playback.*observation=o2/);
  await expect(page.getByTestId("observation-time")).toContainText("Frame 3 / 4");
  await expect(slider(page)).toHaveValue("1.9");
  expect(new URL(page.url()).searchParams.get("keep")).toBe("me");
  expect(await page.evaluate(() => window.history.state.fixture)).toBe("preserved");
  await page.goForward();
  await expect(page).toHaveURL(traceUrl.toString());
  await expect(page.getByText("Departure frame command", { exact: true }).first()).toBeVisible();
  expect(await page.evaluate(() => window.history.state.fixture)).toBe("preserved");
});

test("an index download can be retried without rebuilding playback", async ({ page }) => {
  await mockRun(page);
  let requests = 0;
  let rebuilds = 0;
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route(`**/${indexPath}?*`, async route => {
    requests++;
    if (requests === 1) return route.fulfill({ status: 503, body: "temporarily unavailable" });
    await held;
    return route.fallback();
  });
  page.on("request", request => { if (request.url().includes("/playback/rebuild")) rebuilds++; });
  try {
    await page.goto("/robot-recording?tab=playback");
    const retry = page.getByRole("button", { name: "Retry loading playback" });
    await expect(retry).toBeEnabled();
    await retry.click();
    await expect(retry).toBeDisabled();
    expect(requests).toBe(2);
    release();
    await expect(play(page)).toBeEnabled({ timeout: 60000 });
    expect(rebuilds).toBe(0);
    expect(requests).toBe(2);
  } finally { release(); }
});

for (const width of [64, 640, 8, 16000]) {
  test(`relative zoom starts from Fit for a ${width}px image and respects limits after resize`, async ({ page }) => {
    await page.setViewportSize({ width: 1100, height: 760 });
    await mockRun(page);
    await page.route("**/files/verifier/artifacts/o1.jpg", route => route.fulfill({
      contentType: "image/svg+xml", body: `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${width * 0.75}"><rect width="100%" height="100%" fill="blue"/></svg>`,
    }));
    await page.goto(`/robot-recording?tab=artifacts&file=${root}o1.jpg`);
    const image = page.getByRole("img", { name: "o1.jpg", exact: true });
    await expect(image).toHaveJSProperty("naturalWidth", width);
    const fittedScale = () => image.evaluate(img => {
      const node = img as HTMLImageElement;
      return Math.min(node.clientWidth / node.naturalWidth, node.clientHeight / node.naturalHeight);
    });
    for (const height of [760, 1000]) {
      await page.setViewportSize({ width: 1100, height });
      await page.getByRole("button", { name: "Fit", exact: true }).click();
      await page.waitForTimeout(100); // ResizeObserver has to measure the new content box.
      const fit = await fittedScale();
      const zoomIn = page.getByRole("button", { name: "Zoom in", exact: true });
      const zoomOut = page.getByRole("button", { name: "Zoom out", exact: true });
      if (fit >= 4) await expect(zoomIn).toBeDisabled();
      else {
        await zoomIn.click();
        await expect.poll(() => image.evaluate(img => img.clientWidth)).toBeCloseTo(width * Math.max(0.25, Math.min(4, fit * 1.25)), -1);
      }
      await page.getByRole("button", { name: "Fit", exact: true }).click();
      if (fit <= 0.25) await expect(zoomOut).toBeDisabled();
      else {
        await zoomOut.click();
        await expect.poll(() => image.evaluate(img => img.clientWidth)).toBeCloseTo(width * Math.min(4, Math.max(0.25, fit / 1.25)), -1);
      }
    }
  });
}

test("a new revision loads replaced asset bytes with the browser HTTP cache enabled", async ({ page, request }) => {
  // No page.route/context.route in this test: those would disable HTTP caching.
  await request.post("/cache-playback/control", { data: "cache-v1" });
  const assets = ["recording.rrd", "outcome.json", "o0.jpg"];
  const bodies = new Map<string, Buffer>();
  const pending: Promise<void>[] = [];
  page.on("response", response => {
    const url = new URL(response.url());
    const name = url.pathname.split("/").at(-1)!;
    if (url.pathname.startsWith("/cache-playback/") && assets.includes(name) && name !== "o0.jpg") {
      pending.push(response.body().then(body => { bodies.set(`${url.searchParams.get("revision")}/${name}`, body); }));
    }
  });
  await page.goto("/robot-recording/cache");
  await expect(play(page)).toBeEnabled({ timeout: 60000 });
  const image = page.getByRole("img", { name: "Agent view", exact: true });
  await expect(image).toHaveAttribute("data-frame-index", "0");
  const firstSource = await image.getAttribute("src");
  // Chromium's response.body can be empty after fetch().blob(); inspect the
  // actual blob used by the displayed image instead.
  const imageBytes = async () => Buffer.from(await image.evaluate(async element => {
    const response = await fetch((element as HTMLImageElement).src);
    return Array.from(new Uint8Array(await response.arrayBuffer()));
  }));
  bodies.set("cache-v1/o0.jpg", await imageBytes());
  await page.getByText("Final checks", { exact: true }).click();
  await expect(page.getByText("Failed: ", { exact: true })).toHaveCount(episode.checks.length);
  await expect.poll(() => assets.every(name => bodies.has(`cache-v1/${name}`))).toBe(true);
  await page.getByRole("button", { name: "Publish new revision" }).click();
  await expect(image).toHaveAttribute("data-frame-index", "0");
  await expect(image).not.toHaveAttribute("src", firstSource!);
  bodies.set("cache-v2/o0.jpg", await imageBytes());
  await expect.poll(() => assets.every(name => bodies.has(`cache-v2/${name}`))).toBe(true);
  await expect(page.getByText("Passed: ", { exact: true })).toHaveCount(episode.checks.length);
  await expect(play(page)).toBeEnabled({ timeout: 60000 });
  await Promise.all(pending);
  for (const name of assets) {
    expect(bodies.get(`cache-v1/${name}`)!.length, name).toBeGreaterThan(0);
    expect(bodies.get(`cache-v2/${name}`), name).not.toEqual(bodies.get(`cache-v1/${name}`));
    // The old URL still returns its cached bytes even though storage now serves v2.
    const cached = await page.evaluate(async name => {
      const response = await fetch(`/cache-playback/verifier/artifacts/${name}?revision=cache-v1`);
      return Array.from(new Uint8Array(await response.arrayBuffer()));
    }, name);
    expect(Buffer.from(cached)).toEqual(bodies.get(`cache-v1/${name}`));
  }
});

for (const [tab, directory] of [["files", root], ["artifacts", collected]]) {
  test(`Files trace navigation preserves the ${tab} departure without reloading`, async ({ page }) => {
    await mockRun(page);
    await page.route("**/api/trials/robot-0/trajectory", route => route.fulfill({ json: {
      schema_version: "ATIF-v1.7", session_id: "robot-session",
      agent: { name: "codex", version: "test", model_name: "test-model" },
      steps: [{ step_id: 12, source: "agent", message: "Selected frame command", tool_calls: [] }], final_metrics: null,
    } }));
    await page.goto(`/robot-recording?tab=${tab}&file=${directory}o1.jpg&keep=me`);
    await expect(caption(page)).toContainText("Observation 2 of 4");
    await page.getByRole("button", { name: "Next observation image" }).click();
    await expect(caption(page)).toContainText("Observation 3 of 4");
    await expect.poll(() => new URL(page.url()).searchParams.get("file")).toBe(directory + "o2.jpg");
    const departureUrl = page.url();
    await page.evaluate(() => {
      document.documentElement.dataset.traceNavigation = "same-document";
      window.history.replaceState({ ...window.history.state, fixture: "preserved" }, "");
    });
    const link = caption(page).getByRole("link");
    await expect(link).toHaveAttribute("href", /tab=trajectory.*observation=o2#step-12$/);
    // Observe the event after React's handler, then suppress the browser's
    // native action: headless Chromium does not consistently open modifier tabs.
    await page.evaluate(() => document.addEventListener("click", event => {
      document.documentElement.dataset.modifiedClickPrevented = String(event.defaultPrevented);
      event.preventDefault();
    }, { once: true }));
    await link.click({ modifiers: ["ControlOrMeta"] });
    await expect(page.locator("html")).toHaveAttribute("data-modified-click-prevented", "false");
    await expect(page).toHaveURL(departureUrl);
    await expect(page.getByRole("tab", { name: "Files", exact: true })).toHaveAttribute("data-state", "active");
    await link.click();
    await expect(page.getByRole("tab", { name: "Run", exact: true })).toHaveAttribute("data-state", "active");
    await expect(page.getByText("Selected frame command", { exact: true }).first()).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("data-trace-navigation", "same-document");
    await expect(page).toHaveURL(/tab=trajectory/);
    expect(new URL(page.url()).searchParams.get("observation")).toBe("o2");
    expect(new URL(page.url()).searchParams.has("file")).toBe(false);
    await page.goBack();
    await expect(page).toHaveURL(departureUrl);
    await expect(page.getByRole("tab", { name: "Files", exact: true })).toHaveAttribute("data-state", "active");
    await expect(page.getByRole("combobox", { name: "File filter" })).toHaveValue(tab === "files" ? "all" : "artifacts");
    await expect(caption(page)).toContainText("Observation 3 of 4");
    await expect(page.getByRole("img", { name: "o2.jpg", exact: true })).toBeVisible();
    expect(await page.evaluate(() => window.history.state.fixture)).toBe("preserved");
    await page.goForward();
    await expect(page.getByRole("tab", { name: "Run", exact: true })).toHaveAttribute("data-state", "active");
    await expect(page.getByText("Selected frame command", { exact: true }).first()).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("data-trace-navigation", "same-document");
    expect(new URL(page.url()).searchParams.get("keep")).toBe("me");
  });
}

test("seeks commit without viewer events and ignore older events delivered last", async ({ page }) => {
  await mockRun(page);
  await page.goto("/robot-recording/stale-events");
  await expect(play(page)).toBeEnabled({ timeout: 60000 });
  const image = page.getByRole("img", { name: "Agent view", exact: true });
  await expect(image).toHaveAttribute("data-frame-index", "0");
  await page.getByRole("button", { name: "Next observation", exact: true }).click();
  await expect(slider(page)).toHaveValue("1.1");
  await expect.poll(async () => Number(await page.getByTestId("pending-viewer-events").textContent()))
    .toBeGreaterThan(0);
  await expect(image).toHaveAttribute("data-frame-index", "1");
  await expect(caption(page)).toContainText(command("o1"));
  await page.getByRole("button", { name: "Next observation", exact: true }).click();
  await expect(slider(page)).toHaveValue("1.9");
  await expect(image).toHaveAttribute("data-frame-index", "2");
  await expect(caption(page)).toContainText(command("o2"));
  await expect.poll(async () => Number(await page.getByTestId("pending-viewer-events").textContent()))
    .toBeGreaterThan(1);
  await page.getByRole("button", { name: "Deliver viewer events in reverse" }).click();
  await expect(slider(page)).toHaveValue("1.9");
  await expect(image).toHaveAttribute("data-frame-index", "2");
  await expect(caption(page)).toContainText(command("o2"));
  await play(page).click();
  await expect(slider(page)).toHaveValue("2.7");
  await expect(image).toHaveAttribute("data-frame-index", "3");
  await expect(play(page)).toBeVisible();
});
