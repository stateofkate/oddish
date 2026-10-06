# Appendix

## A. Why GPU trials are not on it

GPU trials were $132k of compute over 30 days, of which the watcher was 3.6%, about $4.7k. Next to an H100 at about $4 an hour, a one-core watcher is a rounding error, so the prize is small.

The reason, in four lines: oddish-mon runs Docker inside the sandbox so the task cannot reach the monitor's credentials or its own score. Docker on Modal needs Modal's VM-style sandbox. Modal's VM-style sandbox cannot have a GPU. Putting the monitor inside the task's own container was rejected for the reason in line one.

The design on the shelf: a normal GPU sandbox with the task in the main container and oddish-mon in a sidecar beside it, talking to the task through our own control plane so no cloud key sits in the sandbox. Probed on an H100 on 24 September: the sidecar runs, its credentials are invisible to the task, it outlives the launcher, it has network. Not built. Linear ABT-1373.

## B. Sources and limits of the numbers

Measured: per-trial costs and daily savings from the Oddish cost ledger, 22 September to 5 October 2026; failure and cancellation rates from the job table; eight-week shares from the 27 July to 20 September ledger; same-task pairs from staging and production in September.

Estimated: the 100% run rate (this week's volume at the 3 October watcher rate), the sandbox cost split per trial, the Modal invoice split, and the GPU and review-job prizes.

Not measured: the ledger against Modal's invoice, and task mix across the two paths.
