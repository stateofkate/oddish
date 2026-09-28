# Command reference

A command-line tool runs typed commands in a terminal. Run `oddish --help` to list commands supported by your installed version, or `oddish run --help` to see a command's options.

| Command                                      | What it does                                 | Changes stored work?   |
| -------------------------------------------- | -------------------------------------------- | ---------------------- |
| `oddish version`                             | Prints the installed version.                | No                     |
| `oddish preflight PATH`                      | Checks local task files.                     | No                     |
| `oddish upload PATH`                         | Registers files or imports existing results. | Yes                    |
| `oddish run PATH`                            | Submits new trials.                          | Yes; can incur charges |
| `oddish ls`                                  | Lists tasks in your workspace.               | No                     |
| `oddish status ID`                           | Shows task, trial, or experiment progress.   | No                     |
| `oddish logs TRIAL_ID`                       | Prints live agent output.                    | No                     |
| `oddish pull ID`                             | Downloads available output files.            | Writes local files     |
| `oddish run ID --retry`                      | Retries failed work.                         | Yes; can incur charges |
| `oddish cancel TASK_ID`                      | Stops active work for a task.                | Yes                    |
| `oddish costs`                               | Shows recorded spending.                     | No                     |
| `oddish collect --task TASK_ID --no-publish` | Creates a private trial collection.          | Yes                    |
| `oddish publish EXPERIMENT_ID`               | Creates a public share link.                 | Yes; exposes results   |
| `oddish unpublish EXPERIMENT_ID`             | Disables a public share link.                | Yes                    |
| `oddish delivery show ID`                    | Lists delivery checks and blockers.          | No                     |
| `oddish skill`                               | Prints the packaged agent guide.             | No                     |

`PATH` means a local folder. `ID` means an identifier returned by Oddish; replace these placeholders before running a command.

Most commands accept `--json` for structured output. `logs`, `link`, and `skill` do not. `run --json` returns after submission rather than watching completion.

`oddish skill --install` installs the packaged agent guide locally. Its installed instructions remain available even when an agent cannot sign into this docs site.

For tasks and examples, start with [Run a task](../guides/first-run.md).
