# Upload task files

You need the [installed CLI and a workspace API key](../getting-started.md). Replace `./example-task` with your Harbor task folder.

```bash
oddish preflight ./example-task
oddish upload ./example-task
```

The upload registers the files and returns task details. It does not run trials. Re-uploading unchanged content does not create another version.

To inspect the task afterward, replace `TASK_ID` with the returned identifier:

```bash
oddish status TASK_ID --versions
```

To import an existing Harbor job, replace `./example-job` with a job folder containing `result.json`:

```bash
oddish upload ./example-job --task TASK_ID
```

That command imports existing trial results into the selected task. It does not execute the job again.

Use [Run a task](first-run.md) when you want Oddish to execute new trials.
