"""CI creates one cached preview with the PR commit and its own settings."""

import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMMIT = "a" * 40


@pytest.fixture
def vercel(monkeypatch, tmp_path):
    path = ROOT / ".github/scripts/preview/redeploy_vercel.py"
    spec = importlib.util.spec_from_file_location("redeploy_vercel", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key, value in {
        "VERCEL_TOKEN": "test-token",
        "VERCEL_PROJECT_ID": "prj_test",
        "VERCEL_ORG_ID": "team_test",
        "GITHUB_REPOSITORY_ID": "123",
        "VERCEL_GIT_BRANCH": "feature/demo",
        "VERCEL_GIT_COMMIT_SHA": COMMIT,
        "PREVIEW_ALIAS_HOSTNAME": "",
        "BACKEND_API_URL": "https://api.example",
        "PREVIEW_BACKEND_LABEL": "oddish-pr-42",
        "PREVIEW_DATABASE_LABEL": "Neon br-preview",
        "PREVIEW_DATABASE_URL": "https://console.neon.tech/app/projects/demo/branches/br-preview",
        "PR_URL": "https://github.com/oddish/oddish/pull/42",
        "PR_TITLE": 'Keep data\nwith a quoted "title"',
        "GITHUB_OUTPUT": str(tmp_path / "out"),
    }.items():
        monkeypatch.setenv(key, value)
    return module


def response(value):
    return io.BytesIO(json.dumps(value).encode())


def test_one_build_has_exact_commit_public_settings_and_cache(
    vercel, monkeypatch, tmp_path
):
    calls = []

    def respond(req, *, timeout):
        calls.append(req)
        if req.method == "GET":
            assert req.full_url == (
                "https://api.vercel.com/v9/projects/prj_test?teamId=team_test"
            )
            return response({"id": "prj_test", "name": "oddish"})
        assert req.method == "POST"
        assert req.full_url == (
            "https://api.vercel.com/v13/deployments"
            "?teamId=team_test&forceNew=1&withCache=1"
        )
        body = json.loads(req.data)
        assert body["project"] == "prj_test"
        assert body["name"] == "oddish"
        assert body["gitSource"] == {
            "type": "github",
            "repoId": "123",
            "ref": "feature/demo",
            "sha": COMMIT,
        }
        assert "target" not in body
        assert "deploymentId" not in body
        expected = {
            "NEXT_PUBLIC_API_URL": "https://api.example",
            "NEXT_PUBLIC_ODDISH_PREVIEW": "true",
            "NEXT_PUBLIC_ODDISH_PREVIEW_BACKEND_LABEL": "oddish-pr-42",
            "NEXT_PUBLIC_ODDISH_PREVIEW_BACKEND_URL": "https://api.example",
            "NEXT_PUBLIC_ODDISH_PREVIEW_DATABASE_LABEL": "Neon br-preview",
            "NEXT_PUBLIC_ODDISH_PREVIEW_DATABASE_URL": (
                "https://console.neon.tech/app/projects/demo/branches/br-preview"
            ),
            "NEXT_PUBLIC_ODDISH_PREVIEW_PR_URL": "https://github.com/oddish/oddish/pull/42",
            "NEXT_PUBLIC_ODDISH_PREVIEW_PR_TITLE": 'Keep data\nwith a quoted "title"',
        }
        assert body["env"] == expected
        assert body["build"]["env"] == expected
        return response(
            {
                "id": "dpl_test",
                "url": "preview.example",
                "gitSource": {"sha": COMMIT},
                "readyState": "READY",
            }
        )

    monkeypatch.setattr(vercel.urllib.request, "urlopen", respond)
    vercel.main()
    assert len(calls) == 2
    assert (
        tmp_path / "out"
    ).read_text() == "preview_url=https://preview.example\npreview_alias_url=\n"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("VERCEL_GIT_BRANCH", "main"),
        ("VERCEL_GIT_BRANCH", "staging"),
        ("VERCEL_GIT_COMMIT_SHA", "main"),
    ],
)
def test_invalid_source_fails_before_any_request(vercel, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        vercel.urllib.request,
        "urlopen",
        lambda *a, **k: pytest.fail("Unexpected API call"),
    )
    with pytest.raises(ValueError):
        vercel.main()


@pytest.mark.parametrize("sha", ["b" * 40, None])
def test_unproved_or_wrong_commit_is_not_published(vercel, monkeypatch, tmp_path, sha):
    calls = []

    def respond(req, *, timeout):
        calls.append(req.method)
        if "/projects/" in req.full_url:
            return response({"id": "prj_test", "name": "oddish"})
        return response(
            {"id": "dpl_test", "url": "preview.example", "gitSource": {"sha": sha}}
        )

    monkeypatch.setattr(vercel.urllib.request, "urlopen", respond)
    with pytest.raises(RuntimeError, match="commit SHA"):
        vercel.main()
    assert calls.count("POST") == 1
    assert not (tmp_path / "out").exists()


