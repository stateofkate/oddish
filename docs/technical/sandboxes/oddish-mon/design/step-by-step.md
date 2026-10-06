# One trial, step by step

The life of one agent trial on the oddish-mon path, as it runs in production since 23 September 2026. The control plane is everything of ours that is not the sandbox: the API, the dispatcher that assigns work, the reconciler that sweeps for problems, and Postgres.

![Thirteen steps from dispatch to slot release: claim, launch, create the sandbox, hand off, check in, heartbeats, result, settle](../../../assets/diagrams/happy-path.svg)

## Before the sandbox exists

1. **Claim.** The dispatcher tick selects a queued trial. The routing rule decides the path: an agent trial on Modal CPU whose trial id falls in the sampled buckets goes to oddish-mon on its first attempt; everything else, and every later attempt, goes to the old worker path. One log line per claim records the route and the reason.
2. **Launch.** A short-lived launcher, the same Modal function type as the old worker, claims the job row, leases a queue slot, resolves the user's model keys and the funding key, prepares the task, captures the task's resource request, and mints a monitor token. The token is the sandbox's only credential to the control plane and is bound to this attempt.
3. **Image.** The launcher looks up the task version in the snapshot cache. On a miss it runs a warm-up first, described in [Images and warm-up](images-and-warm-up.md). On a hit the sandbox boots from the snapshot.
4. **Create and hand off.** The launcher creates the sandbox with the monitor as its entrypoint, passing secrets only through the provider's creation slot, records the sandbox handle on the job row, and exits. The queue slot and the capacity lease stay on the row. From here no process outside the sandbox is dedicated to this trial. Hand-off takes 8 to 19 seconds when the snapshot is warm.

## Inside the sandbox

5. **Boot.** The sandbox is a small virtual machine. Its entrypoint swaps in the launcher's code bundle, starts the Docker daemon with fresh execution state, and starts oddish-mon.
6. **Check-in.** oddish-mon fetches its attempt spec from the API, a typed and secret-free description of what to run, and records its check-in. The check-in is what proves the monitor started; a launcher heartbeat cannot prove that because it shares the worker id.
7. **Run.** oddish-mon runs Harbor's Docker environment: it builds or reuses the task's containers, installs the agent, drives it, then runs the verifier. The task's containers are walled off from the monitor; the agent never sees the monitor token or its own result.
8. **Report while running.** Every 30 seconds oddish-mon posts a heartbeat. Every 15 seconds it reads the control reply, which can tell it to stop (status 410 when the attempt is no longer its own, or the organisation lost approval) or to pause under a quota. It also posts stage changes and the live transcript rows that the dashboard shows.
9. **Finish.** oddish-mon uploads the job directory as one archive through a presigned URL, then posts the result: reward, status, timings and the archive reference. The API accepts a result once per attempt; a duplicate is a no-op.

## After the result

10. **Settle.** The API runs the same settlement code the worker ran: writes the trial, prices the cost spans, advances the task, runs post-success hooks, revokes the token and releases the slot. A settle that was interrupted is finished by the reconciler; since 3 October a job carries a mark once its trial is written, so a retry only closes the job.
11. **Teardown.** The sandbox is stopped. Teardown takes about one second since the fast-stop change on 24 September; it was about ten.

## If anything goes quiet

The reconciler's existing sweep notices a heartbeat older than 15 minutes, or a launch that never checked in before its deadline, and follows [Failure handling](failure-handling.md).
