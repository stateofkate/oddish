// The QA Evals page (the admin panel's QA Evals tab): the fields it reads
// from the backend's /qa-evals and /qa-goldens responses (models in
// oddish/src/oddish/schemas.py), its URL state and its API URLs. The URL
// keys are the API's and the CLI's own names; `oddish qa` prints the same
// links.

import {
  DASHBOARD_DEFAULT_EXPERIMENTS_LIMIT,
  buildDashboardApiPath,
} from "./dashboard-request.ts";
import { parseTaskSearch } from "./tag-query.ts";
import type { AnalysisClassification } from "./types.ts";

// The default of each list comes first.
export const QA_STAGES = ["post_trial", "pre_trial"] as const;
export type QAStage = (typeof QA_STAGES)[number];

/** Each case's one group, in display order. `group` is also the CLI's
 * --filter value; `name` labels the group's chip and its section. */
export const QA_EVAL_GROUPS = [
  {
    group: "wrong",
    name: "wrong",
    title: "Wrong (including invalid output)",
    tone: "text-red-600",
  },
  { group: "right", name: "right", title: "Right", tone: "text-emerald-600" },
  {
    group: "not_judged",
    name: "not judged",
    title: "Not judged: errored or running",
    tone: "text-amber-600",
  },
  {
    group: "no_golden",
    name: "no golden",
    title: "No golden label",
    tone: "text-[color:var(--paper-ink-3)]",
  },
] as const;
export type QAEvalGroup = (typeof QA_EVAL_GROUPS)[number]["group"];
export type QAEvalOutcome =
  | "right"
  | "wrong"
  | "invalid"
  | "errored"
  | "pending"
  | "off";

export type QAGoldenState = "counted" | "waiting" | "stale";
export const QA_GOLDEN_REVIEWS = ["waiting", "stale"] as const;
export type QAGoldenReview = (typeof QA_GOLDEN_REVIEWS)[number];
/** The labeling queue's groups: labeled once a trial's golden counts, else
 * failed or passed by reward. */
export const QA_QUEUE_GROUPS = ["failed", "passed", "labeled"] as const;
export type QAQueueGroup = (typeof QA_QUEUE_GROUPS)[number];

export const QA_EVALS_CHARTS = ["history", "models"] as const;
export type QAEvalsChart = (typeof QA_EVALS_CHARTS)[number];

/** One replay run scored against the counted goldens. */
export interface QAEvalRun {
  experiment_id: string;
  experiment_name: string;
  title: string;
  created_at: string;
  model: string | null;
  model_name: string;
  agent?: string;
  reasoning_effort?: string | null;
  configuration_sha256?: string | null;
  golden_set_sha256?: string | null;
  preflight?: boolean;
  preflight_passed?: boolean;
  prompt_sha256: string;
  subset: boolean;
  /** Full and scored: plotted, and in the frontier and models. */
  charted: boolean;
  replays: number;
  /** The counts below are over replays whose trial has a counted golden. */
  with_golden: number;
  judged: number;
  correct: number;
  errored: number;
  /** All unfinished replays, including those without a counted golden. */
  pending: number;
  accuracy: number | null;
  cost_usd: number;
  cost_per_trial: number | null;
}

export interface QAGoldensSummary {
  counted: number;
  waiting: number;
  stale: number;
  labels: { label: string; count: number }[];
}

export interface QAPromptVersion {
  sha256: string;
  name: string;
  author: string | null;
  first_seen_at: string;
  git_commit: string | null;
  text_available: boolean;
  /** The default diff base. */
  previous_sha256: string | null;
}

export interface QAEvalStatus {
  execution_configurable?: boolean;
  replayable: boolean;
  production_prompt_sha256: string;
  goldens: QAGoldensSummary;
  cost_per_trial: number | null;
  spend: {
    total_usd: number;
    trials: number;
    cost_missing: number;
    by_model: { model_name: string; cost_usd: number }[];
  };
  /** Oldest first. */
  runs: QAEvalRun[];
  /** Run ids, cheapest first. */
  frontier: string[];
  models: { model_name: string; runs: number; latest_run_id: string }[];
  /** Oldest first. */
  versions: QAPromptVersion[];
}

export interface QAEvalCase {
  qa_eval_trial_id: string;
  task_id: string;
  source_trial_id: string;
  /** The trial goldens key on: the source trial, or the original it copies. */
  original_trial_id: string;
  group: QAEvalGroup;
  outcome: QAEvalOutcome;
  golden_id: string | null;
  expected: string | null;
  got: string | null;
  cost_usd: number | null;
  error: string | null;
}

export interface QAEvalRunDetail {
  summary: QAEvalRun;
  error_groups: { message: string; count: number }[];
  cases: QAEvalCase[];
}

export interface QAGoldenRef {
  id: string;
  expected: { classification?: AnalysisClassification };
  state: QAGoldenState;
}

export interface QAGolden extends QAGoldenRef {
  note: string;
}

