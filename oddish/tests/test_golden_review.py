"""The installed skill helper must work without importing server modules."""

from copy import deepcopy
import csv
import importlib.util
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import tarfile
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import pytest

SCRIPT = (
    Path(__file__).parents[1]
    / "src/oddish/assets/skills/oddish/scripts/golden_review.py"
)
spec = importlib.util.spec_from_file_location("golden_review", SCRIPT)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


class SourceFixture:
    api = "https://oddish.example/api"

    def __init__(self, *args):
        self.facts = dict(
            id="task-1",
            task_id="task",
            task_version_id="task-v2",
            task_version=2,
            kind="agent",
            agent="codex",
            model="model",
            status="success",
            reward=0,
            started_at="2026-09-01",
            finished_at="2026-09-01",
            attempts=1,
            has_trajectory=True,
            analysis={"classification": "QA_ANSWER_SENTINEL"},
            pre_trial_findings=["QA_ANSWER_SENTINEL"],
        )
        self.files = {
            "instruction.md": b"Write result.json with the computed answer.\n",
            "task.toml": b'version = "1.0"\n',
            "tests/test.sh": b"test -f /app/result.json\n",
            "environment/blob": b"\xff\x00\x01",
            "qa_result.json": b"QA_ANSWER_SENTINEL",
        }
        self.hash = "original-hash"
        self.requests = []
        self.archive = False
        self.page_size = 1000
        self.missing = None

    def status(self, subject, *options):
        if options:
            assert subject == "task"
            assert options == ("--versions", "--version", "2")
            return dict(
                id="task-v2",
                task_id="task",
                version=2,
                content_hash=self.hash,
                reported_findings=["QA_ANSWER_SENTINEL"],
            )
        return deepcopy(self.facts)

    def get(self, path, **params):
        self.requests.append((path, params))
        if self.missing and path.endswith(self.missing):
            raise ValueError("Evidence request returned HTTP 404")
        if path == "/tasks/task/files":
            assert params["version"] == 2
            start = int(params["cursor"] or 0)
            entries = [
                dict(path=p, size=len(b), url="https://storage.example/" + p)
                for p, b in self.files.items()
            ]
            end = start + self.page_size
            return dict(
                files=entries[start:end],
                source_hash=self.hash,
                cursor=str(end) if end < len(entries) else None,
                truncated=end < len(entries),
                archive_url="https://storage.example/archive" if self.archive else None,
            )
        if path.endswith("/result"):
            return {"verifier_result": {"rewards": {"reward": 0}}}
        if path.endswith("/trajectory"):
            return {"steps": [{"action": "Wrote answer.txt instead of result.json"}]}
        if path.endswith("/logs/structured"):
            assert params == {"verifier_only": "true"}
            return dict(
                verifier={"stdout": "result.json missing", "stderr": ""},
                exception=None,
                analysis="QA_ANSWER_SENTINEL",
                agent="QA_ANSWER_SENTINEL",
            )
        raise AssertionError(path)

    def download(self, url):
        name = url.removeprefix("https://storage.example/")
        if name != "archive":
            return self.files[name]
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for path, content in self.files.items():
                entry = tarfile.TarInfo("./" + path)
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))
        return output.getvalue()


@pytest.fixture
def packet(tmp_path):
    source = SourceFixture()
    case = review.collect_case(source, "task-1", tmp_path)
    assert case["blockers"] == []
    data = dict(schema_version=review.SCHEMA, api_url=source.api, cases=[case])
    path = tmp_path / "review.json"
    review.save_json(path, data)
    return source, path, data


def approve(case):
    case.update(
        decision="approved",
        proposed_classification="BAD_FAILURE",
        expected_classification="GOOD_FAILURE",
        reviewed_by="Fixture reviewer",
        approval_note="Corrected proposal after checking the stated filename.",
        reasoning='The instruction requires result.json, but the agent wrote "answer.txt".\nThe grade is fair.',
        alternatives="A hidden filename requirement is ruled out by instruction.md.",
    )
    case["evidence"] = [
        dict(
            role="requirement",
            artifact="cases/task-1/task/instruction.md",
            lines="1",
            observation="Requires result.json.",
        ),
        dict(
            role="behavior",
            artifact="cases/task-1/trajectory.json",
            lines="1-7",
            observation="Agent wrote answer.txt.",
        ),
        dict(
            role="grading",
            artifact="cases/task-1/verifier.json",
            lines="1-5",
            observation="Verifier reports the missing file.",
        ),
    ]


