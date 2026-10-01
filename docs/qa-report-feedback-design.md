# Independent QA reports and feedback

Internal design proposal, based on staging `00c6a7cb`. This PR implements the
feedback changes below. Separating verdict execution is proposed follow-up work.
QA means the automated review of a task's source files and recorded agent runs.
A trajectory is the sequence of actions taken by an agent during one trial.

## What this PR implements

The task verdict, pre-trial audit, and each trajectory report have independent
Agree/Disagree controls. A pre-trial audit with no findings still accepts feedback.
The trial drawer's five-button Correct label card is removed. The separate admin
queue for labeling evaluation examples remains available.

The existing `feedback` table stores one row per submission. New rows distinguish
`qa_trajectory`, `qa_task_verdict`, and `qa_pre_trial`. The row stores the reviewer,
time, vote, optional explanation, report-producing run ID, and subject trial ID.
For a trajectory the subject is the agent trial; for a verdict or audit it is the
producing run. The target key records the displayed classification, accept/reject,
or `report`, respectively. The server checks organization, task, task version,
report kind, and whether the trajectory was included in that report's input.

Old `qa_verdict` and finding-level `qa_action_item` votes retain their original
meaning. No historical votes are guessed into a new category. Legacy reports
without a producing-run ID, including deterministic rejections with no QA run,
do not show report feedback. The immutable report records proposed below would
supply identities for those deterministic results too.

Each successful submission appends a row. This PR does not add a feedback history
screen, deduplicate repeated submissions after reload, or persist button selection
across reloads. Consumers must treat rows as feedback events, not a count of unique
reviewers. A vote never changes acceptance, classification, or delivery checks.

## Why verdict execution is currently coupled

`oddish/src/oddish/queue.py` schedules `create_qa_trial(..., with_verdict=True)`
after finding eligible agent trials. In `oddish/src/oddish/workers/analysis_trials.py`,
`create_qa_trial` creates one QA run whose prompt requests both trajectory reports
and a task verdict. `_import_qa_result` validates the complete artifact before
saving classifications and publishing the verdict. A malformed verdict can
therefore prevent otherwise useful trajectory results from being imported.

`oddish/src/oddish/core/verdict_state.py` owns verdict state changes.
`queue_verdict` clears the published task verdict when a replacement QA pass is
queued. The trial drawer's Re-run Trajectory analysis action starts task QA,
re-analyzing eligible trials and regenerating that verdict. Changing its caption
or setting `with_verdict=False` alone would leave these publication and status
side effects in place.

## Proposed execution and storage

Use three independently retryable job kinds, reusing the existing trial worker
queue and organization access checks. A verdict consumes completed evidence;
it does not rerun the agent or reclassify its trajectory.

```text
Task version (immutable source files)
    |
    +--> Pre-trial audit job ------> Audit report A -----------+
    |                                [Agree / Disagree]       |
    +--> Agent trials T1...Tn                                  |
    |       |                                                 |
    |       +--> Trajectory jobs --> Reports R1...Rn ----------+
    |                                [Agree / Disagree each]  |
    +--> Baseline trials ----------> Recorded scores ---------+
                                                             |
                                                Evidence snapshot E
                                                (exact report IDs)
                                                             |
                                                Task verdict job
                                                             |
                                                Verdict report V
                                                accept / reject
                                                [Agree / Disagree]
                                                             |
                                                Delivery policy checks

Every report --> Feedback events (report ID + reviewer + vote + note + time)
Feedback events have no write path into verdict or delivery state.
```

Add an immutable `analysis_reports` table: `id`, `org_id`, `task_version_id`,
`kind` (pre-trial, trajectory, verdict), optional `subject_trial_id`, producing
run ID and attempt, schema version, prompt/model provenance, result JSON, and
completion time. A server-generated deterministic verdict gets a report ID even
without a model run. Keep pending/running/failed state on jobs; only successful,
validated results become reports. Old reports survive reruns.

Store verdict inputs in `verdict_report_inputs`: verdict report ID, input report
ID, and baseline trial ID through explicit reference columns with a constraint
requiring exactly one input reference per row. Capture the task content hash,
audited source fingerprint, and ordered input IDs in an evidence digest. Enforce
one active verdict job per task version and evidence digest. A repeated enqueue
returns that job; a deliberate rerun creates a new attempt with new provenance.

Publish the active verdict pointer in one transaction only if the task version
and evidence digest still match the job's inputs. Late results remain historical
reports and cannot replace a verdict for newer evidence. Store current evidence
freshness separately from the last verdict result. The UI can then show
“Accepted on prior evidence; verdict refresh needed” without claiming current
acceptance. Delivery eligibility requires a current accepted verdict plus its
existing policy checks. Failed replacement jobs retain the prior report for
inspection but cannot make stale evidence eligible for delivery.

Re-running one trajectory marks dependent verdicts outdated. It changes that
trajectory job's status without clearing or rewriting the verdict report. A
verdict refresh can be requested explicitly or queued after all required inputs
complete. If no usable solver evidence exists, preserve insufficient-evidence
handling; retain deterministic rejection for established task defects and failed
baseline checks. An oracle baseline is a reference solution expected to pass;
a no-op baseline is an agent that does nothing and should fail. Preserve these
server-owned rules from `core/verdict_sync.py` rather than making them model
preferences.

For feedback history, add a read endpoint keyed by report and reviewer. Keep
append-only events with a request idempotency key so transport retries do not
create duplicate events. Show the latest event per reviewer/report while retaining
the audit trail. Count feedback separately for the three report kinds. Agreeing
with an acceptance is feedback on that judgment; it is never a release approval.

## Implementation sequence and acceptance checks

1. Add report records, input references, and read projections. Backfill only
   provenance that is actually recorded; mark unknown history explicitly. Keep
   existing task verdict and trial analysis response fields for installed clients.
2. Split trajectory and verdict prompts, result validators, importers, and job
   dispatch. Trajectory import must have no calls into verdict publication. A
   malformed verdict must not discard valid trajectory reports. Give verdict
   runs their own cancellation, cost attribution, permission scope, and retries.
3. Change queue admission, rerun endpoints, task/experiment summaries, and delivery
   checks to consume independent job state and evidence freshness. Add a verdict
   refresh endpoint and make the trajectory rerun endpoint affect only its selected
   report. Route source changes and audit reruns through dependency invalidation.
4. Switch the UI and feedback history to report IDs, then retire combined writers
   after old clients and queued jobs are supported by the compatibility reader.

Test concurrent completions, duplicate enqueue requests, cancelled/retried jobs,
audit replacement, new task versions, and report votes after reruns. Verify that
rerunning a trajectory never clears the stored verdict; stale verdicts never pass
delivery checks; verdict failures never erase trajectory reports; foreign
organization reports cannot be read or voted on; and a feedback vote cannot
publish an acceptance. These are requirements for the follow-up, not claims
that this PR already implements the split.
