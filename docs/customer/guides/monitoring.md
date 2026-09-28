# Monitor runs

You need a workspace API key. Replace `TASK_ID`, `TRIAL_ID`, and `EXPERIMENT_ID` with identifiers printed by Oddish.

```bash
oddish ls
oddish status TASK_ID --watch
oddish status TRIAL_ID
oddish status --experiment EXPERIMENT_ID --watch
```

`oddish ls` lists uploaded tasks. `status` shows progress and scores; `--watch` keeps refreshing until work ends.

## Read a running agent's output

```bash
oddish logs TRIAL_ID --follow
```

Live logs are available for supported agents. An empty transcript does not prove that execution has stopped. Inspect `oddish status TRIAL_ID` as well.

Live events are removed after the trial ends. Use [Download results](results.md) for finished trials.

## Read results in a script

```bash
oddish status TASK_ID --json
```

JSON is a structured format for programs. The `trials` list includes QA and audit runs. Top-level trial counters count evaluation trials only; filter rows to `kind == "agent"` when comparing those counters.
