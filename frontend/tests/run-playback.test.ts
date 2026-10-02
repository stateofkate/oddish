import assert from "node:assert/strict";
import test from "node:test";

import {
  decodeDrawerLocation,
  drawerTabParam,
  parsePlaybackIndex,
  resolveDrawerView,
} from "../src/lib/run-playback.ts";

const decode = (query: string) => decodeDrawerLocation(new URLSearchParams(query));

test("existing tab values decode to sections, Run views and file filters", () => {
  assert.deepEqual(
    [
      "summary", "playback", "trajectory", "live", "run", "files", "artifacts",
    ].map(tab => {
      const { section, runView, fileFilter } = decode(`tab=${tab}`);
      return [tab, section, runView, fileFilter];
    }),
    [
      ["summary", "overview", null, "all"],
      ["playback", "run", "scene", "all"],
      ["trajectory", "run", "trace", "all"],
      ["live", "run", "live", "all"],
      ["run", "run", null, "all"],
      ["files", "files", null, "all"],
      ["artifacts", "files", null, "artifacts"],
    ]
  );
});

test("file, lines and observation are carried; a file without a tab opens Files", () => {
  assert.deepEqual(decode("file=a/b.txt&lines=L3-L4"), {
    section: "files", runView: null, fileFilter: "all",
    filePath: "a/b.txt", lines: "L3-L4", observationId: null,
  });
  assert.equal(decode("tab=trajectory&observation=o4").observationId, "o4");
  assert.equal(decode("tab=unknown").section, null);
});

test("every resolved location writes back an existing tab value", () => {
  for (const tab of ["summary", "playback", "trajectory", "live", "files", "artifacts"]) {
    const location = decode(`tab=${tab}`);
    const view = resolveDrawerView(location, { running: true, playback: true })!;
    assert.equal(drawerTabParam(view.section, view.runView, location.fileFilter), tab);
  }
});

test("defaults: Live while running, Scene with playback, otherwise Overview", () => {
  const none = decode("");
  assert.deepEqual(resolveDrawerView(none, { running: true, playback: undefined }), { section: "run", runView: "live" });
  assert.deepEqual(resolveDrawerView(none, { running: false, playback: true }), { section: "run", runView: "scene" });
  assert.equal(resolveDrawerView(none, { running: false, playback: false })!.section, "overview");
  // Discovery still pending: no default yet.
  assert.equal(resolveDrawerView(none, { running: false, playback: undefined }), null);
  // An explicit selection wins over every default.
  assert.equal(resolveDrawerView(decode("tab=summary"), { running: false, playback: undefined })!.section, "overview");
});

test("tab=run picks Live, then Scene, then Trace; Live falls back once finished", () => {
  const run = decode("tab=run");
  assert.equal(resolveDrawerView(run, { running: true, playback: false })!.runView, "live");
  assert.equal(resolveDrawerView(run, { running: false, playback: true })!.runView, "scene");
  assert.equal(resolveDrawerView(run, { running: false, playback: false })!.runView, "trace");
  assert.equal(resolveDrawerView(decode("tab=live"), { running: false, playback: true })!.runView, "trace");
});

test("an index must match the descriptor's revision and carry a valid manifest", () => {
  const manifest = {
    version: 2, title: "t", goal: "g", producer: "verifier", recording: "r.rrd",
    outcome: "o.json", timeline: "simulation", duration_seconds: 1, checks: [],
    start_time: 0, observations: [{ id: "o0", time: 0, image: "o0.jpg" }], actions: [],
  };
  const index = {
    version: 1, manifest_path: "verifier/artifacts/playback.json", manifest,
    commands: {}, observation_paths: {}, asset_hashes: {}, revision: "abc",
  };
  assert.equal(parsePlaybackIndex(index, "abc").revision, "abc");
  assert.throws(() => parsePlaybackIndex(index, "other"), /Unsupported playback index/);
  assert.throws(
    () => parsePlaybackIndex({ ...index, manifest: { ...manifest, recording: "../x.rrd" } }, "abc"),
    /invalid playback description/
  );
  assert.throws(
    () => parsePlaybackIndex({ ...index, manifest: { ...manifest, version: 1 } }, "abc"),
    /invalid playback description/
  );
});
