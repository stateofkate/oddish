"""oddish.mon runs inside the sandbox, where Postgres is not reachable."""

from __future__ import annotations

import ast
from pathlib import Path

import oddish.mon

_FORBIDDEN = ("oddish.db", "sqlalchemy", "asyncpg")


def test_mon_package_never_imports_the_database():
    offenders = []
    for path in Path(oddish.mon.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders += [
                f"{path.name}: {name}" for name in names if name.startswith(_FORBIDDEN)
            ]
    assert offenders == []


def test_attempt_spec_has_no_secret_fields():
    from oddish.mon.spec import AttemptSpec

    secretish = ("token", "secret", "key", "credential", "password", "env")
    fields = set(AttemptSpec.model_fields) - {"task_archive_url", "task_files"}
    assert [f for f in fields if any(word in f for word in secretish)] == []


def test_task_fetch_plan_matches_every_form_the_worker_downloads():
    from oddish.db.storage import StorageClient
    from oddish.workers.queue.monitor_attempt import select_task_objects

    name = StorageClient._TASK_ARCHIVE_OBJECT_NAME
    prefix = "tasks/abc/"
    top = StorageClient._task_archive_key_from_prefix(prefix)

    # 1. archive at the prefix wins over anything else
    assert select_task_objects(prefix, [top, f"{prefix}task.toml"]) == (top, [])
    # 2. else the newest archive at a versioned sub-path
    nested = [f"{prefix}v1/{name}", f"{prefix}v2/{name}"]
    assert select_task_objects(prefix, [*nested, f"{prefix}task.toml"]) == (
        f"{prefix}v2/{name}",
        [],
    )
    # 3. else every loose file, skipping the bare prefix object
    loose = [prefix, f"{prefix}task.toml", f"{prefix}environment/Dockerfile"]
    assert select_task_objects(prefix, loose) == (None, loose[1:])


def _loaded_after_import(module: str) -> set[str]:
    import subprocess
    import sys

    code = f"import sys, {module}; print('\\n'.join(sys.modules))"
    out = subprocess.run(
        [sys.executable, "-c", code], check=True, capture_output=True, text=True
    ).stdout
    return set(out.split())


def test_hook_summaries_come_from_the_shared_spec_not_the_queue():
    """Seen in review: hooks took summarize_hook_event from
    workers.queue.monitor_lifecycle, which pulls in monitor_token and the
    database stack. The AST walk above only sees oddish.mon's own imports."""
    spec = _loaded_after_import("oddish.mon.spec")
    assert sorted(name for name in spec if name.startswith(_FORBIDDEN)) == []

    hooks = _loaded_after_import("oddish.mon.hooks")
    assert "oddish.workers.queue.monitor_lifecycle" not in hooks
    assert "oddish.workers.queue.monitor_token" not in hooks
    # Known debt, not asserted: hooks and runner still load sqlalchemy and
    # oddish.db through oddish.workers.harbor (live_tail -> oddish.workers
    # package -> harbor.runner -> costs.modal_cost -> oddish.db.models).