export interface QAQueueTrial {
  trial_id: string;
  task_id: string;
  task_name: string;
  reward: number | null;
  group: QAQueueGroup;
  qa_classification: AnalysisClassification | null;
  qa_subtype: string | null;
  qa_reason: string | null;
  golden: QAGoldenRef | null;
}

export interface QAQueue {
  title: string;
  /** Failed first. */
  trials: QAQueueTrial[];
}

/** What the page shows. All of it lives in the URL, so every view has a
 * link. */
export interface QAEvalsView {
  stage: QAStage;
  chart: QAEvalsChart;
  /** The selected run's experiment id. */
  run: string | null;
  filter: QAEvalGroup | "all";
  /** The shown prompt version and the one it is diffed against. */
  version: string | null;
  base_version: string | null;
  /** Source trials of the selected run picked to rerun. */
  trial: string[];
  /** Model names hidden in the chart legend. */
  hide: string[];
  /** The labeling view, on by itself while `experiment` or `review` is set. */
  label: boolean;
  /** The queue's experiment (id or name), or its review queue. */
  experiment: string | null;
  review: QAGoldenReview | null;
  /** The experiment list's member (`me`, `all` or a user id) and search. */
  member: string;
  search: string | null;
  queue: QAQueueGroup | "all";
}

type Key<T> = {
  read: (values: string[]) => T;
  /** The URL values; none for the default. */
  write: (value: T) => string[];
};

/** One of `options`, the first being the default. */
function oneOf<T extends string | null>(options: readonly T[]): Key<T> {
  return {
    read: ([value]) => options.find((option) => option === value) ?? options[0],
    write: (value) =>
      value === options[0] || value === null ? [] : [String(value)],
  };
}

const text: Key<string | null> = {
  read: ([value]) => value || null,
  write: (value) => (value ? [value] : []),
};
const list: Key<string[]> = {
  read: (values) => values.filter(Boolean),
  write: (values) => values,
};

const KEYS: { [K in keyof QAEvalsView]: Key<QAEvalsView[K]> } = {
  stage: oneOf(QA_STAGES),
  chart: oneOf(QA_EVALS_CHARTS),
  run: text,
  filter: oneOf(["all", ...QA_EVAL_GROUPS.map((g) => g.group)]),
  version: text,
  base_version: text,
  trial: list,
  hide: list,
  label: {
    read: ([value]) => value === "1",
    write: (on) => (on ? ["1"] : []),
  },
  experiment: text,
  review: oneOf([null, ...QA_GOLDEN_REVIEWS]),
  member: {
    read: ([value]) => value || "me",
    write: (member) => (member === "me" ? [] : [member]),
  },
  search: text,
  queue: oneOf([...QA_QUEUE_GROUPS, "all"]),
};
const KEY_NAMES = Object.keys(KEYS) as (keyof QAEvalsView)[];

function readKey<K extends keyof QAEvalsView>(
  key: K,
  params: { getAll(key: string): string[] }
): QAEvalsView[K] {
  return KEYS[key].read(params.getAll(key));
}

function writeKey<K extends keyof QAEvalsView>(
  key: K,
  view: QAEvalsView
): string[] {
  return KEYS[key].write(view[key]);
}

/** The view a page URL's search params describe; unknown values read as
 * their defaults. */
export function parseView(params: {
  getAll(key: string): string[];
}): QAEvalsView {
  const view = Object.fromEntries(
    KEY_NAMES.map((key) => [key, readKey(key, params)])
  ) as unknown as QAEvalsView;
  view.label ||= Boolean(view.experiment || view.review);
  return view;
}

export const DEFAULT_VIEW: QAEvalsView = parseView(new URLSearchParams());

export const RUNS_VIEW = {
  label: false,
  experiment: null,
  review: null,
  member: DEFAULT_VIEW.member,
  search: null,
  queue: DEFAULT_VIEW.queue,
} satisfies Partial<QAEvalsView>;

/** Opening a run leaves labeling and resets selections belonging to another run. */
export function selectRun(run: string): Partial<QAEvalsView> {
  return { ...RUNS_VIEW, run, version: null, base_version: null, trial: [] };
}

/** The page's link for a view (before the org prefix), defaults left out. */
export function viewHref(view: QAEvalsView): string {
  const params = new URLSearchParams({ tab: "qa-evals" });
  for (const key of KEY_NAMES) {
    // A queue link opens the labeling view without label=1.
    if (key === "label" && (view.experiment || view.review)) continue;
    for (const value of writeKey(key, view)) params.append(key, value);
  }
  return `/admin?${params}`;
}

/** The selected run (the URL's, else the newest charted, scored, with
 * goldens, or any run), and the prompt versions shown: the URL's, else the
 * run's prompt, else production's, diffed against the URL's base, else the
 * version's previous one. */
