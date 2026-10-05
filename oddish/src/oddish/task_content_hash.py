"""Task content hash shared by the CLI and the server.

The CLI hashes the folder it uploads; the ``TASK_EXPAND`` worker hashes the
uploaded archive with the same function, so the server has its own value
instead of trusting the client's.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import tempfile
from pathlib import Path

from harbor.models.task.config import TaskConfig
from harbor.publisher.packager import Packager

# Stored next to each server-computed hash. Change it whenever the bytes fed
# to the hasher change, so old and new values are never compared.
TASK_CONTENT_HASH_ALGORITHM = "sha256-harbor-publishable-v1"

_TASK_TOML_RUNTIME_FIELDS = (
    "schema_version",
    "verifier",
    "agent",
    "environment",
    "solution",
    "multi_step_reward_strategy",
    "steps",
    "artifacts",
)


def _canonical_task_config_bytes(config_path: Path) -> bytes:
    """Serialize only the task config fields that affect Harbor execution."""
    config = TaskConfig.model_validate_toml(config_path.read_text())
    data = config.model_dump(mode="json", exclude_none=True)
    runtime_data = {key: data[key] for key in _TASK_TOML_RUNTIME_FIELDS if key in data}
    return json.dumps(runtime_data, sort_keys=True, separators=(",", ":")).encode()


def compute_task_content_hash(task_path: Path) -> str:
    """Deterministic SHA-256 of a task's execution-relevant contents.

    Uses Harbor's publishable-file selection (including .gitignore handling),
    but hashes task.toml semantically so descriptive metadata edits do not
    create new Oddish task versions.
    """
    hasher = hashlib.sha256()
    task_path = task_path.resolve()
    for file_path in Packager.collect_files(task_path):
        rel = file_path.relative_to(task_path).as_posix()
        if rel == "README.md":
            continue
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\0")
        if rel == "task.toml":
            hasher.update(_canonical_task_config_bytes(file_path))
        else:
            hasher.update(file_path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def compute_task_archive_content_hash(archive_bytes: bytes) -> str:
    """Hash an uploaded ``.oddish-task.tar.gz`` as the CLI hashed its folder.

    The CLI archives every entry of the task folder at the archive root, so
    unpacking it and applying the same file selection reproduces the CLI's
    value. The ``data`` filter refuses absolute paths, links leaving the
    folder, and device files.
    """
    with tempfile.TemporaryDirectory(prefix="oddish-task-hash-") as tmp:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
            tar.extractall(tmp, filter="data")
        return compute_task_content_hash(Path(tmp))
