# Internal CLI reference

Repository-only reference migrated from DOCS.md. Contains operator and advanced workflow details; never publish this file as customer content. Customer guides live in `docs/customer/`. Verify retained historical claims against code before use.

## Usage

**Commands:**

- `oddish run` - submit work, retry failed trials, or re-run task-level QA
- `oddish upload` - register a task or upload existing trials
- `oddish preflight` - run the local task checks that also gate `run` and `upload`
- `oddish ls` - list uploaded tasks
- `oddish status` - view progress
- `oddish qa export` - export existing QA findings and task statuses to CSV
- `oddish qa evals start/status/run/diff` - compare QA models and prompts against reviewed golden labels
- `oddish logs` - stream a running trial's live transcript and cost estimate
- `oddish cancel` - stop in-flight task runs, or just the QA/audit runs with `--qa`
- `oddish backfill-analysis` - (re)run trial analysis for a trial, task, or experiment
- `oddish assign` - assign QA review ownership for a batch of task IDs
- `oddish costs` - view billable-spend accounting (org-wide, per user, or per task split into solver and QA)
- `oddish admin concurrency` - inspect, set, or clear operator queue-key limits
- `oddish cost-exclusions` - hide spend for models and experiments that were never really paid for
- `oddish pull` - download logs and artifacts
- `oddish combine` - merge several experiments into a new one
- `oddish collect` - gather trials from tasks/trial IDs into a shareable read-only collection
- `oddish experiment create` - build a collection experiment from explicit trial IDs
- `oddish experiment add` / `oddish experiment remove` / `oddish experiment rename` - edit a collection in place; its share link keeps working
- `oddish experiment rename-model` - alias a model id to a display name on an experiment's public share view
- `oddish experiment ls` - list experiments like the dashboard; `--queue KEY [--queue-state queued|running]` keeps those with trial jobs on that queue now, and the printed URL opens the same filtered list
- `oddish delete` - delete trials, tasks, or experiments (what's allowed depends on the deployment; see [Delete Data](#delete-data))
- `oddish publish` / `oddish unpublish` - toggle public read-only sharing for an experiment
- `oddish link` - print the dashboard URL for a task or trial (built locally; needs no API key)
- `oddish version` - print the installed CLI version; `--check` prints Homebrew check instructions for Homebrew installs or compares legacy installs with PyPI
- `oddish update` - print Homebrew upgrade instructions for Homebrew installs; legacy PyPI installs can only upgrade to previously published PyPI versions
- `oddish skill` - print or install the packaged SKILL.md agent guide

Most commands accept `--json` for machine-readable output (CI / scripts /
agents). The exceptions are `oddish logs`, `oddish link`, and `oddish skill`,
which print human-readable output only.
`oddish version` and `oddish update` are local and do not require an API key.

### Lifecycle

A typical run flows through these commands:

1. `oddish run` — submit a task, dataset, or sweep. The output (including `--json`) carries the task IDs plus the experiment's name and dashboard URL. Task-level QA runs automatically once every trial settles: it classifies each trial's trajectory, and adds a task verdict using all eligible current-version trials once at least one exists, regardless of agent diversity. With zero eligible trials, validated audit findings or failed baseline checks can still reject the task; otherwise it records insufficient evidence without accepting. Delivery requirements are configured separately; a verdict alone does not qualify a task for delivery. (`oddish upload` without `--task` only registers task files — no trials and no experiment are created; importing trials with `--task` does attach them to an experiment.)
2. `oddish status` — discover what's in flight, then drill into a specific task or experiment to see trial-level progress and rewards.
3. `oddish pull` — once you have a trial, task, or experiment ID, download its logs, results, trajectories, and artifact files to disk.
4. `oddish run --retry` — re-queue failed trials or re-run task-level QA.
5. `oddish cancel` / `oddish delete` — stop in-flight work or remove data when you're done.
6. `oddish publish` — share an experiment publicly (read-only) and get a link.

`oddish pull` accepts a trial, task, or experiment ID and auto-detects which kind it is; `oddish status` takes a task ID (falling back to experiment lookup) or `--experiment`. `oddish ls` supports the dashboard's task, tag, status, date, model, and trial-metric filters.

## Submit a Job

Use `oddish run` to launch a task, dataset, or multi-agent sweep.

```bash
# Single task
oddish run ./my-task -a claude-code -m anthropic/claude-sonnet-4-5 --n-trials 5

# Append trials to an existing task
oddish run --task <task_id> -a gemini-cli -m gemini/gemini-3.1-pro-preview
oddish run --task <task_id> -a antigravity-cli -m google/gemini-3.7-flash
oddish run --task <task_id> -a grok-build -m xai-public/<model-id>
oddish run --task <task_id> -a muse-code -m meta-public/<model-id>

# Complex sweep from config
oddish run ./my-task -c sweep.yaml
```

For Grok Build, `xai/<model-id>` uses the deployment's private xAI key or key
pool; `xai-public/<model-id>` uses its separate public xAI key. The operator
must configure `XAI_PUBLIC_API_KEY` on the worker for the public route. Both
routes call xAI's `api.x.ai` endpoint. If a model is available only through
Chat Completions, add `--agent-kwarg api_backend=chat_completions`.

For Meta, `meta/<model-id>` uses the deployment's internal Meta key and
`meta-public/<model-id>` uses its separate public Meta key. The operator must
configure `META_PUBLIC_API_KEY` on the worker for the public route. Both routes
call the same Meta endpoints, and the public route works with muse-code, tbh and
mini-swe-agent wherever `meta/` does. On a `meta-public/` trial, Oddish sets
`META_API_KEY` and `META_PUBLIC_API_KEY` (and `MSWEA_API_KEY` and
`OPENAI_API_KEY` for mini-swe-agent) to the public key, replacing any `--ae`
value for those names. `meta-public-2/<model-id>` is the same public route on a
second key, `META_PUBLIC_2_API_KEY`, with its own queue and concurrency limit.

Re-submitting the same sweep is declarative for each task version and
experiment: queued, running, and successful trials continue to satisfy the
requested count, while failed trials are replaced immutably. The failed rows
remain available by direct ID for history, but are marked superseded so only
their replacements appear in normal task and experiment views.

Options

- `--path`, `-p PATH` - Harbor-compatible path flag for a local task or dataset directory
- `--dataset`, `-d TEXT` - Registry dataset such as `swebench@1.0`
- `--task TEXT` - Append trials to an existing task ID instead of uploading task files
- `--use-default-version` - When appending to an existing task, pin new trials to the task's current default version instead of the version the target experiment is already running (no effect when the run uploads task files)
- `--config`, `-c PATH` - YAML or JSON config for multi-agent sweeps
- `--agent`, `-a TEXT` - Agent name for simple single-agent runs (defaults to `claude-code`)
- `--model`, `-m TEXT` - Model override for the selected agent. The CLI sends it as typed; the server resolves the provider and rejects a Fireworks or DeepSeek id that is not in its curated catalog, naming the known ids (for example `fireworks/deepseek-v4-flash` is rejected in favor of `fireworks/deepseek-v4-flash-0731`)
- `--allow-unknown-model` - Submit a Fireworks or DeepSeek model id that is not in the curated catalog (per agent in a sweep config: `allow_unknown_model: true`)
- `--harbor TEXT` - Override the Harbor source/ref for this run (`main`, a tag/SHA, `org/repo@ref`, or a git URL@ref); defaults to the locked fork commit (env: `ODDISH_HARBOR`)
- `--n-trials INTEGER` - Number of trials per task
- `--max-trial-attempts INTEGER` - Override the maximum Oddish attempts per trial, including the initial run
- `--task-name`, `-t TEXT` - Include task glob filter; can be passed multiple times
- `--exclude-task-name`, `-x TEXT` - Exclude task glob filter; can be passed multiple times
- `--n-tasks`, `-l INTEGER` - Limit the number of selected tasks after filtering
- `--env`, `-e` - Execution environment. The flag accepts any Harbor environment name, but hosted Oddish honors only `modal`, `daytona`, `ec2`, `gke`, `archil`, `thunder`, and `numinous`; anything else is coerced to `modal` with a warning. EC2 and Numinous are deployment-controlled opt-in backends. When Numinous is enabled it is the first CPU candidate; otherwise Daytona is the CPU baseline and the configured Archil rollout can select the whole submission. Explicit environments win for initial placement; after two failed Archil attempts, an allowed retry runs on Modal. Numinous GPU availability is controlled separately by the deployment operator. A task that requests GPUs (`[environment].gpus` or `--override-gpus`) is placed by the deployment on its GPU backend: Thunder where the operator enabled it, otherwise Modal, and always Modal when the run pulls from a private registry (`--registry-login`). Pass `--env modal` for GPU types Thunder does not offer.
- `--priority`, `-P TEXT` - Queue priority, typically `low` or `high`
- `--experiment`, `-E TEXT` - Reuse or create an experiment ID/name
- `--user`, `-u TEXT` - Override the author attached to the run. Defaults to the authenticated identity (Clerk-linked email for API keys / dashboard sessions); set this only to attribute a run to someone other than yourself.
- `--github-user`, `-G TEXT` - GitHub user attribution for CI metadata. When omitted, the backend auto-fills this from the authenticated user's Clerk-linked GitHub username (if any) so CI-style attribution still works.
- `--github-id TEXT` - GitHub user *id* attribution (immutable, so it survives handle renames). An id that isn't linked to an Oddish user is rejected before anything uploads.
- `--github-meta TEXT` - JSON metadata blob to attach to the task
- `--link TEXT` - Associate URL with the task.
- `--publish/--no-publish` - Publish the experiment for public read-only access (off by default)
- `--watch/--no-watch`, `-w` - Watch progress after submission; enabled by default
- `--background`, `--async`, `-b` - Submit and return immediately
- `--quiet`, `-q` - Suppress startup logs
- `--baseline-gate/--no-baseline-gate` - Hold LLM trials until the task's nop/oracle baselines validate it (on by default). `--no-baseline-gate` runs them immediately, ungated; the baselines themselves still run. Decided per run — retries choose afresh.
- `--disable-verification/--enable-verification` - Skip task verification or tests
- `--force-new-version` - Allocate a new task version even when the content is unchanged
- `--overwrite-current-version` - Replace the selected current version in place; existing trials pinned to it will resolve to the replacement content
- `--submit-concurrency INTEGER` - Max parallel task uploads/submissions (default: adaptive; overrides `ODDISH_TASK_UPLOAD_CONCURRENCY`)
- `--override-cpus INTEGER` - Override environment CPU count
- `--override-memory-mb INTEGER` - Override environment memory
- `--override-gpus INTEGER` - Override environment GPU count
- `--override-storage-mb INTEGER` - Override environment storage
- `--force-build/--no-force-build` - Force a rebuild of the environment image
- `--environment-kwarg`, `--harbor-environment-kwarg TEXT` - Pass Harbor environment kwargs as `KEY=VALUE`; can be used multiple times
- `--ae`, `--agent-env TEXT` - Pass agent env vars as `KEY=VALUE`; can be used multiple times
- `--ak`, `--agent-kwarg TEXT` - Pass agent kwargs as `key=value`; can be used multiple times. Omitted reasoning effort uses the agent’s own default. Set it explicitly with e.g. `--agent-kwarg reasoning_effort=high`; the server saves that choice with the trial.
- `--allow-agent-host TEXT` - Extra hostname for a restricted agent phase (maps to Harbor `extra_allowed_hosts`); usually unnecessary because Oddish auto-injects the model API host. Can be used multiple times
- `--disable-web-tools/--no-disable-web-tools` - Force-disable server-side web tools; usually unnecessary because Oddish does this automatically on closed-internet agent phases (`claude-code`: `disallowed_tools=WebSearch WebFetch`; `codex`: `web_search=disabled`)
- `--artifact TEXT` - Download an environment path as an artifact after the trial
- `--registry-login TEXT` - Per-run container-registry login as `username=USER,token=TOKEN[,registry=docker.io]`; repeatable and honored by `--retry`.
  Wrap comma-bearing values like `--registry-login "username=USER,token='a,b'"`.
  Credentials authenticate sandbox image pulls, are encrypted across the queue, and are logged out on teardown.
  Docker Hub creds can also come from `ODDISH_DOCKERHUB_USERNAME` / `ODDISH_DOCKERHUB_TOKEN`.
  Prefer a Docker Hub access token over an account password.
- `--force` - Submit even if the preflight checks fail; findings are still printed. (Unrelated to `--force-new-version`.)
- `--retry` - Re-run an existing target instead of submitting new work (see below)
- `--qa` - With `--retry`: re-run the task-level QA pass (classify every trial + synthesize the verdict) instead of retrying trials
- `--yes`, `-y` - Skip confirmation prompts (used with `--retry`)
- `--api TEXT` - Override the API URL
- `--json` - Emit JSON for scripts and CI; implies `--background`

### Run on ephemeral EC2

An EC2-enabled deployment can run a trial on one disposable CPU VM by selecting
the backend explicitly:

```bash
oddish run ./my-task --env ec2 -a claude-code -m anthropic/claude-sonnet-4-5
```

The hosted API rejects `--env ec2` when its operator has not enabled and fully
configured the backend. EC2 is not an automatic fallback: CPU-only hosted runs
without `--env` continue to use Daytona. V1 does not accept GPU/TPU requests,
attach mode, retained instances, or caller overrides of platform EC2 settings.
It uses a public address and key-only SSH; the instance is terminated after the
trial or cancellation.

### Run on Thunder

A Thunder-enabled deployment runs GPU trials in disposable Thunder sandboxes.
Any task whose `task.toml` requests GPUs (or any run with `--override-gpus`)
routes there without an `--env` flag unless it needs a private-registry pull;
the flag also selects it explicitly:

```bash
oddish run ./my-task --env thunder -a nop --n-trials 1 --max-trial-attempts 1
```

The hosted API rejects `thunder` unless its operator enabled the backend.
Harbor's Thunder environment needs exactly one `[environment].gpu_types` entry
(A6000, A100, or H100) and a GPU count of 1, 2, 4, or 8. `oddish run` reports
the task's GPU type with its GPU request (an exact `gpu_type` environment kwarg,
else its `gpu_types` list), so a task naming no type, several
types, or one Thunder lacks is routed to Modal instead of failing on Thunder
first; an unsupported GPU count still needs `--env modal`. A trial that fails twice on Thunder (the
deployment's `ODDISH_THUNDER_MAX_FAILED_ATTEMPTS`) has its next retry run on
Modal instead, within the trial's usual `--max-trial-attempts` budget; the
trial keeps its id, and `oddish status` shows `environment: modal` from then
on. Thunder is never a fallback for other providers. Its provider-wide
capacity limit is shared across models, organizations, queue keys, and Harbor
variants. Oddish persists the Thunder sandbox ID before operating it and uses
that ID for normal teardown, cancellation, and orphan cleanup.

### Re-run with `--retry`

`oddish run --retry` re-runs existing work instead of submitting new trials. It
accepts a trial, task, or experiment id — positional, `--task`, or
`--experiment` — and auto-detects the target type.

```bash
# Retry a single failed trial
oddish run <trial_id> --retry

# Retry every failed trial in a task (skip the confirmation prompt)
oddish run <task_id> --retry -y

# Retry all failed trials across an experiment
oddish run <experiment_id> --retry -y

# Re-run the task-level QA pass (classify every trial + synthesize the verdict)
oddish run <task_id> --retry --qa

# Machine-readable summary of what was queued
oddish run <experiment_id> --retry -y --json
```

- Default (`--retry` alone) re-queues failed trials. For task and experiment
  targets, only trials currently in a `failed` state are retried — unless you
  pass `--no-baseline-gate`, which also sweeps up trials the baseline gate
  left in `skipped`.
- `--qa` re-runs the single task-level QA pass: it re-classifies every live
  trial and synthesizes a fresh task verdict, while the previously published
  verdict stays visible until the replacement lands. A trial-shaped id resolves
  to its parent task; experiment targets run QA for each task.
- `--qa` requires `--retry`.
- `-y, --yes` skips the confirmation prompt; `--json` is always non-interactive.

### Sweep Config

Use `oddish run -c sweep.yaml` to run multiple agents:

```yaml
agents:
  - name: claude-code
    model_name: anthropic/claude-sonnet-4-5
    n_trials: 3
  - name: codex
    model_name: openai/gpt-5.3-codex
    n_trials: 3
  - name: nop
    n_trials: 3
  - name: oracle
    n_trials: 3

max_trial_attempts: 3
harbor:
  environment:
    kwargs:
      region: us-east
```

`max_trial_attempts` is optional. It is the total Oddish worker attempt budget
per trial, including the initial run. When omitted, Oddish keeps its default
retry behavior.

## Preflight Checks

Use `oddish preflight` to check local task files for integrity problems before
spending trials on them. It runs entirely locally — no API key needed. It
parses `task.toml`, validates task metadata, requires a justification for open internet access, rejects
repository fetches or exposed `.git` data in the agent image, requires
readable source rather than patch-only solutions, and rejects brittle
source-scanning anti-cheat checks.

The `task_metadata` check requires:

- `[task].name` whose final component matches the task directory. For a
  directory named `fix-login`, `abundant/fix-login`, `category/fix-login`, and
  the legacy bare name `fix-login` are accepted. Prefer `abundant/` when
  authoring new Abundant tasks; preflight does not rename tasks.
- An explicit `[environment].network_mode` (`public`, `no-network`, or
  `allowlist`), or the legacy boolean `allow_internet`. Separate verifier
  environments must declare their own network baseline, including per-step
  environments. Agent/verifier phase overrides can inherit the baseline.
  A public-access declaration still needs the existing written justification.
- A nonempty string in `[metadata].reward_type`. This currently checks the
  declaration only, without imposing a score vocabulary or changing grading.
- Nonblank entries in `gpu_types` when supplied. GPU types remain optional;
  preflight does not impose a provider-specific list of supported hardware.

These checks inspect local source files. They do not run the task, certify
x86/ARM compatibility, or replace the AI source review.

```bash
# Check a task or dataset directory
oddish preflight ./my-task

# Resolve tasks from a registry dataset
oddish preflight --dataset swebench@1.0

# Machine-readable findings for CI
oddish preflight ./my-task --json
```

The same checks run automatically inside `oddish run` and `oddish upload` and
block submission when they fail. Pass `--force` to those commands to submit
anyway; the findings are still printed.

Options

- `PATH` (or `--path`) - Task or dataset directory to check
- `--dataset TEXT` - Harbor dataset name to resolve tasks from
- `--json` - Emit findings as JSON for CI consumers

## List Tasks

Use `oddish ls` to browse uploaded tasks with their current version, trial
counts, reward summary, tags, last run time, linked experiments, and recorded
delivery labs. "Current
version" is the task's user-selected default — not necessarily the
highest-numbered one — and the trial counts cover that version's normal
evaluation trials (not superseded rows or the platform's QA/audit runs).

```bash
oddish ls
oddish ls --query django
oddish ls --tag benchmark --not-tag wip
oddish ls --model openai/gpt-5 --min-steps 100 --min-duration 120
oddish ls --tool bash --tool-min bash=5 --trial-match all
oddish ls --json
```

Common options

- `--query`, `-q TEXT` - Filter tasks by name
- `--tag TEXT` - Require this tag (repeatable; AND semantics)
- `--tag-any TEXT` - Match any of these tags (repeatable; OR semantics)
- `--not-tag TEXT` - Exclude tasks carrying any of these tags (repeatable)
- `--limit`, `-n INTEGER` - Maximum number of tasks to show (default 25, max 100)
- `--offset INTEGER` - Number of tasks to skip
- `--json` - Emit the raw task browser JSON response
- `--api TEXT` - Override the API URL


The [agent navigation reference](task-browser-navigation.md) maps browser URL
parameters to CLI flags and documents deep links for expanded task evidence.

Task run coverage is available in both the Tasks page and `oddish ls`:

```bash
# Find model evidence; oracle/NOP checks do not count as model runs.
oddish ls --run-coverage scored_model_trials --sort model_score_desc
# Find versions with only baseline validation, and prioritize your tasks.
oddish ls --run-coverage validation_only --pin-author me
# Review recorded model results and inspect complete summaries/experiment IDs.
oddish ls --run-coverage model_trials --qa-outcome unreviewed --json
# Select for a customer; delivery readiness is checked by the delivery itself.
oddish ls --run-coverage scored_model_trials --not-delivered-to GDM --ids --json > selection.json
```

`--run-coverage` accepts `any`, `no_trials`, `validation_only`, `model_trials`,
and `scored_model_trials`; all refer to the current default version. `--count`
and `--ids` use the same filters. The text table separates model scores from
oracle/NOP checks; `--json` retains complete `run_summary`, experiment, and
trial-preview fields. Missing summaries display Unavailable. Model sorts are
`model_score_asc`, `model_score_desc`, and `last_model_activity_desc`.
`--author me` restricts results; `--pin-author me` prioritizes them. `--sort`
orders within that scope. Inspect underlying runs with `oddish status TRIAL_ID`
and `oddish status TASK_ID --versions --json`.

These are only the most common filters — `oddish ls` mirrors the dashboard's
full task-browser filter set (status, date, model, trial-metric, tool-usage,
and more, some 70 options in all). Run `oddish ls --help` for the complete
list rather than relying on this page.

### Find tasks for delivery

The same lab, QA, author, and numeric filters used by the Tasks page are
available in `oddish ls`. Lab names can be mapped customers or imported labels.
Repeated `--delivered-to` values match any listed lab; `--not-delivered-to`
excludes tasks with a record for any listed lab. `--never-delivered` means no
record exists; `--has-delivery` requires a record. Missing history does not
prove that a task was never shipped.

```bash
oddish ls --filter-options
oddish ls --delivered-to xai --not-delivered-to GDM --delivery-history
oddish ls --category swe --qa-outcome accepted --steps-p50-min 100 --agent-count-min 3
oddish ls --author me --sort total_trials_desc
oddish ls --pin-author me --trial-finished-within 7d
oddish ls --not-delivered-to GDM --count
```

`--delivery-history` shows lab, batch, date, and source for each task on the
page. `--json` preserves the complete browse response, including `deliveries`.
`--filter-options` lists available labs, categories, and trial filter values;
it cannot be combined with task filters. `--count` prints the total matching
count without fetching task details. `--author me` limits results to the API
key creator's tasks; `--pin-author me` puts those tasks first. Author resolution
requires the hosted API. `--trial-finished-after` / `--trial-finished-before`
accept exact timestamps; `--trial-finished-within` and `--created-within` accept
24h, 7d, 30d, or 90d. `--steps-p50-max` bounds median steps from above.

To select every match rather than one page, save `--ids --json` and pass it to
one delivery mutation. `--exclude-delivery-id` accepts the full ID from
`oddish delivery list`, and prevents selecting existing members.

```bash
oddish ls --not-delivered-to GDM --qa-outcome accepted --ids --json > selection.json
oddish delivery create september-batch --customer GDM --tasks-file selection.json --json
# Or add the saved selection to an existing delivery:
oddish delivery add september-batch --tasks-file selection.json --json
```

`--ids` ignores pagination and returns up to 5,000 IDs. JSON includes
`truncated`; create/add reject a truncated export before sending any request.
Narrow the filters when it is true. Without `--json`, `--ids` prints one ID per
line and exits with an error without printing IDs if the set exceeds 5,000.
The file format is the unmodified `--ids --json` response, not a text ID list.
Create/add deduplicate repeated IDs and send the whole selection in one
transaction, so a rejected task cannot leave a partially populated delivery.
`--ids`, `--count`, and `--filter-options` are mutually exclusive.

## Compare QA models

QA evaluations replay stored solver evidence against reviewed golden labels.
Each selected model gets a saved run in Admin → QA Evals. Find a prompt hash
with `oddish qa evals status`, then run:

```bash
oddish qa evals start --version PROMPT_SHA --agent claude-code \
  --model anthropic/claude-sonnet-5 --model openai/gpt-6-sol \
  --model openai/gpt-6-luna --reasoning-effort high --yes
```

`--agent` selects the same harness as ordinary trials; `mini-swe-agent` and
`codex` are also available. Omitted agent/model retain production QA defaults.
Use `--agent-config settings.json` for the ordinary Harbor agent settings
object, such as `{"kwargs":{"version":"2.1.283","max_turns":100}}`.
The selected model's provider still needs credentials and tool support.

The CLI first runs one case per model and waits for valid QA output before
submitting each full set. An incorrect classification can pass this execution
check; absent or invalid output cannot. `--preflight-only` stops after checks;
`--skip-preflight` explicitly skips them. Retain the printed idempotency key
to resume an interrupted submission without duplicating saved runs.

The page uses the same two steps: select harness/models/settings, check one
golden per model, then replay all goldens when checks pass. Runs display the
harness and reasoning effort and remain separately comparable. These commands
require a CLI release containing `qa evals`; older Homebrew clients can use
the page or a local checkout until the next CLI release.

## Export QA Feedback

Export existing audit and run-review findings for a batch of exact task IDs:

```bash
oddish qa export <task_id> <another_task_id> --output qa-findings.csv
oddish qa export --ids-file task-ids.txt --output qa-findings.csv
oddish qa export --ids-file task-ids.txt --tier must_fix --all-versions
```

The input file is UTF-8 with one task ID per line. Blank lines are ignored;
repeated IDs are fetched once, in first-seen order. Names are not resolved.
Use `oddish ls --query <name> --json` to find an ID first.

The command writes two UTF-8 CSV files (existing files are overwritten):

- `qa-findings.csv`: one row per finding occurrence, combining version audits
  and individual agent-run reviews. Columns include the task ID/name/version,
  current task verdict, audit status, source trial ID (an individual run),
  trial classification, finding ID, `tier`, `problem_type`, `dimension`, full
  title/explanation/recommendation, file and line range, and exploitation
  linkage. `group`, `assignee`, and `resolution` start blank for manual triage.
- `qa-findings-tasks.csv`: one row per unique requested ID, including tasks
  with no matching findings and failed fetches. It contains the full current
  verdict, version audit statuses/errors, QA run statuses/errors, counts of
  agent-run analysis statuses, counts of exported findings by tier, and
  `fetch_error`. Structured detail is stored as JSON inside CSV cells.

The default tier is `must_fix`. Repeat `--tier` to select tiers;
`optional` is also supported. Counts reflect exported occurrences, not unique
defects. A finding reported by two runs keeps two rows with distinct trial IDs;
version-audit findings appear once per version. CSV quoting preserves commas,
quotes, newlines, and Unicode in the original feedback.

The default scope is the task's current version. `--all-versions` also exports
older versions returned by the detail API. The `current_verdict*` columns always
describe the current task verdict, including on older-version finding rows;
they are not historical verdicts. Replaced runs and combined copies are already
excluded by the existing task-detail endpoint. This is an export of currently
stored findings, not a complete history of overwritten QA assessments.

This command only reads results; it never queues or reruns QA. Zero findings
does not mean QA passed: inspect audit, analysis, and verdict status in the
task summary. Failed fetches leave counts blank, preserve successful tasks,
and cause exit code 1 after the batch finishes. Successful fetches exit 0 even
when QA reports defects or is unfinished.

`--concurrency` controls simultaneous requests (default 4, range 1–16).
`--api` overrides the API URL; the usual `ODDISH_API_KEY`, `ODDISH_API_URL`,
and `ODDISH_PREVIEW_PR` settings apply. The exporter uses one existing task-detail
request per ID, with a 30-second HTTP timeout and no automatic retry.

## Check Progress

Use `oddish status` to inspect the system, a task, or an experiment.
Task status tables include a `Detail` column for the current Harbor stage or
terminal reason, such as `cancelled by user`.

```bash
# System overview
oddish status

# Queue & worker scheduler diagnostics
oddish status --queue
oddish status --queue --json

# Task status
oddish status <task_id>

# Single-trial detail (status, tokens, cost, analysis)
oddish status <trial_id>

# Task version history + per-version cost rollups
oddish status <task_id> --detail

# Task version list (or a single version)
oddish status <task_id> --versions
oddish status <task_id> --versions --version 2

# Experiment status
oddish status --experiment <experiment_id> --watch

# Single JSON snapshot (no live watch) for scripts/agents
oddish status <task_id> --json
```

If a positional ID isn't found as a task, `status` automatically retries it as an experiment ID.

One thing to know when scripting against `status <task_id> --json`: the
response's `trials` list is every current-version trial, including the
platform's own QA and audit runs (rows whose `kind` is `"qa"` or `"audit"`),
but the top-level `total`, `completed`, `failed`, and `running` counters count
only evaluation attempts (`kind == "agent"`). Filter the `trials` list on
`trials[].kind == "agent"` when reproducing those counters yourself.

Options

- `TASK_ID` - Task ID to inspect when not using `--experiment`; a trial ID (`{task_id}-{index}`) shows a single-trial detail view, and an unmatched ID falls back to experiment lookup
- `--experiment`, `-e TEXT` - Inspect an experiment instead of a task
- `--detail` - Show a task's version history + per-version cost rollups (`GET /tasks/{id}/detail`; task ID required)
- `--versions` - Show a task's version list; add `--version N` for a single version
- `--version INTEGER` - With `--versions`, show only this version number
- `--queue`, `-Q` - Show queue & worker scheduler diagnostics instead of a task/experiment (see below)
- `--stale-after INTEGER` - Minutes without a heartbeat before a trial/job counts as stale (with `--queue`; default 15)
- `--watch`, `-w` - Poll until the task or experiment finishes
- `--verbose`, `-v` - Extra detail in the system overview
- `--api TEXT` - Override the API URL
- `--json` - Emit a single JSON snapshot (no live watch)

### Queue & Worker Diagnostics

`oddish status --queue` aggregates the scheduler's `/admin/*` diagnostics so you
can debug "queued but not running", stuck slots, and zombie/stale workers
**without direct database access**. It shows:

- **Queue health** — total queued/running, per-queue-key capacity
  (`Queued` ready, `Sched` waiting on retry backoff, `Running`, `Limit`, `Fill`,
  oldest-queued age), and the dispatcher/reconciler heartbeat ages (is the
  scheduler alive?).
- **Slot leases** — how many `queue_slots` are leased per queue key.
- **Stuck / orphaned** — trials whose heartbeat has gone stale and tasks left
  active with no downstream work, including the worker id / slot / last
  heartbeat for each stale trial sample.
- **Worker jobs** — per-`(kind, status)` counts and recent failures (hosted
  Oddish only; omitted on a self-hosted core server).

```bash
oddish status --queue                  # human-readable panel
oddish status --queue --json           # combined JSON for agents/scripts
oddish status --queue --stale-after 30 # widen the stale-heartbeat window
```

To see which experiments a queue key's work belongs to, pass that key to
`oddish experiment ls --queue KEY` (add `--queue-state queued` or `running` to
match the Queued or Running column). The dashboard's queue links open the same
list at `/dashboard?queue=KEY&queue_state=...`. The list needs only a `read` key.

On hosted Oddish these diagnostics require a **full-scope** API key
(`read`/`tasks` keys get a clear error); a self-hosted core server applies no
auth.

### Operator Concurrency Controls

Hosted Oddish operators can inspect every layer of a queue key's concurrency
limit and make a database-backed override without using a raw API request:

```bash
# Read the deploy limit, DB override, controller advisory, and effective limit
oddish admin concurrency get xai/v9m-rl-learnability-tp8

# Set or clear the database override
oddish admin concurrency set xai/v9m-rl-learnability-tp8 300
oddish admin concurrency clear xai/v9m-rl-learnability-tp8

# Machine-readable output for audit logs and automation
oddish admin concurrency get xai/v9m-rl-learnability-tp8 --json
```

Queue keys are canonicalized by the same model-aware helper as the dispatcher.
`set` accepts limits from `0` through `10000`; `0` disables dispatch for that
queue. Both mutation commands read the setting back and fail if the stored
override does not match. These hosted endpoints require an admin key in the
configured operator organization. Use `--api-url`/`-u` to target another API.

## Claude provider selection

The submitted model selects the provider for Claude Code and LiteLLM agents:

| Input | Stored model and queue key | Provider |
| --- | --- | --- |
| `anthropic/claude-opus-4-8` | `anthropic/claude-opus-4-8` | Anthropic |
| `claude/claude-opus-4-8` | `anthropic/claude-opus-4-8` | Anthropic |
| `bedrock/global.anthropic.claude-opus-4-8` | `global.anthropic.claude-opus-4-8` | Bedrock |
| Native Bedrock profile, foundation ID, or ARN | Canonical native Bedrock ID | Bedrock |

Prefix-free `claude-*` uses `ODDISH_CLAUDE_CODE_FORCE_DIRECT_API` at submission:
`1` (the default) chooses Anthropic; `0` chooses Bedrock. The stored choice
survives worker restarts, retries, and later flag changes. An Anthropic key,
including BYOK, never redirects an explicit Bedrock submission. Missing keys
fail on the selected provider. Direct Anthropic BYOK also reaches separate
Harbor child processes. `anthropic-hdo/`, Vertex, and OpenRouter retain
their separate routes. The optional QA gateway does not intercept ordinary
Anthropic or Bedrock routes; its separately opted-in HDO behavior is retained.

Anthropic and Bedrock have independent model queues and concurrency limits.
Existing Bedrock overrides remain Bedrock overrides. Configure Anthropic
limits separately, for example:

```bash
oddish admin concurrency set anthropic/claude-opus-4-8 10
oddish admin concurrency get global.anthropic.claude-opus-4-8
```

This changes the previous meaning of `anthropic/` and `claude/` submissions,
which were stored as Bedrock IDs. Current and older CLI request fields remain
accepted; there is no database migration. Historical rows retain their stored
identity because their original prefix cannot be recovered.

For rollout, first validate actual Anthropic and Bedrock calls in preview,
including current AWS credentials, for Claude Code, a LiteLLM agent, and the
Harbor child process. Then pause submissions and drain **all nonterminal Claude
work**, including blocked trials and scheduled retries. Deploy matching API and
workers, configure the new Anthropic limits, and resume submissions. Verify
paired trials, authentication errors, queue progress, funding, and costs.
Before rollback, pause and drain newly queued work as well: old workers would
reinterpret Anthropic-prefixed models. Do not rewrite historical rows.

## Rerun QA for a delivery

Pre-trial QA checks the task files. Post-trial QA reviews existing agent runs
and updates the task verdict. To rerun both for every task in an active delivery:

```bash
oddish delivery qa rerun "september-batch" --dry-run
oddish delivery qa rerun "september-batch"
oddish delivery qa status "september-batch"
oddish delivery qa status "september-batch" --json
```

To submit at most 100 tasks per invocation:

```bash
oddish delivery qa rerun "september-batch" --limit 100 --dry-run
oddish delivery qa rerun "september-batch" --limit 100
oddish delivery qa rerun "september-batch" --limit 100 --offset 100
```

`--limit` accepts any positive integer; `--offset` skips that many tasks and
must be nonnegative. Tasks are deduplicated and sorted by task ID before
selection. Without these flags, all tasks are submitted. An offset at or past
the total fails without submitting work. Text output shows the selected range
and total; JSON includes `total_tasks`, `offset`, and `limit` (null if omitted).
Each invocation submits only its selected tasks and exits without waiting for
QA to finish. Keep delivery membership unchanged between batches: adding or
removing tasks can shift offsets and cause repeats or omissions. Current task
versions are captured afresh on each invocation. Repeating the same offset
reruns the same tasks; it does not automatically advance to the next batch.

Use a delivery name or ID. `--dry-run` lists the selected task versions without
starting paid work. The rerun captures the delivery's membership and current
task versions, then submits up to four requests at once. Each request queues a
source check followed by a run review; the server finishes both after the CLI
exits. Automatic QA can be disabled on the task: this explicit request still
runs both stages without changing that setting. Existing agent runs are reused.

A source check already in progress is reused and reported as `already_running`;
its post-trial review is explicitly requested. Post-trial QA waits for the source
check's results and any existing agent/QA runs. A changed task version is refused
instead of silently reviewing different files. Each task commits independently;
submission failures are printed with task IDs in `--json`, other tasks continue,
and the command exits 1 if any submission failed. A network timeout means the
submission is unconfirmed: inspect status before retrying. The command does not
automatically retry requests. A later rerun starts another pass for finished tasks.

`status` shows per-task pre-trial execution and post-trial results, with totals
for each state. Pre-trial `success` means the source check completed, not that
the source has no defects. Post-trial `needs_fixes` is a completed rejection;
`error` means the review failed or has insufficient evidence, and `outdated`
means its evidence no longer matches. Status is read-only and exits 0 after a
successful fetch, even if QA is unfinished or rejects tasks. Use
`oddish delivery ready NAME` for a pass/fail shipping check, including manual
sign-off. Finalized deliveries show frozen results and cannot be rerun through
this command. Older snapshots without source-check status show `unknown`.

Both commands accept `--api` and `--json`. The rerun requires a server with
`POST /tasks/{task_id}/qa/full`; an older server returns an error rather than
silently skipping the post-trial request.

## Backfill Analysis

Use `oddish backfill-analysis` to (re)run task-level QA — LLM trajectory
classification plus the task verdict — for an experiment, a task, or a single
trial. Pass exactly one of `--experiment`, `--task`, or `--trial`.

QA is one pass per task, and each invocation queues a fresh pass that re-reads
and re-classifies **every** live trial of the task and recomputes the verdict,
whatever flags you pass. In particular, `--trial` does not analyze just that
trial — it can't; the pass costs the same as a full task re-run. What `--trial`
and `--force` actually control is which *stored* results are cleared up front
(so the dashboard shows them as pending) rather than staying visible until the
new results replace them:

- default: nothing is cleared; new results overwrite old ones as they land.
- `--force` with `--task` or `--experiment`: clears every live trial's stored
  analysis first.
- `--trial <id> --force`: clears just that trial's stored analysis first.

A pass only starts once all of the task's trials are finished, and is refused
while another QA pass or a pre-trial audit is live. Before changing stored
analysis or queuing the replacement, Oddish reads every eligible source trial's
Harbor result, verifier output or exception, and every trajectory advertised by
`has_trajectory`. A present verifier stream is available even when its contents
are empty. For historical trials, the reader can rebuild a malformed Claude
trajectory from that same attempt's captured `claude-code.txt`; a missing S3
pointer is recoverable only for a finished row whose database attempt number
matches the sole stored attempt. If evidence remains unavailable, the command
is refused with the blocking trial IDs; no QA model attempts are launched and
existing analysis and verdict data remain unchanged. If the check passes,
queuing the replacement withdraws the previously published verdict until the
new pass completes.

```bash
oddish backfill-analysis --task <task_id>
oddish backfill-analysis --trial <trial_id> --force
oddish backfill-analysis --experiment <experiment_id>
```

Options

- `--experiment TEXT` - Queue a QA pass for every task in an experiment
- `--task TEXT` - Queue a QA pass for one task
- `--trial TEXT` - Queue a QA pass for the trial's parent task (with `--force`, only this trial's stored analysis is cleared first)
- `--force` - Clear the targeted trials' stored analyses before the pass runs
- `--json` - Emit machine-readable output.
- `--api TEXT` - Override the API URL

## View Costs

Use `oddish costs` to see billable-spend accounting **without direct DB access**.
By default it shows the org-wide breakdown; pass `--user <id>` for one user's
billed spend. Admin-only on hosted Oddish (a full-scope API key); not available
on a self-hosted core server.

```bash
# Org-wide spend over the last 7 days (default)
oddish costs

# All-time, machine-readable
oddish costs --window-days 0 --json

# One user's billed spend over 30 days
oddish costs --user <user_id> --window-days 30

# Per-task cost by type for an experiment
oddish costs --experiment <experiment_id>

# Specific tasks, all versions, as CSV
oddish costs --task my-task --task other-task --csv
```

Each task row splits spend into Solver (the agent runs, excluding imported
runs), QA, Audit, Summaries, Judge & other (Reward Kit judges and
older QA charges), and Verifier (computer-use verification), plus Total and
the QA share. With `--experiment`, Solver counts only that experiment's runs;
review and verifier spend is the task's total, since one QA pass covers the
whole task. `--task` and `--experiment` need only a read key and also work on
a self-hosted server.

Options

- `--user TEXT` - Show one user's billed spend (by id) instead of the org-wide breakdown
- `--window-days INTEGER` - Trailing window in days; `0` = all-time (default 7)
- `--task, -t TEXT` - Per-task cost by type (solver, QA, audit, summaries, judges, verifier); repeatable
- `--experiment, -e TEXT` - The same split for every task in an experiment (id)
- `--csv` - With `--task`/`--experiment`, write CSV
- `--api TEXT` - Override the API URL
- `--json` - Emit the raw cost breakdown JSON

## Remove Spend Tracking

Hide spend that was never really paid for - sponsored capacity, free preview
tiers, vendor credits, a comped run. Excluded spend drops off the admin cost
dashboards and stops counting against quotas. It is still shown on experiment,
task, and trial pages, marked as not real, so the two never disagree silently.

- **Models** - every trial that used the model stops counting, including the
  same model name through another provider.
- **Experiments** - trials the experiment ran itself stop counting. Trials it
  gathered from elsewhere keep counting on the experiment that ran them.

Both lists are deployment-wide and retroactive: adding an entry removes spend
already recorded, removing one puts every dollar back. Operator-only on hosted
Oddish (a full-scope API key in the operator org); not available on a
self-hosted core server. Also editable in the admin dashboard under
Costs -> Remove Spend Tracking.

```bash
# What currently doesn't count
oddish cost-exclusions list
oddish cost-exclusions list --kind model --json

# Stop counting a free model, and a comped experiment
oddish cost-exclusions add model kimi-k2 --label "sponsored"
oddish cost-exclusions add experiment "glm sweep" --label "comped"

# Put the spend back (by row id, model name, or experiment name/id)
oddish cost-exclusions remove model kimi-k2
oddish cost-exclusions remove experiment exp_01j...
```

Options

- `--kind TEXT` - On `list`, limit to one axis: `model` or `experiment`
- `--label TEXT` - On `add`, why it doesn't count (e.g. `sponsored`)
- `--api TEXT` - Override the API URL
- `--json` - Emit raw JSON

`add model` matches what trials actually store, so `kimi-k2` finds trials saved
as `moonshot/kimi-k2`. A model no trial has ever used is rejected rather than
saved as an entry that matches nothing. Experiments take a name or an id;
ambiguous names are rejected, and a collection is rejected because it runs no
trials of its own.

## Targeting a PR Preview

Every open PR gets its own isolated preview stack: a Modal app
(`oddish-pr-<N>`), a Supabase Postgres branch, and a Vercel preview build —
provisioned automatically by `.github/workflows/pr-preview.yml`. To point
the CLI at a preview from your laptop:

```bash
# 1. Point at the preview backend by PR number.
export ODDISH_PREVIEW_PR=35

# 2. Sign in at the preview Vercel URL (printed in the PR's
#    Actions step summary), create an API key in the dashboard,
#    and export it. Preview keys are formatted `ok_pr-<N>_<hex>`
#    so a stray paste into a prod context is visually obvious.
export ODDISH_API_KEY=ok_pr-35_…

# 3. Run as usual — every command now hits the preview Modal +
#    Supabase branch DB.
oddish run /path/to/task --agent gemini-cli --model gemini/gemini-3.1-pro-preview
oddish status
```

API URL resolution order is `ODDISH_API_URL` (explicit) >
`ODDISH_PREVIEW_PR` (derived) > prod default. Forks change the URL
pattern by setting `ODDISH_PREVIEW_URL_TEMPLATE` (with `{n}` for the
PR number).

## Combine Experiments

Use `oddish combine` to merge two or more experiments into a brand-new
result experiment. The source experiments are left untouched; their task
memberships and finished trials (with artifacts) are copied into the new
experiment, so you get a single rolled-up view.

```bash
# Combine two experiments (by ID or name)
oddish combine <experiment_a> <experiment_b>

# Name the result and combine three experiments
oddish combine <exp_a> <exp_b> <exp_c> --name nightly-rollup

# Reference source artifacts in place instead of duplicating them
oddish combine <exp_a> <exp_b> --no-copy-artifacts
```

In-flight trials (still pending/queued/running) have no result to combine
and are skipped; the response reports how many were copied vs. skipped.

Options

- `SOURCE_EXPERIMENT_IDS...` - Two or more experiment IDs or names to combine
- `--name`, `-n TEXT` - Name for the result experiment (auto-generated if omitted)
- `--copy-artifacts / --no-copy-artifacts` - Duplicate each copied trial's
  artifacts so the result is fully independent (default), or reference the
  source artifacts in place (cheaper, shared storage)
- `--json` - Print the raw JSON response
- `--api-url`, `-u TEXT` - Override the API URL

## Delete Data

Use `oddish delete` to delete tasks, experiments, or trials. What each
deployment allows:

- **Trial deletion** (`--trial`) works against hosted Oddish (oddish.app). It
  is admin-only — a full-scope API key — and soft-deletes the trial by setting
  its `deleted_at` timestamp. The database row and S3 artifacts remain for
  audit and restoration, while normal API and dashboard queries hide the row.
- **Whole-task and whole-experiment deletion** is refused by the CLI against
  any Modal-hosted API (hosted oddish.app and Modal self-hosts alike); the
  command exits with "Cleanup is not available for hosted Oddish instances."
- A **standalone core server** has no delete endpoints at all: its API surface
  is append-only by policy, and an operator removes data with the
  `delete_{task,experiment,trial}_core` helpers instead.

```bash
# Delete an experiment
oddish delete --experiment <experiment_id>

# Delete a task
oddish delete <task_id>

# Delete one or more trials and emit a JSON result
oddish delete --trial <trial_id> --json
```

Options

- `TASK_ID` - Task ID to delete when not using `--experiment` (refused for Modal-hosted APIs)
- `--experiment`, `-e TEXT` - Delete an experiment instead of a task (refused for Modal-hosted APIs)
- `--trial`, `-t TEXT` - Delete one or more trials (repeatable); works against hosted Oddish (admin-only)
- `--yes`, `-y` - Skip confirmation prompts
- `--api-url`, `-u TEXT` - Override the API URL
- `--json` - Emit the delete result as JSON (implies `--yes`)

## Assign QA review work

Assign current task versions to an organization member without selecting a
delivery. Active delivery boards show the same owner on their next refresh.
Use a full-scope API key; hosted assignment requires administrator access.

```bash
# Assign explicit task IDs using an email, user ID, or GitHub handle
oddish assign task-1 task-2 --to alice@example.com

# Assign a batch of 200 IDs from a text file, one ID per line
oddish assign --tasks-file task-ids.txt --to @alice --json

# Intentionally replace other people's existing assignments
oddish assign --tasks-file task-ids.txt --to alice@example.com --replace
```

The command accepts up to 1,000 unique task IDs. File contents are separated by
whitespace; repeated IDs are processed once. It skips tasks owned by someone
else unless `--replace` is set, and reports those IDs. Tasks already owned by
the selected person retain their original claim time. Notes and issue categories
are preserved. `--json` returns `owner_user_id`, `assigned_task_ids`,
`unchanged_task_ids`, and `skipped_task_ids`.

All IDs must belong to your organization and have a current version; otherwise
the whole request fails without changing ownership. An unknown or ambiguous
assignee also fails; use the person's user ID to disambiguate. Assignments apply
to the current version, so a new version starts unassigned. Finalized delivery
snapshots remain unchanged. Standalone servers accept a local owner identifier
instead of resolving an organization member.

## QA issues by category

`oddish qa-issues` shows which kinds of QA problems an experiment or a
delivery has. It reads the QA results that Oddish already stores. It does not
run QA again and it changes nothing.

```bash
oddish qa-issues --experiment EXPERIMENT_ID
oddish qa-issues --delivery september-batch
oddish qa-issues --delivery september-batch --category tests-pass-wrong-work
```

The first lines split the tasks into three parts that add up to the total:
blocked (at least one blocking issue), clear (QA finished with no blocking
issue), and waiting for QA (the rest). A table then
counts issues per category. Each category is in one group: grading accuracy,
solution integrity, task design, or environment. Use the slug from the table
with `--category` to list that category's tasks, authors, PRs, and issues.

An issue is a `must_fix` finding (blocking), a trial that QA labeled as a task
problem (not blocking alone), or a reject verdict on a task with no `must_fix`
finding (blocking). A blocking finding blocks a task before its QA verdict is
ready. For an experiment, only the
trials in that experiment count. `--json` prints the full response.

## Deliveries

A delivery is a checklist for a set of tasks. It answers one question: can
we ship these tasks to a customer?

Each task must pass the automated checks. The checks are: the pre-trial
audit passed, the task has sufficient rollouts, the newest QA run accepts
the version, and each must-fix defect has an acknowledgement. The checks
always apply to the current default version of the task. A new version
makes the checks red again.

Customers are records of their own. The dashboard's create dialog offers
a dropdown of existing customers and a form for a new one. `oddish delivery customers` lists their IDs and names;
`oddish delivery create-customer "New Lab"` creates one directly. Both accept
`--json`. A duplicate name is a conflict.

The dashboard board can filter its task list: all tasks, blocked tasks
(a failing check or an open defect), tasks awaiting sign-off (every
check passes), or ready tasks. Use it to hide what is already approved. The filter, the
page, and the page size live in the URL (`?filter=`, `?page=`,
`?per_page=`), so a filtered view can be shared or reloaded. Each row also
has a selection checkbox (the header checkbox selects the whole filtered
view): the bulk bar signs off every clean selected task or removes the
selected tasks from the delivery in one action.

On the dashboard, each task row has a copy-link button. The link opens
the delivery board with that task expanded and scrolled into view, so you
can send a failing task straight to the person who owns it
(`/deliveries/<id>?task=<task-name>`).

Each task also needs a manual sign-off. The server records who signed off
and which version they saw. If the task has a must-fix defect, a person
must acknowledge that defect first. A task can also ship with a failing
automated check, but a person must acknowledge the check first. The server
records who acknowledged each defect and each check. A delivery can define
more manual checks in its check configuration.

```bash
# Create a delivery and add tasks to it over time
oddish delivery create "august-batch" --customer "Acme" -t my-task-name
oddish delivery add august-batch task-3 task-4

# The board: every task, every check, every blocker
oddish delivery show august-batch

# The gate for scripts and CI: exit 0 when green, 1 with blockers
oddish delivery ready august-batch && ./ship.sh

# Acknowledge a defect, then sign the task off (both record who did it)
oddish delivery ack august-batch task-1 <defect-id>
oddish delivery signoff august-batch task-1

# Ship a task with a failing check anyway: acknowledge the check by key
oddish delivery ack august-batch task-1 min_rollouts

# Sign off a task with open blockers: the command warns, lists them, and
# asks for confirmation. On yes, it acknowledges each one in your name.
oddish delivery signoff august-batch task-2

# Sign off every task with no open blockers in one command
oddish delivery signoff august-batch --all

# Tick a custom sign-off check (defined in the delivery's check config)
oddish delivery check august-batch proofread --task task-1

# Pin versions and freeze the record once everything is green
oddish delivery finalize august-batch

# A task's QA trail: versions, audits, rollouts, defects, QA runs
oddish delivery history task-1

# Historical delivery metadata (docs/delivery-metadata-backfill.md):
# export this organization's task identities for the planner, then preview
# and apply a reviewed plan. --apply needs admin and a preview from the last day.
oddish delivery inventory --output inventory.json
oddish delivery import-history plan.json --inventory inventory.json --customer meta=Meta
oddish delivery import-history plan.json --inventory inventory.json --apply
oddish delivery import-receipts
```

Commands

- `customers` / `create-customer NAME` - List or create customers (`--json` supported)
- `list` - List deliveries
- `create NAME` - Create a delivery. `--customer` is required: an existing
  customer's name or id, or a new name (the server creates the customer).
  Also `--description`, `-t/--task`, and `--tasks-file` for a saved selection.
- `show DELIVERY` - Render the readiness board (`--json` for the full matrix)
- `ready DELIVERY` - Exit 0 if every check passes, 1 with the blockers listed
- `add DELIVERY TASKS...` / `remove DELIVERY TASK` - Manage membership
  (tasks accepted by id or name)
- `signoff DELIVERY TASK` - Sign a task off (`--off` removes the sign-off).
  With open blockers, the command warns and asks first; `-y` skips the
  prompt and acknowledges them. `--all` signs off every task with no open
  blockers and lists the skipped ones
- `ack DELIVERY TASK REF` - Acknowledge one must-fix defect (by defect id)
  or one failing automated check (by check key)
- `check DELIVERY KEY` - Tick a custom manual check (`--task` for
  task-scoped, `--off` to untick, `--note` to annotate)
- `finalize DELIVERY` - Pin task versions and freeze the delivery (`-y` skips
  the prompt)
- `history TASK` - Per-version QA history for one task

Deliveries accept an ID or a unique name everywhere. On the hosted API,
mutations need an admin role (or a full-scope key); reads work with a
`tasks`-scope key.

## Drag-and-drop import (UI)

The dashboard's **Tasks** page has an **Import** button next to the
search input that opens the same flow as `oddish upload`, but driven
from the browser. Drop one or both of:

- a Harbor task zip (e.g. `zip -r my-task.zip my-task`)
- a Harbor run zip — either a single job dir (with `result.json`) or a
  parent dir of job dirs

The dialog accepts:

- **Task only** → registers a new task version (or no-op when content
  is unchanged).
- **Run only** → imports every Harbor trial in the zip into the target
  task (ID or name; leave the field blank and the backend infers the task
  from the run zip's job-dir name).
- **Task + run** → uploads the task first, then imports the trials
  against it (the UI equivalent of `oddish upload ./jobs --path ./my-task`).

The optional **Experiment name** field maps to `--experiment`; leaving
it blank auto-generates a fresh experiment, matching the CLI default.
The **Tags** picker attaches tags to the imported task (no CLI
equivalent in `oddish upload`). Re-uploading the same task content is
idempotent — content-hash unchanged → no new version.

For very large archives or scripted/CI flows, prefer the CLI: the UI
caps each uploaded zip at 1 GiB.

## Benchmark Metrics (metrics.json)

A task can report structured benchmark numbers by having its **verifier**
write `metrics.json` next to `reward.txt` (i.e. `/logs/verifier/metrics.json`
inside the sandbox). Oddish persists the parsed object onto the trial and
returns it as the trial's `result` in the API.

Contract:

- A single JSON **object**, at most **64 KiB**. Anything else (missing,
  malformed, oversized, non-object) is ignored — metrics can never fail a
  trial whose reward already settled.
- Include `"schema_version": 1` so downstream consumers can evolve.
- Recommended keys for performance benchmarks (all optional):
  `latency_ms`, `step_time_ms`, `ttft_ms`, `throughput_tokens_per_sec`,
  `mxu_utilization_pct`, and for MoE workloads `routing_overhead_ms`,
  `gating_overhead_ms`, `ici_time_ms`, `expert_load_balance`.
  Task-specific keys are fine alongside.

```bash
# tests/test.sh
echo 1 > /logs/verifier/reward.txt
cat > /logs/verifier/metrics.json <<'JSON'
{"schema_version": 1, "ttft_ms": 12.5, "throughput_tokens_per_sec": 4300}
JSON
```

## Test Results (ctrf.json)

Test-based tasks can expose their `tests` (total), passed, failed, skipped, pending, and other counts
by writing a [Common Test Report Format](https://ctrf.io/) report to
`/logs/verifier/ctrf.json`. Current Harbor tasks commonly do this with
`pytest-json-ctrf`:

```bash
uvx --with pytest --with pytest-json-ctrf \
  pytest --ctrf /logs/verifier/ctrf.json /tests -rA
```

Oddish keeps the full report with the trial artifacts and persists only its
compact `results.summary` counts, `results.tool.name`, and the trial-relative
report artifact path under the reserved `trial.result._verifier` key. The
dashboard shows those counts as a small passed/total line in the trial
drawer's summary. `results.summary` must carry all six counts (`tests`,
`passed`, `failed`, `skipped`, `pending`, `other`) as non-negative integers —
a report missing any of them is dropped whole. Missing, malformed, or
oversized CTRF reports are ignored and never change the settled `reward`;
verifiers without a test report simply show no test line.

Delivery sign-off requires resolving or individually acknowledging every
reported defect, including historical `optional` findings.
`oddish delivery check`, `ack`, and `signoff` send the task version shown by the
board; a version change requires reviewing the board again. `oddish delivery
history` retains original severity labels and shows the current shipment
requirement. An acknowledgment permits an exception without deleting a finding.

### Homebrew installation

The private Homebrew tap requires GitHub read access to `abundant-ai/homebrew-tap`.
Install `gh` with `brew install gh`, then run `gh auth login` and
`gh auth setup-git`. Install with `brew install abundant-ai/tap/oddish`.
Set `ODDISH_API_KEY` for your Oddish organization before making API requests.
Update with `brew update && brew upgrade abundant-ai/tap/oddish`. Homebrew owns
this installation; `oddish update` directs you to Homebrew instead of replacing
it from PyPI. `oddish version --check` prints the Homebrew check command.
The Homebrew package includes CLI and shared client helpers, not the Oddish
server, database, or worker implementations. Releases are maintained in
https://github.com/abundant-ai/homebrew-tap.

### Share a task selection

Save the exact IDs of the current matching tasks for your organization, or open
an existing selection from the Tasks page. This uses the hosted saved-view API:

```bash
oddish ls --qa-outcome rejected --steps-p50-min 100 --share-selection "Long horizon needs work" --json
oddish ls --selection-id SAVED_ID --json
oddish ls --selection-id SAVED_ID --ids --json > selection.json
oddish delivery add BATCH --tasks-file selection.json
```

Sharing requires 1–5,000 tasks and refuses truncated results before creating a
selection. The selected IDs stay fixed; their current task versions and metadata
can change. A saved filter view instead reevaluates its criteria whenever opened.
`rejected` is the UI's “Needs work”; `failed` means the QA run failed, and `outdated`
means the current task version needs a new review. Median steps include unsuccessful
agent trials with recorded steps; selecting a model does not recalculate that median.
