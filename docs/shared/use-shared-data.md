# Use data Abundant shares with you

Abundant shares tasks with your organization in Oddish. A task is one folder: an instruction for an AI agent, the environment it works in, and the tests that score its work. You browse tasks on the website, download them, and run them on your own machines. Your view is read-only: you cannot start runs on Oddish or change anything there.

## Sign in

Sign in at [oddish.app](https://oddish.app) with your work email. People at your company's email domain join your organization on first sign-in. Ask your Abundant contact to add people with other email addresses.

The navigation has two pages:

- **Deliveries** lists groups of tasks shared with you, newest first, with how many tasks and runs each holds. Open one to see its tasks and the runs Abundant made on them, with logs and files.
- **Tasks** lists every task you can open, whether it was shared on its own or as part of a delivery. A delivered task shows its delivery name and the version that shipped.

## Download a task

Open a task from **Tasks**. It opens on `instruction.md`, the prompt the agent receives; the file tree holds the rest. **Download task** at the top right saves the whole task as one archive named after the task, for example `task-7f3a.tar.gz`. The link expires after a short time, so reopen the page for a fresh one.

Unpack it into its own folder:

```bash
mkdir task-7f3a
tar -xzf task-7f3a.tar.gz -C task-7f3a
```

The folder holds `instruction.md`, `task.toml` (settings such as time limits), `environment/` (the container the agent works in), `tests/` (the scoring script) and usually `solution/` (a reference solution).

## Run a task yourself

Tasks use the [Harbor](https://github.com/laude-institute/harbor) format. Install Harbor and Docker, then point `harbor run` at the folder that holds `task.toml`:

```bash
uv tool install harbor
harbor run -p ./task-7f3a -a oracle
harbor run -p ./task-7f3a -a claude-code -m anthropic/claude-opus-5-5
```

`-a oracle` runs the reference solution and should score 1; use it to check the task works on your machines. The second command runs an agent and model of your choice; `harbor run --help` lists the agents.

## What you do not see

Abundant chooses for each thing it shares whether you see its QA results (shown unless Abundant turned them off) and its costs (hidden unless Abundant turned them on), so one delivery can show QA while another does not. Other customers' work and Abundant's internal notes never appear.

## Access

Ask your Abundant contact to stop sharing something. A share Abundant removes disappears from your pages immediately; archives you already downloaded stay with you.
