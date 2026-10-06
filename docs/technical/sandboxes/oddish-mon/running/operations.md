# Operations

How to steer oddish-mon in production, what to check after a change, and where to look when something is wrong.

## The rollout switch

Two values in the production deploy workflow for the Modal backend:

- Sample percent. The share of eligible trials, by stable trial-id bucket, that run on oddish-mon for their first attempt. 70 since 5 October 2026. Set it lower to roll back, 0 to disable. Running trials finish where they are.
- Fallback after attempts. Attempts after this number run on the old path. 1 in production.

A change takes effect on the next production deploy. Promotion follows the normal release process: a pull request from staging to main with the release template, then the promote command. Staging runs 100% with no fallback.

## Who is eligible

A trial runs on oddish-mon only if all of these hold: it is an agent trial, not audit, QA or summary; its provider has a monitor adapter, today Modal; it does not request a GPU; it is not on Daytona; the sandbox can reach the control plane. Every claim writes one log line with the route and the reason, so the ineligible reasons can be counted.

## Checks after a deploy or a ramp step

Using the deploy time as the window start:

- Route split by attempt: first attempts split close to the configured sample.
- Monitor jobs by outcome and attempt: hand-off only on attempt 1 while fallback is 1.
- Fallbacks: trials whose attempt 1 was monitor and attempt 2 was worker.
- Failure text on failed monitor jobs, top twenty.
- Stuck: running monitor jobs whose trial is already finished. Should be zero.
- Stale heartbeats still running past 15 minutes. Should be near zero after each sweep.
- GPU on the monitor: zero.
- Cost per finished trial, monitor against worker, same window.
- Warm-up cache labels in the launch log line: hit or waited should dominate; warm failed, wait expired and error near zero.

The SQL for these lives beside the report in the Oddish notes.

## Where the logs are

- Modal app logs for the production app, filtered by the metric line names: monitor launch, monitor route, monitor result, monitor settle, monitor status check, monitor warm timing. With a since-time the history is complete; without one only a short window is returned.
- The monitor's own log is stored with every trial's files.
- Logfire holds the operation traces and is what the production reviews used.

## Known limits

- Modal refuses sandbox creation above about 5 per second. A refused launch falls back to the old path, so trials still run, but the saving leaks. Raising the quota to 10 is a support request not yet made.
- Daytona is disabled. Its warm-up code is kept and tested but off.
- The ledger's rate card dates from July; reconcile against the invoice before quoting dollars.
