# Architecture

Text version of the designed page. Shares are of the compute a CPU trial cost on the old path on 3 October 2026 (Modal, agent trials).

## Before

The Oddish queue (API, dispatcher, reaper; shared, not per trial) claims a trial and starts a watcher machine: one core, held for the whole trial, billed at three times the base rate because it must not be interrupted. The watcher creates and drives the task sandbox, where the trial actually runs, then a verifier box, and heartbeats to the database every 30 seconds for hours before writing the result.

| Part | Share of a trial | Dollars |
| --- | --- | --- |
| Watcher machine | 37% | $0.22 |
| Task sandbox | 62% | about $0.37 |
| Verifier box | 1.5% | $0.009 |

## After

The queue claims a trial and starts a launcher, the same machine type, which creates the sandbox and exits after about 20 seconds. The sandbox is a small VM holding oddish-mon and the task's own containers, walled off from each other. oddish-mon runs the trial, heartbeats every 30 seconds and posts the result. On the first run of a task version, a warm-up box builds the images once and keeps a snapshot. If a sandbox goes quiet, the queue asks it three times, then cleans up and retries on the old path.

| Part | Share of a trial | Dollars |
| --- | --- | --- |
| Launcher | 1% | $0.007 |
| Task sandbox, with oddish-mon and the verifier inside | 62% | about $0.37 |
| Warm-up, spread over the task's trials | 1% | $0.007 |
| Total | 65% of the old path | about $0.38 |

## How it stays safe

Behaviour was kept identical: same results, same files, one extra log. The ramp is by sample with automatic fallback to the old path after one failed attempt. Images are built once per task version. Restricted-network tasks got a stricter firewall. Memory headroom of 5% stays so 8 and 16 GiB tasks do not run short.

Deliberately not built: a shared launch host (set aside 23 September, worth about 0.5% of compute), a GPU path (see the appendix), review jobs on the new path, and deleting the old path before 100% and full coverage.

## Modal constraints

Non-preemptible work costs 3x. Docker needs a VM and VMs cannot have GPUs. Sandbox creation is rate limited at about 5 a second; a quota of 10 needs a support request. Creating a sandbox takes 16 to 39 seconds; the snapshot cache hides most of it. The ledger's rate card was last checked in July, so percentages are safer than dollars.
