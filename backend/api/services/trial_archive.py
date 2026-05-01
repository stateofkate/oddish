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
