# Oddish CLI

Daytona execution is disabled. Use `oddish run <task> --env modal` for CPU tasks,
`--env archil` for another CPU provider, or `--env thunder` / `--env modal` for
GPU tasks. The task must still declare its GPU count and type. Requests and
retries selecting Daytona fail without switching providers; resubmit with an
explicit supported provider. Existing Daytona sandboxes remain available for
status checks and cleanup. The default `ODDISH_HARBOR_ENVIRONMENT` is `modal`.

Oddish runs Harbor tasks, records trial progress and scores, and downloads their logs and artifacts. Harbor is the tool that executes the task format.

## Install

You need Homebrew and GitHub read access to `abundant-ai/homebrew-tap`, the private repository distributing the installer. Homebrew supplies Python 3.13.

```bash
brew install gh
gh auth login
gh auth setup-git
brew install abundant-ai/tap/oddish
oddish version
```

Set `ODDISH_API_KEY` to the key supplied for your own workspace before using API-backed commands. A docs invitation does not grant workspace access or an API key. Never paste the key into shared logs or source files.

Update with `brew update` followed by `brew upgrade abundant-ai/tap/oddish`. New versions are no longer published to PyPI. See the [installation and migration guide](../docs/customer/getting-started.md).

## Commands

| Task | Command |
| --- | --- |
| Check local task files | `oddish preflight PATH` |
| Register files / import existing trials | `oddish upload PATH` |
| Submit trials / retry work | `oddish run` |
| Browse / inspect / stream live output | `oddish ls`, `oddish status`, `oddish logs` |
| Download outputs | `oddish pull ID` |
| Prepare playback from saved trial files | `oddish playback rebuild TRIAL_ID` |
| Inspect recorded spending | `oddish costs` |
| Stop work / remove data | `oddish cancel`, `oddish delete` |
| Combine / collect / edit results | `oddish combine`, `oddish collect`, `oddish experiment` |
| Publish / revoke a public share | `oddish publish`, `oddish unpublish` |
| Review task quality | `oddish qa export`, `oddish qa evals`, `oddish qa-issues`, `oddish backfill-analysis`, `oddish assign` |
| Review and manage shipments | `oddish delivery` |
| Print a dashboard link | `oddish link` |
| Read / install the packaged agent guide | `oddish skill` |
| Inspect / update the local installation | `oddish version`, `oddish update` |

Replace `PATH` with a local folder and `ID` with an identifier returned by Oddish. Run `oddish COMMAND --help` for installed options. Submitting or retrying trials can incur charges. `collect` publishes by default; pass `--no-publish` for a private collection.

Customer guides have one maintained source in [docs/customer](../docs/customer/getting-started.md). [DOCS.md](../DOCS.md) indexes them and retained internal references. The website rollout and invitation requirements are recorded there. Operator commands such as `admin concurrency`, `cost-exclusions`, and `models` belong in the [internal CLI reference](../docs/cli-advanced-internal.md).

Delivery QA: `oddish delivery qa rerun NAME --dry-run` previews the tasks; `oddish delivery qa rerun NAME` requests both review stages; `oddish delivery qa status NAME` reads their progress. Use `--limit 100 --offset 0` to select a batch in task-ID order; keep delivery membership unchanged between batches. See the [internal delivery QA reference](../docs/cli-advanced-internal.md#rerun-qa-for-a-delivery).

Self-hosting: [SELF_HOSTING.md](../SELF_HOSTING.md). Package internals: [AGENTS.md](../AGENTS.md). Installed agents can still run `oddish skill` without a browser or docs account.

## License

[PolyForm Noncommercial 1.0.0](LICENSE) allows personal and other noncommercial
use, plus use by the organizations listed in the license. Commercial use outside
those terms requires a separate license from the rights holders. Contact
[the maintainer](https://github.com/RishiDesai).

Rights already granted for code released under Apache 2.0 remain in place.
See [LICENSE-APACHE-2.0](LICENSE-APACHE-2.0). Third-party code keeps its own license.
