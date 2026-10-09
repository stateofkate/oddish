# Getting started

Oddish runs evaluation tasks: instructions and tests used to measure what an AI agent can do. It stores each run's progress, logs, and scores.

## Get access

Ask your Oddish contact for an Oddish workspace. Once you can sign in at oddish.app, create an API key (a credential that lets commands act for your workspace) under **Settings → API keys**. Keep its value out of screenshots, shared commands, and source files.

## Install the command-line tool

Your API key is all you need. Apple Silicon macOS and Linux x86_64 are supported.

```bash
export ODDISH_API_KEY=ok_...
curl -fsSL https://abundant-ai--api.modal.run/cli/install.sh | sh
oddish version
```

The script installs [uv](https://docs.astral.sh/uv/) if it is missing, downloads the newest release with your key, checks its SHA-256 checksum, installs it under `~/.oddish/cli`, and links `~/.local/bin/oddish`. Add `~/.local/bin` to your `PATH` if the script asks you to.

```bash
oddish ls
```

Success is a task list, which can be empty. What a key can do depends on your workspace: a workspace that runs jobs can submit runs and pull its results; a view-only workspace gets read keys and reads what Abundant shared with it. If access is denied, see [Troubleshooting](troubleshooting.md).

People with GitHub read access to `abundant-ai/homebrew-tap` can instead run `brew install abundant-ai/tap/oddish`.

## Update

Re-run the install command. `oddish update` prints the same instruction. Homebrew installs update with `brew upgrade abundant-ai/tap/oddish`.

Next: [Learn the terms](concepts.md) or [Run a task](guides/first-run.md).
