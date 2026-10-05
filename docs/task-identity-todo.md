# Task identity

Started October 2, 2026. Linear: ABT-1429 (delivery selection unusable because
of duplicated tasks), ABT-1513 ("Deduplicate ID's").

Goal: one real task has one identity in Oddish, every task version has a content
fingerprint, and delivery selection reads history across every copy of a task.
Oddish becomes the place that answers "what is this task, and who has it".

Progress (2026-10-05): 4a is in production (#2054, promoted in #2055).
Section 1's server-side hashing and fingerprint table are in review (#2063).
Next: 4b, the cross-task hash check.

## Constraints from the Forge incident (Sept 21–25)

An AI agent in Neel's Forge pipeline ran on a Modal app holding the production
database password and ran SQL on production outside GitHub and staging. It left
28 database objects that no migration created; one failed a production
migration and another caused 218 `POST /tasks/sweep` 503s. They were removed
2026-09-25 00:27–00:31 UTC. Rules from #team-code-infra (Meji and Kyle,
2026-09-24): database changes only through migrations; no code reaches prod
except from a GitHub PR; only Oddish's own apps get the `prod` secret; no agent
gets shell access to prod; pipelines integrate through the Oddish API.

For this plan:

- Every production write (migrations, backfills, link detection, the delivery
  history re-import) is repo code merged by PR and deployed by promotion.
  Backfills run as admin-only API endpoints plus CLI commands, dry run first,
  rehearsed on staging, started by a person, like
  `oddish delivery import-history`. No SQL or `modal container exec` writes.
  Agents (Claude included) build and test the code and hold no production
  credentials.
- Never UPDATE `task_versions`. Production has `task_versions_immutable_guard`,
  a BEFORE UPDATE trigger no migration created, blocking changes to
  `content_hash` among other columns. Backfilled hashes, fingerprints and git
  provenance go into new tables created by migrations. New uploads are
  unaffected: the server writes their hash on INSERT.
- Each migration is rehearsed by the staging deploy (every merge to
  `staging` runs it) before promotion to production. Decided 2026-10-05:
  migrations here only add tables, so no separate production-schema copy.
- Migrations add tables only. The `content_hash` index is built with
  `CREATE INDEX CONCURRENTLY`; any unavoidable `ALTER TABLE` uses
  `oddish/src/oddish/db/migration_locks.py` (5 s lock window, 60 retries). No
  trigger changes: the 2026-10-01 deploy deadlocked on `DROP TRIGGER ... ON
  tasks`.
- The repo index reads GitHub through a GitHub App installed on abundant-ai
  with read-only repository contents permission, its key held only by the
  Oddish app. Not a personal token.
- Factories (Forge, smithy, sherpa) reach Oddish only through the API, so the
  upload resolver (section 4) is the one place that can deduplicate their
  uploads without them touching the database.

## Findings

All numbers come from the production inventory of the Abundant organization
(`8ebde5d0`) captured 2026-09-18 and the import plan built from it, both in
`/Users/kyle/Desktop/oddish/metadata-backfill-results/20260918-production/`
(`inventory.json`, `preview/plan.json`). They are two weeks old; a fresh
`oddish delivery inventory` is a read-only call.

### Oddish identifies a task by name only

`initialize_task_upload` (`oddish/src/oddish/core/tasks.py`) looks up the
uploaded name in the organization. A known name gets a new version of that
task. An unknown name gets a new task with ID `<name>-<8 random hex>`, without
checking whether the same files already exist under another name. The only
database rule is that live names are unique (`idx_tasks_unique_org_name` in
`oddish/src/oddish/db/models.py`). Tasks cannot be renamed; `task_aliases` is
written only by the delivery backfill import.

So every changed folder name creates a second task:

- Renames. Delivery records say `implement-wc` became `word-counter`; both are
  live Oddish tasks. 114 such pairs, all diverged in content since.
