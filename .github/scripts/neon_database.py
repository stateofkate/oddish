#!/usr/bin/env python3
"""Resolve staging or a persistent PR database with the Neon management API.

Neon's root/default branch ``main`` is staging. Git's main branch does not
have a Neon branch. Deploys reuse ``pr-N`` without resetting data or passwords.
Only ``delete-preview`` removes a branch, after checking its name and parent.

API: https://api-docs.neon.tech/reference/createprojectbranch
     https://api-docs.neon.tech/reference/getconnectionuri
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

API_ROOT = "https://console.neon.tech/api/v2"
READY_TIMEOUT = 180
POLL_INTERVAL = 2
RESOURCE_ID = re.compile(r"[a-z0-9-]{1,60}\Z")


class NeonError(RuntimeError):
    """An error that is safe to show in CI logs."""


class NeonAPIError(NeonError):
    def __init__(self, status: int | None):
        self.status = status
        detail = f"HTTP {status}" if status else "network error"
        super().__init__(f"Neon API request failed ({detail}); no database was reset.")


def resource_id(value: str) -> str:
    if not isinstance(value, str) or not RESOURCE_ID.fullmatch(value):
        raise NeonError("Neon returned an invalid resource ID.")
    return value


class NeonClient:
    def __init__(self, project_id: str, api_key: str):
        self.project_id = resource_id(project_id)
        self.api_key = api_key

    def request(self, method: str, path: str, *, query=None, body=None) -> dict:
        url = f"{API_ROOT}/projects/{self.project_id}{path}"
        if query:
            url += "?" + urlencode(query)
        request = Request(
            url,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            data=json.dumps(body).encode() if body is not None else None,
        )
        for attempt in range(3):
            try:
                with urlopen(request, timeout=20) as response:
                    raw = response.read()
                data = json.loads(raw) if raw else {}
                if not isinstance(data, dict):
                    raise NeonError("Neon returned an invalid API response.")
                return data
            except HTTPError as exc:
                # Neon documents 423 and 503 as safe to retry for all methods.
                retryable = exc.code in {423, 503} or (
                    method == "GET" and exc.code in {429, 500, 502, 504}
                )
                exc.close()
                if not retryable or attempt == 2:
                    raise NeonAPIError(exc.code) from None
            except (URLError, TimeoutError, OSError):
                # A lost POST/DELETE response can follow a successful write.
                # Leave it alone; the next run resolves the existing branch.
                if method != "GET" or attempt == 2:
                    raise NeonAPIError(None) from None
            except (ValueError, UnicodeError):
                raise NeonError("Neon returned an invalid API response.") from None
            time.sleep(POLL_INTERVAL * (attempt + 1))
        raise AssertionError("unreachable")

    def find_branch(self, name: str) -> dict | None:
        matches = []
        cursor = None
        seen = set()
        while True:
            query = {"search": name, "limit": 100, "include_deleted": "false"}
            if cursor:
                query["cursor"] = cursor
            response = self.request("GET", "/branches", query=query)
            matches.extend(b for b in response["branches"] if b["name"] == name)
            cursor = response.get("pagination", {}).get("next")
            if not cursor:
                break
            if cursor in seen:
                raise NeonError("Neon repeated a branch list page.")
            seen.add(cursor)
        if len(matches) > 1:
            raise NeonError("More than one Neon branch has the requested name.")
        return matches[0] if matches else None


def validate_main(branch: dict | None) -> dict:
    if (
        not branch
        or branch.get("name") != "main"
        or branch.get("parent_id")
        or branch.get("default") is not True
    ):
        raise NeonError("Neon main must exist and be the root/default staging branch.")
    resource_id(branch["id"])
    return branch


def preview_name(pr_number: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]*", pr_number):
        raise NeonError("PR_NUMBER must be a positive whole number.")
    return f"pr-{pr_number}"


def validate_preview(branch: dict, parent: dict, name: str) -> dict:
    if (
        branch.get("name") != name
        or not re.fullmatch(r"pr-[1-9][0-9]*", name)
        or branch.get("parent_id") != parent["id"]
        or branch.get("id") == parent["id"]
        or branch.get("default") is not False
        or branch.get("protected") is True
    ):
        raise NeonError(
            "Preview must be an unprotected, non-default child of Neon main."
        )
    resource_id(branch["id"])
    return branch


def select_branch(client: NeonClient, mode: str, pr_number: str = "") -> tuple:
    name = preview_name(pr_number) if mode != "staging" else "main"
    parent = validate_main(client.find_branch("main"))
    if mode == "staging":
        return parent, parent, False
    branch = client.find_branch(name)
    if mode == "delete-preview":
        if branch is None:
            print(f"Neon {name} is already absent.")
            return None, parent, False
        # Re-read before deletion so a stale list cannot hide a rename,
        # parent change, or promotion to the default branch.
        branch_id = resource_id(branch["id"])
        branch = client.request("GET", f"/branches/{branch_id}")["branch"]
        validate_preview(branch, parent, name)
        client.request("DELETE", f"/branches/{branch_id}")
        print(f"Deleted Neon {name}.")
        return None, parent, False
    created = False
    if branch is None:
        try:
            response = client.request(
                "POST",
                "/branches",
                body={
                    "branch": {"name": name, "parent_id": parent["id"]},
                    "endpoints": [{"type": "read_write"}],
                },
            )
            branch = response["branch"]
            created = True
        except NeonAPIError as exc:
            # Another run may have created the same PR branch. Reuse it;
            # never delete it to recover from a create conflict.
            if exc.status != 409:
                raise
            branch = client.find_branch(name)
            if branch is None:
                raise
    return validate_preview(branch, parent, name), parent, created


def wait_for_endpoint(client: NeonClient, branch: dict, parent: dict) -> dict:
    deadline = time.monotonic() + READY_TIMEOUT
    branch_id = resource_id(branch["id"])
    while True:
        current = client.request("GET", f"/branches/{branch_id}")["branch"]
        if branch_id == parent["id"]:
            validate_main(current)
        else:
            validate_preview(current, parent, branch["name"])
        endpoints = client.request("GET", f"/branches/{branch_id}/endpoints")[
            "endpoints"
        ]
        writable = [e for e in endpoints if e.get("type") == "read_write"]
        if len(writable) > 1:
            raise NeonError("Neon returned more than one write endpoint.")
        if writable:
            endpoint = writable[0]
            if endpoint.get("branch_id") != branch_id or endpoint.get("disabled"):
                raise NeonError(
                    "Neon write endpoint is disabled or belongs to another branch."
                )
            # Idle computes wake on connection; archived branches remain queryable.
            if current.get("current_state") in {"ready", "archived"} and endpoint.get(
                "current_state"
            ) in {"active", "idle"}:
                resource_id(endpoint["id"])
                return endpoint
        if time.monotonic() >= deadline:
            raise NeonError("Neon branch did not become ready within three minutes.")
        time.sleep(POLL_INTERVAL)


def connection_urls(
    uri: str, endpoint: dict, database: str, role: str, *, pooled: bool = False
) -> tuple[str, str]:
    if not isinstance(uri, str) or any(c in uri for c in "\r\n\x00"):
        raise NeonError("Neon returned an invalid database URL.")
    try:
        parts = urlsplit(uri)
        expected_host = endpoint["host"]
        if pooled:
            endpoint_name, region = expected_host.split(".", 1)
            expected_host = f"{endpoint_name}-pooler.{region}"
        valid = (
            parts.scheme in {"postgres", "postgresql"}
            and parts.hostname == expected_host
            and (pooled or "-pooler" not in parts.hostname)
            and unquote(parts.path.lstrip("/")) == database
            and unquote(parts.username or "") == role
            and parts.password
            and not parts.fragment
            and parts.port in {None, 5432}
        )
    except ValueError:
        valid = False
    if not valid:
        raise NeonError("Neon database URL does not match the selected endpoint.")
    # SQLAlchemy passes sslmode/channel_binding as unsupported asyncpg kwargs.
    # A shared ssl=require URL also fails in Oddish's raw asyncpg DSN path.
    # Keep the runtime URL query-free: asyncpg prefers TLS and Neon requires it.
    runtime = urlunsplit(("postgresql+asyncpg", parts.netloc, parts.path, "", ""))
    # Keep the libpq URL usable by psql and direct asyncpg callers. Neither
    # channel_binding nor options is needed; force TLS for migration tools.
    query = [(k, v) for k, v in parse_qsl(parts.query) if k == "application_name"]
    query.append(("sslmode", "require"))
    direct = urlunsplit(("postgresql", parts.netloc, parts.path, urlencode(query), ""))
    return runtime, direct


def mask(value: str) -> None:
    escaped = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::add-mask::{escaped}")


def append_values(path: str, values: dict) -> None:
    with Path(path).open("a", encoding="utf-8") as stream:
        for key, value in values.items():
            if any(c in str(value) for c in "\r\n\x00"):
                raise NeonError("Cannot write a multi-line GitHub environment value.")
            stream.write(f"{key}={value}\n")


def run(mode: str) -> None:
    required = ["NEON_PROJECT_ID", "NEON_API_KEY"]
    if mode != "delete-preview":
        required += ["GITHUB_ENV", "GITHUB_OUTPUT"]
    for name in required:
        if not os.environ.get(name):
            raise NeonError(f"Set {name} before running this command.")
    client = NeonClient(os.environ["NEON_PROJECT_ID"], os.environ["NEON_API_KEY"])
    branch, parent, created = select_branch(
        client, mode, os.environ.get("PR_NUMBER", "")
    )
    if branch is None:
        return
    endpoint = wait_for_endpoint(client, branch, parent)
    database = os.environ.get("NEON_DATABASE_NAME") or "neondb"
    role = os.environ.get("NEON_ROLE_NAME") or "neondb_owner"
    query = {
        "branch_id": branch["id"],
        "endpoint_id": endpoint["id"],
        "database_name": database,
        "role_name": role,
    }
    response = client.request(
        "GET",
        "/connection_uri",
        query={**query, "pooled": "false"},
    )
    uri = response["uri"]
    migration, direct = connection_urls(uri, endpoint, database, role)
    pooled_uri = client.request(
        "GET", "/connection_uri", query={**query, "pooled": "true"}
    )["uri"]
    runtime, _ = connection_urls(pooled_uri, endpoint, database, role, pooled=True)
    for value in {
        uri,
        pooled_uri,
        migration,
        runtime,
        direct,
        urlsplit(uri).password,
        unquote(urlsplit(uri).password),
    }:
        mask(value)
    append_values(
        os.environ["GITHUB_ENV"],
        {
            "ODDISH_DATABASE_URL": migration,
            "ODDISH_RUNTIME_DATABASE_URL": runtime,
            "PREVIEW_DATABASE_URL": direct,
            "NEON_BRANCH_ID": branch["id"],
            "NEON_PARENT_BRANCH_ID": parent["id"],
        },
    )
    append_values(
        os.environ["GITHUB_OUTPUT"],
        {
            "branch_id": branch["id"],
            "branch_ref": branch["id"],
            "branch_was_created": str(created).lower(),
        },
    )
    print(f"Neon {branch['name']} is ready ({'created' if created else 'reused'}).")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preview", "staging", "delete-preview"))
    args = parser.parse_args()
    try:
        run(args.mode)
    except NeonError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    except (KeyError, TypeError, ValueError):
        # Never dump an API response or URL in a traceback.
        print("::error::Neon returned an incomplete API response.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
