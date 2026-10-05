"""The server's archive hash matches the CLI's folder hash, and is recorded."""

import io
import tarfile
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

from oddish.cli.api import archive_task_dir
from oddish.db import TaskModel, TaskVersionFingerprintModel, TaskVersionModel
from oddish.task_content_hash import (
    TASK_CONTENT_HASH_ALGORITHM,
    compute_task_archive_content_hash,
    compute_task_content_hash,
)
from oddish.workers.queue import task_expand_handler

TASK_TOML = """version = "1.0"

[task]
name = "abundant/word-counter"

[metadata]
difficulty = "easy"

[verifier]
timeout_sec = 120.0

[agent]
timeout_sec = 300.0

[environment]
build_timeout_sec = 600.0
"""


def _task_folder(root):
    files = {
        "task.toml": TASK_TOML,
        "instruction.md": "Count words.\n",
        "README.md": "Not hashed.\n",
        ".gitignore": "*.log\n",
        "environment/Dockerfile": "FROM ubuntu:24.04\n",
        "tests/test.sh": "#!/bin/sh\nexit 0\n",
        "tests/run.log": "ignored by .gitignore\n",
        "solution/solve.sh": "#!/bin/sh\nwc -w\n",
        "notes.txt": "outside Harbor's publishable folders\n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def test_archive_hash_matches_the_cli_folder_hash(tmp_path):
    folder = _task_folder(tmp_path / "word-counter")

    archive = archive_task_dir(folder).read_bytes()

    assert compute_task_archive_content_hash(archive) == compute_task_content_hash(
        folder
    )


def test_archive_hash_changes_with_executed_files(tmp_path):
    folder = _task_folder(tmp_path / "word-counter")
    before = compute_task_archive_content_hash(archive_task_dir(folder).read_bytes())

    (folder / "README.md").write_text("Edited readme.\n")
    (folder / "tests" / "run.log").write_text("still ignored\n")
    assert (
        compute_task_archive_content_hash(archive_task_dir(folder).read_bytes())
        == before
    )

    (folder / "tests" / "test.sh").write_text("#!/bin/sh\nexit 1\n")
    assert (
        compute_task_archive_content_hash(archive_task_dir(folder).read_bytes())
        != before
    )


def test_archive_with_a_path_outside_the_folder_is_refused():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))

    with pytest.raises(tarfile.FilterError):
        compute_task_archive_content_hash(buffer.getvalue())


class _Storage:
    async def upload_bytes(self, *_args, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_promotion_records_replaces_and_clears_the_fingerprint(
    session, monkeypatch
):
    task = TaskModel(
        id="fp-task-1a2b3c4d", name="fp-task", user="tester", task_path="s3://t/"
    )
    session.add(task)
    await session.flush()
    session.add(
        TaskVersionModel(
            id="fp-task-1a2b3c4d-v1",
            task_id=task.id,
            version=1,
            task_path="s3://tasks/fp-task-1a2b3c4d/v1/",
            content_hash="client-hash",
        )
    )
    await session.flush()

    @asynccontextmanager
    async def _get_session():
        yield session

    # The test session rolls back at teardown; keep commits inside it.
    monkeypatch.setattr(session, "commit", session.flush)
    monkeypatch.setattr(task_expand_handler, "get_session", _get_session)

    async def _promote(server_hash):
        return await task_expand_handler._promote_expansion_if_current(
            _Storage(),
            task_id="fp-task-1a2b3c4d",
            version=1,
            expected_content_hash="client-hash",
            manifest_key="tasks/fp-task-1a2b3c4d/v1-expanded/x/.oddish-manifest.json",
            manifest_bytes=b"{}",
            server_content_hash=server_hash,
        )

    async def _fingerprints():
        session.expire_all()
        return (await session.scalars(select(TaskVersionFingerprintModel))).all()

    assert await _promote("server-hash-1") is True
    [row] = await _fingerprints()
    assert row.task_version_id == "fp-task-1a2b3c4d-v1"
    assert row.content_hash == "server-hash-1"
    assert row.client_content_hash == "client-hash"
    assert row.algorithm == TASK_CONTENT_HASH_ALGORITHM

    assert await _promote("server-hash-2") is True
    [row] = await _fingerprints()
    assert row.content_hash == "server-hash-2"

    # An overwrite whose archive cannot be hashed drops the old archive's row.
    assert await _promote(None) is True
    assert await _fingerprints() == []
