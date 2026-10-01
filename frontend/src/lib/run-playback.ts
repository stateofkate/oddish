// Pure playback types, validation and drawer-location decoding. No runtime
// imports: unit tests load this file directly.

interface PlaybackDescription {
  title: string;
  goal: string;
  producer: "verifier";
  recording: string;
  outcome: string;
  timeline: string;
  duration_seconds: number;
  checks: { key: string; label: string }[];
  /** Present when the recording embeds its own scene-only Rerun layout. */
  viewer_layout?: "embedded_scene";
}

export type PlaybackManifest = PlaybackDescription & {
  version: 2;
  start_time: number;
  observations: { id: string; time: number; image: string }[];
  actions: { id: string; time: number; end_time: number; label: string;
    observation_id: string; result_observation_id: string }[];
};

export type PlaybackObservation = PlaybackManifest["observations"][number];

/** The tool invocation that returned an observation, copied from the trace. */
export interface RecordedCommand {
  step_id: number | null;
  tool_call_id: string;
  function_name: string;
  arguments: Record<string, unknown> | string;
}

/** `_oddish/playback-index/<revision>.json`, written by `oddish.core.playback`. */
export interface PlaybackIndex {
  version: 1;
  manifest_path: string;
  manifest: PlaybackManifest;
  commands: Record<string, RecordedCommand | null>;
  observation_paths: Record<string, string>;
  asset_hashes: Record<string, string>;
  revision: string;
}

/** `trials.result._playback`, written when the trial's artifacts are stored. */
export interface TrialPlaybackDescriptor {
  manifest_path: string | null;
  index_status?: "ready" | "absent" | "invalid" | null;
  index_path?: string | null;
  index_revision?: string | null;
  index_error?: string | null;
}

export function runFileUrl(filesUrl: string, path: string) {
  return `${filesUrl}/${path.split("/").map(encodeURIComponent).join("/")}`;
}

// These files are supplied by tasks. Validate the version and references before
// presenting them as playback; references must stay beside the verifier manifest.
export function parsePlayback(value: unknown): PlaybackManifest {
  const m = value as PlaybackManifest | null;
  const sibling = (p: unknown) =>
    typeof p === "string" && /^[\w.-]+$/.test(p) && p !== "." && p !== "..";
  if (
    !m ||
    m.version !== 2 ||
    m.producer !== "verifier" ||
    typeof m.title !== "string" ||
    typeof m.goal !== "string" ||
    typeof m.timeline !== "string" ||
    !m.timeline ||
    !sibling(m.recording) ||
    !m.recording.endsWith(".rrd") ||
    !sibling(m.outcome) ||
    !Number.isFinite(m.duration_seconds) ||
    m.duration_seconds < 0 ||
    !Array.isArray(m.checks) ||
    !m.checks.every(
      (c) => c && typeof c.key === "string" && typeof c.label === "string"
    ) ||
    (m.viewer_layout !== undefined && m.viewer_layout !== "embedded_scene")
  ) {
    throw new Error("Unsupported or invalid playback description");
  }
  const time = (t: number) => Number.isFinite(t) && t >= 0 && t <= m.duration_seconds;
  if (!time(m.start_time) || !Array.isArray(m.observations) || !m.observations.length ||
    !m.observations.every((o, i) => o && typeof o.id === "string" && o.id && time(o.time) &&
      sibling(o.image) && o.image.endsWith(".jpg") &&
      (i === 0 ? o.time === m.start_time : o.time >= m.observations[i - 1].time)) ||
    new Set(m.observations.map(o => o.id)).size !== m.observations.length ||
    !Array.isArray(m.actions) || !m.actions.every((a, i) => {
      const before = m.observations.find(o => o.id === a?.observation_id);
      const after = m.observations.find(o => o.id === a?.result_observation_id);
      return a && typeof a.id === "string" && a.id && typeof a.label === "string" &&
        time(a.time) && time(a.end_time) && a.end_time > a.time &&
        before?.time === a.time && after?.time === a.end_time &&
        (i === 0 || a.time >= m.actions[i - 1].end_time);
    }) || new Set(m.actions.map(a => a.id)).size !== m.actions.length)
    throw new Error("Invalid action and observation links");
  return m;
}

