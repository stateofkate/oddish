# Delivery metadata implementation

Started September 16, 2026. Branch: `backfilling-oddish-metadata` (worktree `abundant/oddish/backfilling-oddish-metadata/`, based on staging `edbb9e7e`).

Goal: select tasks for a lab using permanent identity, task characteristics,
recorded delivery history, and evidence for the exact version being reviewed.

## Done: historical backfill and stored facts

The September 10 delivery research was turned into an offline import plan
(stable record IDs, conflicting IDs and name-only matches quarantined, nothing
inferred about shipped versions or customer acceptance). Migration
`delivery_history_001` added source-backed task aliases, metadata assertions
and historical delivery records, with task foreign keys `ON DELETE RESTRICT`
so retired tasks keep their history. The operator runs
`oddish delivery inventory` and `oddish delivery import-history`
(preview, then apply) against `GET /deliveries/task-inventory` and
`POST /deliveries/history-imports`; the import has run on production for the
Abundant organization (`8ebde5d0`). Commands and output definitions are in
[the runbook](delivery-metadata-backfill.md).

Task identity (which uploads are the same task, duplicate families, and the
history the import refused because the name matched no task or several) moved
to [the task identity plan](task-identity-todo.md).

## 3. Select tasks for a lab

The task browser (`/tasks`) is the selection surface; the delivery board
stays the place that verifies and ships. Backfilled facts reach the browser
through `browse_tasks_core` as `EXISTS` probes over the imported tables and
finalized deliveries, and the per-version summary row carries the stored
numbers the selection sorts on, so choosing a batch never aggregates trials
per request.

- [x] Browser filters for previous recipients (`delivered_to`,
  `not_delivered_to`, `never_delivered`; imported history plus finalized
  deliveries), imported `category`, median trajectory length, and distinct
  agent count, with sorts on those stored columns; cards show every
  recorded recipient (2026-09-18).
- [ ] Filters for domain, language, environment, and source. Preserve original
  classifications during normalization.
- [ ] Review tool for identity matches the planner refuses on its own:
  section 2 of [the task identity plan](task-identity-todo.md).
- [x] One task filter toolbar (delivery history, category, numeric presets,
  version-aware QA outcome, author, and sort),
  every control a URL parameter; the signed-in user's tasks sort first by
  default (`pin_author=me`, `mine=first|only|off`); the selection persists
  in the browser per org, "Select all" materializes the filter through
  `ids_only=true` (capped at 5,000), and "Add to delivery" posts the ids to
  an active delivery or a new one (2026-09-18).
- [ ] Record the originating filter on the delivery; the board shows the
  summary columns. The delivered-before warning, counting copies of the same
  task, is section 3 of [the task identity plan](task-identity-todo.md).
- [ ] Capture lab/program requirements with revisions and required evidence.
- [ ] Show meets / fails / unknown for each requirement and exact task version.
- [ ] Exclude prior deliveries to the same lab by default, including across
  programs; permit explicit replacements linked to the earlier submission.
- [ ] Allow reuse across labs while preserving organization access boundaries.
- [ ] Show incomplete history separately from confirmed absence of delivery.

## 4. Review, export, and submission

- [ ] Define reviewer responsibility, required checks, blockers, and exception policy.
- [ ] Bind sign-off to exact contents as well as version ID; in-place content
  replacement must invalidate approval even when the version ID stays the same.
- [ ] Generate the spreadsheet and package from one approved, frozen selection.
- [ ] Record submission attempts and per-task receipts separately from approval.
- [ ] Reconcile partial/uncertain receipts before retrying; reuse attempt IDs.
- [ ] Record customer review, rejection reasons, and linked replacement shipments.
- [ ] Verify a second teammate can complete a delivery using the stored history
  and instructions without relying on the original operator's memory.

## Boundaries

The first implementation prepares local evidence and proposed changes. It does
not infer customer acceptance from upload success, assign historical versions
from current defaults, or treat CSV pass rates as current-version QA approval.
Production application, UI filtering, and shipment execution remain later steps.
