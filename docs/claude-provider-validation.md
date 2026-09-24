# ABT-1335 validation and rollout

This change makes an explicit Claude provider survive submission, storage,
scheduling, credentials, and execution. The model spelling contract and rollout
instructions are in [DOCS.md](../DOCS.md#claude-provider-selection).

## Evidence

Regression tests were run before the corresponding fixes. Initial routing tests
had 52 expected failures; runtime selector tests had 15. Real Postgres submission
on the original code stored an Anthropic submission as Bedrock. Independent
review then reproduced two failures beyond AgentConfig: Harbor truncated ARN
resource paths in the actual command, and Mini SWE omitted required AWS
credentials. Both now have tests through real Harbor consumers and the final
environment merge, with sandbox execution mocked.

The new suites exercise real normalization and provider selection:

- `test_claude_routing.py`: prefixes, aliases, API-only IDs, profiles, ARNs,
  independent limits, fixed provider-price fixtures, regional allowlists.
- `test_claude_persistence.py`: real Postgres create and append transactions,
  including bare defaults under both flags; later attempts preserve the stored
  provider and both trial/worker-job queues after the default changes.
- `test_claude_transport.py`: credential availability and overlay matrix,
  real Claude Code and Mini SWE command generation in normal/child configs,
  full ARN identity, AWS credentials, provider hosts, and funding eligibility.
- Trial preparation and local-runner tests cover BYOK resolution, child payloads,
  configured platform keys, and the matching funding fingerprint.
- CLI E2E tests submit to a real local server and Postgres. Actual Homebrew
  releases **0.1.17 and 0.1.16 each passed six provider cases** (three spellings,
  two agents). Existing startup tests deny network access and LiteLLM imports.

Initial local validation: **942 core tests passed, 13 skipped** across the relevant
routing, runtime, scheduling, accounting, persistence, and startup suites.
The backend selection had **46 passes and the two baseline probe failures**
described below; the full CLI E2E suite had **14 passes**. The load-only guard,
its nine tests, changed-file lint, and whitespace checks passed. Independent
review found no remaining actionable blocker after remediation. An isolated
child environment using the current Harbor pin imported the entrypoint and
constructed both provider agents, preserving full ARN metadata; this did not
make a provider call. Repository CI and live preview inference remain release
gates and are not established by these local results.

Staging advanced during review to `d1838cfa`, including Harbor pin `45d50cc` and
a shared funding-selection helper. After integration, the expanded core run had
**1,065 passes, 13 skips, and one existing Vertex catalog failure**. The full CLI
E2E suite again passed all **14 tests**. Independent review reran **299 tests**
on the new pin, including actual Claude/Mini SWE commands and public xAI, and
verified isolated child imports and agent construction. All passed. The shared
funding helper retains provider-based BYOK eligibility for its callers.

The subsequent conventions review found and fixed two consistency gaps:
the Kimi Claude Code adapter rewrote explicit Claude routes to Moonshot, and
historical bare Claude responses changed identity with the admission flag.
Each regression produced three expected failures before its fix. Runtime tests
now use Harbor's actual scoped/persistent environment merge, mocking only
sandbox execution; removing the model selector protection produced three
expected failures. Independent re-review passed 23 focused cases. Existing
environment tests now check explicitly cleared competing credentials and build
a real agent/task configuration; these 34 tests also run in routing CI.

After those fixes, the affected core selection had **741 passes and the same
Vertex catalog failure**. Postgres persistence and read projections passed
**32 tests**, the full CLI E2E suite passed **14**, and the backend routing
selection had **36 passes and the same two probe-fixture failures**.

Integration with staging `956ee78e` retained both CI test lists and passed
**364 checks**, covering Claude routing, Postgres, CLI startup, and the incoming
curated-model/cost commands. The expanded CLI E2E suite passed all **18 tests**.

No request field, schema migration, or dependency pin changed. The intentional
compatibility change is the meaning of explicit `anthropic/` and `claude/`
models; older clients can still submit their existing request shape.

## Baseline failures

Before changes, Ke's four named suites had **333 passing tests**. The wider
baseline had two stale concurrency fixtures using four dispatcher tuple fields;
these now supply the required fifth priority field. Assertions for the old
force-direct behavior were replaced with explicit-route coverage.

Pricing tests remain catalog-dependent: the bundled catalog produced eight
failures (54 passed), while the live catalog produced five (57 passed) before
the fix. The failures concern missing/changed OpenRouter, Fireworks, z.ai, and
Geometric catalog entries. New attribution tests use fixed prices and pass;
this change does not update unrelated catalog rates.

The post-integration bundled-pricing run had **55 passes and the same eight
failures**. The expanded Vertex suite also expects a price for
`vertex_ai/gemini-3.8-flash`, absent from the bundled catalog. That assertion
fails using unchanged staging pricing code too; it is separate from the new
fixed-fixture provider-price tests, which pass.

Two backend probe tests also fail on unchanged staging:
`test_probe_uploads_cli_to_harness` and `test_local_probe_env_contract`. Their
fixture omits a task version but expects `ODDISH_PROBE_TASK_VERSION`. They are
left unchanged.

## Release gates

Mocked sandbox execution establishes command/model/environment agreement. It
does **not** establish provider access. Live Anthropic/Bedrock preview calls,
including current Bedrock credentials, must pass before rollout. AWS profile,
web-identity file, or instance-metadata credentials additionally require their
sources to be available inside the sandbox; forwarding a selector cannot make
a host-only file available there.

Run a minimal paired preview task through Claude Code and Mini SWE, then another
pair through the separate Harbor child process. Check successful model calls,
stored model/provider/queue, final transport evidence, funding, and costs.
Use one attempt and short timeouts. Missing matching credentials must fail on
the selected provider.

Pause submissions and drain all old nonterminal Claude work, including blocked
trials and scheduled retries. Deploy matching API and workers, configure
Anthropic concurrency separately, resume, and inspect paired trials, queue
progress, authentication errors, and attribution. Existing Bedrock overrides
stay attached to Bedrock. Never rewrite historical rows or guess their original
prefix. Rollback likewise requires pausing and draining newly queued work
before old workers return.

## Preview validation on 2026-09-23

At PR head `53ffc571`, task `provider-canary-fcfa7571` version 2 completed
all six requested model calls successfully, each with reward 1 and one attempt:

| Experiment | Agent / path | Anthropic trial | Bedrock trial |
| --- | --- | --- | --- |
| `cb63ce95` | Claude Code / worker | `-8` | `-9` |
| `cb63ce95` | Mini SWE / worker | `-10` | `-11` |
| `6914cf9a` | Claude Code / child | `-13` | `-14` |

Trial suffixes refer to the task ID above. Both experiments are available on
[the PR preview](https://pr-1824.oddish.app). Stored provider, model, trial queue,
and worker-job queue agreed for every trial. Pulled configs showed the bare API
ID for direct Claude Code, explicit provider prefixes for Mini SWE, and native
Bedrock IDs plus `CLAUDE_CODE_USE_BEDROCK=1` for Bedrock Claude Code. Agent
allowlists contained only the selected provider's hosts (AWS STS included for
Bedrock). Serialized secret placeholders are not proof of final secret values.

The four worker trials recorded non-estimated model costs: $0.04000325 and
$0.10755275 for Claude Code Anthropic/Bedrock, and $0.015686 and $0.015536 for
Mini SWE Anthropic/Bedrock. The two child trials succeeded but returned null
usage/cost fields; live child cost attribution remains unverified. The tasks-scoped
preview key cannot inspect administrator queue-capacity endpoints. Live limit
configuration and the drain/deploy rollout remain operator checks.

Version 1 failed before inference because its environment-wide no-network policy
blocked package installation. Version 2 uses public installation, a restricted
agent phase, and an offline verifier. These results precede the subsequent merge
of staging `8ee5e3a9` (including the new monitor execution path and Harbor pin),
so they do not establish live execution on the merged revision.

After integrating staging `8ee5e3a9`, local validation passed 342 routing/startup
checks, 416 runtime/credential/monitor-boundary checks, 18 CLI E2E tests, and six
real Postgres persistence cases. The backend local-runner/gateway selection had
29 passes and the same two documented probe-fixture failures. The persistence
test now calls the shared `trial_steps.prepare_trial_run` entrypoint introduced
by staging. No old `_claude_code_forces_direct_api` call remains in application
code. GitHub CI and live validation of this merged revision remain pending.


## Bot review follow-up

All six review threads were investigated; three concerned the same legacy-row
route selection. Runtime context now derives bare historical execution spelling
from the persisted provider, leaving the stored model and queue untouched.
Unknown historical providers fail explicitly rather than using today's default.
HDO keeps its dedicated route on the Kimi adapter. Built-in Mini SWE child/local
runs now use Oddish's required wrapper for Anthropic as well as Bedrock.
File/profile/web-identity/container credential sources are resolved on the worker
into scalar AWS credentials before sandbox transfer. Profile resolution uses a
real credentials-file fixture; web-identity tests mock the external SDK boundary.
Those snapshots do not provide indefinite credential refresh for long trials.

Regressions failed before their fixes. The final combined routing, child-process,
legacy-attempt, credential, Postgres and startup selection passed 413 tests;
the backend local/gateway selection passed 29 with the two known probe tests
excluded. An earlier broader runtime selection passed 382 tests and exposed one
obsolete stock-child-agent assertion, subsequently corrected and rerun in the
413-test selection. Live calls and CI must be repeated for this review revision.
