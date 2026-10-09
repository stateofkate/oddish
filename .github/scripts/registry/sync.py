"""Upload task folders merged to a registry repo's main and tag them registry-main.

Run by .github/workflows/registry-sync.yml. For every repo in ``REPOS``, the
script diffs main from the last commit it synced (state file, else the commit
the aliases were loaded from), uploads each changed task folder (a folder
holding task.toml) that still exists, and tags its task ``registry-main`` so
the delivery picker's registry filter includes it.

Each folder's Oddish task is stored as a task alias named after its path, e.g.
``harbor-lh~tasks~sysio-rust-impl``. A folder with an alias uploads into that
task by ID, so a task named differently from its folder, or a folder name used
twice, lands on the right task. Other folders upload by folder name, as
``oddish upload`` does, and get an alias for next time. Unchanged files add no
version.

A repo's state only advances when all its folders succeed, so the next run
retries a failure.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import httpx

TAG = "registry-main"
# Folders checked out at once. 532 harbor-lh folders with Git LFS files filled a
# runner's ~14 GB disk; LFS keeps every file twice (cache and checkout).
BATCH = 50
# Registry repos and the main commit their aliases were loaded from.
REPOS = {
    # No aliases loaded; its first commit has no tasks, so every task uploads.
    "abundant-ai/DeepSWE-test": "386a2eaf72a17177cee980d9c7fead4bc67737d0",
    "abundant-ai/harbor-forge-archive": "c49f8effe9d1b97792622f32b4221287f15ffbb8",
    "abundant-ai/harbor-forge-v2": "1e1fb983268f009ab2293b2d49c16d12fa24787c",
    "abundant-ai/harbor-lh": "abee65a3cc3c6190a545718a5be123bc8e1d3c33",
    "abundant-ai/smithy": "cbdb8ad4217fed22b07eaaeabc798d9df8a02801",
    "abundant-ai/sre-world": "28d44ce2f9beaaeb3b231167200aec950e0c95b7",
}


def alias_prefix(repo_name: str) -> str:
    return repo_name.split("/")[1] + "~"


def alias_of(prefix: str, folder: str) -> str:
    return prefix + folder.replace("/", "~")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def is_ancestor(repo: Path, sha: str | None, head: str) -> bool:
    return bool(sha) and (
        subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, head],
            capture_output=True,
        ).returncode
        == 0
    )


def changed_task_folders(repo: Path, base: str, head: str) -> list[str]:
    """Task folders at ``head`` with a file changed since ``base``."""
    task_dirs = {
        str(Path(p).parent)
        for p in git(repo, "ls-tree", "-r", "--name-only", head).splitlines()
        if Path(p).name == "task.toml"
    }
    folders = set()
    for name in git(
        repo, "diff", "--name-only", "--no-renames", base, head
    ).splitlines():
        for parent in Path(name).parents:
            if str(parent) in task_dirs:
                folders.add(str(parent))
                break
    return sorted(folders)


class RetryTransport(httpx.HTTPTransport):
    """Retries dropped connections; the API resets idle pooled ones mid-run."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        for attempt in range(3):
            try:
                return super().handle_request(request)
            except httpx.TransportError:
                if attempt == 2:
                    raise
                time.sleep(2**attempt)
        raise AssertionError("unreachable")


def get_task(client: httpx.Client, task_id: str) -> dict:
    return (
        client.get(f"/tasks/{task_id}", params={"include_trials": "false"})
        .raise_for_status()
        .json()
    )


def sync_folders(
    repo: Path,
    folders: list[str],
    prefix: str,
    client: httpx.Client,
    tag_id: str,
    upload: Callable[..., dict],
    dry_run: bool = False,
) -> list[str]:
    """Upload and tag each folder; returns the folders that failed."""
    aliases = (
        client.get("/task-aliases", params={"prefix": prefix})
        .raise_for_status()
        .json()["aliases"]
    )
    failed = []
    for folder in folders:
        alias = alias_of(prefix, folder)
        want = aliases.get(alias)
        try:
            task = None
            if want:  # a deleted or missing task 404s; never upload into it
                task = get_task(client, want)
            if dry_run:
                print(
                    f"would upload {folder} -> {want or 'by name ' + Path(folder).name}"
                )
                continue
            with tempfile.TemporaryDirectory() as tmp:
                src = repo / folder
                if want:
                    # The server names an upload Path(name).stem, which would cut
                    # a task ID with a dot (qwen2.5) short; the suffix is what
                    # it strips instead.
                    src = Path(tmp) / f"{want}.task"
                    shutil.copytree(repo / folder, src, symlinks=True)
                got = upload(src)
                if not want and got.get("matched_by") == "content_hash":
                    # The server matched another task's files, writing nothing;
                    # a new folder still gets its own task.
                    got = upload(src, force_new_version=True)
            if want and got["task_id"] != want:
                raise RuntimeError(f"uploaded to {got['task_id']}, expected {want}")
            if not want:
                added = client.post(
                    "/task-aliases",
                    json={"aliases": {alias: got["task_id"]}, "dry_run": False},
                ).raise_for_status()
                # A retried POST finds the alias it already added as existing.
                if alias not in added.json()["created"] + added.json()["existing"]:
                    raise RuntimeError(f"alias {alias} not added: {added.json()}")
            task = task or get_task(client, got["task_id"])
            # Assign 409s once a task holds the org's tag cap, even when this
            # tag is one of them, so only assign a missing tag.
            if all(t["tag_id"] != tag_id for t in task.get("user_tags", [])):
                assigned = client.post(
                    "/tags/assign",
                    json={
                        "tag_id": tag_id,
                        "scope": "TASK",
                        "target_id": got["task_id"],
                    },
                )
                # A retried POST 409s when its first attempt already assigned
                # the tag (now at the cap); that tag being there is success.
                tagged = assigned.status_code == 409 and any(
                    t["tag_id"] == tag_id
                    for t in get_task(client, got["task_id"]).get("user_tags", [])
                )
                if not tagged:
                    assigned.raise_for_status()
            unchanged = " (unchanged)" if got.get("content_unchanged") else ""
            print(f"ok {folder} -> {got['task_id']} v{got.get('version')}{unchanged}")
        except (Exception, SystemExit) as exc:  # upload_task exits via typer.Exit
            failed.append(folder)
            print(f"::error::{folder}: {exc!r}")
    return failed


