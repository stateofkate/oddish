# Share results

Publishing creates a public read-only link. Anyone with that link can read the shared results without a docs invitation. Review the content before publishing.

You need a full-scope API key authorized for the experiment. Replace `EXPERIMENT_ID` with its identifier:

```bash
oddish publish EXPERIMENT_ID
```

The command prints the public URL. QA is hidden by default; an organization administrator can choose to include it in the app's share dialog.

To stop future access through that link:

```bash
oddish unpublish EXPERIMENT_ID
```

This cannot erase copies someone already downloaded.

## Collect trials without publishing

Replace `TASK_ID` with a task in your workspace:

```bash
oddish collect --task TASK_ID --name example-collection --no-publish
```

The collection references the task's current-version trials. Keep `--no-publish`: without it, `collect` publishes by default. Inspect the collection before deciding to share it.
