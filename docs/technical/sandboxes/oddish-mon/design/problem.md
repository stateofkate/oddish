# Problem and principles

Written from the code as it stood on staging on 18 September 2026 and the first-principles note of the same day.

## How a trial ran before

Every trial was handled by a Modal function called the worker. The worker was not idle: it ran Harbor's trial loop in its own process. Harbor is the evaluation harness that installs the agent, drives it against the task, runs the verifier and collects the artifacts. The worker lived for the whole trial, often hours, on a non-preemptible container that Modal bills at three times the base rate because it must never be interrupted.

The worker did three jobs in one container.

| Part | What it did | How long | Verdict |
| --- | --- | --- | --- |
| Launch | lease a queue slot, claim the job, authorise, decrypt credentials, prepare the task, build Harbor's config | seconds | keep as is; it becomes a short function that ends by starting the sandbox |
| Wait and drive | run Harbor, heartbeat, tail logs, watch for cancel | hours | this is what moves into oddish-mon |
| Settle | read the outcome, upload artifacts, write the trial row, advance the task, price the cost spans, release the slot | seconds | keep the code, change what triggers it |

Only the middle part cost money, so the change cuts the container at those two seams.

## How liveness worked before

![Workers wrote a heartbeat into Postgres; a timed reconciler read it and reaped stale rows; Modal was never asked](../../../assets/diagrams/old-liveness.svg)

A worker proved it was alive only by writing a timestamp every 30 seconds, and a sweep declared it dead when the timestamp was 15 minutes old. A dead worker and an unreachable database looked the same; a pooler blip once reaped 25 to 70 healthy trials, and a wrongly reaped trial kept running beside its retry.

![From container death to a replacement worker: fifteen to twenty minutes](../../../assets/diagrams/old-liveness-timeline.svg)

## Principles agreed on 18 September

Agreed between Mark, Meji and Kyle before any code was written.

1. Everything the worker did keeps working. Behaviour stays identical; the migration adds no product feature.
2. The same driver code runs inside the sandbox as oddish-mon. Its database writes become calls to the control plane's HTTP API: heartbeat, live events, result.
3. The only new behaviour is a status check before any reap. When a heartbeat goes stale, the control plane asks the sandbox itself, up to three times, before deciding the trial is dead. An error from the provider or the API is inconclusive, never a death.
4. Change as little as possible. Launch and settle keep their code and get a different trigger.
5. Prove it on previews, then staging, then production, ramped by sample with automatic fallback.

## Duty by duty

Where each thing the worker did during a trial now lives.

| Duty during the trial | Interval | New home |
| --- | --- | --- |
| Run Harbor: install the agent, drive it, run the verifier, collect artifacts | whole trial | oddish-mon, same runner code, Harbor's local Docker environment |
| Heartbeat to the job and trial rows; renew the capacity lease on the EC2 and Thunder lanes | 30 s | the monitor calls the heartbeat endpoint; the API runs the same function the worker ran |
| Ownership probe, which is how a user cancel stops a run | 30 s | the heartbeat reply: status 410 when the row no longer belongs to this attempt |
| Organisation approval re-check | 15 s | the API re-checks on every heartbeat and answers 410; the reconciler stays as backup |
| Live transcript into the events table, with token and cost checkpoint and secret redaction | 30 s | the monitor runs the existing tail and redaction locally and posts the same rows |
| Settle: trial outcome, job outcome, costs, retries, post-success hooks, token revoke, slot release | at end | a short settle function, same code, triggered by the result |
| Dead-worker detection | 15 min, then sweep | same threshold, same reap, with up to three status checks first |

What is genuinely new: a heartbeat endpoint, an events endpoint, a result call, the status check in the reconciler, a few columns on the job row, and the monitor process. Everything else is existing code reached through a different door.

Out of scope: faster detection thresholds, stall rules, a new image pipeline, dashboard changes, and the jobs that have no sandbox.