@pytest.mark.parametrize("archive", [False, True])
def test_collect_pinned_sources_without_qa_answers(tmp_path, archive):
    source = SourceFixture()
    source.archive = archive
    source.page_size = 2
    case = review.collect_case(source, "task-1", tmp_path)
    assert not case["blockers"]
    assert case["omitted_task_files"] == ["qa_result.json"]
    assert (
        tmp_path / "cases/task-1/task/environment/blob"
    ).read_bytes() == b"\xff\x00\x01"
    for name in case["artifacts"]:
        assert b"QA_ANSWER_SENTINEL" not in (tmp_path / name).read_bytes()
    assert case["decision"] == "pending"
    assert case["expected_classification"] is None
    assert len([p for p, _ in source.requests if p == "/tasks/task/files"]) == 3


def test_archive_lookup_uses_posix_names_on_windows(tmp_path, monkeypatch):
    source = SourceFixture()
    source.archive = True
    # Model Windows path parsing while keeping actual file I/O in tmp_path.
    monkeypatch.setattr(review, "Path", PureWindowsPath)
    omitted = review.collect_task(source, source.facts, tmp_path, source.hash)
    assert omitted == ["qa_result.json"]
    assert (tmp_path / "tests/test.sh").read_bytes() == source.files["tests/test.sh"]
    assert (tmp_path / "environment/blob").read_bytes() == source.files[
        "environment/blob"
    ]


def test_saved_artifact_paths_use_forward_slashes(tmp_path, monkeypatch):
    relative_to = Path.relative_to

    def windows_relative_path(path, *args, **kwargs):
        return PureWindowsPath(relative_to(path, *args, **kwargs).as_posix())

    monkeypatch.setattr(Path, "relative_to", windows_relative_path)
    case = review.collect_case(SourceFixture(), "task-1", tmp_path)
    assert not case["blockers"]
    assert "cases/task-1/facts.json" in case["artifacts"]
    assert "cases/task-1/task/tests/test.sh" in case["artifacts"]
    assert all("\\" not in path for path in case["artifacts"])


@pytest.mark.parametrize(
    "operation", ["read_review", "approved_rows", "verify_remote", "render"]
)
def test_utf8_review_and_facts_ignore_locale(tmp_path, monkeypatch, operation):
    source = SourceFixture()
    source.facts["model"] = "modèle-模型"
    case = review.collect_case(source, "task-1", tmp_path)
    assert not case["blockers"]
    approve(case)
    case["reviewed_by"] = "Zoë 李"
    data = dict(schema_version=review.SCHEMA, api_url=source.api, cases=[case])
    path = tmp_path / "review.json"
    review.save_json(path, data)
    read_text = Path.read_text

    def ascii_locale(path, encoding=None, errors=None):
        return read_text(path, encoding=encoding or "ascii", errors=errors)

    monkeypatch.setattr(Path, "read_text", ascii_locale)
    if operation == "read_review":
        assert review.read_review(path) == data
    elif operation == "approved_rows":
        assert "Zoë 李" in review.approved_rows(path, data)[0]["note"]
    elif operation == "verify_remote":
        review.verify_remote(source, case, tmp_path)
    else:
        review.render(path)
        assert "Zoë 李" in path.with_suffix(".md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "missing", ["result", "trajectory", "logs/structured", "files"]
)
def test_missing_artifacts_block_approval(tmp_path, missing):
    source = SourceFixture()
    source.missing = missing
    case = review.collect_case(source, "task-1", tmp_path)
    assert case["blockers"] == ["Evidence request returned HTTP 404"]
    approve(case)
    with pytest.raises(ValueError, match="blockers"):
        review.approved_rows(tmp_path / "review.json", {"cases": [case]})


