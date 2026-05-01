"""Build tar.gz archives of trial artifacts for the agent-sandbox-service."""
from __future__ import annotations

import io
import tarfile

from oddish.db.storage import StorageClient


# Heavy / binary noise that adds no chat value. Mirrors what cc-chat's
# orchestrator already skips.
_DEFAULT_SKIP_PARTS = frozenset(
    {"sessions", "skills", "backups", "node_modules", "__pycache__"}
)
_DEFAULT_SKIP_SUFFIXES = (".sqlite", ".sqlite-journal", ".db", ".pyc", ".so")


async def build_trial_archive(
    storage: StorageClient,
    *,
    experiment_id: str,
    skip_path_parts: frozenset[str] = _DEFAULT_SKIP_PARTS,
    skip_suffixes: tuple[str, ...] = _DEFAULT_SKIP_SUFFIXES,
) -> bytes:
    """Build a tar.gz of every trial artifact under tasks/{experiment_id}/trials/.

    Member names are relative to that prefix (e.g., "trial_1/result.json")."""
    prefix = f"tasks/{experiment_id}/trials/"
    keys = await storage.list_keys(prefix)

    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for key in sorted(keys):
            rel = key[len(prefix):]
            if not rel:
                continue
            if any(part in skip_path_parts or part.startswith(".") for part in rel.split("/") if part):
                continue
            if rel.endswith(skip_suffixes):
                continue
            content = await storage.download_bytes(key)
            info = tarfile.TarInfo(name=rel)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))

    return out.getvalue()


async def build_probe_trials_archive(
    storage: StorageClient,
    *,
    task_id: str,
    task,  # TaskModel instance
    probe_trials: list,  # list of TrialModel rows, all .harbor_config.mode=='probe'
    skip_path_parts: frozenset[str] = _DEFAULT_SKIP_PARTS,
    skip_suffixes: tuple[str, ...] = _DEFAULT_SKIP_SUFFIXES,
) -> bytes:
    """Bundle task source (under task/) + per-trial artifacts (under jobs/{trial_id}/).

    Used by agent-sandbox-service's HttpxOddishClient.get_task_probe_bundle."""
    from pathlib import Path

    from api.services.task_archive import build_task_archive

    out = io.BytesIO()

    # Resolve task source archive
    task_archive = None
    if task.task_path:
        task_path = Path(task.task_path)
        if not task_path.is_absolute():
            task_path = Path.home() / task.task_path
        if task_path.is_dir():
            task_archive = build_task_archive(task_path, fallback_name=task.name)

    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        # Inline task source under task/
        if task_archive is not None:
            with tarfile.open(fileobj=io.BytesIO(task_archive.body), mode="r:gz") as inner:
                for member in inner.getmembers():
                    if not member.isfile():
                        continue
                    f = inner.extractfile(member)
                    if f is None:
                        continue
                    body = f.read()
                    new_info = tarfile.TarInfo(name=f"task/{member.name}")
                    new_info.size = len(body)
                    tar.addfile(new_info, io.BytesIO(body))

        # Per-trial artifacts under jobs/{trial_id}/
        for trial in probe_trials:
            prefix = f"tasks/{task_id}/trials/{trial.id}/"
            keys = await storage.list_keys(prefix)
            for key in sorted(keys):
                rel = key[len(prefix):]
                if not rel:
                    continue
                if any(
                    part in skip_path_parts or part.startswith(".")
                    for part in rel.split("/")
                    if part
                ):
                    continue
                if rel.endswith(skip_suffixes):
                    continue
                content = await storage.download_bytes(key)
                info = tarfile.TarInfo(name=f"jobs/{trial.id}/{rel}")
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))

    return out.getvalue()
