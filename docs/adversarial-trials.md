# Adversarial QA

Adversarial QA is an organization-funded, explicitly requested attack on one task version's
grader. It is an ordinary worker trial with `trials.kind = 'adversarial'`, run
on the task's real environment by the existing worker. Its exploit is replayed
alone on a fresh copy; QA never reads attacks. `kind` is a text column, so no
migration is involved.
v1 targets two weaknesses: hidden-file access and grading shortcuts.

The task-page Run/Rerun button submits immediately using the code-owned model;
there is no model, harness or effort dialog. Explicit API execution overrides
remain supported for existing programmatic callers.

## Request

`POST /tasks/{task_id}/qa/adversarial`

```json
{
  "expected_version_id": "task-id-v2",
  "request_id": "attack-request-1",
  "experiment_id": "experiment-id",
  "execution": {"agent": "claude-code", "model": "claude-sonnet-5-5", "environment": "modal"},
  "max_trial_attempts": 1
}
```

The request uses the existing `TrialSpec` execution settings and the same provider
validation and model normalization as ordinary trials. Omitted execution, or
an omitted model, uses the code constant `ADVERSARIAL_MODEL` (`openrouter/z-ai/glm-5.3`).
`agent: "auto"` selects the harness from the model family and protocol, then
records a concrete agent/model pair. The default uses `mini-swe-agent` with
GLM-5.3 through OpenRouter (OpenRouter id `z-ai/glm-5.3`); the Fireworks route
was unreliable. Workers use the existing `OPENROUTER_API_KEY` credential route.
Explicit harness and model overrides remain
available. The latest ordinary model run on this version supplies the backend
and environment overrides; a baseline is used if no model run exists. Neither
its model, agent flags nor agent credentials are inherited. An explicit backend
override wins; otherwise a version with no recorded backend uses the worker
default. The task's `task.toml` controls
timeouts. Attempts default to one, with a maximum of three. Attacks are stored
in the requested experiment and use the ordinary worker queue. Older requests
may still supply the top-level `environment` field. If both environment fields
are present they must agree. An omitted experiment is resolved to the active ordinary experiment with the
most recent run of the selected version, falling back to the newest active task
association. QA report experiments are excluded. The version guard still rejects
a stale request instead of switching the target.

Under the task row lock the launch checks organization access and the expected
version (409 on mismatch, nothing written), then resolves in this order:

1. The same `request_id` returns the same trial (`"status": "existing"`).
2. Another request while a run for this version is live returns that run
   (`"already_running"`).
3. Manual launches do not wait for oracle or nop baseline checks. Missing,
   pending, or failed baselines do not block the attack.
4. `ATTACKS_PER_LAUNCH` (4) trials and their `TRIAL` jobs are created in one
   commit (`"queued"`, with every id in `trial_ids`). The published verdict,
   any live QA trial and the task status are left alone.

A new `request_id` after the previous run finished is an explicit rerun and
creates a new trial; earlier non-superseded runs stay in the evidence.

The trial's `harbor_config.adversarial` holds `request_id`, `task_content_hash`,
`prompt_version` (`adversarial-v4`), `prompt_text`, `prompt_sha256`, and the
`execution` it ran with (agent,
model, agent overrides, attempts, environment). Track it with the normal trial status
and log endpoints. The server reads `analyze/adversarial_prompt.txt` at submission, decodes
UTF-8 without changing newlines, fills in its `{mode}` and `{start}` lines
for each attack (`attack_prompt`), and stores that text and its SHA-256 digest.
Half the attacks are black box: the prompt carries no pre-trial audit findings
or other knowledge of the grader, so the attacker knows only what any solver of
the task knows. Every second attack (`white_box: true`) is white box: the worker
appends the code files from `tests/` (`GRADER_CODE_SUFFIXES`, at most
`GRADER_CODE_MAX_BYTES` in all, test.sh first and then smallest first; the rest are named) after the instruction. Data files are left out,
because pasting expected answers into an exploit would pass without showing a
hole a solver could use. `solution/` is never included. The `{mode}` line
asks a black-box attacker to list what the grader will run or read that it can
write, and a white-box attacker to name the grader line it beats. The `{start}`
line rotates through `ATTACK_START_IDEAS`, so the two attacks in a mode each
begin from a different numbered idea in the prompt. Each worker
attempt verifies that digest and executes the saved text. An older queued run
without the snapshot fails with `Adversarial prompt snapshot is missing; submit
a new adversarial run`. Completed historical runs remain readable.

Adversarial QA belongs to the organization's QA spending. Trials have
`billed_user_id=null`; `harbor_config.adversarial.requested_by_user_id` records
who submitted them. A user's exhausted budget does not block submission, and
attacks do not consume personal spending, reservations or unattributed-payer pools.

The existing `ODDISH_QUOTA_COUNTS_ANALYSIS_AND_COMPUTE` setting controls whether
QA spend counts toward the organization's monthly cap. When enabled, submission
checks only that cap and returns HTTP 402 if exhausted. Queued runs reserve
organization budget, settled model costs count once, and organization-budget
cancellation includes active attacks. Personal-budget cancellation excludes them.
When the setting is disabled, model and compute costs are still recorded and
reported as organization QA spend. There is no separate QA-only limit setting.

