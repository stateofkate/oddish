"""Preview reuse must inspect the immutable deployment, not today's project env."""

import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest


@pytest.fixture
def vercel():
    path = (
        Path(__file__).resolve().parents[2]
        / ".github/scripts/preview/redeploy_vercel.py"
    )
    spec = importlib.util.spec_from_file_location("redeploy_vercel", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("state", ["QUEUED", "INITIALIZING", "BUILDING", "READY"])
def test_reuses_only_matching_build_and_runtime_configuration(
    vercel, monkeypatch, state
):
    expected = {
        "NEXT_PUBLIC_API_URL": "https://preview.example",
        "NEXT_PUBLIC_ODDISH_PREVIEW": "true",
    }
    requests = []

    def respond(request, *, timeout):
        requests.append(request.full_url)
        assert timeout == 30
        return io.BytesIO(json.dumps({"env": expected, "buildEnv": expected}).encode())

    monkeypatch.setattr(vercel.urllib.request, "urlopen", respond)
    assert vercel.deployment_matches_config(
        "token", "team", {"uid": "dpl_1", "state": state}, expected
    )
    assert requests == [
        "https://api.vercel.com/v3/env/pull/dpl_1?teamId=team&source=vercel-cli%3Aenv%3Apull"
    ]


@pytest.mark.parametrize(
    "snapshot",
    [
        {"env": {"API": "new"}, "buildEnv": {"API": "old"}},
        {"env": {"API": "old"}, "buildEnv": {"API": "new"}},
        {"env": {"API": "new"}},
        {"env": {"API": "new"}, "buildEnv": {}},
        {},
    ],
)
def test_stale_or_unknown_snapshot_requires_redeploy(vercel, monkeypatch, snapshot):
    monkeypatch.setattr(
        vercel.urllib.request,
        "urlopen",
        lambda *a, **k: io.BytesIO(json.dumps(snapshot).encode()),
    )
    assert not vercel.deployment_matches_config(
        "token", "team", {"uid": "dpl_1", "state": "READY"}, {"API": "new"}
    )


@pytest.mark.parametrize("state", ["ERROR", "CANCELED", None])
def test_failed_deployment_is_never_reused(vercel, state):
    assert not vercel.deployment_matches_config(
        "token", "team", {"uid": "dpl_1", "state": state}, {}
    )


@pytest.mark.parametrize("code", [400, 403, 404, 401, 429, 500])
def test_snapshot_access_failures(vercel, monkeypatch, code):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://api.vercel.com", code, "failure", {}, None
        )

    monkeypatch.setattr(vercel.urllib.request, "urlopen", fail)
    if code in {400, 403, 404}:
        assert not vercel.deployment_matches_config(
            "token", "team", {"uid": "dpl_1", "state": "READY"}, {}
        )
    else:
        with pytest.raises(urllib.error.HTTPError):
            vercel.deployment_matches_config(
                "token", "team", {"uid": "dpl_1", "state": "READY"}, {}
            )


def test_lookup_ignores_other_commits(vercel, monkeypatch):
    wrong = {"uid": "dpl_wrong", "meta": {"githubCommitSha": "old"}}
    right = {"uid": "dpl_right", "meta": {"githubCommitSha": "wanted"}}
    monkeypatch.setattr(
        vercel.urllib.request,
        "urlopen",
        lambda *a, **k: io.BytesIO(
            json.dumps({"deployments": [wrong, right]}).encode()
        ),
    )
    assert (
        vercel.find_existing_deployment("token", "project", "team", "branch", "wanted")
        == right
    )


@pytest.mark.parametrize("matches", [True, False])
def test_main_redeploys_only_when_configuration_requires_it(
    vercel, monkeypatch, tmp_path, matches
):
    for key, value in {
        "VERCEL_TOKEN": "token",
        "VERCEL_PROJECT_ID": "project",
        "VERCEL_ORG_ID": "team",
        "VERCEL_GIT_BRANCH": "branch",
        "VERCEL_GIT_COMMIT_SHA": "commit",
        "BACKEND_API_URL": "https://api.example",
        "PREVIEW_BACKEND_LABEL": "preview",
        "PREVIEW_DATABASE_LABEL": "project branch",
        "PREVIEW_DATABASE_URL": "https://db.example",
        "PR_URL": "https://pr.example",
        "PR_TITLE": "Change",
        "GITHUB_OUTPUT": str(tmp_path / "out"),
    }.items():
        monkeypatch.setenv(key, value)
    existing = {"uid": "dpl_existing", "name": "project", "url": "existing.example"}
    monkeypatch.setattr(vercel, "find_existing_deployment", lambda *a: existing)

    def compare(token, team, deployment, expected):
        assert deployment is existing
        assert expected["NEXT_PUBLIC_API_URL"] == "https://api.example"
        assert expected["NEXT_PUBLIC_ODDISH_PREVIEW_PR_TITLE"] == "Change"
        assert len(expected) == 8
        return matches

    monkeypatch.setattr(vercel, "deployment_matches_config", compare)
    calls = []

    def redeploy(*args):
        calls.append(args)
        return {"url": "new.example"}

    monkeypatch.setattr(vercel, "redeploy", redeploy)
    vercel.main()
    assert len(calls) == (0 if matches else 1)
    assert (
        tmp_path / "out"
    ).read_text() == f"preview_url=https://{'existing' if matches else 'new'}.example\n"


def test_encrypted_runtime_uses_complete_build_snapshot(vercel, monkeypatch):
    snapshot = {
        "env": {"VERCEL_ENCRYPTED_ENV_KEY": "opaque"},
        "buildEnv": {"API": "new"},
    }
    monkeypatch.setattr(
        vercel.urllib.request,
        "urlopen",
        lambda *a, **k: io.BytesIO(json.dumps(snapshot).encode()),
    )
    assert vercel.deployment_matches_config(
        "token", "team", {"uid": "dpl_1", "state": "READY"}, {"API": "new"}
    )
