# Oddish Frontend

## Overview

This is the Next.js App Router frontend for Oddish. It provides the authenticated dashboard, task browser, experiment views, public share and dataset pages, Clerk-based auth, and server-side API routes that proxy requests to the backend API.

Current app surface:

- `/` public landing page for signed-out users; signed-in users are redirected to `/orgs/{orgSlug}/dashboard`
- `/dashboard` main dashboard and experiment entrypoint
- `/tasks` authenticated task browser with search, pagination, per-task version summaries, and links back to experiments
- `/experiments` base page directing users to select an experiment
- `/experiments/[experiment]` experiment detail, task and trial inspection, logs, results, files, version history, share controls, per-task retry actions, and **cancel** for in-flight work (task drawer **Cancel (N)** or experiment table bulk **Cancel** when tasks are selected; both use `POST /tasks/cancel` with one or more task ids)
- `/usage` usage and cost reporting
- `/settings` organization management and API key management
- `/admin` includes an **Overview** with editable per-queue-key concurrency limits and queue health, plus detailed **Worker Jobs** and **Concurrency** tabs for job state and `queue_slots` leases
- `/share/[token]` read-only public experiment view
- `/datasets` and `/datasets/[token]` public dataset listing and detail pages

## Robot playback tests

```bash
REVIEW_TEST_PORT=3211 pnpm exec playwright test -c playwright.review.config.ts robot-recording.spec.ts
```

The port defaults to 3207; the runner refuses to reuse another worktree's server.

## Quick Start

### 1. Install dependencies

```bash
pnpm install
```

### 2. Configure environment

```bash
cp env.example .env.local
```

Minimum setup:

```bash
# Clerk
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_test_...
CLERK_SECRET_KEY=sk_test_...

# Backend API URL
NEXT_PUBLIC_API_URL=http://localhost:8000
```

Useful optional variables:

```bash
# Recommended for org-aware backend auth
CLERK_JWT_TEMPLATE=oddish

# Direct API mode: the browser calls NEXT_PUBLIC_API_URL itself instead of the
# /api/* proxy routes (one fewer hop per request). Needs the same template
# name published to the browser and the app's origin allowed by the backend's
# CORS_ALLOWED_ORIGINS / CORS_ALLOWED_ORIGIN_REGEX. Off unless set to 1.
NEXT_PUBLIC_API_DIRECT=1
NEXT_PUBLIC_CLERK_JWT_TEMPLATE=oddish


# Optional Clerk route overrides
NEXT_PUBLIC_CLERK_AFTER_SIGN_IN_URL=/dashboard
NEXT_PUBLIC_CLERK_AFTER_SIGN_UP_URL=/dashboard

# Optional absolute app URL, mainly useful for local HTTPS / production-like Clerk flows
NEXT_PUBLIC_APP_URL=https://local.oddish.app
```

### 3. Start the dev server

```bash
pnpm dev
```