def test_failed_create_is_not_retried_or_published(
    vercel, monkeypatch, tmp_path, capsys
):
    posts = []

    def respond(req, *, timeout):
        if req.method == "GET":
            return response({"id": "prj_test", "name": "oddish"})
        posts.append(req)
        raise urllib.error.HTTPError(
            req.full_url, 500, "failure", {}, io.BytesIO(b"private environment value")
        )

    monkeypatch.setattr(vercel.urllib.request, "urlopen", respond)
    with pytest.raises(RuntimeError, match="HTTP 500") as error:
        vercel.main()
    assert len(posts) == 1
    assert not (tmp_path / "out").exists()
    assert "private environment value" not in str(error.value)
    assert "private environment value" not in str(capsys.readouterr())


def test_waits_for_ready_then_assigns_the_exact_alias(vercel, monkeypatch, tmp_path):
    monkeypatch.setenv("PREVIEW_ALIAS_HOSTNAME", "pr-42.oddish.app")
    monkeypatch.setattr(vercel.time, "sleep", lambda _: None)
    states = iter(["BUILDING", "READY"])
    calls = []
    built = {"id": "dpl_test", "url": "preview.example", "gitSource": {"sha": COMMIT}}

    def respond(req, *, timeout):
        calls.append((req.method, req.full_url))
        if "/projects/" in req.full_url:
            return response({"id": "prj_test", "name": "oddish"})
        if req.method == "GET":
            assert req.full_url == (
                "https://api.vercel.com/v13/deployments/dpl_test"
                "?teamId=team_test&withGitRepoInfo=true"
            )
            return response({**built, "readyState": next(states)})
        if "/aliases?" in req.full_url:
            assert req.full_url == (
                "https://api.vercel.com/v2/deployments/dpl_test/aliases?teamId=team_test"
            )
            assert json.loads(req.data) == {"alias": "pr-42.oddish.app"}
            assert len(calls) == 5
            return response({"alias": "pr-42.oddish.app", "uid": "alias_test"})
        return response({**built, "readyState": "INITIALIZING"})

    monkeypatch.setattr(vercel.urllib.request, "urlopen", respond)
    vercel.main()
    assert [method for method, _ in calls] == ["GET", "POST", "GET", "GET", "POST"]
    assert (tmp_path / "out").read_text() == (
        "preview_url=https://preview.example\n"
        "preview_alias_url=https://pr-42.oddish.app\n"
    )


@pytest.mark.parametrize("state", ["ERROR", "CANCELED"])
def test_failed_build_never_assigns_alias_or_outputs_url(
    vercel, monkeypatch, tmp_path, state
):
    monkeypatch.setenv("PREVIEW_ALIAS_HOSTNAME", "pr-42.oddish.app")
    monkeypatch.setattr(
        vercel, "create_preview", lambda *a: {"id": "dpl_test", "readyState": state}
    )
    monkeypatch.setattr(
        vercel,
        "request",
        lambda *a, **k: pytest.fail("Failed build must not be aliased"),
    )
    with pytest.raises(RuntimeError, match="did not become ready"):
        vercel.main()
    assert not (tmp_path / "out").exists()


def test_build_wait_has_a_deadline(vercel, monkeypatch, tmp_path):
    clock = iter([0, 601])
    monkeypatch.setattr(vercel.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(
        vercel,
        "create_preview",
        lambda *a: {"id": "dpl_test", "readyState": "BUILDING"},
    )
    monkeypatch.setattr(
        vercel, "request", lambda *a, **k: pytest.fail("Expired wait must stop")
    )
    with pytest.raises(RuntimeError, match="within 10 minutes"):
        vercel.main()
    assert not (tmp_path / "out").exists()


def test_alias_failure_is_not_published(vercel, monkeypatch, tmp_path):
    monkeypatch.setenv("PREVIEW_ALIAS_HOSTNAME", "pr-42.oddish.app")
    monkeypatch.setattr(
        vercel,
        "create_preview",
        lambda *a: {
            "id": "dpl_test",
            "readyState": "READY",
            "gitSource": {"sha": COMMIT},
        },
    )

    def respond(req, *, timeout):
        assert req.method == "POST" and "/aliases?" in req.full_url
        raise urllib.error.HTTPError(req.full_url, 403, "failure", {}, None)

    monkeypatch.setattr(vercel.urllib.request, "urlopen", respond)
    with pytest.raises(RuntimeError):
        vercel.main()
    assert not (tmp_path / "out").exists()