@pytest.mark.parametrize(
    "agent", ["nop", "oracle-v2", "agent-nop", "agent-oracle-test"]
)
def test_baselines_are_not_agent_trials(tmp_path, agent):
    source = SourceFixture()
    source.facts["agent"] = agent
    assert review.collect_case(source, "task-1", tmp_path)["blockers"] == [
        "Expected a finished agent trial"
    ]


def test_collection_detects_task_change(tmp_path):
    source = SourceFixture()
    get = source.get

    def changed(path, **params):
        if path.endswith("/logs/structured"):
            source.hash = "changed-hash"
        return get(path, **params)

    source.get = changed
    assert review.collect_case(source, "task-1", tmp_path)["blockers"] == [
        "Task changed during collection"
    ]


def test_collection_blocks_replaced_agent_trial(tmp_path):
    source = SourceFixture()
    source.facts["superseded_by_trial_id"] = "task-2"
    case = review.collect_case(source, "task-1", tmp_path)
    assert case["blockers"] == [
        "task-1: replaced by task-2; review the replacement trial"
    ]
    assert source.requests == []
    assert case["decision"] == "pending"


def test_collection_rejects_file_traversal(tmp_path):
    source = SourceFixture()
    source.files["../escape"] = b"bad"
    case = review.collect_case(source, "task-1", tmp_path)
    assert "Unsafe evidence path" in case["blockers"][0]
    assert not (tmp_path / "cases/task-1/escape").exists()


def test_collection_blocks_copy_before_reading_its_evidence(tmp_path):
    source = SourceFixture()
    source.facts["harbor_config"] = {
        "imported_source": {"trial_id": "original-1"},
    }
    case = review.collect_case(source, "task-1", tmp_path)
    assert len(case["blockers"]) == 1
    assert "copied from original-1" in case["blockers"][0]
    assert "Collect and review original-1 directly" in case["blockers"][0]
    assert source.requests == []
    assert case["artifacts"] == {}
    assert case["decision"] == "pending"


@pytest.mark.parametrize(
    "config",
    [None, {}, {"imported_source": {}}, {"imported_source": {"trial_id": "task-1"}}],
)
def test_direct_trial_identity_can_be_collected_and_verified(tmp_path, config):
    source = SourceFixture()
    source.facts["harbor_config"] = config
    case = review.collect_case(source, "task-1", tmp_path)
    assert case["blockers"] == []
    review.verify_remote(source, case, tmp_path)


def test_export_corrected_label_csv_quoting_and_exclusions(packet, capsys):
    _, path, data = packet
    approve(data["cases"][0])
    for decision in ("pending", "unresolved"):
        other = deepcopy(data["cases"][0])
        other.update(source_trial_id="task-" + decision, decision=decision)
        data["cases"].append(other)
    review.save_json(path, data)
    output = path.parent / "approved.csv"
    assert review.main(["export", str(path), "--out", str(output)]) == 0
    with output.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["expected_classification"] == "GOOD_FAILURE"
    assert data["cases"][0]["reasoning"] in rows[0]["note"]
    assert "excluded 2" in capsys.readouterr().out
    assert review.main(["export", str(path), "--out", str(output)]) == 1


@pytest.mark.parametrize(
    "key,value,match",
    [
        ("expected_classification", "UNRESOLVED", "invalid expected"),
        ("evidence", [], "missing evidence"),
        ("reviewed_by", " ", "missing reviewed_by"),
        ("approval_note", None, "missing approval_note"),
        ("open_questions", ["Why did grading fail?"], "open_questions"),
        ("blockers", ["Missing trajectory"], "blockers"),
        ("alternatives", "", "missing alternatives"),
        ("task_version_id", "wrong-version", "identity differs"),
    ],
)
def test_export_rejects_invalid_approved_record(packet, key, value, match):
    _, path, data = packet
    case = data["cases"][0]
    approve(case)
    case[key] = value
    with pytest.raises(ValueError, match=match):
        review.approved_rows(path, data)


