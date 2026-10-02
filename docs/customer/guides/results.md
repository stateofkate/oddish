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

## Robot playback

A robot task can describe its simulation for replay. Its verifier writes
`verifier/artifacts/playback.json`, the playback description. It names a
recording of the simulation (an `.rrd` file from the Rerun viewer, version 0.38),
the camera images the agent received, the actions between them, and the checks
the verifier evaluated.

When a finished trial has a valid playback description, its trial panel adds a
**Scene** tab beside **Summary**, **Files**, **Trajectory** and **Artifacts**.
Trials without one, such as coding-agent trials, show only those four tabs.
Summary also lists the task's goal and its **Final checks**, each marked passed,
failed or not evaluated for the finished attempt.

In **Scene**, press Play, drag the time slider, or pick an action to move
through the recording. **Before** and **After** jump to the moment the current action
starts and ends. If the description sets `"viewer_layout":
"embedded_scene"`, the agent's camera images appear beside the scene, with the
command that produced each image underneath. **View agent trace** under a
command opens that step in **Trajectory**. **Agent time** is how long the agent
ran; a dash means it was not recorded. You can also open an `.rrd` file from
**Files** or **Artifacts** to explore it with the viewer's own timeline.

If a trial's verifier wrote a playback description but the trial has no Scene
tab, for example because it finished before Oddish supported playback, prepare
it from its saved files:

```bash
oddish playback rebuild TRIAL_ID --json
```

Replace `TRIAL_ID` with the trial's ID. The command does not rerun the
simulation or change the score. Your API key needs task-write access; add
`--api API_URL` to target another Oddish server. It prints the playback status:
`ready` (Scene is available), `absent` (no playback description), or `invalid`
with a reason such as a missing image, in which case it exits with code 1.
`oddish status TRIAL_ID` shows the same status, or `not prepared` for a trial
that was never checked. An invalid trial still gets a Scene tab, which shows
the reason. Download the files it references with `oddish pull TRIAL_ID`.

Next: [Retry or cancel](retries.md).