- Re-uploading a downloaded folder named after a task ID, e.g.
  `aarch64-task-56bb4ae5-8e96e7e6` copies `aarch64-task-56bb4ae5`. 86 live tasks
  are named exactly after another task's ID; 355 are another live name plus
  `-<8 hex>`.
- Suffixed copies: `-tmldbg`, `-fixed`, `-ctl`, `-tr1`, `-base1`.

### Exact copies

The content hash (`compute_task_content_hash`, `oddish/src/oddish/cli/api.py`)
covers every file Harbor runs (instruction, tests, environment, solution) and
skips `README.md` and descriptive `task.toml` fields. Equal hashes mean the
tasks run identically.

663 groups of live tasks share a current hash (1,496 tasks, 833 extra copies;
540 pairs, the largest group has 6). Every group has different names.

### Effect on delivery

The task browser filters `delivered_to`, `not_delivered_to` and
`never_delivered` (`_delivery_record_exists`, `oddish/src/oddish/core/endpoints/tasks_query.py:931`) match
history by exact task ID, so a copy looks undelivered.

- 8 identical-content pairs have one side delivered and the other not. Example:
  `custom-optimizer-821f1177` went to gdm, meta, tml and xai;
  `custom-optimizer-tmldbg-c1c7ad11` is identical and shows no history.
- The 2026-09-18 import (receipt `fe63b3d3`) refused history it could not pin
  to one task:

  | Refusal | Groups | Unattached history entries |
  | --- | ---: | ---: |
  | Rename pair, both names live | 114 | 418 |
  | Record names an ID whose name another live task now holds | 118 | 446 |
  | Name-only match, one candidate | 1,870 | ~2,700 (with next row) |
  | Name-only match, two candidates | 34 | |
  | No candidate | 120 | 293 |

  Tasks in these groups show as never delivered although the lab has them.

### Missing content hashes

12,262 of 63,563 live tasks have no hash on their current version:

- 8,085 registered from GitHub CI runs. Files live in
  `s3://abundant-github-workflows-bucket/abundant-ai/<repo>/pr-N/run-N/agent-…/<task>/…/task`.
  Created through the sweep path (`oddish/src/oddish/queue.py` ~line 885), which
  stores the request's `content_hash` and these requests sent none. 0 of 8,085
  have a hash.
- 4,176 older uploads at `s3://tasks/<task_id>/` (no `vN/` folder), from before
  hashing; 2 have a hash.

The hash is computed only by the CLI during `oddish upload`. The server has no
code that computes one.

### Tasks in GitHub repos

Measured 2026-10-02 against the default branch of all 617 abundant-ai repos
(GitHub tree API; the five repos too large for one call, harbor-lh,
harbor-forge-v2, harbor-forge-archive, ProgramSmith-100 and
multiverse-tasks-owen, through `git clone --filter=blob:none --depth 1` plus
`git ls-tree -r -t HEAD`). A task folder is a folder holding `task.toml`.

- 77 repos hold 31,580 task folders. Largest: harbor-lh 5,415,
  harbor-forge-archive 4,773, harbor-forge-v2 4,384, gemini-code-rl-export-0
  2,771, experiments 2,558.
- 14,589 of 55,768 uploaded Oddish tasks (26%) have a name, after stripping
  `-<8 hex>` suffixes, equal to a repo task folder name. The other 41,179 are
  mostly generated or variant names (`qa-post-trial-…` 1,417, `post-train-…`,
  `harv…`, `refmodel…`, `…-no-skill`, `…-colab-v10`); factories appear to upload
  to Oddish without committing.
- 1,208 of the 8,085 CI-imported tasks match by name. harbor-forge alone
  produced 4,200 of them across 3,959 PRs while its main branch holds 131
  tasks, so most live only on PR branches.