The stored
trial kind remains `adversarial` for API compatibility; the UI and QA cost chart
label it `Adversarial QA`. Execution still uses the task's real environment and
grader, with the saved attack prompt.

From the CLI, `oddish adversarial <task_id>` launches one round on the task's
current version (`-a`/`-m` pick the attacker, `-e` the sandbox). `--watch`
waits until the attacks and their replays settle and prints each attack's
mode, reward, replay and result (MUST FIX when the replay passed).

## Results

QA waits for attacks but never reads them: `qa_eligible_trial_ids` is agent-only
and the QA payload has no attack field. Admission (`task_adversarial_pending`)
holds QA while the current version's `adversarial_status` is QUEUED, or RUNNING
with an attack, its replay, or either's settle hook outstanding. The last hook
sets SUCCESS and re-enters admission. A hook that keeps failing stops holding QA
one hour after its trial ended. A replay's must-fix finding is therefore in
`reported_findings`, and counted by the verdict badge and deliveries, when the
verdict publishes. The QA model's own verdict never sees attacks.

- The recorded `reward` is the original grader's score, never rewritten.
- A passing replay is the defect. The task page shows it as Must fix with the
  attacker's `/logs/agent/adversarial_report.md`.
- An attack that passed the grader but whose replay failed, or that left no
  exploit.sh, reads "Needs a human".

## Execution

