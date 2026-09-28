import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_VIEW,
  configurationKey,
  compareThinkingEffort,
  QA_EVAL_GROUPS,
  experimentFromInput,
  formatScore,
  labelExperimentsUrl,
  labelTrialRequest,
  parseView,
  pollInterval,
  queueUrl,
  runUrl,
  selection,
  statusUrl,
  trialGoldenUrl,
  trialHref,
  versionTextUrl,
  viewHref,
  type QAEvalRun,
  type QAEvalStatus,
  type QAPromptVersion,
} from "../src/lib/qa-evals.ts";

const view = (query: string) => parseView(new URLSearchParams(query));

test("every URL key round-trips, repeated keys in order", () => {
  const href =
    "/admin?tab=qa-evals&stage=pre_trial&chart=models&run=run-1&filter=not_judged&version=abc&base_version=def&trial=t-1&trial=t-2&hide=claude-haiku-4-5&hide=gpt-5&member=all&search=author%3Aana+sweep&queue=labeled";
  const parsed = view(href.split("?")[1]);
  assert.deepEqual(parsed.trial, ["t-1", "t-2"]);
  assert.deepEqual(parsed.hide, ["claude-haiku-4-5", "gpt-5"]);
  assert.equal(parsed.search, "author:ana sweep");
  assert.equal(viewHref(parsed), href);
});

test("defaults and unknown values are left out of links", () => {
  assert.equal(viewHref(DEFAULT_VIEW), "/admin?tab=qa-evals");
  const unknown = view("stage=mid&chart=pie&filter=x&review=someday&queue=q");
  assert.deepEqual(unknown, DEFAULT_VIEW);
  assert.equal(DEFAULT_VIEW.member, "me");
  assert.equal(DEFAULT_VIEW.queue, "failed");
  assert.deepEqual(DEFAULT_VIEW.trial, []);
});

test("a queue link opens the labeling view without label=1", () => {
  assert.equal(view("label=1").label, true);
  assert.equal(viewHref(view("label=1")), "/admin?tab=qa-evals&label=1");
  const review = view("review=waiting");
  assert.equal(review.label, true);
  assert.equal(viewHref(review), "/admin?tab=qa-evals&review=waiting");
  assert.equal(
    viewHref({ ...DEFAULT_VIEW, label: true, experiment: "my sweep" }),
    "/admin?tab=qa-evals&experiment=my+sweep"
  );
});

test("every group is a filter", () => {
  for (const { group } of QA_EVAL_GROUPS) {
    assert.equal(view(`filter=${group}`).filter, group);
  }
});

test("API URLs name the backend routes through the proxies", () => {
  assert.equal(
    statusUrl("post_trial"),
    "/api/qa-evals/status?stage=post_trial"
  );
  assert.equal(
    versionTextUrl("pre_trial", "abc"),
    "/api/qa-evals/versions/abc?stage=pre_trial"
  );
  assert.equal(runUrl("run 1"), "/api/qa-evals/runs/run%201");
  assert.equal(
    trialGoldenUrl("task-1-3"),
    "/api/qa-goldens?stage=post_trial&trial=task-1-3"
  );
  assert.equal(queueUrl(DEFAULT_VIEW), null);
  assert.equal(
    queueUrl({ experiment: "my sweep", review: null }),
    "/api/qa-goldens/trials?experiment=my+sweep"
  );
  assert.equal(
    queueUrl({ experiment: "x", review: "stale" }),
    "/api/qa-goldens/trials?review=stale"
  );

  const [labelUrl, label] = labelTrialRequest("task-1-3", {
    classification: "BAD_FAILURE",
  });
  assert.equal(labelUrl, "/api/qa-goldens/trials/task-1-3");
  assert.equal(label.method, "PUT");
  assert.deepEqual(JSON.parse(label.body as string), {
    classification: "BAD_FAILURE",
  });
});

test("the experiment list reads author: and tag: words as the dashboard does", () => {
  assert.equal(
    labelExperimentsUrl({ member: "me", search: "sonnet" }),
    "/api/dashboard?experiments_limit=25&experiments_author=me&experiments_query=sonnet&include_queues=false&include_tasks=false&include_usage=false"
  );
  assert.equal(
    labelExperimentsUrl({
      member: "all",
      search: "author:ana tag:smoke sweep",
    }),
    "/api/dashboard?experiments_limit=25&experiments_query=sweep&experiments_author_query=ana&experiments_tags=smoke&include_queues=false&include_tasks=false&include_usage=false"
  );
});

test("an experiment is read from a pasted link, an id or a name", () => {
  assert.equal(
    experimentFromInput(
      " https://www.oddish.app/orgs/abundant/experiments/dc7a482f?tab=tasks "
    ),
    "dc7a482f"
  );
  assert.equal(experimentFromInput("dc7a482f"), "dc7a482f");
  assert.equal(experimentFromInput(" my sweep "), "my sweep");
  assert.equal(experimentFromInput("   "), null);
});