def test_export_detects_evidence_edits_and_bad_citations(packet):
    _, path, data = packet
    case = data["cases"][0]
    approve(case)
    case["evidence"][0]["lines"] = "99"
    with pytest.raises(ValueError, match="outside file"):
        review.approved_rows(path, data)
    case["evidence"][0]["lines"] = "1"
    (path.parent / "cases/task-1/task/instruction.md").write_text("changed")
    with pytest.raises(ValueError, match="evidence missing or changed"):
        review.approved_rows(path, data)


def test_duplicate_and_unapproved_packets_cannot_export(packet):
    _, path, data = packet
    with pytest.raises(ValueError, match="No approved"):
        review.approved_rows(path, data)
    approve(data["cases"][0])
    duplicate = deepcopy(data["cases"][0])
    duplicate["expected_classification"] = "BAD_FAILURE"
    data["cases"].append(duplicate)
    with pytest.raises(ValueError, match="Duplicate trial"):
        review.approved_rows(path, data)


def test_resume_preserves_decisions_and_checks_deployment(packet, monkeypatch):
    source, path, data = packet
    approve(data["cases"][0])
    review.save_json(path, data)
    monkeypatch.setattr(review, "Source", lambda *args: source)
    monkeypatch.setattr(
        source,
        "status",
        lambda *args: pytest.fail("Resume must not refetch saved evidence"),
    )
    assert (
        review.main(
            ["collect", "task-1", "--api", source.api, "--out", str(path.parent)]
        )
        == 0
    )
    assert json.loads(path.read_text()) == data
    assert "Corrected proposal" in path.with_suffix(".md").read_text()
    source.api = "https://different.example"
    assert (
        review.main(
            ["collect", "task-1", "--api", source.api, "--out", str(path.parent)]
        )
        == 1
    )


@pytest.mark.parametrize("interruption", ["during_download", "before_review_save"])
def test_resume_recovers_unsaved_case_and_continues_batch(
    packet, monkeypatch, interruption
):
    source, path, data = packet
    approve(data["cases"][0])
    review.save_json(path, data)
    approved = deepcopy(data["cases"][0])
    status, get, save = source.status, source.get, review.save_json
    interrupted = False
    orphan = path.parent / "cases/task-2"

    def batch_status(subject, *options):
        if options:
            return status(subject, *options)
        assert subject != "task-1", "Saved approval must not be recollected"
        facts = status(subject)
        facts["id"] = subject
        return facts

    def interrupt_once():
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            (orphan / "unfinished.txt").write_text(
                "Old incomplete evidence", encoding="utf-8"
            )
            raise KeyboardInterrupt()

    def interrupted_get(endpoint, **params):
        if interruption == "during_download" and endpoint == "/trials/task-2/result":
            interrupt_once()
        return get(endpoint, **params)

    def interrupted_save(target, value):
        if (
            interruption == "before_review_save"
            and target == path
            and len(value["cases"]) == 2
        ):
            interrupt_once()
        return save(target, value)

    monkeypatch.setattr(review, "Source", lambda *args: source)
    monkeypatch.setattr(source, "status", batch_status)
    monkeypatch.setattr(source, "get", interrupted_get)
    monkeypatch.setattr(review, "save_json", interrupted_save)
    command = [
        "collect",
        "task-1",
        "task-2",
        "task-3",
        "--api",
        source.api,
        "--out",
        str(path.parent),
    ]
    with pytest.raises(KeyboardInterrupt):
        review.main(command)
    assert review.read_review(path)["cases"] == [approved]
    old_files = {
        p.relative_to(orphan): p.read_bytes() for p in orphan.rglob("*") if p.is_file()
    }
    assert review.main(command) == 0
    resumed = review.read_review(path)["cases"]
    assert [case["source_trial_id"] for case in resumed] == [
        "task-1",
        "task-2",
        "task-3",
    ]
    assert resumed[0] == approved
    assert all(
        case["decision"] == "pending" and not case["blockers"] for case in resumed[1:]
    )
    assert not (orphan / "unfinished.txt").exists()
    backups = list(path.parent.glob(".interrupted-task-2-*/evidence"))
    assert len(backups) == 1
    assert {
        p.relative_to(backups[0]): p.read_bytes()
        for p in backups[0].rglob("*")
        if p.is_file()
    } == old_files
    assert "unfinished.txt" not in json.dumps(resumed)


