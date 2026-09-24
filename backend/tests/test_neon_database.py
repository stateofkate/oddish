"""Preview deploys must keep data and must never reset Neon staging."""

import importlib.util
from pathlib import Path
from urllib.error import URLError

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / ".github/scripts/neon_database.py"
spec = importlib.util.spec_from_file_location("neon_database", SCRIPT)
neon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(neon)

MAIN = {"id": "br-main", "name": "main", "default": True, "current_state": "ready"}
PREVIEW = {
    "id": "br-preview",
    "name": "pr-42",
    "parent_id": "br-main",
    "default": False,
    "current_state": "ready",
}
ENDPOINT = {
    "id": "ep-preview",
    "host": "ep-preview.us-east-2.aws.neon.tech",
    "branch_id": "br-preview",
    "type": "read_write",
    "current_state": "idle",
}
URI = (
    "postgresql://neondb_owner:p%40ssword@ep-preview.us-east-2.aws.neon.tech/neondb"
    "?sslmode=require&channel_binding=require"
)


class FakeClient:
    def __init__(self, branches=None):
        self.branches = [
            dict(b) for b in (branches if branches is not None else [MAIN, PREVIEW])
        ]
        self.calls = []
        self.endpoint = dict(ENDPOINT)

    def find_branch(self, name):
        return next((b for b in self.branches if b["name"] == name), None)

    def request(self, method, path, *, body=None, query=None):
        self.calls.append((method, path, body, query))
        if method == "POST":
            assert path == "/branches"
            assert body == {
                "branch": {"name": "pr-42", "parent_id": "br-main"},
                "endpoints": [{"type": "read_write"}],
            }
            self.branches.append(dict(PREVIEW))
            return {"branch": dict(PREVIEW)}
        if method == "DELETE":
            assert path == "/branches/br-preview"
            return {}
        assert method == "GET"
        if path == "/connection_uri":
            return {
                "uri": URI.replace("ep-preview.", "ep-preview-pooler.")
                if query["pooled"] == "true"
                else URI
            }
        if path.endswith("/endpoints"):
            return {"endpoints": [self.endpoint]}
        return {
            "branch": next(b for b in self.branches if path.endswith("/" + b["id"]))
        }


def test_first_preview_is_a_child_of_staging_and_repeat_has_no_writes():
    client = FakeClient([MAIN])
    first, parent, created = neon.select_branch(client, "preview", "42")
    assert created is True
    assert first["parent_id"] == parent["id"] == "br-main"
    client.calls.clear()
    second, _, created = neon.select_branch(client, "preview", "42")
    assert created is False
    assert second["id"] == first["id"]
    assert client.calls == []


def test_staging_uses_main_without_creating_a_git_branch():
    client = FakeClient([MAIN])
    branch, parent, created = neon.select_branch(client, "staging")
    assert branch == parent == MAIN
    assert created is False
    assert client.calls == []


@pytest.mark.parametrize(
    "changes", [{"default": False}, {"parent_id": "br-other"}, {"name": "staging"}]
)
def test_staging_rejects_wrong_root(changes):
    client = FakeClient([{**MAIN, **changes}])
    with pytest.raises(neon.NeonError, match="root/default"):
        neon.select_branch(client, "staging")
    assert client.calls == []


@pytest.mark.parametrize("mode", ["preview", "delete-preview"])
@pytest.mark.parametrize(
    "changes",
    [
        {"default": True},
        {"parent_id": "br-other"},
        {"parent_id": None},
        {"protected": True},
    ],
)
def test_preview_rejects_wrong_lineage_or_default_without_writes(mode, changes):
    client = FakeClient([MAIN, {**PREVIEW, **changes}])
    with pytest.raises(neon.NeonError, match="child of Neon main"):
        neon.select_branch(client, mode, "42")
    assert all(method == "GET" for method, *_ in client.calls)


def test_delete_only_removes_verified_preview_and_missing_preview_is_ok():
    client = FakeClient()
    assert neon.select_branch(client, "delete-preview", "42")[0] is None
    assert [(c[0], c[1]) for c in client.calls] == [
        ("GET", "/branches/br-preview"),
        ("DELETE", "/branches/br-preview"),
    ]
    missing = FakeClient([MAIN])
    neon.select_branch(missing, "delete-preview", "42")
    assert missing.calls == []


