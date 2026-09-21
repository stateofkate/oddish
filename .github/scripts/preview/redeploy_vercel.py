"""Reuse this commit's Vercel preview when its resolved configuration matches.

The Git integration builds immediately on push. First-time branch configuration
still needs a redeploy; subsequent pushes can use that build unchanged. Compare
both build-time and runtime values from the deployment itself, never the mutable
project environment (which may have changed after the build started).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_LOOKUP_ATTEMPTS = 18
LOOKUP_POLL_INTERVAL_S = 10


def deployment_commit_sha(deployment):
    meta = deployment.get("meta") or {}
    git_source = deployment.get("gitSource") or {}
    for candidate in (
        meta.get("githubCommitSha"),
        meta.get("githubCommitSHA"),
        meta.get("gitCommitSha"),
        git_source.get("sha"),
    ):
        if candidate:
            return candidate
    return None


def find_existing_deployment(token, project_id, team_id, branch, commit_sha):
    params = urllib.parse.urlencode(
        {
            "projectId": project_id,
            "teamId": team_id,
            "limit": 20,
            "target": "preview",
            "branch": branch,
        }
    )
    url = f"https://api.vercel.com/v6/deployments?{params}"
    headers = {"Authorization": f"Bearer {token}"}

    for _ in range(MAX_LOOKUP_ATTEMPTS):
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
        for deployment in payload.get("deployments", []):
            if deployment_commit_sha(deployment) == commit_sha:
                return deployment
        time.sleep(LOOKUP_POLL_INTERVAL_S)
    raise SystemExit(
        "No Vercel preview deployment found yet for "
        f"branch {branch!r} at commit {commit_sha!r}"
    )


def redeploy(token, team_id, project_name, deployment_id):
    url = (
        "https://api.vercel.com/v13/deployments"
        f"?teamId={urllib.parse.quote(team_id, safe='')}&forceNew=1"
    )
    body = json.dumps({"name": project_name, "deploymentId": deployment_id}).encode()
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def deployment_matches_config(token, team_id, deployment, expected):
    if deployment.get("state") not in {"QUEUED", "INITIALIZING", "BUILDING", "READY"}:
        return False
    # This is the deployment-specific endpoint used by Vercel CLI's env pull.
    # It returns the resolved snapshot, including values embedded by Next.js.
    params = urllib.parse.urlencode(
        {"teamId": team_id, "source": "vercel-cli:env:pull"}
    )
    request = urllib.request.Request(
        f"https://api.vercel.com/v3/env/pull/{deployment['uid']}?{params}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            snapshot = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code not in {400, 403, 404}:
            raise
        # Older deployments or tokens may not support reading the snapshot.
        # Retain the existing redeploy path when reuse cannot be established.
        print(
            f"Deployment environment unavailable (HTTP {exc.code}); requesting a fresh preview",
            file=sys.stderr,
        )
        return False
    # Vercel CLI uses buildEnv as the complete resolved snapshot. With large
    # env encryption, env contains decryption keys instead of the runtime vars.
    build_env = snapshot.get("buildEnv")
    runtime_env = snapshot.get("env")
    return (
        isinstance(build_env, dict)
        and isinstance(runtime_env, dict)
        and all(build_env.get(key, "") == value for key, value in expected.items())
        and all(
            runtime_env[key] == value
            for key, value in expected.items()
            if key in runtime_env
        )
    )


def main():
    token = os.environ["VERCEL_TOKEN"]
    project_id = os.environ["VERCEL_PROJECT_ID"]
    team_id = os.environ["VERCEL_ORG_ID"]
    branch = os.environ["VERCEL_GIT_BRANCH"]
    commit_sha = os.environ["VERCEL_GIT_COMMIT_SHA"]

    deployment = find_existing_deployment(
        token, project_id, team_id, branch, commit_sha
    )
    expected = {
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
    if deployment_matches_config(token, team_id, deployment, expected):
        print(
            f"Reusing Vercel deployment {deployment['uid']} for {commit_sha}",
            file=sys.stderr,
        )
    else:
        deployment = redeploy(token, team_id, deployment["name"], deployment["uid"])
        print(f"Created Vercel deployment for {commit_sha}", file=sys.stderr)
    preview_url = "https://" + deployment["url"]

    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        f.write(f"preview_url={preview_url}\n")

    print(preview_url)


if __name__ == "__main__":
    main()
