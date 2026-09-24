"""Build the exact PR commit, wait for it, and set its preview alias."""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def request(token, team_id, method, path, *, body=None, query=None):
    params = urllib.parse.urlencode({"teamId": team_id, **(query or {})})
    req = urllib.request.Request(
        f"https://api.vercel.com{path}?{params}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # API errors can contain resolved environment values. Do not print them.
        raise RuntimeError(
            f"Vercel {method} request failed (HTTP {exc.code})"
        ) from None


def deployment_shas(deployment):
    return {
        sha
        for sha in (
            (deployment.get("gitSource") or {}).get("sha"),
            (deployment.get("meta") or {}).get("githubCommitSha"),
        )
        if sha
    }


def create_preview(token, project_id, team_id, repo_id, branch, commit_sha, env):
    if not branch or branch in {"main", "staging"}:
        raise ValueError("A PR branch other than main or staging is required")
    if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        raise ValueError("VERCEL_GIT_COMMIT_SHA must be the full PR commit SHA")
    if not re.fullmatch(r"[1-9][0-9]*", repo_id):
        raise ValueError("GITHUB_REPOSITORY_ID must be the GitHub repository ID")

    project = request(
        token, team_id, "GET", f"/v9/projects/{urllib.parse.quote(project_id, safe='')}"
    )
    if project.get("id") != project_id or not project.get("name"):
        raise RuntimeError("Vercel returned a different or unnamed project")

    # Keep the branch cache, pin the commit, and omit target for a preview build.
    deployment = request(
        token,
        team_id,
        "POST",
        "/v13/deployments",
        query={"forceNew": "1", "withCache": "1"},
        body={
            "name": project["name"],
            "project": project_id,
            "gitSource": {
                "type": "github",
                "repoId": repo_id,
                "ref": branch,
                "sha": commit_sha,
            },
            "env": env,
            "build": {"env": env},
        },
    )
    if not deployment_shas(deployment) and deployment.get("id"):
        deployment = request(
            token,
            team_id,
            "GET",
            f"/v13/deployments/{urllib.parse.quote(deployment['id'], safe='')}",
            query={"withGitRepoInfo": "true"},
        )
    if deployment_shas(deployment) != {commit_sha}:
        raise RuntimeError("Vercel did not confirm the requested PR commit SHA")
    if deployment.get("target") not in {None, "preview"}:
        raise RuntimeError("Vercel did not create a preview deployment")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", deployment.get("url", "")):
        raise RuntimeError("Vercel did not return a valid preview URL")
    return deployment


def wait_for_ready(token, team_id, deployment, commit_sha, *, timeout=600):
    deployment_id = deployment["id"]
    deadline = time.monotonic() + timeout
    while True:
        if deployment.get("id") != deployment_id:
            raise RuntimeError("Vercel returned a different deployment")
        state = deployment.get("readyState")
        if state == "READY":
            if deployment_shas(deployment) != {commit_sha}:
                raise RuntimeError("Vercel built a different or unknown PR commit")
            if deployment.get("target") not in {None, "preview"}:
                raise RuntimeError("Vercel did not build a preview deployment")
            return
        if state not in {"QUEUED", "INITIALIZING", "BUILDING"}:
            raise RuntimeError(f"Vercel preview build did not become ready ({state})")
        if time.monotonic() >= deadline:
            raise RuntimeError("Vercel preview build did not finish within 10 minutes")
        time.sleep(3)
        deployment = request(
            token,
            team_id,
            "GET",
            f"/v13/deployments/{urllib.parse.quote(deployment_id, safe='')}",
            query={"withGitRepoInfo": "true"},
        )


def assign_alias(token, team_id, deployment_id, hostname):
    result = request(
        token,
        team_id,
        "POST",
        f"/v2/deployments/{urllib.parse.quote(deployment_id, safe='')}/aliases",
        body={"alias": hostname},
    )
    if result.get("alias") != hostname:
        raise RuntimeError("Vercel did not confirm the requested preview alias")
    return f"https://{hostname}"


def main():
    token = os.environ["VERCEL_TOKEN"]
    team_id = os.environ["VERCEL_ORG_ID"]
    commit_sha = os.environ["VERCEL_GIT_COMMIT_SHA"]
    alias = os.environ.get("PREVIEW_ALIAS_HOSTNAME", "")
    if alias and not re.fullmatch(r"pr-[1-9][0-9]*\.oddish\.app", alias):
        raise ValueError("PREVIEW_ALIAS_HOSTNAME must be a PR's oddish.app hostname")
    env = {
        "NEXT_PUBLIC_API_URL": os.environ["BACKEND_API_URL"],
        "NEXT_PUBLIC_ODDISH_PREVIEW": "true",
        "NEXT_PUBLIC_ODDISH_PREVIEW_BACKEND_LABEL": os.environ["PREVIEW_BACKEND_LABEL"],
        "NEXT_PUBLIC_ODDISH_PREVIEW_BACKEND_URL": os.environ["BACKEND_API_URL"],
        "NEXT_PUBLIC_ODDISH_PREVIEW_DATABASE_LABEL": os.environ[
            "PREVIEW_DATABASE_LABEL"
        ],
        "NEXT_PUBLIC_ODDISH_PREVIEW_DATABASE_URL": os.environ["PREVIEW_DATABASE_URL"],
        "NEXT_PUBLIC_ODDISH_PREVIEW_PR_URL": os.environ.get("PR_URL", ""),
        "NEXT_PUBLIC_ODDISH_PREVIEW_PR_TITLE": os.environ.get("PR_TITLE", ""),
    }
    deployment = create_preview(
        token,
        os.environ["VERCEL_PROJECT_ID"],
        team_id,
        os.environ["GITHUB_REPOSITORY_ID"],
        os.environ["VERCEL_GIT_BRANCH"],
        commit_sha,
        env,
    )
    wait_for_ready(token, team_id, deployment, commit_sha)
    alias_url = assign_alias(token, team_id, deployment["id"], alias) if alias else ""
    preview_url = "https://" + deployment["url"]
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"preview_url={preview_url}\n")
        output.write(f"preview_alias_url={alias_url}\n")
    print("Created the PR's Vercel preview with build cache enabled.", file=sys.stderr)
    print(preview_url)


if __name__ == "__main__":
    main()