def test_delete_rechecks_branch_after_list(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(client, "request", lambda *args, **kwargs: {"branch": MAIN})
    with pytest.raises(neon.NeonError, match="child of Neon main"):
        neon.select_branch(client, "delete-preview", "42")


def test_create_conflict_reuses_branch_without_deletion(monkeypatch):
    client = FakeClient([MAIN])

    def request(method, path, **kwargs):
        assert method == "POST"
        client.branches.append(dict(PREVIEW))
        raise neon.NeonAPIError(409)

    monkeypatch.setattr(client, "request", request)
    branch, _, created = neon.select_branch(client, "preview", "42")
    assert branch == PREVIEW
    assert created is False


def test_branch_search_checks_exact_name_and_all_pages(monkeypatch):
    client = neon.NeonClient("test-project", "secret")
    queries = []

    def request(method, path, *, query):
        queries.append(query)
        if "cursor" not in query:
            return {
                "branches": [{**PREVIEW, "name": "pr-420"}],
                "pagination": {"next": "page-2"},
            }
        return {"branches": [PREVIEW]}

    monkeypatch.setattr(client, "request", request)
    assert client.find_branch("pr-42") == PREVIEW
    assert queries[-1]["cursor"] == "page-2"
    assert all(q["include_deleted"] == "false" for q in queries)


def test_endpoint_must_belong_to_selected_branch():
    client = FakeClient()
    client.endpoint["branch_id"] = "br-main"
    with pytest.raises(neon.NeonError, match="another branch"):
        neon.wait_for_endpoint(client, PREVIEW, MAIN)


@pytest.mark.parametrize(
    "uri",
    [
        URI.replace("ep-preview.", "ep-preview-pooler."),
        URI.replace("ep-preview.", "ep-main."),
        URI.replace("/neondb?", "/other?"),
        URI.replace("neondb_owner:", "other:"),
        URI + "\nBAD=value",
    ],
)
def test_wrong_endpoint_or_database_url_is_rejected_without_secret_in_error(uri):
    with pytest.raises(neon.NeonError) as exc:
        neon.connection_urls(uri, ENDPOINT, "neondb", "neondb_owner")
    assert "p%40ssword" not in str(exc.value)


def test_run_exports_secrets_only_to_env_and_masks_them_first(
    monkeypatch, tmp_path, capsys
):
    client = FakeClient()
    env_path = tmp_path / "env"
    output_path = tmp_path / "output"
    for name, value in {
        "NEON_PROJECT_ID": "test-project",
        "NEON_API_KEY": "secret",
        "PR_NUMBER": "42",
        "GITHUB_ENV": str(env_path),
        "GITHUB_OUTPUT": str(output_path),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("NEON_DATABASE_NAME", raising=False)
    monkeypatch.delenv("NEON_ROLE_NAME", raising=False)
    monkeypatch.setattr(neon, "NeonClient", lambda *args: client)
    neon.run("preview")
    env = dict(line.split("=", 1) for line in env_path.read_text().splitlines())
    output = dict(line.split("=", 1) for line in output_path.read_text().splitlines())
    assert env["NEON_BRANCH_ID"] == "br-preview"
    assert env["NEON_PARENT_BRANCH_ID"] == "br-main"
    assert "sslmode=require" in env["PREVIEW_DATABASE_URL"]
    assert output == {
        "branch_id": "br-preview",
        "branch_ref": "br-preview",
        "branch_was_created": "false",
    }
    assert "p%40ssword" not in output_path.read_text()
    assert [
        call[3]["pooled"] for call in client.calls if call[1] == "/connection_uri"
    ] == ["false", "true"]
    assert client.calls[-1][3]["branch_id"] == "br-preview"
    assert "ep-preview-pooler." in env["ODDISH_RUNTIME_DATABASE_URL"]
    assert "ep-preview-pooler." not in env["ODDISH_DATABASE_URL"]
    stdout = capsys.readouterr().out
    assert "::add-mask::p@ssword" in stdout
    assert "::add-mask::p%2540ssword" in stdout
    assert all(
        line.startswith("::add-mask::")
        for line in stdout.splitlines()
        if "ssword" in line
    )


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_lost_write_response_is_not_retried(monkeypatch, method):
    calls = []

    def fail(request, **kwargs):
        calls.append(request)
        raise URLError("secret should not leak")

    monkeypatch.setattr(neon, "urlopen", fail)
    with pytest.raises(neon.NeonAPIError, match="network error") as exc:
        neon.NeonClient("test-project", "secret").request(method, "/branches")
    assert len(calls) == 1
    assert "secret" not in str(exc.value)
