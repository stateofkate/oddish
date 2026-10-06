# oddish-mon: trials run themselves

Until September 2026, every Oddish trial had a second machine beside it that did nothing but wait for the trial to finish. That supervision now runs inside the trial's own sandbox. As of 5 October 2026, 70% of CPU trials in production run that way.

## What changed, in one paragraph

Before: the queue handed a trial to a watcher machine, one core, non-preemptible and billed at three times the base rate, which lived for the whole trial and drove a separate task sandbox. After: a launcher creates the sandbox in about 20 seconds and exits. Inside the sandbox, oddish-mon runs the trial, sends a heartbeat every 30 seconds and posts the result. If a sandbox goes quiet, the queue asks it three times, then cleans up and retries on the old path. Task images are built once per task version in a warm-up box and reused from a snapshot. The drawings are in [Architecture](architecture.md); the reasoning behind the design is in [Problem and principles](design/problem.md).

## Headline numbers

| Measure | Share | Dollars | Basis |
| --- | --- | --- | --- |
| Compute per CPU trial | 35% less | $0.21 less per trial | 3 October production averages, Modal |
| Saved 2 to 4 October | 10% of Modal CPU-trial compute | about $3,600 | ledger, those three days |
| Ceiling at 100% on Modal CPU | about 30% of CPU-trial compute, 14% of all compute, 3.7% of total spend | about $4,500 a day | this week's volume at the 3 October watcher rate; estimate |
| First-attempt failure rate | at or below the old path | | 2 to 4 October, both paths |

The same saving is 30%, 14% or 3.7% depending on the denominator. Each figure above names its own. Day-by-day figures and service levels are in [Rollout and results](results.md); the evidence behind every claim is in [What was proven](delivery/proven.md).

## Decisions requested

1. Go to 100% on Modal CPU. Three full days of first-attempt failure rates at or below the old path. Fallback stays on.
2. Ask Modal for a 10 per second sandbox-creation quota. A support ticket, not code.
3. Park the GPU design until the two above are done.

## Reading order

Design: problem and principles, architecture, one trial step by step, failure handling, network isolation, images and warm-up. Delivery: rollout and results, what was proven, what changed along the way. Running it: costs and how to measure, operations. Roadmap: not built, and why. Then the appendix.

Code: the Oddish repository, launcher and monitor packages. Linear: ABT-1285. The production sample is one line in the Modal deploy workflow; 0 disables it.
