# Terms used in these guides

| Term         | Meaning                                                                                                       |
| ------------ | ------------------------------------------------------------------------------------------------------------- |
| Task         | A folder containing instructions, an execution environment, and tests.                                        |
| Harbor       | The tool that executes Oddish's task format.                                                                  |
| Agent        | A program that uses an AI model and tools to attempt a task.                                                  |
| Trial        | One agent's attempt at a task.                                                                                |
| Task version | A saved revision of the task's files. The selected default can be an older revision.                          |
| Experiment   | A named group of task trials, used to compare results.                                                        |
| Sweep        | A submission that runs several agent/model combinations or repeats them.                                      |
| Verifier     | The task's tests or scoring program.                                                                          |
| Reward       | The verifier's score for a trial. A missing score is not a zero score.                                        |
| Baselines    | Checks using `nop` (no solution) and `oracle` (the supplied solution) to test whether the task can be graded. |
| QA           | Quality review: checks of task files and completed attempts. A task verdict is separate from a trial's score. |
| Trajectory   | The recorded sequence of an agent's messages and tool calls.                                                  |
| Artifact     | A saved file from a trial, such as a log or test report.                                                      |
| Delivery     | A set of tasks with recorded checks and human approval before shipment.                                       |

A docs account and an Oddish workspace account grant different access. Reading these definitions does not create workspace membership.

Next: [Run a task](guides/first-run.md).