def test_first_interruption_saves_deployment_before_evidence(tmp_path, monkeypatch):
    source = SourceFixture()
    status = source.status

    def interrupt(*args):
        raise KeyboardInterrupt()

    monkeypatch.setattr(review, "Source", lambda *args: source)
    monkeypatch.setattr(source, "status", interrupt)
    command = ["collect", "task-1", "--api", source.api, "--out", str(tmp_path)]
    with pytest.raises(KeyboardInterrupt):
        review.main(command)
    saved = review.read_review(tmp_path / "review.json")
    assert saved["api_url"] == source.api and saved["cases"] == []
    original_api = source.api
    source.api = "https://different.example"
    assert review.main(command) == 1
    source.api = original_api
    monkeypatch.setattr(source, "status", status)
    assert review.main(command) == 0
    assert len(review.read_review(tmp_path / "review.json")["cases"]) == 1


def test_remote_check_blocks_stale_export(packet, monkeypatch):
    source, path, data = packet
    approve(data["cases"][0])
    review.save_json(path, data)
    monkeypatch.setattr(review, "Source", lambda *args: source)
    source.hash = "changed-hash"
    output = path.parent / "approved.csv"
    assert (
        review.main(["export", str(path), "--out", str(output), "--verify-remote"]) == 1
    )
    assert not output.exists()
    source.hash = "original-hash"
    assert (
        review.main(["export", str(path), "--out", str(output), "--verify-remote"]) == 0
    )


def test_remote_check_blocks_trial_replaced_after_review(packet, monkeypatch, capsys):
    source, path, data = packet
    approve(data["cases"][0])
    review.save_json(path, data)
    source.facts["superseded_by_trial_id"] = "task-2"
    monkeypatch.setattr(review, "Source", lambda *args: source)
    output = path.parent / "approved.csv"
    assert (
        review.main(["export", str(path), "--out", str(output), "--verify-remote"]) == 1
    )
    assert not output.exists()
    assert (
        "task-1: replaced by task-2; review the replacement trial"
        in capsys.readouterr().out
    )


def test_remote_check_blocks_copy_in_previously_approved_packet(
    packet, monkeypatch, capsys
):
    source, path, data = packet
    approve(data["cases"][0])
    review.save_json(path, data)
    # Older packets omit imported_source, and all saved FACTS still match.
    source.facts["harbor_config"] = {
        "imported_source": {"trial_id": "original-1"},
    }
    monkeypatch.setattr(review, "Source", lambda *args: source)
    output = path.parent / "approved.csv"
    assert (
        review.main(["export", str(path), "--out", str(output), "--verify-remote"])
        == 1
    )
    assert not output.exists()
    message = capsys.readouterr().out
    assert "copied from original-1" in message
    assert "Collect and review original-1 directly" in message


@pytest.mark.parametrize("character", ["a", "界"])
def test_export_note_limit_matches_import_api(packet, character, capsys):
    from oddish.schemas import QAGoldenCreate
    from pydantic import ValidationError

    _, path, data = packet
    case = data["cases"][0]
    approve(case)
    original = review.approved_rows(path, data)[0]
    case["reasoning"] += character * (20_000 - len(original["note"]))
    row = review.approved_rows(path, data)[0]
    assert len(row["note"]) == 20_000
    QAGoldenCreate(
        stage="post_trial",
        source_trial_id=row["source_trial_id"],
        expected={"classification": row["expected_classification"]},
        note=row["note"],
    )
    with pytest.raises(ValidationError, match="20000 characters"):
        QAGoldenCreate(
            stage="post_trial",
            source_trial_id=row["source_trial_id"],
            expected={"classification": row["expected_classification"]},
            note=row["note"] + character,
        )
    case["reasoning"] += character
    review.save_json(path, data)
    output = path.parent / "approved.csv"
    assert review.main(["export", str(path), "--out", str(output)]) == 1
    assert not output.exists()
    message = capsys.readouterr().out
    assert "task-1" in message and "20001" in message and "20000" in message


