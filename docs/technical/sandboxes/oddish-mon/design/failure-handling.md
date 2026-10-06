# Failure handling

What the control plane does when a trial on oddish-mon stops reporting, and where a launch can fail. Four observations, four actions, and "could not find out" is never treated as "dead".

## When heartbeats stop

![Missed heartbeat flow: accepted result check, then a status check with four verdicts, alive, unknown, unresponsive, confirmed gone](../../../assets/diagrams/heartbeat-verdicts.svg)

The sweep runs every 240 seconds. A heartbeat older than 15 minutes triggers an accepted-result check first, then the monitor's own status command through the provider's exec API. The check count lives on the job row, so overlapping sweeps cannot make a fourth check or a double reap.

| Verdict | What was observed | Action |
| --- | --- | --- |
| Alive | the monitor answered | keep the attempt, reset the count |
| Unknown | the provider or the API could not be reached | keep the attempt, try again next sweep; never reap on an error |
| Unresponsive | the provider says the sandbox is running but the monitor did not answer | after three sweeps, terminate: a hung sandbox takes its monitor with it and would otherwise hold a slot until the deadline |
| Confirmed gone | the monitor exited, or the provider says the sandbox ended | reap now |

A reap re-checks ownership and the heartbeat in one statement, then runs the existing path: mark the job for retry or failure, mirror it to the trial, release the slot and the capacity lease, revoke the token, tear down the sandbox. Provider calls happen outside the database transaction. The next attempt runs on the old worker path.

Every attempt is bounded by a 12 hour deadline from claim, enforced by the sweep and by the monitor itself, so the queue slot is always freed inside its lease.

This is why the connection-pooler incident that reaped dozens of healthy trials under the old design cannot repeat: when every heartbeat fails at once, every status check still answers, and nothing dies.

## Where a launch can fail

![Launcher timeline with four crash points: before create, created but handle unsaved, handle saved but no check-in, after check-in](../../../assets/diagrams/launcher-crash-points.svg)

The gap between creating a sandbox and saving its handle is bounded by the provider's timeout, not recovered; closing it by sandbox name is a roadmap item. A sandbox that dies within 20 seconds of creation fails the launch at once (since 24 September); one that never checks in is reaped at the launch deadline.

Completion arriving during hand-off, a duplicate completion, a stale attempt reporting after a retry, and recovery overlapping completion are covered by attempt fencing: every write carries the attempt it observed, and a write for an older attempt changes nothing.

## Drills that were run

On staging, 23 September, on the final code: a killed sandbox was detected as gone and retried within a minute; a frozen monitor was reaped after three unanswered sweeps, 24 to 27 minutes; a 28 minute API outage was survived with the result delivered afterwards on the same attempt; two backend deploys during a live trial finished on attempt 1. The reconciler's own bounded-pass and rotation rules are covered by real-Postgres tests.