/** Check the index the server wrote before trusting its paths. */
export function parsePlaybackIndex(value: unknown, revision: string): PlaybackIndex {
  const index = value as PlaybackIndex | null;
  if (
    !index ||
    index.version !== 1 ||
    index.revision !== revision ||
    typeof index.manifest_path !== "string" ||
    !index.manifest_path.endsWith("playback.json") ||
    typeof index.commands !== "object" || index.commands === null ||
    typeof index.observation_paths !== "object" || index.observation_paths === null
  )
    throw new Error("Unsupported playback index");
  parsePlayback(index.manifest);
  return index;
}

/** Display names for the stable codes the indexer stores in `index_error`. */
export function playbackIndexError(code: string | null | undefined): string {
  switch (code) {
    case "invalid_manifest":
      return "This run's playback description is invalid.";
    case "missing_reference":
      return "This run's playback references a missing recording, result, or image.";
    case "unreadable_artifacts":
      return "This run's playback files could not be read.";
    case "index_write_failed":
      return "This run's playback index could not be saved.";
    default:
      return "This run's playback could not be prepared.";
  }
}

// ---------------------------------------------------------------------------
// Drawer location
// ---------------------------------------------------------------------------

export type DrawerSection = "overview" | "run" | "files";
export type RunView = "scene" | "trace" | "live";

/** Where the trial drawer is pointed, decoded once from its URL. */
export interface DrawerLocation {
  /** Null when the URL names no section; defaults then decide. */
  section: DrawerSection | null;
  /** Null inside Run when the URL asks for "run" without a subview. */
  runView: RunView | null;
  fileFilter: "all" | "artifacts";
  filePath: string | null;
  lines: string | null;
  observationId: string | null;
}

const TAB_LOCATIONS: Record<string, Pick<DrawerLocation, "section" | "runView" | "fileFilter">> = {
  summary: { section: "overview", runView: null, fileFilter: "all" },
  playback: { section: "run", runView: "scene", fileFilter: "all" },
  trajectory: { section: "run", runView: "trace", fileFilter: "all" },
  live: { section: "run", runView: "live", fileFilter: "all" },
  run: { section: "run", runView: null, fileFilter: "all" },
  files: { section: "files", runView: null, fileFilter: "all" },
  artifacts: { section: "files", runView: null, fileFilter: "artifacts" },
};

/** Decode the drawer's existing `tab`, `file`, `lines` and `observation` params. */
export function decodeDrawerLocation(params: URLSearchParams): DrawerLocation {
  const tab = TAB_LOCATIONS[params.get("tab") ?? ""];
  const filePath = params.get("file") || null;
  // A file link without a tab has always opened the file browser.
  const base = tab ?? (filePath ? TAB_LOCATIONS.files : { section: null, runView: null, fileFilter: "all" as const });
  return {
    ...base,
    filePath,
    lines: params.get("lines") || null,
    observationId: params.get("observation") || null,
  };
}

/** The legacy `tab` value that addresses a resolved location. */
export function drawerTabParam(
  section: DrawerSection,
  runView: RunView,
  fileFilter: "all" | "artifacts"
): string {
  if (section === "overview") return "summary";
  if (section === "files") return fileFilter === "artifacts" ? "artifacts" : "files";
  return runView === "scene" ? "playback" : runView === "trace" ? "trajectory" : "live";
}

/** Facts about the trial that resolve a location without an explicit view. */
export interface DrawerDefaults {
  running: boolean;
  /** Ready, invalid, or discovered playback; undefined while still discovering. */
  playback: boolean | undefined;
}

/**
 * The section and Run view to show. Explicit URL selections win; otherwise a
 * running trial opens Live, a terminal trial with playback opens its Scene,
 * and everything else opens Overview. Null means discovery is still pending.
 */
export function resolveDrawerView(
  location: Pick<DrawerLocation, "section" | "runView">,
  facts: DrawerDefaults
): { section: DrawerSection; runView: RunView } | null {
  const runDefault = (): RunView | null =>
    facts.running ? "live" : facts.playback === undefined ? null : facts.playback ? "scene" : "trace";
  if (location.section === "run") {
    const view = location.runView === "live" && !facts.running ? "trace" : location.runView ?? runDefault();
    return view ? { section: "run", runView: view } : null;
  }
  if (location.section) return { section: location.section, runView: runDefault() ?? "trace" };
  if (facts.running) return { section: "run", runView: "live" };
  if (facts.playback === undefined) return null;
  return facts.playback ? { section: "run", runView: "scene" } : { section: "overview", runView: "trace" };
}
