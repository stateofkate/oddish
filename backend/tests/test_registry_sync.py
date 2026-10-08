"""Registry sync uploads merged task folders into their mapped tasks and retries failures."""

import importlib.util
import json
import subprocess
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / ".github/scripts/registry/sync.py"
spec = importlib.util.spec_from_file_location("registry_sync", SCRIPT)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)
Client = httpx.Client  # the end-to-end test patches httpx.Client to return the fake


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _task(repo, folder, instruction):
    path = repo / folder
    (path / "tests").mkdir(parents=True, exist_ok=True)
    (path / "task.toml").write_text("[agent]\ntimeout_sec = 60.0\n")
    (path / "instruction.md").write_text(instruction)
    (path / "tests" / "test.sh").write_text("true\n")


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def registry(tmp_path):
    """A repo whose second commit edits three task folders and a README."""
    repo = tmp_path / "registry"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _task(repo, "tasks/renamed-folder", "v1")
    _task(repo, "tasks/plain", "v1")
    _task(repo, "tasks/removed", "v1")
    (repo / "README.md").write_text("hi\n")
    built_from = _commit(repo, "A")
    (repo / "tasks/renamed-folder/instruction.md").write_text("v2")
    (repo / "tasks/plain/tests/test.sh").write_text("false\n")
    _task(repo, "tasks/added", "v1")
    _git(repo, "rm", "-rq", "tasks/removed")
    (repo / "README.md").write_text("bye\n")
    return repo, built_from, _commit(repo, "B")


class Oddish:
    """Fake API: tasks named ``gone-*`` 404, uploads land on the folder's name."""

    def __init__(self, wrong=(), aliases=None, pretagged=(), same_files=None):
        self.uploads, self.tagged, self.wrong = [], [], set(wrong)
        self.same_files = same_files  # task whose files every upload matches
        self.aliases = dict(aliases or {})
        self.pretagged = set(pretagged)

    def upload(self, src, force_new_version=False):
        name = Path(src.name).stem  # the server's _normalize_task_name
        if self.same_files and not force_new_version and "-" not in name:
            return {"task_id": self.same_files, "matched_by": "content_hash"}
        self.uploads.append((name, (src / "instruction.md").read_text()))
        if name == "plain" and "plain" in self.wrong:
            raise SystemExit(1)  # upload_task exits via typer.Exit
        task_id = name if "-" in name else f"{name}-0000aaaa"
        return {
            "task_id": task_id + ("-x" if name in self.wrong else ""),
            "version": 2,
        }

    def client(self):
        def handle(request):
            path = request.url.path
            if path == "/tags":
                items = [{"id": "T1", "key": "registry-main", "value": None}]
                return httpx.Response(200, json={"items": items})
            if path.startswith("/tasks/"):
                assert request.url.params["include_trials"] == "false"
                if "gone" in path:
                    return httpx.Response(404, json={})
                on = path.removeprefix("/tasks/") in {*self.tagged, *self.pretagged}
                tags = [{"tag_id": "T1"}] if on else []
                return httpx.Response(200, json={"user_tags": tags})
            if path == "/task-aliases" and request.method == "GET":
                return httpx.Response(200, json={"aliases": self.aliases})
            if path == "/task-aliases":
                added = json.loads(request.content)["aliases"]
                self.aliases.update(added)
                return httpx.Response(200, json={"created": list(added)})
            self.tagged.append(json.loads(request.content)["target_id"])
            return httpx.Response(200, json={})

        return Client(base_url="http://oddish", transport=httpx.MockTransport(handle))


def test_changed_task_folders_skips_deleted_folders_and_non_task_files(registry):
    repo, built_from, head = registry

    folders = sync.changed_task_folders(repo, built_from, head)

    assert folders == ["tasks/added", "tasks/plain", "tasks/renamed-folder"]


def test_aliased_folders_upload_into_their_task_and_new_folders_get_aliases(
    registry,
):
    repo, built_from, head = registry
    oddish = Oddish(aliases={"r~tasks~renamed-folder": "other-name-1234abcd"})
    folders = sync.changed_task_folders(repo, built_from, head)

    failed = sync.sync_folders(
        repo, folders, "r~", oddish.client(), "T1", oddish.upload
    )

    assert failed == []
    assert oddish.uploads == [
        ("added", "v1"),
        ("plain", "v1"),
        ("other-name-1234abcd", "v2"),
    ]
    assert oddish.tagged == ["added-0000aaaa", "plain-0000aaaa", "other-name-1234abcd"]
    assert oddish.aliases == {
        "r~tasks~renamed-folder": "other-name-1234abcd",
        "r~tasks~added": "added-0000aaaa",
        "r~tasks~plain": "plain-0000aaaa",
    }


def test_failures_do_not_stop_the_other_folders(registry):
    repo, built_from, head = registry
    aliases = {
        "r~tasks~renamed-folder": "other-name-1234abcd",
        "r~tasks~added": "gone-1",
    }
    oddish = Oddish(wrong={"plain", "other-name-1234abcd"}, aliases=aliases)
    folders = sync.changed_task_folders(repo, built_from, head)

    failed = sync.sync_folders(
        repo, folders, "r~", oddish.client(), "T1", oddish.upload
    )

    # added: mapped task 404s, never uploaded. plain: upload exits.
    # renamed-folder: server answered with another task, so it is not tagged.
    assert failed == ["tasks/added", "tasks/plain", "tasks/renamed-folder"]
    assert [name for name, _ in oddish.uploads] == ["plain", "other-name-1234abcd"]
    assert oddish.tagged == []


