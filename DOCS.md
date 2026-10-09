# Oddish documentation

Daytona execution is disabled. Use `oddish run <task> --env modal` for CPU tasks,
`--env archil` for another CPU provider, or `--env thunder` / `--env modal` for
GPU tasks. The task must still declare its GPU count and type. Requests and
retries selecting Daytona fail without switching providers; resubmit with an
explicit supported provider. Existing Daytona sandboxes remain available for
status checks and cleanup. The default `ODDISH_HARBOR_ENVIRONMENT` is `modal`.

`--env unikraft` runs a task on Unikraft Cloud where the deployment enables it.
Hosted workers cannot build images, so the CLI builds and pushes each task's
image with the `unikraft` CLI first; it needs that CLI plus `UKC_TOKEN`,
`UKC_METRO` and `UKC_USER`. `oddish run --task <id> --env unikraft` appends
trials without local task files, so the image must already exist from an
earlier submission of that task version (remote builds are coming soon).

Customer guides are maintained in [docs/customer](docs/customer/getting-started.md). Read the guides at https://docs.abundant.dev/docs/oddish. The website requires a separate Abundant Docs account with Oddish documentation access. A docs account grants no task-workspace membership.

The [command reference](docs/customer/reference/commands.md) explains common commands. Run `oddish COMMAND --help` for options in your installed version. [Internal CLI details](docs/cli-advanced-internal.md) remain repository-only. [Migration inventory](docs/customer-docs-migration.md) records each moved section.

## Installation

See [Installation](docs/customer/getting-started.md).

## Usage

See [Usage](docs/cli-advanced-internal.md#usage).

## Agent Skill

See [Agent Skill](docs/customer/reference/commands.md).

## Submit a Job

See [Submit a Job](docs/cli-advanced-internal.md#submit-a-job).

## Preflight Checks

See [Preflight Checks](docs/cli-advanced-internal.md#preflight-checks).

## Upload Without Running

See [Upload Without Running](docs/customer/guides/upload.md).

## List Tasks

See [List Tasks](docs/cli-advanced-internal.md#list-tasks).

## Compare QA models

See [Compare QA models](docs/cli-advanced-internal.md#compare-qa-models).

## Export QA Feedback

See [Export QA Feedback](docs/cli-advanced-internal.md#export-qa-feedback).

## Check Progress

See [Check Progress](docs/cli-advanced-internal.md#check-progress).

## Claude provider selection

See [Claude provider selection](docs/cli-advanced-internal.md#claude-provider-selection).

## Stream Live Logs

See [Stream Live Logs](docs/customer/guides/monitoring.md).

## Cancel In-Flight Runs

See [Cancel In-Flight Runs](docs/customer/guides/retries.md).

## Rerun QA for a delivery

See [Rerun QA for a delivery](docs/cli-advanced-internal.md#rerun-qa-for-a-delivery).

## Backfill Analysis

See [Backfill Analysis](docs/cli-advanced-internal.md#backfill-analysis).

## View Costs

See [View Costs](docs/cli-advanced-internal.md#view-costs).

## Remove Spend Tracking

See [Remove Spend Tracking](docs/cli-advanced-internal.md#remove-spend-tracking).

## Download Outputs

See [Download Outputs](docs/customer/guides/results.md).

## Targeting a PR Preview

See [Targeting a PR Preview](docs/cli-advanced-internal.md#targeting-a-pr-preview).

## Combine Experiments

See [Combine Experiments](docs/cli-advanced-internal.md#combine-experiments).

## Collect Trials into a Shared Collection

See [Collect Trials into a Shared Collection](docs/customer/guides/sharing.md).

## Delete Data

See [Delete Data](docs/cli-advanced-internal.md#delete-data).

## Share an Experiment

See [Share an Experiment](docs/customer/guides/sharing.md).

## Assign QA review work

See [Assign QA review work](docs/cli-advanced-internal.md#assign-qa-review-work).

## QA issues by category

See [QA issues by category](docs/cli-advanced-internal.md#qa-issues-by-category).

## Deliveries

See [Deliveries](docs/cli-advanced-internal.md#deliveries).

## Drag-and-drop import (UI)

See [Drag-and-drop import (UI)](docs/cli-advanced-internal.md#drag-and-drop-import-ui).

## Benchmark Metrics (metrics.json)

See [Benchmark Metrics (metrics.json)](docs/cli-advanced-internal.md#benchmark-metrics-metricsjson).

## Test Results (ctrf.json)

See [Test Results (ctrf.json)](docs/cli-advanced-internal.md#test-results-ctrfjson).