- The repos have their own copies: 4,409 groups of byte-identical task folders
  (same git tree SHA), 3,919 of them spanning more than one repo. Export
  workflows copy tasks between repos (`hammer-to-export-on-merge.yaml` in
  `task-workflows` copies tbench-hammer tasks into nov-5-export). The 31,580
  folders have only 17,444 distinct names, so repo plus folder name is not a
  unique identity either.
- Oddish records no git information on uploads. The experiments repo's
  `oddish-experiment.yml` clones the task repo to `/tmp/task-repo` and runs
  `oddish run` there, but passes only `--github-meta` for the experiments PR,
  so which task repo and commit ran is lost.

Name matching on default branches undercounts: it misses PR branches and
anything renamed between repo and Oddish. Content matching (section 5) is the
real measure.

## 1. A content hash for every task version

- [x] Move `compute_task_content_hash` (and `_canonical_task_config_bytes`) into
  shared code so CLI and server produce byte-identical hashes. Test: the same
  folder hashes the same through both. Done in `oddish/src/oddish/task_content_hash.py`;
  `compute_task_archive_content_hash` unpacks an uploaded archive and applies
  the same function.
- [x] Fingerprint table, new and migration-created, e.g.
  `task_version_fingerprints`: version ID, content hash, and a fixed set of
  git-format hashes (folder tree SHA; `instruction.md` and `task.toml` blob
  SHAs; `tests/`, `environment/`, `solution/` tree SHAs), plus where each value
  came from (client, server, backfill). Git format (blob = SHA-1 of
  `"blob <size>\0" + bytes`) is what GitHub returns, so Oddish versions match
  repo folders by lookup (section 5). Not one row per file: the reverted file
  index (#1662, reverted in #2010) reached 2.26M rows and 3.3 GB for 6,423
  versions. Readers use `task_versions.content_hash` when set, else this table.
  Built in PR #2063 (open, 2026-10-05; migration `task_fingerprints_001`)
  with the server hash, the algorithm label, and the client's hash at the
  time; the git-format hashes are added with 4c and 5a, their first readers.
- [ ] Dry run (admin endpoint + CLI, executed by the deployed Oddish app on
  Modal, which holds the storage credentials): count hashless versions,
  current and older, by storage location, and how many still have readable
  files. Writes nothing.
- [ ] Backfill, same route: download each hashless version's files, compute,
  insert into the fingerprint table (never UPDATE `task_versions`). Record
  versions whose files are gone as unrecoverable rather than leaving them
  blank. Batches of a few hundred rows per transaction, resumable. Rehearse on
  staging, then production. Matching to repos needs only each
  version's `instruction.md` blob hash, one small file, so that pass can run
  first and cheaply. The 8,085 CI tasks may need no S3 read: their path names
  repo, PR and Actions run (`…/harbor-forge/pr-935/run-20624475126/…/workflow-orchestrator/task`),
  the run ID gives the commit through the GitHub API, and the folder at that
  commit gives the files.
- [x] Compute the hash on the server for uploads (upload complete and the
  `oddish run` sweep). The `TASK_EXPAND` worker job, queued after each
  upload, hashes the archive and writes the row in the transaction that
  publishes the expansion. A failed hash logs and leaves the expansion
  untouched; a client/server mismatch logs a warning. Not covered: archives
  over `tasks_expand_max_bytes` (1 GiB), pre-archive loose-file tasks, and
  versions expanded before this change; the backfill handles those.
- [ ] CI imports. The sweep registers them with a `task_path` in
  `abundant-github-workflows-bucket`, which the expand job does not read (it
  resolves keys in Oddish's own bucket and finds nothing), so they still get
  no hash. Either the CI workflow sends `content_hash` with the sweep request,
  or the worker reads that bucket. Decide with whoever owns the harbor-forge
  workflow.
- [ ] Note the behavior change: after backfill, uploading identical content to
  an existing name returns "content unchanged" instead of a new version.

## 2. Duplicate families

Link copies rather than merging them. Version IDs (`<task_id>-vN`), storage
paths (`s3://tasks/<task_id>/vN/`), trials, experiments and delivery memberships
all key on task ID; moving them is a large migration with no extra benefit for
delivery.

- [ ] Detection report (admin CLI): candidate families with evidence.
  High confidence: equal hash on any version, rename records from the delivery
  sources, git renames and repo lineage (section 5), name equal to another
  task's ID. Review needed: `-<8 hex>` names and suffix variants.
- [ ] Link table: org, task, canonical task, evidence, decided by, decided at.
  The canonical member is chosen per family; for diverged pairs it is often the
  newer copy (`-fixed`). Retiring the other copy is optional. Confirmed old
  names become `task_aliases` rows.
- [ ] Review tool for links and for the 1,870 one-candidate name matches
  (moves here from `docs/delivery-metadata-todo.md` line 99).
- [ ] Re-run the delivery history import with families so the 114 rename and
  118 collision groups attach their 864 entries.

## 3. Delivery reads through families

- [ ] `delivered_to` / `not_delivered_to` / `never_delivered` count history on
  any family member.
- [ ] The delivery board flags a task whose family member was already delivered
  to the same lab (covers `docs/delivery-metadata-todo.md` line 116).
- [ ] The picker shows one row per family.
- [ ] `get_task_qa_history_core` (`oddish/src/oddish/core/deliveries.py`)
  resolves a name through the family instead of taking whichever row comes
  back first.

## 4. Stop new copies

Ship this before sections 2–3: steps 4a, 4c and 4d need no backfill, and every
day without them adds copies the later sections must clean up.

### Where copies start

Copies are made by `oddish run ./folder` (`oddish/src/oddish/cli/run.py:273`),
the command people launch experiments with. For each folder it calls
`upload_task` (`oddish/src/oddish/cli/api.py:804`), which sends
`"name": task_path.name`. `initialize_task_upload` versions a task with that
name or creates `<name>-<8 hex>`, and `/tasks/sweep`
(`oddish/src/oddish/core/endpoints/sweep.py:746`) runs trials on whichever ID
came back. Task creation is a side effect of launching an experiment, decided
by folder name, and never shown to the user. The habits that make copies:

- `cp -r task task-tmldbg` to debug, then `oddish run ./task-tmldbg`.
- Re-running a pulled task: `oddish pull` writes `tasks/<task_id>/files/`
  (`oddish/src/oddish/cli/pull.py:440`), so the name is an ID.
- Renaming or regenerating a folder in a task repo.

The cost lands weeks later on whoever builds a delivery.

### Design rule

No prompts, flags, or errors in `oddish run`; the experiment launches as it does
today. The server decides alone and records why. Attaching an upload to the
wrong task mixes histories and is hard to undo, so attach only on proof.
Linking two tasks is a reviewable, deletable row, so link on suggestion.
Never merge task IDs.

| Upload | Decision | User sees |
| --- | --- | --- |
Evidence order, strongest first: same repo lineage (same repo and path, or a
path git records as renamed from it), then name, alias or task ID, then marker,
then content hash, then `instruction.md` blob.

| Upload | Decision | User sees |
| --- | --- | --- |
| Committed repo folder whose repo + path (or git-renamed path) maps to task X | New version or unchanged of X | Nothing new |
| Folder name is a live task name | New version or unchanged (today) | Nothing new |
| Folder name is a task ID or live `task_aliases` name | New version of that task | One dim line |
| Content hash equals a version of another task | Run on that task; folder name becomes an alias | One dim line: `x: same files as y, running there` |
| Untracked folder in a checkout whose files match a committed task folder (`git ls-files -s` blobs) | Identical: that folder's task. Edited: new task linked `fork_of` it | Nothing |
| Non-git folder whose marker names task X, name and content differ | New task, linked `fork_of` X at version N | Nothing |
| Nothing matches | New task (today) | Nothing |
| Nothing matches, `instruction.md` blob equals another task's | New task, linked `similar` by a background job | Nothing |

### Steps

- [x] 4a. Resolver on the server (PR #2054, merged; promoted to production in
  #2055). `_resolve_upload_task` in
  `oddish/src/oddish/core/tasks.py`, called by `initialize_task_upload`: live
  name, then live alias, then name-as-task-ID. The sweep create path needs no
  call: it only creates rows for task IDs that init minted. Old CLIs and CI get
  it without upgrading; `oddish run` prints a dim line when the response's
  `matched_by` is set. Not done: restoring a soft-deleted task with that name.
  `idx_tasks_unique_org_name` excludes deleted rows on purpose so a deleted
  name can be reused, and restoring would bring back history the user deleted.
- [ ] 4b. Cross-task hash check. Needs server-side hashing (section 1, new
  uploads only, not the backfill). Index `task_versions.content_hash` and the
  fingerprint table's hash (`CREATE INDEX CONCURRENTLY`); take
  `pg_advisory_xact_lock` on org + hash before checking, because `oddish run`
  uploads a directory in parallel (`upload_tasks_with_progress`, `api.py:945`)
  and two identical folders in one batch would both miss. Not a unique
  constraint: one task's versions can share a hash after a revert.
- [ ] 4c. Git provenance on every upload. When the task folder is inside a git
  checkout, `oddish run` / `oddish upload` send `git remote get-url origin`,
  the path relative to the repo root, `git rev-parse HEAD`, and
  `git rev-parse HEAD:<path>` (tree SHA), or a dirty flag when the folder has
  uncommitted edits; the server stores them on the version. Resolve symlinks
  first: `oddish-experiment.yml`'s glob mode runs on symlinks in
  `/tmp/tasks-merged`. For an untracked folder (a `cp -r` debug copy), the CLI
  runs `git hash-object` on its files and looks the blobs up in
  `git ls-files -s` to find the committed folder it came from. Nothing is
  written into the repo. CI gets this with no workflow change because it
  already runs `oddish run` inside the clone.
- [ ] 4d. Marker file, for folders outside git only (pulled tasks, factory
  output). `oddish run`, `oddish upload` and `oddish pull` write
  `.oddish/task.json` (task ID, name, version, content hash); skip read-only
  folders such as `--dataset` caches and folders inside a git checkout. `cp -r`
  copies it, so copies become versions (identical) or forks (edited)
  automatically. Excluded from the content hash and the archive, like
  `README.md` (`api.py:481`). Pulled folders named after the task name, not
  the ID.
- [ ] 4e. Background `similar` linker: tasks with equal `instruction.md` blob
  hashes (section 1 fingerprint table). Link only when the blob belongs to one
  lineage; shared templates exist (the largest identical repo group is
  `adapters/humanevalfix/template` in four repos). Not keyed on `tests/`: test
  boilerplate is likely shared across unrelated generated tasks (unmeasured).
- [ ] 4f. Family links written by the resolver with evidence (`same_content`,
  `fork_of`, `rename`, `exported_to`, `similar`) into the section 2 link
  table. Section 2's detection then only cleans up the existing backlog.
- [ ] 4g. Rename command (the canceled ABI-139 request): changes `tasks.name`,
  writes the old name to `task_aliases` with `source = 'rename'`.

### Section 4 open questions

- Trials from a debug run on identical content land on the canonical task.
  Fine if pass rates and QA read per experiment; check whether QA history or
  delivery pass rates aggregate all trials per task.
- The experiment page will show the canonical name for a run launched as
  `task-tmldbg`. Store the submitted folder name on the experiment–task link
  and show "ran as".
- Confirm Harbor's `Packager.collect_files` would not ship `.oddish/`; exclude
  it explicitly either way.
- How the 8,085 CI tasks entered Oddish is untraced. Their S3 paths match the
  output layout of `task-workflows`' `harbor-validate-tasks.yaml` (Harbor run
  in GitHub Actions, results in `abundant-github-workflows-bucket`), not
  `oddish run`, which today sends the hash (`run.py:1113–1115`).

## 5. GitHub repos as an identity source

Git already records what Oddish lacks: a stable location (repo + path), content
fingerprints for every file and folder (blob and tree SHAs, returned by the
GitHub API without downloading contents), renames as facts in commit diffs, and
history including PR branches (`refs/pull/N/head`). It covers only committed
tasks (26% of uploaded tasks by name on default branches); the rest rely on
sections 1 and 4.

- [ ] 5a. Repo index on the server. A sync job (push webhooks or a schedule,
  authenticated as a read-only GitHub App, not the existing `GITHUB_TOKEN` of
  `GitHubClient` in `oddish/src/oddish/integrations/github/client.py`) records
  every folder holding `task.toml` in the org's repos: repo, path, commit, and
  the same fixed hash set as the fingerprint table (section 1), plus renames
  from `git log -M --diff-filter=R`. Trees-only clones
  (`--filter=blob:none`) are enough; harbor-lh, about 5 GB in full, took
  about a minute.
- [ ] 5b. Include PR branches. Fetch `refs/pull/*/head` trees-only for the task
  repos; this is where harbor-forge's 4,200 CI tasks live.
- [ ] 5c. Match Oddish versions to repo folders by `instruction.md` blob (and
  the other fingerprint hashes where available), plus 4c provenance for new
  uploads.
  Accept a link only when the blob belongs to one lineage.
- [ ] 5d. Family links from git: `rename` from git renames; `exported_to` for
  byte-identical folders in another repo, canonical member = the oldest
  commit; `same_content` otherwise.
- [ ] 5e. Show the repo, path and commit on the task drawer and make them
  searchable in the task browser.
- [ ] First check, before building: fetch harbor-forge's `refs/pull/*/head`
  trees and count how many of its 4,200 CI tasks match by `instruction.md`
  blob. Shows whether PR branches close most of the CI gap.

## Open questions

- Does "source of truth" include recording submissions, receipts and customer
  outcomes in Oddish first (section 4 of `docs/delivery-metadata-todo.md`),
  replacing the `harbor-lh` ledger, pass-rate CSV, `deliveries/` catalogs and
  Nexus Dashboard as the place people record deliveries?
- Do the 8,085 CI-imported tasks belong in delivery selection at all, or should
  the browser hide them by default?
- `task_versions_immutable_guard`: adopt it in a migration or drop it in one
  (decide with Meji). Not a blocker: this plan never updates `task_versions`.
  `--overwrite-current-version` does update `content_hash`, so either that
  feature fails on production or the guard has an exception not yet seen.
- Production numbers still unread: the guard's definition and how many
  versions lack a hash today. Read-only queries, run by a person (Claude's
  production reads are blocked by its permission check):

  ```sql
  select pg_get_triggerdef(t.oid) from pg_trigger t
    join pg_class c on c.oid = t.tgrelid
    where c.relname = 'task_versions' and not t.tgisinternal;
  select pg_get_functiondef(p.oid) from pg_proc p
    where p.proname ilike '%immutable%';
  select count(*) filter (where content_hash is null), count(*) from task_versions;
  ```

## Limits of the findings

- Hash matching cannot see the 12,262 hashless tasks until section 1 runs.
- The inventory has only each task's current hash; copies edited after copying
  are found only through names or rename records. Comparing every version's
  hash needs a new read-only query.
- File existence in both buckets is unchecked (local AWS login had expired on
  2026-10-02).
- The repo counts (2026-10-02) match the 2026-09-18 inventory by name only and
  read default branches only.

## Order

4a (resolver: name, alias, task ID) → section 1 server-side hashing and the
fingerprint table → 4b (cross-task hash check) → 4c (git provenance) and
4d (marker outside git) → 5a–5b (repo index) → 4e, 4f and 5c–5d (links) →
section 1 backfill and section 2 cleanup of the backlog → section 3 →
4g (rename command).
