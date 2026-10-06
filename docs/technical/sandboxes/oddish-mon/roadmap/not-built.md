# Not built, and why

Designs that exist on paper, were set aside, or are waiting for a decision. Each has an owner in Linear.

## Shared launch host, set aside

A follow-on design, called v1.1, removed the per-trial launcher too: one preemptible Modal call per dispatcher tick would claim a batch of trials and start their sandboxes, so no non-preemptible container would be held even for the 20 second launch. It was planned in full on 23 September and set aside the same day. The launcher residual is about 0.5% of compute, and the design traded a known-safe per-trial process for a shared one. Branches remain; the plan page is in the notes.

## GPU trials, waiting

GPU trials run on the old path by rule. The reason and the shelved sidecar design are in the [Appendix](../appendix.md). Worth about 3.6% of GPU trial compute, which is why it waits behind the items below. Linear ABT-1373.

## Archil trials, not now

Archil is a second CPU sandbox provider that began taking 40% of new CPU submissions on 30 September. It has no oddish-mon adapter and stays on the old path for now; those trials keep paying a watcher. If that changes, an adapter is one file: start, stop, status and sandbox state.

## Review jobs

Audit, QA and summary trials run on the old path because the launcher modifies the task copy and mints extra credentials for them, which the monitor path does not carry yet. About 0.5% of compute.

## Deleting the old path, last

The old worker path is the fallback after a failed monitor attempt and the only path for GPU, Archil, review jobs, Thunder, GKE and EC2. It is deleted only when every category has a home and production has run at 100% through a clean window of checks. Linear ABT-1296.

## Smaller items

- Closing the create-to-record gap by naming sandboxes deterministically, so a launcher that dies between creating a sandbox and saving its handle leaves something the reconciler can find. Linear ABT-1311.
- A snapshot sweep for Daytona before its warm-up is re-enabled. Linear ABT-1375.
- A monitor log for trials that fail before Harbor starts, which Modal does not keep today.
- Reconciling the ledger rate card with the Modal invoice.
