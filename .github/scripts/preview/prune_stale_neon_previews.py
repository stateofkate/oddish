"""Remove old Neon previews for closed PRs and stop their remaining apps.

Open PRs keep their data at every age. Unknown PR state keeps all resources.
Stop an app only after its GKE teardown succeeds; delete its database last.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from neon_database import (
    NeonAPIError,
    NeonClient,
    NeonError,
    validate_main,
    validate_preview,
)


def created_at(branch: dict) -> datetime:
    stamp = datetime.fromisoformat(branch["created_at"].replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise NeonError("Neon branch creation time has no time zone.")
    return stamp


def list_previews(client: NeonClient, parent: dict) -> dict:
    previews = {}
    cursor = None
    seen = set()
    while True:
        query = {"limit": 100, "include_deleted": "false"}
        if cursor:
            query["cursor"] = cursor
        response = client.request("GET", "/branches", query=query)
        for branch in response["branches"]:
            name = branch["name"]
            if not re.fullmatch(r"pr-[1-9][0-9]*", name):
                continue
            # Protected and unrelated branches are never owned by this sweep.
            if (
                branch.get("protected")
                or branch.get("default")
                or branch.get("parent_id") != parent["id"]
            ):
                continue
            validate_preview(branch, parent, name)
            created_at(branch)  # Validate the whole listing before any write.
            if name in previews:
                raise NeonError("Duplicate Neon preview names; cleanup is refused.")
            previews[name] = branch
        cursor = response.get("pagination", {}).get("next")
        if not cursor:
            return previews
        if cursor in seen:
            raise NeonError("Neon repeated a branch list page.")
        seen.add(cursor)


def pr_state(pr_number: str) -> str:
    request = Request(
        f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/pulls/{pr_number}",
        headers={
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            result = json.load(response)
        state = result.get("state")
        return state if state in {"open", "closed"} else "unknown"
    except (HTTPError, URLError, TimeoutError, ValueError, AttributeError):
        return "unknown"


def deployed_apps(environment: str) -> set[str]:
    result = subprocess.run(
        ["modal", "app", "list", "--env", environment, "--json"],
        capture_output=True,
        text=True,
        check=True,
    )
    apps = json.loads(result.stdout)
    if not isinstance(apps, list):
        raise NeonError("Modal returned an invalid app list.")
    deployed = set()
    for app in apps:
        if not isinstance(app, dict):
            raise NeonError("Modal returned an invalid app entry.")
        # Modal versions use lowercase keys or the display column names.
        description = app.get("description", app.get("Description"))
        state = app.get("state", app.get("State"))
        if not isinstance(description, str) or not isinstance(state, str):
            raise NeonError("Modal returned an invalid app entry.")
        if state == "deployed" and re.fullmatch(r"oddish-pr-[1-9][0-9]*", description):
            deployed.add(description)
    return deployed


def stop_app(app: str, environment: str) -> None:
    # Keep the owner and its reaper alive if GKE cleanup fails.
    subprocess.run(
        [str(SCRIPT_DIR / "run_gke_teardown.sh"), app],
        env={**os.environ, "MODAL_ENVIRONMENT": environment},
        check=True,
    )
    subprocess.run(
        ["modal", "app", "stop", "-y", "--env", environment, app], check=True
    )
    subprocess.run(
        ["modal", "secret", "delete", "-y", "--env", environment, f"{app}-db"],
        check=True,
    )


def prune(client: NeonClient, *, environment: str, age_days: int, dry_run: bool) -> int:
    parent = validate_main(client.find_branch("main"))
    branches = list_previews(client, parent)
    apps = deployed_apps(environment)
    cutoff = datetime.now(timezone.utc) - timedelta(days=age_days)
    numbers = {name[3:] for name in branches} | {
        app[len("oddish-pr-") :] for app in apps
    }
    failed = False
    for number in sorted(numbers, key=int):
        name = f"pr-{number}"
        app = f"oddish-{name}"
        state = pr_state(number)
        if state != "closed":
            print(f"Keep {name}: PR state is {state}.")
            continue
        branch = branches.get(name)
        delete_branch = branch is not None and created_at(branch) < cutoff
        if dry_run:
            if app in apps:
                print(f"DRY RUN: would stop {app}; PR is closed.")
            if delete_branch:
                print(
                    f"DRY RUN: would delete Neon {name}; PR is closed and branch is old."
                )
            continue
        try:
            if app in apps:
                stop_app(app, environment)
                print(f"Stopped {app}; PR is closed.")
            if not delete_branch:
                continue
            # Recheck both identities. A concurrent replacement branch must
            # never be removed by a stale list or a name-only delete.
            current = client.request("GET", f"/branches/{branch['id']}")["branch"]
            validate_preview(current, parent, name)
            if current["id"] != branch["id"] or created_at(current) >= cutoff:
                raise NeonError("Preview changed during cleanup; keep its database.")
            if pr_state(number) != "closed":
                print(f"Keep Neon {name}: PR state changed during cleanup.")
                continue
            client.request("DELETE", f"/branches/{branch['id']}")
            print(f"Deleted Neon {name}; PR is closed and branch is old.")
        except NeonAPIError as exc:
            if exc.status == 404:
                continue  # The observed branch is already gone.
            print(f"::error::Could not clean {name}: {exc}", file=sys.stderr)
            failed = True
        except (NeonError, subprocess.CalledProcessError) as exc:
            print(f"::error::Could not clean {name}: {exc}", file=sys.stderr)
            failed = True
    return int(failed)


def main() -> int:
    try:
        for key in (
            "NEON_API_KEY",
            "NEON_PROJECT_ID",
            "GITHUB_REPOSITORY",
            "GITHUB_TOKEN",
        ):
            if not os.environ.get(key):
                raise NeonError(f"Set {key} before running preview cleanup.")
        raw_age = os.environ.get("MAX_AGE_DAYS", "7")
        if not raw_age.isascii() or not raw_age.isdecimal():
            raise NeonError("MAX_AGE_DAYS must be a whole number of days.")
        dry_run = os.environ.get("DRY_RUN", "false")
        if dry_run not in {"true", "false"}:
            raise NeonError("DRY_RUN must be true or false.")
        return prune(
            NeonClient(os.environ["NEON_PROJECT_ID"], os.environ["NEON_API_KEY"]),
            environment=os.environ.get("MODAL_ENVIRONMENT", "preview"),
            age_days=int(raw_age),
            dry_run=dry_run == "true",
        )
    except (
        NeonError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        subprocess.CalledProcessError,
    ):
        print(
            "::error::Preview cleanup failed. Check Neon, Modal, and GitHub access and metadata.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