def github_auth_env(token: str) -> dict[str, str]:
    """Git config, as environment variables, that sends ``token`` to github.com.

    A token in the clone URL lands in .git/config and in git and Git LFS error
    text; a header (which both read from http.extraHeader) appears in neither.
    """
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


def checkout(repo_name: str, dest: Path) -> str:
    url = f"https://github.com/{repo_name}.git"
    subprocess.run(
        ["git", "clone", "-q", "--filter=blob:none", "--no-checkout", url, str(dest)],
        check=True,
        capture_output=True,
        text=True,
    )
    return git(dest, "rev-parse", "origin/main").strip()


def git_error(exc: Exception) -> str:
    if not isinstance(exc, subprocess.CalledProcessError):
        return str(exc)
    step = exc.cmd[3] if exc.cmd[1] == "-C" else exc.cmd[1]
    return f"git {step} exited {exc.returncode}: {(exc.stderr or '').strip()}"


def materialize(repo: Path, head: str, folders: list[str]) -> None:
    """Check out only ``folders`` at ``head``, with their Git LFS files.

    Cone mode keeps a sparse index; a full index of harbor-lh is ~580 MB.
    ``git lfs pull`` in a blob-less clone fetches every blob of the tree one at a
    time, so LFS files come through the checkout's smudge filter instead.
    """
    attrs = subprocess.run(
        ["git", "-C", str(repo), "show", f"{head}:.gitattributes"],
        capture_output=True,
        text=True,
    ).stdout
    if "filter=lfs" in attrs:
        git(repo, "lfs", "install", "--local")
    git(repo, "sparse-checkout", "set", "--cone", "--sparse-index", *folders)
    git(repo, "checkout", "-q", head)


def main() -> None:
    from oddish.cli.api import upload_task
    from oddish.cli.config import get_api_url, get_auth_headers

    names = ("ODDISH_API_URL", "ODDISH_API_KEY", "REGISTRY_READ_TOKEN")
    if not all(os.environ.get(n) for n in names):
        print(
            "REGISTRY_SYNC_API_URL, REGISTRY_SYNC_API_KEY or REGISTRY_READ_TOKEN not set"
        )
        return
    auth = github_auth_env(os.environ["REGISTRY_READ_TOKEN"])
    # Actions masks the secret itself, not its base64 form.
    print(f"::add-mask::{auth['GIT_CONFIG_VALUE_0'].split()[-1]}")
    os.environ.update(auth)
    dry_run = os.environ.get("DRY_RUN") == "true"
    only, base_input = os.environ.get("ONLY_REPO", ""), os.environ.get("BASE", "")
    state_path = Path(os.environ["STATE_FILE"])
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    api = get_api_url()
    failed_repos = []
    with (
        httpx.Client(
            base_url=api,
            timeout=60.0,
            headers=get_auth_headers(),
            transport=RetryTransport(),
        ) as client,
        tempfile.TemporaryDirectory() as work,
    ):
        tags = client.get("/tags").raise_for_status().json()["items"]
        tag_id = next(
            (t["id"] for t in tags if t["key"] == TAG and t.get("value") is None), None
        )
        if tag_id is None:
            sys.exit(f"no {TAG} tag at {api}; run registry_tag.py --apply first")
        for name, built_from in REPOS.items():
            if only and only != name:
                continue
            repo = Path(work) / name.split("/")[1]
            try:
                head = checkout(name, repo)
                candidates = (base_input, state.get(name), built_from)
                base = next((s for s in candidates if is_ancestor(repo, s, head)), None)
                if base is None:
                    raise RuntimeError("no base commit on main; pass one as `base`")
                folders = changed_task_folders(repo, base, head)
                print(
                    f"{name}: {len(folders)} task folders changed in {base[:12]}..{head[:12]}"
                )
            except (subprocess.CalledProcessError, RuntimeError) as exc:
                print(f"::error::{name}: {git_error(exc)}")
                failed_repos.append(name)
                continue
            # ponytail: sequential; after the first catch-up a run sees a few folders.
            failed = []
            for i in range(0, len(folders), BATCH):
                batch = folders[i : i + BATCH]
                if not dry_run:
                    try:
                        # Replaces the previous batch's folders in the checkout.
                        materialize(repo, head, batch)
                    except subprocess.CalledProcessError as exc:
                        print(f"::error::{name}: {git_error(exc)}")
                        failed += batch
                        continue
                failed += sync_folders(
                    repo,
                    batch,
                    alias_prefix(name),
                    client,
                    tag_id,
                    lambda src, **kw: upload_task(
                        api, src, register=True, quiet=True, **kw
                    ),
                    dry_run,
                )
                shutil.rmtree(repo / ".git" / "lfs" / "objects", ignore_errors=True)
            if failed:
                failed_repos.append(name)
            if failed or dry_run:
                continue
            state[name] = head
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(state, indent=1) + "\n")
    if failed_repos:
        sys.exit(f"failed: {', '.join(failed_repos)}; the next run retries them")


if __name__ == "__main__":
    main()
