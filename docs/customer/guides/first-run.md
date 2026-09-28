# Run a task

Before starting, [install Oddish and obtain workspace access](../getting-started.md). You also need a Harbor task folder and an agent/model combination enabled for your workspace. Runs consume compute and may incur model charges.

In these commands, replace `./example-task` with your task folder. Replace `MODEL_ID` with the enabled model name supplied by your Oddish contact.

## Check the files

```bash
oddish preflight ./example-task
```

This inspects local files without launching a trial. Resolve reported problems before continuing. It does not prove the task will run successfully.

## Submit one trial

```bash
oddish run ./example-task -a claude-code -m MODEL_ID --n-trials 1 --background
```

`-a` selects the agent, `-m` selects its model, and `--background` returns after submission. Save the task ID and experiment link printed by the command.

The default baseline checks can hold the agent trial until the task's tests pass those checks. QA reviews the task after trials settle. See [Terms](../concepts.md).

## Watch it finish

Replace `TASK_ID` with the returned task ID:

```bash
oddish status TASK_ID --watch
```

The output shows trial progress and, when available, scores. Submission is not completion; inspect each trial's final state.

Next: [Monitor runs](monitoring.md) and [Download results](results.md).
