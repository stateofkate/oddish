# Troubleshooting

## I cannot sign into these docs

Use the exact email address invited to Abundant Docs. An Oddish task-workspace account does not automatically grant docs access. Ask the person who arranged access to check the invitation and whether the account was removed.

## I can read docs but cannot see tasks

Docs and task access are separate. Ask your Oddish contact for access to your own workspace. Do not request membership in Abundant's internal workspace.

## The install script cannot get the release

"Could not get the CLI release" means the API did not accept `ODDISH_API_KEY`. Check that it is exported in the same shell, has not expired, and was not revoked under **Settings → API keys**. "Checksum mismatch" means the download was altered or cut short; run the command again.

## Homebrew cannot find or download Oddish

Confirm that your GitHub account has read access to `abundant-ai/homebrew-tap`. Run `gh auth status` to inspect the selected GitHub account. Follow [Installation](getting-started.md) after access is granted.

## A command says access is denied

Confirm that `ODDISH_API_KEY` is set to the key supplied for your workspace. Do not paste its value into a support message. If the key is present, ask your workspace administrator to check its permissions and workspace approval.

## My task stays queued

Run `oddish status TASK_ID`, replacing `TASK_ID` with your task's ID. A baseline check may still be running, or execution capacity may be unavailable. Send your Oddish contact the task ID, timestamp, and displayed status. Do not submit repeated copies to diagnose the queue.

## Live logs are empty

Check whether the trial has finished or uses an agent without live-log support. Use `oddish pull TRIAL_ID` to fetch permanent logs for finished trials.

## I need help with a failed run

Record the task/trial ID, installed version from `oddish version`, command with credentials removed, and exact error. Inspect [Download results](guides/results.md) before deciding to retry. Share only files approved for your support contact.
