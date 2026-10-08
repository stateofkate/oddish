# Task browsing for agents

Use `/orgs/ORG/tasks` with URL query parameters to choose the task set and
presentation. Every committed filtering choice is read from the URL; agents can
navigate directly without opening dropdowns. Parameters compose with one another.

| Goal | Browser URL parameter | CLI equivalent |
|---|---|---|
| Search | `q=wine` | `oddish ls --query wine` |
| Only my tasks | `mine=only` | `oddish ls --author me` |
| Everyone, mine first | `mine=first` | `oddish ls --pin-author me` |
| Everyone, no priority | omit `mine` (default) or `mine=off` | `oddish ls` without author flags |
| Coverage | `run_coverage=scored_model_trials` | `oddish ls --run-coverage scored_model_trials` |
| Review outcome | `qa_outcomes=unreviewed,outdated` | `--qa-outcome unreviewed --qa-outcome outdated` |
| Model | `models=openai/gpt-5` | `--model openai/gpt-5` |
| Sort | `sort=model_score_desc` | `--sort model_score_desc` |
| Registry tasks (on a registry repo's `main`) | `tags=registry-main` | `--tag registry-main` |
| No recorded delivery to customer | `not_delivered_to=GDM` | `--not-delivered-to GDM` |
| Page | `offset=24` | `--offset 24` |
| Exact saved task set | `selection_id=ID` | `--selection-id ID` |
| Delivery destination | `delivery=ID` | `oddish delivery add ID --tasks-file selection.json` after explicit selection |
| Presentation | `view=cards` or `view=table` | `--json` exposes all fields independently of presentation |
| Table columns | `columns=QA%20outcome,Trials,Delivery%20history` | JSON includes all columns |
| Expanded task evidence | `expanded=TASK_ID` | `oddish ls --query TASK_NAME --json` includes the same preview, summaries, and experiments |
| Advanced-criteria panel | `filters=1` | `oddish ls --help` and `oddish ls --filter-options` |
| Expanded criteria groups | `filter_groups=Task,Trial` | Presentation only |
| Find a criterion | `filter_search=Median%20steps` | Presentation only |

Coverage accepts `any`, `no_trials`, `validation_only`, `model_trials`, and
`scored_model_trials`, all on the current default version. Model sorting also
accepts `model_score_asc` and `last_model_activity_desc`. Other filters and sort
values are documented by `oddish ls --help` and in the task-filter registry at
`frontend/src/lib/tasks-filters.ts`. Comma-separated browser values correspond
to repeated CLI flags where supported.

`oddish ls --json` includes exact `run_summary`, experiments, and the bounded
`latest_trials` preview with agent, model, reasoning effort, reward, and status.
Read an individual trial with `oddish status TRIAL_ID --json`. Inspect version
history with `oddish status TASK_ID --versions --json`, and task headers/cost rollups
with `oddish status TASK_ID --detail --json`. In the browser, trial
links use `/orgs/ORG/tasks/TASK_ID?trial=TRIAL_ID`; experiment task drawers use
`/orgs/ORG/experiments/EXPERIMENT_ID?task=TASK_ID`.

For an expanded row, include the filters and offset that put it on the page.
If absent from that page, the UI offers its task-detail link. Copy view retains
the expansion and its offset. Presentation-only changes do not fetch another
page or alter the matching count. Draft input, clipboard notifications, and
in-flight request state are temporary UI state rather than navigable resources.

URLs select read-only views and prepare a destination; opening a URL never
launches runs, changes reviews, or adds tasks to a delivery. Those operations
use explicit CLI commands or authenticated button actions. For example:

```sh
oddish ls --tag registry-main --run-coverage scored_model_trials --not-delivered-to GDM --ids --json > selection.json
oddish delivery add DELIVERY_ID --tasks-file selection.json
```

The IDs response exposes truncation; delivery commands reject an incomplete
selection. Working checkbox selections remain in the existing local basket.
Use `oddish ls --share-selection NAME [filters]` to create an organization-shared
exact set and address it with `selection_id`; opening that set does not overwrite
the user's basket or execute a delivery action.
