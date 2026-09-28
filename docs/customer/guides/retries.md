# Retry or cancel

You need permission to change the target task in its workspace. Retries can incur new compute and model charges.

Replace `TASK_ID` with the task you inspected:

```bash
oddish status TASK_ID
oddish run TASK_ID --retry
```

The retry command asks for confirmation before retrying failed trials. Review its target count. Successful trials are retained.

## Request another quality review

```bash
oddish run TASK_ID --retry --qa
```

This queues a replacement QA review. The previous task verdict is withdrawn while that replacement runs. If it fails, inspect the error; the old verdict is not restored automatically.

## Stop active work

```bash
oddish cancel TASK_ID
```

Confirm only after checking the task ID. Cancellation stops its active runs and preserves completed trials. To stop only its QA/audit runs, use `oddish cancel TASK_ID --qa`.

Run `oddish status TASK_ID` afterward to inspect what changed.
