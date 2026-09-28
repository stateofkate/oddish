# Evaluation and operations tools

These are manually invoked tools, not modules called by the application. Lack of
an application import is not evidence that an operator tool is unused. Before
retiring one, check whether its operational purpose has a replacement.

## Read-only diagnostics

Run the Modal commands from `backend/` using its installed dependencies. Modal is
the cloud runtime that executes the diagnostic with the worker image and secrets.
`MODAL_SECRET_ENVIRONMENT` selects the secret environment (default `main`);
these tools use `oddish-prod` and `oddish-logfire` from that environment. Check
the intended environment before running. Output may contain internal logs.

| Command | Purpose |
| --- | --- |
| `modal run check_alembic_current_modal.py` | Read both database migration-version tables; compare with `uv run alembic heads` in `oddish/` and `backend/`. Does not apply migrations. |
| `python3 -m check_clerk_onboarding CLERK_DOMAIN` | From `backend/`, verify that Clerk requires organization membership and disables automatic and user-created organizations. Reads public settings without credentials; see [onboarding setup](../backend/README.md#prevent-accidental-organization-creation) for the dashboard changes. |
| `modal run probe_docker_failures_modal.py --hours 72 --limit 200` | Compare unscored trial state, saved result exceptions, and worker errors. Pending/running trials are not automatically failures; storage-read errors are reported separately. |
| `modal run scripts/tail_budget_report.py --eid <experiment-id>` | Measure serialized trajectory sizes and the fraction retained at different proposed byte limits. Token counts are estimates; this does not change reviewer limits. |
| `modal run ops_compare_token_costs.py --since <YYYY-MM-DD> --model <provider/model> --input-rate <rate> --cache-read-rate <rate> --cache-write-rate <rate> --output-rate <rate>` | Compare recorded trial costs with an estimate using explicit USD-per-million-token rates. Does not change costs or reconcile invoices. |

The cost report includes nondeleted completed trials in nondeleted experiments,
filtered to billed or native non-combined runs. This is the tool's
comparison population, not a claim to reproduce every dashboard billing filter.

## Staging database

Git staging uses Neon `main`. The data move is an offline operator task; no
copy or bootstrap runs in CI. The old source connection is saved in the staging
Modal secret `oddish-staging-supabase-source` for recovery.

Normal deploys use `.github/scripts/staging/deploy_staging.sh`. They apply both
migration stacks, publish Neon's pooled URL to `oddish-staging-db`, and deploy.
They keep the existing data and API keys.

Keep **Automatic branching** off in the Supabase GitHub integration. Git `main`
keeps its production database and deployment path. Removing `supabase/config.toml`
does not change that cloud setting. See the
[Supabase GitHub integration guide](https://supabase.com/docs/guides/deployment/branching/github-integration).

## Preview historical delivery metadata

`python -m oddish.core.ingest.delivery_backfill` prepares a local preview from
the consolidated delivery-research JSON. It preserves source rows, resolves
explicit task IDs against an optional organization inventory, and reports identity
and category conflicts. It does not write database rows or set delivery approval.
The organization inventory it matches against comes from `oddish delivery
inventory`, and a reviewed plan is previewed or applied with `oddish delivery
import-history` (apply is admin-only). Both go through the hosted API; see
[the backfill runbook](delivery-metadata-backfill.md) for scope and tests.

## Rebuild task statistics

From `oddish/`, with the intended database configured:

```sh
uv run python -m oddish.core.backfill_task_version_model_metrics --batch 200 --limit 500
```

This rebuilds task-browser summaries for existing task versions using the current
aggregation code. It does not launch evaluations. Every batch commits separately;
resume with `--after-id <last-logged-version-id>`. Without that argument a rerun
starts at the beginning. Versions with no eligible trials do not stall paging.

For a preview whose browse counters need repair,
run `uv run python ../.github/scripts/preview/refresh_browse_summaries.py` from
`backend/`, with `ODDISH_DATABASE_URL` pointing to the preview and the normal
`NEON_BRANCH_ID` and `NEON_PARENT_BRANCH_ID` set. This rebuilds browse
counters from the preview's own trials, in batches of 200
versions. This is a manual full repair. Normal deploys skip it. First branch setup
refreshes only versions whose copied active trials it stops, in the same database
transaction. Reruns of this tool repair stale counters without replacing tasks.

## QA evals: goldens and prompt versions

The QA Evals page (`/admin?tab=qa-evals`, for org admins) scores `/qa-evals`
replays of QA prompt versions against golden cases: human labels stored in
`qa_golden_cases`, at most one per trial. Only counted goldens score runs:
reviewed, with the task unchanged since labeling (otherwise waiting or
stale). Every QA route requires an org admin (for an API key, the full scope).

Every `oddish qa evals` and `oddish qa goldens` command takes `--api` and `--json` and prints the link to
the page view showing the same data, on the dashboard serving that API
(a PR preview API links `pr-N.oddish.app`, staging links `staging.oddish.app`;
`ODDISH_DASHBOARD_URL` overrides). The page and the CLI share URL keys, each
left out at its default: `stage` (`post_trial`), `chart` (`history` or
`models`), `run`, `filter` (a case group: `wrong`, `right`, `not_judged`,
`no_golden`), `version` and `base_version`, repeated `trial` (picked to
rerun) and `hide` (model names hidden in the chart legend). The labeling view
adds `label=1`, implied by `experiment` (id or name) or `review`
(`waiting` or `stale`), plus `member` (`me`, `all` or a user id), `search`
(with `author:` and `tag:` words) and `queue` (`failed`, `passed`, `labeled`
or `all`).

| Command | Purpose |
| --- | --- |
| `oddish qa evals status [--stage S]` | Goldens by label, waiting and stale; every run newest first with its score, cost and a `frontier` mark (no cheaper run is as accurate); each model's latest run; spend per model. |
| `oddish qa evals run RUN [--filter GROUP]` | One run's group counts, its cases (of one group) and its error groups. |
| `oddish qa evals versions [--stage S]`, `diff SHA [--base SHA] [--stage S]` | Prompt versions newest first; a unified diff against `--base` or the previous version with text. SHA may be any unique prefix of a version with recorded text. |
| `oddish qa evals start (--prompt FILE \| --version SHA) [--trial ID…] [--rerun RUN [--filter GROUP]] [--model M] [--name N] [--stage S] [--yes]` | Replay every counted golden's trial with one prompt, or only picked trials (a subset run, left out of the charts). Asks first, with a cost estimate from past runs. |
| `oddish qa goldens experiments [--member me\|all\|WHO] [--search TEXT] [--limit N]` | Experiments to label from: yours by default, everyone's, or one member's by email or GitHub handle. |
| `oddish qa goldens trials (EXPERIMENT \| --review waiting\|stale) [--queue failed\|passed\|labeled\|all]` | The labeling queue: finished solver trials, failed first, with production QA's answer and each one's golden. |
| `oddish qa goldens label TRIAL CLASSIFICATION [--note TEXT]` | Label a trial; the label counts at once. The same label again confirms it. |
| `oddish qa goldens list [--stage S] [--source S] [--waiting] [--trial ID…]`, `review ID… \| --all-waiting [--source S]`, `retire ID…` | List, review (count, and re-pin the task's content) and retire goldens. `--waiting` and `--all-waiting` take the goldens waiting for review, not stale ones. |
| `oddish qa goldens import FILE [--source S] [--where COL=VAL…] [--review-where COL=VAL…] [--review-all] [--dry-run]` | Add unreviewed goldens from a CSV/TSV (`source_trial_id` or `trial_id`, and `expected_classification` or `gold_class`) or a `gdm_source_audit_answer_key_v1` JSON answer key (pre-trial). Rows matching an existing golden (a copied trial's is found under its original) are only reviewed when chosen and waiting, so reruns are safe; rows disagreeing with one are refused (exit 1). |
| `uv run python -m oddish.core.backfill_qa_prompts --repo .. [--apply]` (from `oddish/`) | Record every committed version of the shipped QA prompts and recover past replay prompts' text. Previews unless `--apply`. |
| `uv run python scripts/seed_local_qa_evals.py --trials CSV [--comparison-run JSON] [--api URL] [--api-key-env NAME]` (from `backend/`) | Development only: copy the CSV's trials, their tasks and the comparison runs' replays from a deployed API (key in `ODDISH_API_KEY`, or the variable `--api-key-env` names) into the localhost database under org `qa-dev-org`, and print a local API key. |

## Urgent hotfix release rehearsal

Read-only dry-run of the promote gates (never pushes):

```sh
.github/scripts/promote/rehearse_urgent_hotfix.sh
.github/scripts/promote/rehearse_urgent_hotfix.sh <staging_commit_sha>
```

Procedure, required checks, rollback, and the filled rehearsal record:
[urgent-hotfix-release.md](urgent-hotfix-release.md).

## Evaluation instructions and historical material

Use [the SWE-Marathon runbook](swe-marathon-eval-runbook.md) for the evaluation
workflow. Its existing advice is unchanged by this restoration.
The [August campaign record](archive/swe-marathon-terra-campaign.md) is restored
verbatim from `eval-ops/PLAN.md` before #1628, including its original historical
qualification. The referenced campaign scripts remain in Git history.

## Scope of restoration from PR #1628

The diagnostics above and the statistics rebuild command have repeatable uses
beyond the original incident. Their tests and this index accompany restoration.
The old `eval-ops/` dispatch/delete loops embed campaign paths and experiments;
the historical record is retained without reactivating them. `eval-ops/passk.py`
also hardcodes local paths and duplicates the maintained success-rate estimator.

The `legacy_discover/transfer/validate` scripts implement a specific Sauron import;
the old cost-repricing and queue-key backfills implement historical data repairs.
They remain in Git at `205aa996^` for an explicitly scoped migration or repair,
rather than returning as current operational instructions. The Daytona teardown
test treated any lookup exception as proof of deletion, including network/auth
errors; it is not restored as a reliable test. The egress probe's incident notes
are recoverable there too, but its default sandbox does not reproduce a task's
image and network policy. Obsolete UI routes/components and uncalled internal
helpers are outside this operational restoration.
