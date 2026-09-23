# Proposed CI and deployment architecture

Status: proposal, September 21, 2026. The runner-consolidation changes in this
worktree are an initial step. This document does not change repository rules,
deployment ownership, or release policy.

The recommendation is to keep GitHub Actions, require deterministic code checks
before merging, and require a verified staging deployment before promotion.
Preview environments exist for review and debugging. Each environment has one
deployment owner and one recorded result for a specific source commit.

## What the current system actually does

- The live staging ruleset requires `Require working preview` and a `Preview`
  deployment. It does not require the Python or browser test jobs. The preview
  gate checks deployment outcomes and HTTP reachability; a reachable login page
  or backend OpenAPI document does not prove an authenticated task operation works.
- `verify_promotion_target.sh` checks the branch relationship and a successful
  `Staging Deploy` run for the target commit. It does not require E2E results.
  `staging-deploy.yml` ends after deploying the Modal backend. Vercel handles the
  staging frontend through its Git integration separately.
- Production already has the right ordering: core database migrations, hosted
  database migrations, Modal backend deployment, then Vercel frontend deployment.
  Its running migration job is not cancelled by a newer push. Preserve these
  guarantees.
- Browser test discovery mixes pure helper tests, UI tests with intercepted API
  responses, and real authenticated requests. For example, the six copied-command
  cases and two experiment-grouping cases are collected by both the dashboard
  config and the effort suite. They need neither a browser nor Clerk login.
- The signed-in suite signs in repeatedly even for many cases that replace the
  API responses. The workflow gates the entire suite on Clerk credentials.
- Vercel starts preview builds on push. GitHub then updates branch-specific
  configuration and inspects the existing build, requesting another if the
  configuration differs. `redeploy_vercel.py` explicitly handles this race.
- Preview preparation rotates the database password on every run. It can restore
  a production schema snapshot, sample production rows, preserve selected preview
  rows, and repair failed upgrades. `find_last_deploys.py` reconstructs previous
  successful component deployments from workflow job and step names.
- The private CI image contains Python, Node, PostgreSQL clients, provider CLIs,
  cached packages, source copies, and two installed Python environments. Small
  checks pay for pulling/starting this image even when their work takes seconds.

The observed dashboard run 35663417616 consumed about 28.5 minutes of elapsed
runner execution across four jobs. These are execution timings, not a billing
invoice or a measurement of the account's remaining allowance.

## Target flow

```text
Feature PR
  |
  +-- Plan + inexpensive repository checks
  |      |
  |      +-- Python checks: one environment + one local PostgreSQL
  |      +-- Frontend checks: one dependency install + fixture apps
  |      |
  |      +-- CI result: required before merge
  |
  +-- Preview when requested / ready for visual review
         configure -> deploy -> verify -> publish links

Merge to staging
  -> check the actual staging commit with the same local test suites
  -> migrate staging database
  -> deploy staging backend
  -> deploy staging frontend
  -> authenticated deployed smoke tests
  -> mark that commit ready for promotion

Promote that exact commit
  -> verify its successful staging result
  -> fast-forward main
  -> migrate production -> backend -> frontend -> production smoke

PR close / periodic cleanup
  -> remove preview resources
```

PR checks validate the proposed merge with its base branch. Staging checks
validate the actual combined commit after squash merges and concurrent PRs.
Those are distinct inputs and both checks are justified. Promotion and the main
push do not need another copy of the local test suites: promotion preserves the
already-validated staging commit. Production smoke tests still verify the new
deployment, configuration, and connections.

Implement the code checks as one reusable workflow called by PR validation and
staging deployment. Keep orchestration in YAML and substantive commands in
ordinary scripts that also run locally. Do not build a generic pipeline engine,
per-test runner allocator, or custom dependency graph service.

## Code checks: two substantial jobs

The always-running planning job checks Git history and determines which package
groups changed. The Python and frontend jobs are its two substantial consumers;
a small final job reports one stable required check, `CI`.

The final check must fail when planning fails or a selected job fails, is
cancelled, or unexpectedly skips. An intentionally unselected group may skip.
Required workflow execution must not disappear because a top-level path filter
matched nothing. Documentation-only changes still receive a real result.

**Python job.** Install the locked dependencies once per needed package. Run
dependency-pin and database-column guards as steps. Start one local Postgres
instance for database-backed tests and the real CLI-to-API tests. Use separate
disposable databases when suites need different schemas or retained data; do not
make suite B depend on suite A's fixture having created tables.

Run both migration-head checks here when applicable. For migration changes,
test a clean installation and an upgrade from the base revision's schema with
representative deterministic rows. Model `create_all()` remains suitable for
some isolated tests, but it cannot establish that the actual migration scripts
work. Keep hosted-only schema prerequisites explicit.

Inventory the existing Python suites before declaring the job comprehensive.
The current CI invokes selected tests, not the hundreds of test files present.
Group tests by actual requirements: local unit tests, local database integration,
and explicitly opt-in provider tests. Every existing CI assertion must retain an
owner during the transition. Do not silently disable unrelated tests to fit a
runtime target.

**Frontend job.** Install Node dependencies once. Run type checks and existing
Node unit tests; move browser-free helper assertions into that unit-test command.
Run browser UI tests against the local fixture apps using intercepted responses.
Build each fixture app once, using a production build rather than compilation
on the first page request. The two existing fixture apps may remain separate
within the same runner; merging their source trees is not a prerequisite.