test("links, scores and polling", () => {
  assert.equal(
    trialHref("task 1", "task 1-3"),
    "/tasks/task%201?trial=task+1-3"
  );
  assert.equal(
    formatScore({ accuracy: 0.834, correct: 25, judged: 30 }),
    "83% (25/30)"
  );
  assert.equal(
    formatScore({ accuracy: null, correct: 0, judged: 0 }),
    "— (0/0)"
  );
  assert.equal(pollInterval([{ pending: 0 }, { pending: 2 }]), 10_000);
  assert.equal(pollInterval([{ pending: 0 }]), 0);
  assert.equal(pollInterval(undefined), 0);
});

const run = (id: string, fields: Partial<QAEvalRun> = {}): QAEvalRun => ({
  experiment_id: id,
  experiment_name: id,
  title: id,
  created_at: "2026-09-24T10:00:00Z",
  model: null,
  model_name: "claude-sonnet-5",
  prompt_sha256: "p1",
  subset: false,
  charted: false,
  replays: 1,
  with_golden: 0,
  judged: 0,
  correct: 0,
  errored: 0,
  pending: 0,
  accuracy: null,
  cost_usd: 0,
  cost_per_trial: null,
  ...fields,
});

const version = (
  sha256: string,
  fields: Partial<QAPromptVersion> = {}
): QAPromptVersion => ({
  sha256,
  name: sha256,
  author: null,
  first_seen_at: "2026-09-20T00:00:00Z",
  git_commit: null,
  text_available: true,
  previous_sha256: null,
  ...fields,
});

const status = (runs: QAEvalRun[]): QAEvalStatus => ({
  replayable: true,
  production_prompt_sha256: "p2",
  goldens: { counted: 1, waiting: 0, stale: 0, labels: [] },
  cost_per_trial: null,
  spend: { total_usd: 0, trials: 0, cost_missing: 0, by_model: [] },
  runs,
  frontier: [],
  models: [],
  versions: [
    version("p0", { text_available: false }),
    version("p1"),
    version("p2", { previous_sha256: "p1" }),
  ],
});

test("a link without run= opens the newest charted, else scored, else labeled run", () => {
  const charted = run("charted", { charted: true, accuracy: 1 });
  const subset = run("subset", { subset: true, accuracy: 0.5, with_golden: 1 });
  const labeled = run("labeled", { with_golden: 1 });
  const bare = run("bare");
  const pick = (...runs: QAEvalRun[]) =>
    selection(status(runs), DEFAULT_VIEW).runId;
  assert.equal(pick(charted, subset, labeled, bare), "charted");
  assert.equal(pick(subset, labeled, bare), "subset");
  assert.equal(pick(labeled, bare), "labeled");
  assert.equal(pick(bare), "bare");
  assert.equal(pick(), null);
  assert.equal(
    selection(status([charted, bare]), view("run=bare")).runId,
    "bare"
  );
});

test("the shown version is the link's, else the run's prompt, else production's", () => {
  const s = status([run("r1", { prompt_sha256: "p1" })]);
  assert.equal(selection(s, view("version=p2")).shown?.sha256, "p2");
  assert.equal(selection(s, DEFAULT_VIEW).shown?.sha256, "p1");
  assert.equal(selection(status([]), DEFAULT_VIEW).shown?.sha256, "p2");
});

test("a picked base is used only when it has text and differs from the shown version", () => {
  const s = status([]);
  const base = (query: string) => selection(s, view(query)).base?.sha256;
  assert.equal(base("version=p2"), "p1");
  assert.equal(base("version=p2&base_version=p0"), "p1");
  assert.equal(base("version=p2&base_version=p2"), "p1");
  assert.equal(base("version=p1&base_version=p2"), "p2");
});


test("chart identity ignores execution hashes and provider spelling for every normalized model", () => {
  for (const model of [
    "gpt-6-luna",
    "claude-sonnet-5",
    "gemini-3-pro",
    "glm-5",
    "grok-4",
  ]) {
    const first = run("old", {
      model: `provider/${model}`,
      model_name: model,
      agent: "claude-code",
      reasoning_effort: "high",
      configuration_sha256: "old-fingerprint",
    });
    const second = { ...first, model, configuration_sha256: "new-fingerprint" };
    assert.equal(configurationKey(first), configurationKey(second));
    assert.notEqual(
      configurationKey(first),
      configurationKey({ ...second, reasoning_effort: "low" })
    );
    assert.notEqual(
      configurationKey(first),
      configurationKey({ ...second, agent: "codex" })
    );
    assert.notEqual(
      configurationKey(first),
      configurationKey({ ...second, model_name: "different-model" })
    );
  }
});

test("thinking effort filters and sorting round-trip in shared links", () => {
  const state = view("effort=high&sort=effort");
  assert.equal(viewHref(state), "/admin?tab=qa-evals&effort=high&sort=effort");
  assert.deepEqual(view(viewHref(state).split("?")[1]), state);
  assert.equal(view("sort=unknown").sort, "accuracy");
  assert.equal(view("effort=unrecorded").effort, "unrecorded");
});

test("thinking effort sorts by intensity and keeps missing effort distinct from none", () => {
  assert.deepEqual(
    [
      "high",
      null,
      "none",
      "max",
      "low",
      "ultra",
      "medium",
      "minimal",
      "xhigh",
    ].sort(compareThinkingEffort),
    ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", null]
  );
  assert.notEqual(
    configurationKey(run("r", { reasoning_effort: null })),
    configurationKey(run("r", { reasoning_effort: "none" }))
  );
});
