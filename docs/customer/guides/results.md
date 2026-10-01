# Download results

You need a workspace API key and a trial, task, or experiment ID. Replace `TRIAL_ID` with your trial's ID:

```bash
oddish pull TRIAL_ID --out ./results
```

Oddish downloads available logs and artifacts into `./results`. Without `--out`, it writes to `./.oddish/<target>`, where `<target>` is the supplied ID.

To include task files when downloading an experiment:

```bash
oddish pull EXPERIMENT_ID --include-task-files --out ./results
```

Replace `EXPERIMENT_ID` with the experiment's ID. Repeating the download skips files whose stored size already matches. Add `--watch` to fetch new or changed files until the run ends.

A trial can finish without a valid score. Check its error and verifier output before treating a missing reward as failure or success.

If the task writes `verifier/metrics.json` or `verifier/ctrf.json`, those files contain task metrics or test counts. Their absence does not itself change the reward.

[Download an example metrics file](../assets/example-metrics.json). Its values are fictional.

Runs whose verifier writes a playback description open on **Run → Scene and
observations**. Overview shows the task's goal and final checks, which describe
the finished attempt and do not change while you scrub. Use Play, the time
slider, or an action to move through the recording; **After** shows the
observation an action produced. Interactive robot runs show the camera images
the agent received beside the complete scene. Beneath each image is the agent
command that produced it; **View agent trace** opens that step, and Back
returns to the image. Run also holds **Full agent trace** and, while running,
**Live output**. **Agent time** shows elapsed agent-phase time; a dash means
none was recorded.

**Files** lists every run file, with an **Artifacts only** filter. Recordings
(`.rrd`, Rerun 0.38) open in the full viewer there. Images fit the pane, with
100% and zoom controls; recorded observations add previous/next buttons,
thumbnails and arrow-key navigation. Retry and Delete are in the Trial actions
menu. Upload task bundles and robot assets with `oddish upload /path/to/task`.

If a completed run says **Playback has not been prepared**, select **Prepare
playback**, or run:

```bash
oddish playback rebuild TRIAL_ID --json
```

This checks the saved recording and images and prepares their links to agent
commands. It does not rerun the simulation or change its score. The key needs
task-write access. Use `--api API_URL` to target a preview or self-hosted server.
`oddish status TRIAL_ID` reports playback as `ready`, `absent` (no playback
description), `invalid` (with a reason), or `not prepared`. An invalid rebuild
exits with code 1; use `oddish pull TRIAL_ID` to inspect the referenced files.
Playback is available after execution; Live output contains the running agent's
text. Raw recordings in Files have their own timeline controls.

Next: [Retry or cancel](retries.md).
