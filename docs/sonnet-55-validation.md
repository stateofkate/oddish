# Sonnet 5.5 support check — 2026-09-28

Changes are saved on `codex/sonnet-55-support`, based on staging commit
`899a5dfc` (including PR #1956). They are not deployed.

Reviewed preceding QA changes: [#1944](https://github.com/abundant-ai/oddish/pull/1944)
(OpenAI runner selection), [#1947](https://github.com/abundant-ai/oddish/pull/1947)
(ordinary trial configurations and one-case preflight),
[#1951](https://github.com/abundant-ai/oddish/pull/1951) (Luna default, local history),
and [#1956](https://github.com/abundant-ai/oddish/pull/1956) (model identity and effort charts).

## Findings and changes

- `oddish/src/oddish/config.py:1150`: added the previously absent
  `claude-sonnet-5-5` → `global.anthropic.claude-sonnet-5-5` mapping, following
  the existing Bedrock naming convention. Without it, a bare Sonnet 5.5 name
  on the AWS route raised `No Bedrock model id mapping for Claude model`.
  Account availability of this inference profile has not been verified.
- `oddish/src/oddish/model_pricing.py:48`: added a missing fallback rate:
  $2 input, $10 output, $0.20 cache read, $2.50 five-minute cache write per
  million tokens. This prevents missing cost estimates when LiteLLM's model
  catalog has not updated. Source: [Anthropic model page](https://platform.claude.com/docs/en/models/sonnet-5-5/overview).
- `frontend/src/lib/reasoning-effort.ts:59`: Sonnet 5.5 now offers
  low/medium/high/xhigh/max instead of only low/medium/high. These are the
  provider's supported adaptive-thinking effort levels. Sonnet 5 is unchanged.
- Added `oddish/tests/test_sonnet_55.py` covering routing, fallback prices,
  and QA creation retaining model, effort, source evidence and queue identity.
  Extended frontend unit tests and the existing admin QA browser fixture to
  compare Sonnet 5.5 with Luna at max effort, including the required one-case
  checks and safe retry after partial full-run submission.

## Observed verification

Before fixes: `8 failed, 2 passed` in the new Python file; two new frontend
Sonnet effort cases failed because xhigh and max were absent.

After fixes: `191 passed, 30 skipped` across Sonnet, queue configuration,
analysis creation and QA scoring. Skips need a test database. Frontend unit
checks: `38 passed`; browser fixture: `5 passed (19.4s)`. Browser API responses
are simulated; this proves the UI submission flow, not production execution.

Claude Code 2.1.284 executed a real direct-provider request with
`--model claude-sonnet-5-5 --tools '' --no-session-persistence` and returned:
`"result":"SONNET_55_OK"`, `"is_error":false`,
`"canonicalModel":"claude-sonnet-5-5"`. Reported cost was $0.223574.
This used local Claude authentication, not the hosted worker's credentials.

AWS profile lookup failed before contacting the model:
`The provided authorization grant is invalid, expired, revoked, or malformed`.
Restore AWS login and verify the inference profile before using the AWS route.
No production evaluation was submitted.

The broader network-enabled pricing suite has five failures reproduced against
unchanged staging pricing code: Codex Mini lookup, Kimi router price,
router-rate priority, Fireworks GLM cached price, and ZAI cache-write price.
They reflect external catalog changes and are outside this Sonnet patch.
Ruff also reports an existing E402 import-order finding at `config.py:40`,
reproduced on unchanged staging. The new Python test and pricing change pass Ruff.

## Admin usage after deployment

Open Admin → QA Evals → New run. Choose `claude-code`, then enter
`anthropic/claude-sonnet-5-5` in Models (one per line). Add `openai/gpt-6-luna`
on a second line for a comparison. Choose an effort and click
“Check one golden per model”; a golden is a saved case with a reference label.
Only replay the full set after both saved checks pass. This preserves the
existing prompt, evidence, and configuration checks from PR #1947.

The explicit `anthropic/` prefix selects the direct Anthropic API and avoids
relying on the unverified AWS profile. Hosted workers still need valid provider
credentials and a compatible Claude Code release. The production QA default
remains GPT-6 Luna. Full hosted execution and resulting quality scores remain
unverified until deployment and successful saved checks.
