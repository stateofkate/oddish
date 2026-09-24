"""Keep preview links in the PR body and its sticky comment up to date."""

import json
import os
import re
import urllib.error
import urllib.request

MARKER = "<!-- oddish-preview-links -->"
END_MARKER = "<!-- /oddish-preview-links -->"


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def gh_request(method: str, path: str, body: dict | None = None):
    token = env("GITHUB_TOKEN")
    repo = env("GITHUB_REPOSITORY")
    url = f"https://api.github.com/repos/{repo}{path}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request) as response:
            if response.status == 204:
                return None
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {method} {path} failed: {detail}") from exc


def build_body() -> str:
    commit_sha = env("PREVIEW_COMMIT_SHA")
    short_sha = commit_sha[:7] if commit_sha else "unknown"
    vercel_url = env("VERCEL_PREVIEW_URL")
    vercel_deployment_url = env("VERCEL_DEPLOYMENT_URL")
    modal_api_url = env("MODAL_API_URL")
    neon_branch_id = env("NEON_BRANCH_ID")
    neon_project_id = env("NEON_PROJECT_ID")
    for name, value, pattern in (
        ("NEON_BRANCH_ID", neon_branch_id, r"br-[a-z0-9]+(?:-[a-z0-9]+)*"),
        ("NEON_PROJECT_ID", neon_project_id, r"[a-z0-9]+(?:-[a-z0-9]+)*"),
        ("MODAL_API_URL", modal_api_url, r"https://[A-Za-z0-9.-]+(?::[0-9]+)?/?"),
        ("VERCEL_PREVIEW_URL", vercel_url, r"https://[A-Za-z0-9.-]+(?::[0-9]+)?/?"),
    ):
        if not re.fullmatch(pattern, value):
            raise ValueError(f"A valid {name} is required for this PR preview")
    backend_label = env("PREVIEW_BACKEND_LABEL") or f"oddish-pr-{env('PR_NUMBER')}"
    database_label = env("PREVIEW_DATABASE_LABEL") or f"Neon {neon_branch_id}"
    database_url = (
        f"https://console.neon.tech/app/projects/{neon_project_id}"
        f"/branches/{neon_branch_id}"
    )

    rows = [
        f"| Frontend | [{vercel_url}]({vercel_url}) | Vercel preview for `{short_sha}` |",
        f"| Backend | [{backend_label}]({modal_api_url}) | `{backend_label}` |",
        f"| Database | [{database_label}]({database_url}) | `{database_label}` |",
    ]

    return "\n".join(
        [
            MARKER,
            "## Oddish preview",
            "",
            f"Commit: `{commit_sha or 'unknown'}`",
            "",
            "| Surface | Link | Target |",
            "| --- | --- | --- |",
            *rows,
            "",
            *(
                [f"Vercel deployment URL: {vercel_deployment_url}", ""]
                if vercel_deployment_url and vercel_deployment_url != vercel_url
                else []
            ),
            "_Updated by the PR Preview workflow._",
            END_MARKER,
        ]
    )


def update_body(current: str, preview: str) -> str:
    """Replace our marked block without changing the author's other text."""
    start = current.find(MARKER)
    end = current.find(END_MARKER, start) if start >= 0 else -1
    if end >= 0:
        return current[:start] + preview + current[end + len(END_MARKER) :]
    separator = "\n\n" if current else ""
    return current + separator + preview


def find_existing_comment(pr_number: str):
    page = 1
    while True:
        comments = gh_request(
            "GET",
            f"/issues/{pr_number}/comments?per_page=100&page={page}",
        )
        existing = next(
            (comment for comment in comments if MARKER in comment.get("body", "")),
            None,
        )
        if existing or len(comments) < 100:
            return existing
        page += 1


def main() -> None:
    pr_number = env("PR_NUMBER")
    body = build_body()
    existing = find_existing_comment(pr_number)

    if existing:
        gh_request("PATCH", f"/issues/comments/{existing['id']}", {"body": body})
    else:
        gh_request("POST", f"/issues/{pr_number}/comments", {"body": body})

    pull = gh_request("GET", f"/pulls/{pr_number}")
    current = pull.get("body") or ""
    updated = update_body(current, body)
    if updated != current:
        gh_request("PATCH", f"/pulls/{pr_number}", {"body": updated})


if __name__ == "__main__":
    main()
