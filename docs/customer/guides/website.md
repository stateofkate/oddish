# Use the website

Sign in at [oddish.app](https://oddish.app). The navigation shows **Docs**, **Dashboard** and **Tasks**, plus **Shared** when another organization has shared work with yours. Everything here reads the same workspace as the command-line tool.

## Dashboard

**Recent Experiments** lists your workspace's experiments, newest first, with **Tasks**, **Trials**, **Avg score**, **Avg steps** and **Avg trial cost** for each.

- **Members** narrows the list to one person; **Mine** is your own.
- The search box accepts words (all must match), `OR`, `"exact name"`, `-word` to exclude, `author:` and `tag:`. **Search syntax** shows the full list.
- The status menu filters by **Active trials**, **Retrying trials**, **Completed**, **Rejected tasks**, **Tasks without a verdict** or **Failures**.

A workspace with no experiments shows **Get set up**: four steps that create your first API key, install the CLI, copy instructions for a coding agent, and submit a first job. Each step shows when it is done.

## An experiment

Open an experiment from the dashboard.

- The tiles at the top show **Avg score**, **Trials finished**, **Tasks**, **QA verdicts** (click a count to list those tasks) and run cost.
- The table has one row per task and one column per agent and model. Each cell holds that agent's trials, colored by status: **PASS**, **FAIL**, **ERROR**, **SCORELESS** (finished without a score), **QUEUED**, **RUNNING**.
- **Search tasks (comma-separated)** filters rows. **View** shows **All**, **Any error**, **Any failed** or **All failed** rows.
- Select rows to **Rerun trials**, **Cancel**, **Tag** or **Generate QA verdicts**. **Copy TSV** copies the table for a spreadsheet.
- **Run trials** starts more trials on the experiment's tasks with a model you choose.
- **Publish** (organization admins only) creates a read-only link; see [Share results](sharing.md).

Click a trial to open it. Its tabs are **Summary** (timeline and QA analysis), **Live** (while it runs), **Files** (logs and outputs), **Trajectory** (the agent's messages and tool calls) and **Artifacts**. **Retry Trial** runs it again; **Next trial** moves down the table.

## QA verdicts

QA reviews each task after its trials finish. A task's verdict is one of **Verdict accepted**, **Verdict rejected**, **Verdict queued**, **Verdict failed**, **No verdict yet** or **No verdict: insufficient trials**. A rejected task lists its findings; **Must fix** marks findings that block acceptance. A verdict judges the task, not a trial's score.

## Tasks

**Tasks** lists every task in your workspace. The sidebar searches and filters by model, agent, run state, review state, experiment and author; **Sort** orders by last run, name, failures, cost and more. **Copy view link** saves the current filters as a link. **Table** and **Cards** switch the layout.

Open a task to see its cost, trials per agent, linked experiments and versions. **View task files** opens its files and its QA overview. **Make default** chooses which version new runs use.

Next: [Settings and API keys](settings.md).
