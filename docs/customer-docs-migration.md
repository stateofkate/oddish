# Customer documentation migration

Source: DOCS.md at c94498b9. This inventory accounts for every level-two section. Customer guides are rewritten and example IDs are placeholders. Internal sections retain details until an owner reviews them. No job was launched to validate commands.

| Original section | Destination | Decision |
| --- | --- | --- |
| Installation | [docs/customer/getting-started.md](../docs/customer/getting-started.md) | Rewritten for customers; advanced options available from installed --help. |
| Usage | [docs/cli-advanced-internal.md#usage](../docs/cli-advanced-internal.md#usage) | Retained privately pending detailed content-owner review; common workflow covered in customer guides. |
| Agent Skill | [docs/customer/reference/commands.md](../docs/customer/reference/commands.md) | Rewritten for customers; advanced options available from installed --help. |
| Submit a Job | [docs/cli-advanced-internal.md#submit-a-job](../docs/cli-advanced-internal.md#submit-a-job) | Retained privately pending detailed content-owner review; common workflow covered in customer guides. |
| Preflight Checks | [docs/cli-advanced-internal.md#preflight-checks](../docs/cli-advanced-internal.md#preflight-checks) | Retained privately pending detailed content-owner review; common workflow covered in customer guides. |
| Upload Without Running | [docs/customer/guides/upload.md](../docs/customer/guides/upload.md) | Rewritten for customers; advanced options available from installed --help. |
| List Tasks | [docs/cli-advanced-internal.md#list-tasks](../docs/cli-advanced-internal.md#list-tasks) | Retained privately pending detailed content-owner review; common workflow covered in customer guides. |
| Compare QA models | [docs/cli-advanced-internal.md#compare-qa-models](../docs/cli-advanced-internal.md#compare-qa-models) | Retained privately pending detailed content-owner review; common workflow covered in customer guides. |
| Export QA Feedback | [docs/cli-advanced-internal.md#export-qa-feedback](../docs/cli-advanced-internal.md#export-qa-feedback) | Retained privately pending detailed content-owner review; common workflow covered in customer guides. |
| Check Progress | [docs/cli-advanced-internal.md#check-progress](../docs/cli-advanced-internal.md#check-progress) | Retained privately pending detailed content-owner review; common workflow covered in customer guides. |
| Claude provider selection | [docs/cli-advanced-internal.md#claude-provider-selection](../docs/cli-advanced-internal.md#claude-provider-selection) | Retained privately pending detailed content-owner review; common workflow covered in customer guides. |
| Stream Live Logs | [docs/customer/guides/monitoring.md](../docs/customer/guides/monitoring.md) | Rewritten for customers; advanced options available from installed --help. |
| Cancel In-Flight Runs | [docs/customer/guides/retries.md](../docs/customer/guides/retries.md) | Rewritten for customers; advanced options available from installed --help. |
| Backfill Analysis | [docs/cli-advanced-internal.md#backfill-analysis](../docs/cli-advanced-internal.md#backfill-analysis) | Retained privately pending detailed content-owner review; common workflow covered in customer guides. |
| View Costs | [docs/cli-advanced-internal.md#view-costs](../docs/cli-advanced-internal.md#view-costs) | Retained privately pending detailed content-owner review; common workflow covered in customer guides. |
| Remove Spend Tracking | [docs/cli-advanced-internal.md#remove-spend-tracking](../docs/cli-advanced-internal.md#remove-spend-tracking) | Retained privately pending detailed content-owner review; common workflow covered in customer guides. |
| Download Outputs | [docs/customer/guides/results.md](../docs/customer/guides/results.md) | Rewritten for customers; advanced options available from installed --help. |
| Targeting a PR Preview | [docs/cli-advanced-internal.md#targeting-a-pr-preview](../docs/cli-advanced-internal.md#targeting-a-pr-preview) | Retained privately pending detailed content-owner review; common workflow covered in customer guides. |
| Combine Experiments | [docs/cli-advanced-internal.md#combine-experiments](../docs/cli-advanced-internal.md#combine-experiments) | Retained privately pending detailed content-owner review; common workflow covered in customer guides. |
| Collect Trials into a Shared Collection | [docs/customer/guides/sharing.md](../docs/customer/guides/sharing.md) | Rewritten for customers; advanced options available from installed --help. |
| Delete Data | [docs/cli-advanced-internal.md#delete-data](../docs/cli-advanced-internal.md#delete-data) | Retained privately pending detailed content-owner review; common workflow covered in customer guides. |
| Share an Experiment | [docs/customer/guides/sharing.md](../docs/customer/guides/sharing.md) | Rewritten for customers; advanced options available from installed --help. |
| Assign QA review work | [docs/cli-advanced-internal.md#assign-qa-review-work](../docs/cli-advanced-internal.md#assign-qa-review-work) | Retained privately pending detailed content-owner review; common workflow covered in customer guides. |
| QA issues by category | [docs/cli-advanced-internal.md#qa-issues-by-category](../docs/cli-advanced-internal.md#qa-issues-by-category) | Retained privately pending detailed content-owner review; common workflow covered in customer guides. |
| Deliveries | [docs/cli-advanced-internal.md#deliveries](../docs/cli-advanced-internal.md#deliveries) | Retained privately pending detailed content-owner review; common workflow covered in customer guides. |
| Drag-and-drop import (UI) | [docs/cli-advanced-internal.md#drag-and-drop-import-ui](../docs/cli-advanced-internal.md#drag-and-drop-import-ui) | Retained privately pending detailed content-owner review; common workflow covered in customer guides. |
| Benchmark Metrics (metrics.json) | [docs/cli-advanced-internal.md#benchmark-metrics-metricsjson](../docs/cli-advanced-internal.md#benchmark-metrics-metricsjson) | Retained privately pending detailed content-owner review; common workflow covered in customer guides. |
| Test Results (ctrf.json) | [docs/cli-advanced-internal.md#test-results-ctrfjson](../docs/cli-advanced-internal.md#test-results-ctrfjson) | Retained privately pending detailed content-owner review; common workflow covered in customer guides. |

Verified first-run, retry, upload, status, collect and publish flags against `oddish/src/oddish/cli/`. Corrected the old retry guide: `core/verdict_state.py:101` withdraws a verdict when its replacement is queued. The old guide incorrectly said it stayed visible. No content includes real task IDs, task source, private dataset names or production screenshots.
