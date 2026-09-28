# Getting started

Oddish runs evaluation tasks: instructions and tests used to measure what an AI agent can do. It stores each run's progress, logs, and scores.

## Get access

A docs invitation lets you read these guides. It does not give you an Oddish workspace, tasks, or an API key (a credential that lets commands access your workspace).

Ask your Oddish contact for access to your own workspace and a key for it. You do not need access to Abundant's internal workspace.

## Install the command-line tool

You need Homebrew (a package installer) and GitHub read access to `abundant-ai/homebrew-tap`, the private repository that distributes the installer. Docs access does not include that GitHub permission. Ask your Oddish contact to arrange it if installation is denied.

```bash
brew install gh
gh auth login
gh auth setup-git
brew install abundant-ai/tap/oddish
oddish version
```

The last command prints the installed version. Set `ODDISH_API_KEY` in your terminal to the key supplied for your workspace. Keep its value out of screenshots, shared commands, and source files.

```bash
oddish ls
```

Success is a task list, which can be empty. If access is denied, see [Troubleshooting](troubleshooting.md).

## Update

```bash
brew update
brew upgrade abundant-ai/tap/oddish
```

New releases are no longer published to PyPI, the Python package registry. For an old Python installation, install through Homebrew, then remove the old package with the Python environment and installer that installed it. Use `command -v oddish` and `oddish version` to confirm which installation runs.

Next: [Learn the terms](concepts.md) or [Run a task](guides/first-run.md).
