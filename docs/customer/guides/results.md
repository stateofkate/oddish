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

Next: [Retry or cancel](retries.md).
