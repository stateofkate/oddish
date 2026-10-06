# What was proven

Observed on previews, staging or production and written down with a count. "Partly" means a database or unit proof exists but the live case was not run.

## The monitor does its job

Both providers, final Harbor commit, 20 to 21 September.

| Claim | Evidence |
| --- | --- |
| One monitor per sandbox, one check-in per attempt, heartbeats every 30 s | 38 of 38 probe trials, 0 failed heartbeats |
| Real model end to end; Compose task; agent timeout; fail once then pass; 8 at once; cancel | all passed |
| Sandbox sized to the task | 8 GB task sees 8 GB plus 5% headroom |
| Survives an API outage or a redeploy mid-run | 6 minute outage and a redeploy during a 30 minute trial, both finished on attempt 1 |
| Rollback switch | flipped off and on twice; trials followed |
| Private registry login, multi-hour trial | not run |

## The control plane polls and acts

| Claim | Evidence |
| --- | --- |
| Sandbox killed: verdict gone, retry passes | detected and retried within a minute, both providers |
| Monitor frozen: three unanswered sweeps, then kill and retry | reaped after 24 to 27 minutes |
| API outage: monitor answers the poll, trial kept, result delivered later | 28 minute block on Modal, success on attempt 1 |
| Provider unreachable reaps nothing; a late check cannot touch a newer attempt | real-Postgres proofs only |

## Nothing stored was lost

Three same-task pairs per provider, old path against new, 21 September: no trial field empty on the new path, every stored file present (the new path adds the monitor's log), model cost matched to the cent.

## Network isolation

Per provider on the final Harbor commit: no-network 22 of 22, allow-list 22 of 22, public 20 of 20, phase switching 22 of 22, two-service task 33 of 33.

## Regression on real tasks

Kyle's 40-task set, oracle and no-op agents on both providers, 22 September: 156 pairs. 115 matched the baseline, 17 did better, 18 failed for reasons that predate the migration, 6 were two tasks with memory or offline-build problems. On staging the 9 September end-to-end set reran through oddish-mon: 120 of 120 finished, 119 identical to the baseline.

## Production

First-attempt failure rate at or below the old path on 2, 3 and 4 October, and the per-day savings, in [Rollout and results](../results.md).

Not proven: a Daytona outage drill, private registry login, a multi-hour trial, and the real-agent before-and-after on staging. GPU trials never ran on the monitor by design.