export function selection(status: QAEvalStatus, view: QAEvalsView) {
  const { runs, versions } = status;
  const runId =
    view.run ??
    (
      runs.findLast((run) => run.charted) ??
      runs.findLast((run) => run.accuracy !== null) ??
      runs.findLast((run) => run.with_golden > 0) ??
      runs.at(-1)
    )?.experiment_id ??
    null;
  const run = runs.find((r) => r.experiment_id === runId);
  const find = (sha: string | null | undefined) =>
    versions.find((v) => v.sha256 === sha);
  const shown =
    find(view.version) ??
    find(run?.prompt_sha256) ??
    find(status.production_prompt_sha256) ??
    versions.at(-1);
  const pickedBase = find(view.base_version);
  const base =
    pickedBase?.text_available && pickedBase !== shown
      ? pickedBase
      : find(shown?.previous_sha256);
  return { runId, run, shown, base };
}

const JSON_HEADERS = { "Content-Type": "application/json" };

export function statusUrl(stage: QAStage): string {
  return `/api/qa-evals/status?stage=${stage}`;
}

export function versionTextUrl(stage: QAStage, sha256: string): string {
  return `/api/qa-evals/versions/${encodeURIComponent(sha256)}?stage=${stage}`;
}

export function runUrl(experimentId: string): string {
  return `/api/qa-evals/runs/${encodeURIComponent(experimentId)}`;
}

export type QAEvalStart = {
  stage: QAStage;
  name: string;
  prompt_sha256: string;
  model: string | null;
  agent?: string;
  agent_config?: { kwargs?: Record<string, unknown>; [key: string]: unknown };
  preflight?: boolean;
  preflight_run_id?: string;
} & ({ goldens: true } | { source_trial_ids: string[] });

export function configurationKey(run: QAEvalRun): string {
  return run.configuration_sha256 ?? `${run.agent ?? "claude-code"}:${run.model_name}:${run.reasoning_effort ?? "default"}`;
}

export function configurationLabel(run: QAEvalRun): string {
  return `${run.model_name} · ${run.agent ?? "claude-code"}${run.reasoning_effort ? `/${run.reasoning_effort}` : ""}`;
}

/** The trial's live golden, as a list of zero or one QAGolden. */
export function trialGoldenUrl(trialId: string): string {
  const params = new URLSearchParams({ stage: "post_trial", trial: trialId });
  return `/api/qa-goldens?${params}`;
}

/** The view's labeling queue, or null while it names none. */
export function queueUrl(
  view: Pick<QAEvalsView, "experiment" | "review">
): string | null {
  const param = view.review
    ? ["review", view.review]
    : view.experiment
      ? ["experiment", view.experiment]
      : null;
  return param && `/api/qa-goldens/trials?${new URLSearchParams([param])}`;
}

/** PUT a trial's label; the response is its QAGolden. Without `note` an
 * unchanged label keeps its note. */
export function labelTrialRequest(
  trialId: string,
  body: { classification: AnalysisClassification; note?: string }
): [string, RequestInit] {
  return [
    `/api/qa-goldens/trials/${encodeURIComponent(trialId)}`,
    { method: "PUT", headers: JSON_HEADERS, body: JSON.stringify(body) },
  ];
}

/** The labeling view's experiments: the dashboard's list for the member
 * and search, `author:` and `tag:` words read as the dashboard reads them. */
export function labelExperimentsUrl(
  view: Pick<QAEvalsView, "member" | "search">
): string {
  const search = parseTaskSearch(view.search ?? "");
  return buildDashboardApiPath({
    include_queues: false,
    include_tasks: false,
    include_usage: false,
    experiments_limit: DASHBOARD_DEFAULT_EXPERIMENTS_LIMIT,
    experiments_author: view.member,
    experiments_query: search.text,
    experiments_tags: search.all.join(","),
    experiments_tags_any: search.any.join(","),
    experiments_tags_none: search.none.join(","),
    experiments_author_query: search.authors.join(","),
  });
}

/** SWR `refreshInterval`: every 10 s while any of `runs` is still running. */
export function pollInterval(
  runs: readonly { pending: number }[] | undefined
): number {
  return runs?.some((run) => run.pending > 0) ? 10_000 : 0;
}

/** The trial's drawer on its task page (before the org prefix). */
export function trialHref(taskId: string, trialId: string): string {
  return `/tasks/${encodeURIComponent(taskId)}?${new URLSearchParams({ trial: trialId })}`;
}

/** The experiment an input names: the id in a pasted experiment link, else
 * the text itself (an id or a name); null when empty. */
export function experimentFromInput(input: string): string | null {
  const text = input.trim();
  const linked = /\/experiments\/([^/?#\s]+)/.exec(text)?.[1];
  return (linked ? decodeURIComponent(linked) : text) || null;
}

export function formatAccuracy(accuracy: number | null): string {
  return accuracy === null ? "—" : `${Math.round(accuracy * 100)}%`;
}

/** "83% (25/30)": accuracy with correct over judged. */
export function formatScore(
  run: Pick<QAEvalRun, "accuracy" | "correct" | "judged">
): string {
  return `${formatAccuracy(run.accuracy)} (${run.correct}/${run.judged})`;
}

export function shortSha(sha: string): string {
  return sha.slice(0, 8);
}
