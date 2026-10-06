# What it fixed

Problems that existed before oddish-mon, or were found during the rollout, and the state of each.

| Problem | Fix | Since |
| --- | --- | --- |
| A non-preemptible machine held for every trial's whole life, a third of a CPU trial's compute | the trial supervises itself from inside its sandbox; a launcher runs for about 20 seconds | 23 Sep, 70% of Modal CPU trials by 5 Oct |
| A stale heartbeat was the only liveness signal; a database blip once reaped 25 to 70 healthy trials | the control plane asks the sandbox itself, up to three times, before any reap; an error is never a death | 23 Sep |
| A wrongly reaped trial kept running beside its retry | reaps revoke the attempt's token and tear down the sandbox; a late write for an old attempt changes nothing | 23 Sep |
| Task images rebuilt inside every billed sandbox | built once per task version in a warm-up box, reused from a snapshot | 24 Sep |
| A sandbox that died at creation sat for 15 to 18 minutes before anyone noticed | failed at launch within 20 seconds | 24 Sep |
| A restricted-network task could ping out and bypass the firewall with a marked socket | NET_RAW dropped from task containers; no-network and allow-list rules tightened in Harbor | Harbor main, 3 Oct |
| GPU trials sampled onto the monitor got a sandbox Modal refused | GPU trials route to the old path by rule | 24 Sep |
| Monitor jobs stayed open after an interrupted settle and blocked dependent trials | settle fits inside the reconciler deadline and is never cancelled once started | 3 Oct |
| Teardown took about 10 seconds per trial | about 1 second | 24 Sep |

Not fixed by this work: the watcher on GPU, Archil, Thunder, GKE and EC2 trials and on review jobs, which stay on the old path. See [Not built, and why](../roadmap/not-built.md).