Use explicit Playwright projects/test directories so each case belongs to one
suite. Retain separate reports and per-test names. Existing client isolation and
database isolation must survive any increase in worker count. Split a job only
after measured runtime or resource contention justifies it.

Move mocked scenarios out of the Clerk-dependent suite only after their
replacement assertions pass. Preserve a small real authentication and service
integration suite for deployed smoke tests. Test login itself separately; reuse
authenticated state for other cases when their server-side state permits it.

## Change selection

Use a small explicit path-to-package mapping, with tests for shared dependencies,
renames, lockfile changes, and workflow changes. Unknown changes select more work.
Do not attempt individual-test dependency inference initially.

PR test selection compares the proposed change with its base. Preview deployment
selection compares the desired source with the last successfully deployed
preview. Share path classification, not the assumption that these comparison
bases are interchangeable. Backend changes may require frontend contract tests;
frontend request/response changes may require Python integration tests.

Staging runs the complete local validation set before it is declared ready for
promotion. Cancel superseded pure test runs. Serialize environment mutations;
do not cancel a running migration, credential update, or schema restoration.

## Preview deployments

Replace the mandatory preview deployment rule with required `CI` only after the
new check is established. Keep automatic previews for UI/product PRs ready for
review and an explicit request for backend or provider investigation. A draft,
documentation edit, or isolated unit-test edit should not provision cloud services
just to become mergeable. This is a repository-policy change, separate from the
local consolidation patch.

GitHub should own creation of preview and staging Vercel deployments, as it
already owns production. Configure the backend target first, request the build
for an explicit commit, and record the returned deployment ID. Remove dependence
on finding and repairing an automatically started build. Set up this path before
disabling the corresponding Vercel Git triggers.

For a PR preview, deploy all changed components from one declared source
revision. Reused components must have unchanged relevant source/configuration.
Current default Actions checkout and Vercel's explicit PR-head input can refer
to different revisions; the new coordinator must make that choice explicit.

Create an isolated preview database and its credential once, retain the credential
in the existing per-preview secret, and rotate it on explicit reset or recovery.
Reuse healthy branches and apply pending migrations. Default review data should
be small, deterministic seed data. Preserve production-sample refresh and upgrade
rehearsal as explicit operator tools until versioned fixtures cover their cases.
Do not discard the current recovery machinery before its replacement is proven.

Publish a successful environment record only after verification, using existing
GitHub deployment records/provider metadata. Include source revision, component
deployment identifiers, and database migration heads. A frontend URL or the
name of a successful workflow step alone is insufficient evidence that the
current environment is healthy. This replaces job-history scraping without
introducing another service or database.

Fold posting preview links into the deployment's completion step. Keep close and
stale-resource cleanup: previews can start real worker/provider resources, so
removing their cleanup would exchange Actions savings for cloud waste.

## Staging and production

The staging workflow should own the whole deployment and its verification, using
the same schema-before-backend-before-frontend ordering as production. A green
staging result must mean local checks passed, both components are deployed from
the intended revision, and the deployed smoke tests passed.

Smoke tests exercise the actual edges the fixture apps cannot: real sign-in,
organization access, browser-to-backend routing, one seeded read/write operation,
and CLI submission to a queue. Use dedicated disposable test data. Provider
execution tests that start paid sandboxes stay explicit and bounded; they are
separate from every-PR code validation.

Promotion requires that successful staging result for the exact selected commit.
No missing-workflow bootstrap bypass should remain in the established release
path. Keep the existing fast-forward model and commit pinning.

A source commit can be reused across environments; a frontend build with preview
URLs and credentials embedded in its configuration cannot automatically be used
in production. Continue producing environment-correct deployments. Do not add
cross-environment artifact promotion solely to avoid a build.

## Runner choice and measurement

Start with normal Linux runners and lockfile-keyed package caches for tests.
Retain the database/provider tool image only where those deployment tools are
actually needed. Compare image pull time with cached installation time before
adding another prebuilt image.

Measure total execution minutes per merged change, queue time, PR time to first
actionable failure, time to ready staging, and retries/skips per suite. Count
preview and deployment costs too. Do not optimize only the number of check names
or the duration of the fastest successful run.

If capacity remains the constraint after restructuring, move the same jobs to
managed ephemeral runners. Choosing a different runner provider does not require
rewriting the workflow architecture. Permanent shared runners would add state
and maintenance that this design is trying to remove.

## Implementation order

1. Finish and validate the current consolidation patch in GitHub.
2. Inventory test ownership, remove duplicate discovery, and separate pure helper
   assertions from browser/login tests. Preserve coverage while making the two
   local test jobs reproducible from a checkout.
3. Introduce the reusable checks workflow and its final `CI` result. Run it beside
   the existing checks briefly; then change the merge rules and retire redundant
   jobs in the same rollout. Avoid leaving two permanent copies running.
4. Make staging own frontend deployment and real smoke tests. Make promotion
   require that complete result; remove redundant main-push local tests.
5. Give previews one deployment owner and an explicit lifecycle. Change automatic
   preview policy, credential reuse, and seed strategy in separately reviewable
   changes with their existing recovery and cleanup tests.

References: `.github/workflows/pr-preview.yml`, `staging-deploy.yml`,
`modal-deploy.yml`, `.github/scripts/promote/verify_promotion_target.sh`,
`.github/scripts/preview/{find_last_deploys.py,redeploy_vercel.py,prepare_preview_database.sh,bootstrap_preview_db.py}`,
`frontend/playwright*.config.ts`, and the live staging branch ruleset.