def test_dry_run_uploads_and_tags_nothing(registry):
    repo, built_from, head = registry
    oddish = Oddish()
    folders = sync.changed_task_folders(repo, built_from, head)

    failed = sync.sync_folders(
        repo, folders, "r~", oddish.client(), "T1", oddish.upload, dry_run=True
    )

    assert failed == [] and oddish.uploads == [] and oddish.tagged == []
    assert oddish.aliases == {}


def test_state_advances_only_when_every_folder_succeeds(
    registry, tmp_path, monkeypatch
):
    repo, built_from, head = registry
    state = tmp_path / "state" / "state.json"
    monkeypatch.setattr(sync, "REPOS", {"abundant-ai/registry": built_from})
    monkeypatch.setattr(
        sync,
        "checkout",
        lambda name, dest: (
            subprocess.run(["git", "clone", "-q", str(repo), str(dest)], check=True),
            _git(dest, "rev-parse", "origin/main"),
        )[1],
    )
    for name, value in {
        "ODDISH_API_URL": "http://oddish",
        "ODDISH_API_KEY": "k",
        "REGISTRY_READ_TOKEN": "t",
        "STATE_FILE": str(state),
    }.items():
        monkeypatch.setenv(name, value)

    def run(oddish):
        import oddish.cli.api as api
        import oddish.cli.config as config

        monkeypatch.setattr(
            api, "upload_task", lambda url, src, **kw: oddish.upload(src)
        )
        monkeypatch.setattr(config, "get_auth_headers", lambda *a: {})
        monkeypatch.setattr(sync.httpx, "Client", lambda **kw: oddish.client())
        sync.main()

    with pytest.raises(SystemExit, match="failed: abundant-ai/registry"):
        run(Oddish(wrong={"plain"}))
    assert not state.exists()

    retry = Oddish()
    run(retry)
    assert [name for name, _ in retry.uploads] == ["added", "plain", "renamed-folder"]
    assert json.loads(state.read_text()) == {"abundant-ai/registry": head}

    quiet = Oddish()
    run(quiet)
    assert quiet.uploads == []


def test_token_stays_out_of_the_clone_url_and_git_config(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sync.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(sync, "git", lambda *a: "sha\n")
    env = sync.github_auth_env("ghs_secret")

    sync.checkout("abundant-ai/registry", tmp_path / "r")

    assert "ghs_secret" not in repr(calls) + repr(env)
    assert (
        env["GIT_CONFIG_VALUE_0"]
        == "Authorization: Basic eC1hY2Nlc3MtdG9rZW46Z2hzX3NlY3JldA=="
    )


def test_dotted_task_ids_land_on_their_task_and_tagged_tasks_skip_assign(registry):
    repo, built_from, head = registry
    dotted = "fit-qwen2.5-1-1a4dd1d7"
    oddish = Oddish(
        aliases={"r~tasks~renamed-folder": dotted}, pretagged={"plain-0000aaaa"}
    )
    folders = sync.changed_task_folders(repo, built_from, head)

    failed = sync.sync_folders(
        repo, folders, "r~", oddish.client(), "T1", oddish.upload
    )

    assert failed == []
    assert oddish.uploads[-1] == (dotted, "v2")
    assert oddish.tagged == ["added-0000aaaa", dotted]


def test_new_folders_with_another_tasks_files_get_their_own_task(registry):
    repo, built_from, head = registry
    aliases = {"r~tasks~renamed-folder": "other-name-1234abcd"}
    oddish = Oddish(aliases=aliases, same_files="other-name-1234abcd")
    folders = sync.changed_task_folders(repo, built_from, head)

    failed = sync.sync_folders(
        repo, folders, "r~", oddish.client(), "T1", oddish.upload
    )

    assert failed == []
    assert oddish.aliases["r~tasks~added"] == "added-0000aaaa"
    assert oddish.aliases["r~tasks~plain"] == "plain-0000aaaa"


def test_folders_are_checked_out_one_batch_at_a_time(registry, tmp_path, monkeypatch):
    repo, built_from, head = registry
    monkeypatch.setattr(sync, "BATCH", 1)
    monkeypatch.setattr(sync, "REPOS", {"abundant-ai/registry": built_from})
    monkeypatch.setattr(
        sync,
        "checkout",
        lambda name, dest: (
            subprocess.run(["git", "clone", "-q", str(repo), str(dest)], check=True),
            _git(dest, "rev-parse", "origin/main"),
        )[1],
    )
    for name, value in {
        "ODDISH_API_URL": "http://oddish",
        "ODDISH_API_KEY": "k",
        "REGISTRY_READ_TOKEN": "t",
        "STATE_FILE": str(tmp_path / "state.json"),
    }.items():
        monkeypatch.setenv(name, value)
    oddish, on_disk = Oddish(), []

    def upload(src):
        on_disk.append(sorted(p.name for p in src.parent.iterdir() if p.is_dir()))
        return oddish.upload(src)

    import oddish.cli.api as api
    import oddish.cli.config as config

    monkeypatch.setattr(api, "upload_task", lambda url, src, **kw: upload(src))
    monkeypatch.setattr(config, "get_auth_headers", lambda *a: {})
    monkeypatch.setattr(sync.httpx, "Client", lambda **kw: oddish.client())

    sync.main()

    assert [name for name, _ in oddish.uploads] == ["added", "plain", "renamed-folder"]
    assert on_disk == [["added"], ["plain"], ["renamed-folder"]]