At launch, `resolve_task_file_source` selects the version's storage source.
The run records `harbor_config.task_artifact.archive_key` and `etag` (the storage
identifier for the object's contents). The shared downloader sends `If-Match`
with that ETag; an overwritten archive fails instead of executing different
bytes. Unverified mounted copies and local fallbacks cannot substitute for a
pinned archive. Local self-hosted tasks use a SHA-256 fingerprint of raw file
contents, without parsing `task.toml`. Database locks are released during hashing
and archive transfer. Before creating a run, launch reacquires the task/version
locks and rechecks source identity, active runs, experiment membership and budget.
A source change returns HTTP 409. A concurrent duplicate returns the winning run;
only a snapshot uploaded by the losing request is deleted.

Older versions stored as individual files get a server-created archive. Every
file read requires its listed ETag, and another listing detects added or deleted
files. The version's storage pointer then references that archive for later runs.
Missing files or multiple possible historical archives return an explicit launch
error. The upload-time `content_hash` remains version bookkeeping; execution no
longer recomputes it through the worker's Harbor parser.

`_prepare_trial_task` (workers/queue/trial_handler.py) extracts the verified
archive into a disposable directory and prepends the saved `prompt_text`
to `instruction.md`. Other task files retain their bytes. `"adversarial"` is not
in `ANALYSIS_TRIAL_KINDS`, so the run uses the original task environment and grader.

The common runner pins default Codex, Claude Code, Gemini CLI and mini-swe-agent
package versions; explicit `agent_config.kwargs.version` overrides still win.
For backends supporting changes to network access, trusted harness installation
runs with public network access and restores the task's original policy before
task setup hooks, agent execution or grading. A backend unable to change network
access needs the harness preinstalled in its clean starting image. Claude's local
protocol adapter reuses a preinstalled LiteLLM environment only when its version
matches. Image preparation and cache support still depend on the backend; this
change adds no cache of containers left behind by solvers.

## Replay

The prompt asks the attacker to write `/logs/agent/exploit.sh` early and keep it
current. When an attack settles, the post-trial hook reads that file from the
attempt's artifacts (`agent/exploit.sh`). If it does more than `exit 0`,
`queue_exploit_replay` queues one replay of that attack attempt: a trial with
`kind = 'adversarial'`, agent `exploit-replay`, on the nop/oracle queue, with
`harbor_config.adversarial` = `replay_of`, `attack_attempt`, `exploit_text`,
`exploit_sha256`. The stored agent name matches no nop/oracle baseline check.
The worker runs it as Harbor's `oracle` agent (`execution_agent`) on a fresh
copy of the task whose `solution/` holds only the exploit as `solve.sh`, then
the grader runs. Multi-step tasks are refused, because the oracle would run each
step's own solution.

A replay that finishes with reward 1 confirms its attack attempt. Nothing else
happens server side: the task page reads the replay's result.

## Validating detection

The fixture setup phase permits trusted agent installation. Explicit no-network
agent and verifier phases use Oddish's model-only transport injection; the model
cannot use setup networking. Each fixture declares its build timeout, and staging
rewrites the task name to match its new directory.

`tests/fixtures/adversarial/` holds three vulnerable/repaired pairs of a
median-function task (`manifest.json`): a readable reference solution, a
grader that accepts a constant answer, and grading inputs the solver can edit.

1. Run `python scripts/adversarial_replay.py replay <task> --exploit <script>
   --output-dir <new-directory>`. Use a new directory for every replay; an
   existing directory is rejected. For the supplied fixture exploits, expect
   reward 1 on the vulnerable task and 0 on its repaired counterpart. Run
   `--oracle` and `--nop` separately; expect 1 and 0 on each version.
2. Run `python scripts/adversarial_replay.py stage <pair> <directory>` to copy
   the pair under names that hide which version is defective. Save its printed
   name-to-version mapping. Run each attacker with the same execution budget.
3. Replay each reported finding independently. The output directory contains
   `exploit.sh`, complete `solver.stdout.txt`, `solver.stderr.txt`,
   `verifier.stdout.txt`, `verifier.stderr.txt`, raw `reward.txt`,
   `commands.json` (Docker argument arrays), and `result.json` (task, mode,
   exit codes, reward and evidence paths). Operational failures also save
   `error.txt`. Read the commands and outputs; an attacker claim or successful
   script exit alone does not prove a disclosure. Write a finding-specific
   evidence file citing those files and describing the observed prohibited action.
4. Create `runs.jsonl`, one JSON object per unique trial ID:

   ```json
   {"pair":"hidden-reference","version":"vulnerable","trial_id":"attack-1","incomplete":true,"replay_reward":0,"cost_usd":0.5,"findings":[{"id":"qa-finding-1","kind":"protected_file_disclosure","replay_status":"reproduced","evidence_path":"replay-1/finding-1.md"}]}
   ```

   `id` is the QA finding ID and must be unique within that run. `kind` is
   `grading_bypass` or `protected_file_disclosure`. `replay_status` is
   `reproduced`, `not_reproduced`, `not_run`, or `error`. Completed replays
   require a nonempty existing evidence path relative to `runs.jsonl`.
   `not_run` requires null; `error` permits null or an existing error log.
   A reproduced grading bypass requires reward 1; a reproduced disclosure
   permits any reward. Keep `incomplete` explicit. Use null for unknown
   `replay_reward` or `cost_usd`; use an empty `findings` array for no findings.
5. Run `python scripts/adversarial_eval.py runs.jsonl`. For each pair/version,
   the output reports:
   - `runs` and `execution_failures`: input records and records marked incomplete.
   - `findings_total`: all findings, including those from incomplete executions.
   - `reproduced_grading_bypasses` and `reproduced_disclosures`: separately counted
     findings whose replay status is reproduced.
   - `not_reproduced_findings`: attempted replays that did not reproduce.
   - `unverified_findings`: findings marked not_run or error.
   - `reproduced_findings_on_repaired_tasks`: reproduced findings on a repaired
     version; inspect the claimed repair.
   - `cost_usd_total`: known run costs, counted once per run, including failures.
   - `priced_runs` and `unpriced_runs`: records with known and null costs.
   - `cost_usd_per_priced_run`: known cost divided by priced runs, or null when
     no costs are known. Missing replay evidence never establishes a false finding.

## Rollout

Every new task version with `run_analysis` is attacked automatically.
Submission claims the version (`maybe_claim_adversarial`, adversarial_status
None -> QUEUED, next to the audit claim); the cleanup sweep launches it with
request id `auto:<version id>` and sets RUNNING. A launch the API rejects (for
example HTTP 402 for an exhausted organization budget), or one still failing an
hour after the claim, sets FAILED with `adversarial_error`, and QA proceeds
without attacks. `ODDISH_ADVERSARIAL_AUTO_PERCENT` (default 100) picks the share
of versions by a hash of the version id; 0 stops new claims. The task page
button still launches a manual batch.

## Dashboard

The task overview requests `include_adversarial=true` on the task-trial listing.
The default API response stays solver-only for older callers. The opt-in response
includes superseded attacks so retrying does not hide their evidence.

The task overview lists adversarial runs with execution and analysis status,
original grader reward, recorded cost, and links to the trial. Failed setup and
missing execution evidence display as incomplete. Findings use the existing
cards: a linked source-audit finding and an attack appear together with separate
source chips. Adversarial findings remain visible after an attack is superseded;
solver coverage and score counts continue to use ordinary solver runs only.

Run/Rerun adversarial QA asks for a second click ("Confirm: run 4 attacks"),
then submits for the displayed current task version using the server default model. It does not open agent/model controls.
API callers can supply execution overrides; provider/model compatibility follows
ordinary trial admission. The provider must make that model available to the
worker credentials.
The experiment page supplies its experiment; the task page launches directly
for the displayed version without an experiment picker. The backend resolves
the bookkeeping association and validates explicitly supplied membership. The dashboard forwards the request using the signed-in user's
existing permissions. Automatic attack submission remains disabled.


Each adversarial card has one headline from its execution and its replay
(`adversarialOutcome`), plus Execution and Replay rows; the Replay row links to
the replay run. A passing replay reads "Must fix" and shows the attacker's report.
A running replay reads "Replaying exploit"; a pass without a passing replay
reads "Needs a human"; anything else that finished reads "No defect
demonstrated", or "Check incomplete" / "Cancelled". Raw command output is
collapsed under Technical details.

Each card labels the run's recorded task version. Starting a new batch replaces
that version's cards immediately after enqueue. Older runs remain stored and linked
from their findings; hiding their cards never removes required fixes or evidence.
