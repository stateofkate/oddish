# Rollout and results

Text version of the designed page. Measured from the Oddish cost ledger and job table, 22 September to 5 October 2026.

## Savings by day

Share of that day's Modal CPU-trial compute avoided, and the share of first attempts that actually ran on oddish-mon.

| Day | Configured | Ran on oddish-mon | Saved, share of CPU-trial compute | Dollars |
| --- | --- | --- | --- | --- |
| 23 to 30 Sep | 1%, then 5% | under 2% | 0.4% | $308 |
| 1 Oct | 20% | 12% | 3.3% | $226 |
| 2 Oct | 40% | 23% | 7.0% | $1,355 |
| 3 Oct | 40% | 36% | 11.5% | $1,485 |
| 4 Oct (Sunday) | 40% | 38% | 11.0% | $736 |
| 5 Oct, partial | 70% | | 13.4% by mid-afternoon | $189 |

## Timeline

18 Sep problem agreed. 19 to 22 Sep built and proven on previews. 23 Sep staging at 100%, production at 1%. 24 Sep image warm-up and early-death detection. 28 Sep 5%. 1 Oct 20%. 2 Oct 40%, with a settlement bug fixed the next day. 5 Oct 70%. Next: 100% on Modal CPU.

## Where the money goes

Of everything spent over eight weeks, 73% is model tokens and 27% compute. Of compute, 86% is sandboxes and 14% the watcher. Of one CPU trial on 3 October, 63% is the sandbox and 37% the watcher.

## Service levels

| Promise | Old path | oddish-mon | Evidence |
| --- | --- | --- | --- |
| First-attempt failure rate, 2 / 3 / 4 Oct | 8.7%, 3.8%, 2.1% | 8.8%, 2.6%, 1.3% | no regression |
| Everything stored that was stored before | yes | yes, plus the monitor's own log | same-task pairs |
| Time from claim to trial running | worker boot, then sandbox | 8 to 19 s hand-off; repeat tasks boot a snapshot | measured |
| A sandbox that dies at start is noticed | 15 to 18 min | within 20 s | live since 24 Sep |
| A sandbox that goes quiet is handled | 15 min, then reaped | 15 min, asked 3 times, then reaped and retried | drilled |
| A failed new-path attempt still completes | n/a | next attempt runs on the old path | in production |
| Rollback | n/a | one number, one redeploy | one line |
| Cancellations, first attempt, 2 Oct | 3.6% | 12.6%, back to 6 to 7% since | spike on the day of the stuck-job bug |

## What runs on it

Agent trials on Modal CPU: oddish-mon at 70%. Not on it: Archil CPU trials (no adapter, stays on the old path for now), Modal GPU trials (provider limit), review jobs, Thunder, GKE and EC2. Daytona is switched off.