Open [http://localhost:3000](http://localhost:3000).

## Scripts

```bash
pnpm dev           # Next.js dev server
pnpm build         # Production build
pnpm start         # Run production server
pnpm lint          # ESLint
pnpm test          # Node unit tests in tests/*.test.ts
pnpm test:e2e      # Playwright end-to-end suite
pnpm typecheck:e2e # Type-check the Playwright project
pnpm format        # Prettier formatting
pnpm format:check  # Check Prettier formatting
```

## Architecture

The frontend uses server-side route handlers in `src/app/api/*` as the boundary between browser code and the backend. Browser components call internal Next.js routes, and those handlers resolve the real backend URL and forward auth headers when needed.

Request flow:

```text
Browser UI
  -> Next.js pages and client components
  -> Next.js route handlers in src/app/api/*
  -> backend API (FastAPI or Modal)
```

The backend URL is configured via a single `NEXT_PUBLIC_API_URL` env variable in `src/lib/backend-config.ts`. Set it to `http://localhost:8000` for local development or to a deployed API URL for staging/production.

Browser requests are written as `/api/...` URLs and normally reach the backend through the Next.js route handlers under `src/app/api`, which mint the backend token server-side. With `NEXT_PUBLIC_API_DIRECT=1` the same requests go from the browser straight to `NEXT_PUBLIC_API_URL` (`src/lib/api.ts` maps the path and attaches a token from the Clerk client); the SWR keys do not change. Use `apiFetch` from `src/lib/api.ts` for any new `/api/...` mutation so it takes part in that mapping.

Global client-side fetching defaults live in `src/app/providers.tsx`, which installs an `SWRConfig` with deduping and conservative revalidation settings for the entire app.

## Auth And Routing

The app uses [Clerk](https://clerk.com) for authentication and organization context.

Public routes:

- `/`
- `/sign-in/*`
- `/sign-up/*`
- `/share/*`
- `/datasets/*`
- `/experiments/*` at the middleware layer, so unfurl bots can read metadata;
  the authenticated app layout still redirects ordinary signed-out visitors
- `/api/client-traces/*`
- `/api/public/*`

Everything else is protected by Clerk middleware.

If you want backend JWTs to include org context, configure a Clerk JWT template and set `CLERK_JWT_TEMPLATE`. Oddish expects claims like:

```json
{
  "email": "{{user.primary_email_address}}",
  "org_id": "{{org.id}}",
  "org_role": "{{org.role}}"
}
```

## API Route Groups

The frontend proxies backend requests through `src/app/api/*`. Main groups:

- `/api/dashboard` for dashboard data
- `/api/tasks/*` for task browse/search, task detail, versions, trials, files, direct-to-S3 upload init/complete, `POST /api/tasks/cancel`, and task-level QA retry/cancel actions
- `/api/trials/*` for trial logs, structured logs, result payloads, retries, trajectories, and files
- `/api/experiments/*` for experiment detail, task listing, publish, unpublish, and share token creation
- `/api/settings/api-keys*` for API key management
- `/api/admin/*` for queue slots, queue status, orphaned state, and the unified `worker-jobs` matrix (`/api/admin/worker-jobs`)
- `/api/public/*` for public experiment, dataset, task-file, and trial artifact access

The experiment share dialog has a **Show QA** switch. It is off by default.
Publishing sends `show_qa` to the backend; **Save changes** updates the same
link. When QA is on, the share page shows read-only task checks and trial
reviews. The public task checks proxy is
`/api/public/experiments/[token]/tasks/[task_id]/qa`; it forwards the task
version and returns 404 when QA is off or the task is outside the share link.
QA shares start with the graph hidden and task links open the QA overview.
The page waits for share settings before choosing that view. Links to a file
still open that file, and viewers can turn the graph on.

## Project Structure

```text
frontend/
├── src/
│   ├── app/
│   │   ├── page.tsx              # Public landing page / signed-in redirect
│   │   ├── (app)/                # Authenticated app shell
│   │   │   ├── dashboard/
│   │   │   ├── tasks/
│   │   │   ├── experiments/
│   │   │   ├── settings/
│   │   │   └── admin/
│   │   ├── share/[token]/        # Public experiment page
│   │   ├── datasets/             # Public dataset pages
│   │   ├── api/                  # Backend proxy route handlers
│   │   └── providers.tsx         # Shared SWR config
│   ├── components/               # Dashboard, detail panels, charts, nav, UI primitives
│   ├── lib/                      # API helpers, backend config, shared types, utilities
│   └── middleware.ts             # Clerk route protection
├── public/oddish.png
└── run-prod-clerk-local.sh       # Local HTTPS helper for production Clerk keys
```

## Development Workflows

Set `NEXT_PUBLIC_API_URL` in `.env.local` to point at the backend you want to use, then run:

```bash
pnpm dev
```

`NEXT_PUBLIC_API_URL` defaults to `http://localhost:8000` if not set. For backend setup and deployment instructions, see [`AGENTS.md`](../AGENTS.md) and [`backend/README.md`](../backend/README.md).

## Deployment

The hosted dashboard (oddish.app) deploys from `.github/workflows/modal-deploy.yml`,
which orders it after the database migrations and the Modal backend. `vercel.json`
disables Vercel's git deployments for `main` on purpose — the pipeline creates the
production deployment through the Vercel API once the backend is live. Re-enabling
git deployments for `main` (there or in the Vercel dashboard) restores the race
between a new frontend and the old backend.

`next.config.ts` enables `output: "standalone"`, and the checked-in `Dockerfile` builds a production container around the generated standalone server:

```bash
docker build -t oddish-frontend .
docker run --rm -p 3000:3000 --env-file .env.local oddish-frontend
```

### Use Clerk production keys locally

If you need production-origin Clerk behavior locally:

1. Add a hosts entry:

```bash
echo "127.0.0.1 local.oddish.app" | sudo tee -a /etc/hosts
```

2. Set production Clerk keys plus app URL in `.env.local`:

```bash
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_live_...
CLERK_SECRET_KEY=sk_live_...
NEXT_PUBLIC_APP_URL=https://local.oddish.app
```

3. Start the local HTTPS dev server:

```bash
./run-prod-clerk-local.sh
```

`next.config.ts` allows `local.oddish.app` as a dev origin for this workflow.

## UI Stack

- Next.js 16 App Router
- React 19
- Tailwind CSS
- shadcn/ui and Radix primitives
- SWR for client-side data fetching
- Clerk for auth
- Recharts for charts and graphs
- Shiki for syntax highlighting
- @tanstack/react-virtual for virtualized lists

## Troubleshooting

### "Failed to fetch" or disconnected backend

Check that the backend is running and reachable at the configured URL:

```bash
curl ${NEXT_PUBLIC_API_URL:-http://localhost:8000}/openapi.json
```

### Clerk auth issues

- Verify your Clerk keys in `.env.local`
- If org-scoped backend access is failing, confirm `CLERK_JWT_TEMPLATE` is set and includes `org_id`
- If using production Clerk keys locally, use `./run-prod-clerk-local.sh`

### CORS-like browser errors

The frontend is intended to call `src/app/api/*`, not the backend directly from browser code. If requests fail:

- verify `NEXT_PUBLIC_API_URL` in `.env.local`
- make sure the request is going through the Next.js route handlers

### Task browsing and delivery selection

Tasks keeps the per-agent trial cards; Dashboard remains the experiment view.
The Tasks toolbar owns search, author, sort, and a single filter popover, with
removable active conditions. Numeric filters offer presets and validated custom
ranges. QA outcome refers to the verdict for the current task version, not the
success of the QA process; delivery readiness remains on the delivery board.

Delivery history appears first in Filters, with visible Sent to lab and Not sent
to lab selectors using mapped customers and unmapped imported labels. Cards and
delivery-picker rows share destination chips with batch/date details. No delivery
recorded means missing history, not proof that a task was never sent. The Tasks
page owns the search draft and debounce timer; both Clear filters buttons cancel
pending search writes and reset that draft while retaining the delivery and sort.

A delivery's Add tasks link opens `/tasks?delivery=<id>` with its customer history
filter. The same browser uses a comparison table with completion/failure/pending
counts and expandable trial cards. The destination stays in the URL through
filters, saved searches, and pagination. Page/count/ID requests exclude existing
members. Selections are stored separately per organization and delivery, and
only submitted IDs are removed after a successful add. Paste task IDs remains
available on the delivery board. Run these local flows with
`pnpm exec playwright test -c playwright.delivery.config.ts`.

Task browsing keeps QA, median steps and lab-history controls visible. Table/card
presentation and optional columns use URL parameters without refetching task data.
“Copy view link” shares current criteria; “Share selection” stores exact task IDs
through the saved-view API and opens `/tasks?selection_id=...`. These links retain
normal organization permissions and do not grant public access. Delivery picking
shows existing members as disabled selections and uses the same creation dialog
as the Deliveries page. Adding a task does not finalize or submit the batch.

The admin queue-health tiles read additive cycle outcomes from the existing
status payload. A recent dispatcher report with `ok: false` warns and shows its
last completed run; deferred reconciliation names phases left for another pass.
Legacy payloads without these fields retain their previous rendering. These
values are derived directly from the response, with no new polling or state.

The trial drawer's Summary and Live tabs share retained setup history. `/api/trials/[trial_id]/progress-history` forwards the authorized
backend read without caching. Missing history on older deployments is
shown explicitly. Fixture coverage lives in `e2e/trial-progress.spec.ts` in the
`effort` project of `playwright.delivery.config.ts`.

Environment progress accepts both legacy phase pairs and v2 operation IDs.
V2 pairs are matched within the trial attempt, preserve parent relationships,
and do not reopen after a replayed start. Legacy events retain their phase-based pairing.
The compact progress view shows each primary phase duration once, with setup
operations expandable beneath Environment Setup. The original Live transcript
header and output remain independent, with setup details above the output.
Public read-only views retain their original conditional timeline. The whole
Summary timeline can be minimized. Worker metadata is not part of the setup view. Unknown timing never implies successful completion.

Secondary setup operations render by measured start time.
Flat `completed_activities` snapshots fold by operation ID, including corrected
wrapper durations; the new setup root is authoritative for its measured scope.
The retained snapshot keeps supporting activities and their parent identity.
The view never sums children into parent durations or displays a residual-time row.

Measured progress reuses the standard `HarborStageTimeline`, `Card` and stage
badge presentation. Additive stage state/duration overrides preserve measured
outcomes while setup details remain collapsible inside the original row.
Theme tokens and the original timeline indicators are unchanged.
The environment list renders only direct secondary operations and legacy rootless
phases with their measured inclusive durations. Body operations, deeper provider
children and progress delivery remain retained data but are not rendered. Only the primary Environment Setup section is expandable.
Timeline disclosures preserve explicit user toggles during live clock/history
updates. Navigable stages retain their file targets, and live-stream errors are
visible before the first agent message without opening an empty output section.
Initializing follows current-attempt phase progress even when timing is absent;
its duration still requires recorded timestamps. Pending, queued and retrying
rows do not reuse stale initialization milestones or timing from an earlier run.

The task Overview labels a rejection from a different review as “Previous task review: Rejected” when a trial is selected, including on the same task version. “View rejected review” links directly to the verdict’s `_graded_by` source; opening that source does not label it as a different review. Unmatched prior-version rejections remain explicitly unconfirmed for the selected version. Source-review and audit links open in a new tab so the original run remains available. This adds no history view or API request.

## Task browser preview

The left sidebar exposes model and agent selection, mutually exclusive model-run
states, execution errors, review, experiment, author, and delivery history. These
primary controls do not require expanding a section. Model, agent, and experiment
search only narrow existing choices; typed text never creates a filter value.
Sort field and direction are separate. Cards keep pass/fail/error/pending
counts visible and put trial previews and experiment history in disclosures;
the table retains the same outcome counts. Numeric sorting displays the server's
`sort_value` beside each task. Live refresh defaults on and can be paused.
Selection is disabled on cards and table rows while the query is changing or its
last update failed. Stored selections, old coverage links and saved views retain
their existing semantics. Lower-priority controls share one Additional filters
section using the installed Radix accordion. Tool-name CSV entry is hidden until
a recorded tool-name vocabulary is available; existing URLs remain readable and
removable through their active-filter chip.

Run `DELIVERY_TEST_PORT=3219 pnpm exec playwright test -c playwright.delivery.config.ts tasks-picker.spec.ts task-run-coverage.spec.ts`
for the fixture-backed browser checks. The corresponding PostgreSQL checks are
`oddish/tests/test_task_run_coverage.py`; run against a disposable local database.

Internal task reviews use `/api/tasks/{task_id}/versions/{version}/reviews` and
its rating/comment routes. These authenticated proxies forward to the hosted API;
direct API mode uses the same route contract. Public share pages do not use them.