def test_cli_status_decodes_utf8_on_non_utf8_locale(monkeypatch):
    facts = {"id": "task-1", "error_message": "Zoë 李"}
    stdout = json.dumps(facts, ensure_ascii=False).encode("utf-8")
    stderr = "Diagnostic: café".encode("utf-8")
    program = (
        f"import sys; sys.stdout.buffer.write({stdout!r}); "
        f"sys.stderr.buffer.write({stderr!r})"
    )
    run = subprocess.run

    def run_cli(command, **kwargs):
        return run([sys.executable, "-c", program], **kwargs)

    # Force the default decoder even when the test runner enables UTF-8 mode.
    monkeypatch.setattr(subprocess, "_text_encoding", lambda: "ascii")
    monkeypatch.setattr(subprocess, "run", run_cli)
    assert review.Source("https://oddish.example", "oddish").status("task-1") == facts


def test_cli_failure_never_echoes_captured_qa_or_credentials(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 2, "QA_ANSWER_SENTINEL", "SECRET_TOKEN"
        ),
    )
    with pytest.raises(ValueError, match="CLI status failed") as error:
        review.Source("https://oddish.example", "oddish").status("task-1")
    assert "SENTINEL" not in str(error.value)
    assert "SECRET" not in str(error.value)


def test_helper_labels_match_import_contract():
    from oddish.analyze.models import Classification
    from oddish.analyze.qa_stages import QAStage, STAGES

    assert review.LABELS == {label.value for label in Classification}
    for label in review.LABELS:
        STAGES[QAStage.POST_TRIAL].parse_expected({"classification": label})


def test_installed_cli_http_collection_to_importable_csv(tmp_path, monkeypatch):
    """Use the actual CLI and helper subprocesses against a local source server."""
    source = SourceFixture()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            requests.append((parsed.path, self.headers.get("Authorization")))
            if parsed.path.startswith("/bytes/"):
                body = source.files[parsed.path.removeprefix("/bytes/")]
            elif parsed.path == "/trials/task-1":
                body = json.dumps(source.facts).encode()
            elif parsed.path == "/tasks/task/versions/2":
                body = json.dumps(
                    source.status("task", "--versions", "--version", "2")
                ).encode()
            else:
                params = {
                    k: v[0]
                    for k, v in parse_qs(parsed.query, keep_blank_values=True).items()
                }
                if "version" in params:
                    params["version"] = int(params["version"])
                data = source.get(parsed.path, **params)
                if parsed.path == "/tasks/task/files":
                    for entry in data["files"]:
                        entry["url"] = api + "/bytes/" + entry["path"]
                body = json.dumps(data).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    api = f"http://127.0.0.1:{server.server_port}"
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv("ODDISH_API_KEY", "fixture-key")
    monkeypatch.setenv("PYTHONPATH", str(SCRIPT.parents[5]))
    cli = tmp_path / "oddish"
    cli.write_text(f"#!{sys.executable}\nfrom oddish.cli import app\napp()\n")
    cli.chmod(0o755)
    packet_dir = tmp_path / "packet"

    def run(*args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )

    try:
        result = run(
            "collect",
            "task-1",
            "--api",
            api,
            "--out",
            str(packet_dir),
            "--cli",
            str(cli),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "QA_ANSWER_SENTINEL" not in result.stdout
        path = packet_dir / "review.json"
        output = packet_dir / "approved.csv"
        assert run("export", str(path), "--out", str(output)).returncode == 1
        assert not output.exists()
        data = json.loads(path.read_text())
        # Simulates a human decision for this controlled fixture, not a live approval.
        approve(data["cases"][0])
        review.save_json(path, data)
        assert run("render", str(path)).returncode == 0
        result = run(
            "export",
            str(path),
            "--out",
            str(output),
            "--verify-remote",
            "--cli",
            str(cli),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        from oddish.cli.qa_goldens import _table_rows

        imported = _table_rows(output.read_text())
        assert len(imported) == 1
        assert imported[0].subject == "task-1"
        assert imported[0].expected == {"classification": "GOOD_FAILURE"}
        assert requests
        assert all(
            auth is None for path, auth in requests if path.startswith("/bytes/")
        )
        assert all(
            auth == "Bearer fixture-key"
            for path, auth in requests
            if not path.startswith("/bytes/")
        )
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
