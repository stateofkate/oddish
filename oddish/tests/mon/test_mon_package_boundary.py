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
    from oddish.mon.contract import AttemptSpec

    secretish = ("token", "secret", "key", "credential", "password", "env")
    fields = set(AttemptSpec.model_fields) - {"task_archive_url", "task_files"}
    assert [f for f in fields if any(word in f for word in secretish)] == []


def test_task_fetch_plan_matches_every_form_the_worker_downloads():
    from oddish.db.storage import StorageClient
    from oddish.workers.launcher.attempt import select_task_objects

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
    spec = _loaded_after_import("oddish.mon.contract")
    assert sorted(name for name in spec if name.startswith(_FORBIDDEN)) == []

    hooks = _loaded_after_import("oddish.mon.hooks")
    assert "oddish.workers.launcher.hook_events" not in hooks
    assert "oddish.workers.launcher.token" not in hooks
    # What hooks still loads through workers.harbor is walked, edge by edge,
    # by test_mon_reaches_the_database_only_through_the_listed_edges.


# --- Transitive boundary -----------------------------------------------------

# Import edges the monitor's transitive closure crosses into the database
# today. Listed one edge at a time so a new one fails, and checked to still
# exist so a paid-down one is removed from here. The mon image keeps the
# server extra until this list is empty.
_DB_EDGE_DEBT = {
    # live_tail persists ticks to Postgres itself; the seam split moves that
    # to live_tail_db and passes persist in.
    ("oddish.workers.harbor.live_tail", "oddish.db"),
    ("oddish.workers.harbor.live_tail", "oddish.db.connection"),
    ("oddish.workers.harbor.live_tail", "sqlalchemy"),
    ("oddish.workers.harbor.live_tail", "sqlalchemy.dialects"),
    ("oddish.workers.harbor.live_tail", "sqlalchemy.dialects.postgresql"),
    # live_tail takes its rich.Console from workers.queue.shared, whose
    # package loads the whole queue. Moves to a neutral module in the seam PR.
    ("oddish.workers.harbor.live_tail", "oddish.workers.queue"),
    ("oddish.workers.harbor.live_tail", "oddish.workers.queue.shared"),
    # quota_control's default refresh/record read and write TrialModel; the
    # seam split moves them to quota_db.
    ("oddish.workers.harbor.quota_control", "oddish.db"),
    ("oddish.workers.harbor.quota_control", "oddish.core.quota_pause"),
    # The driver itself. Removing these is separate, larger work.
    ("oddish.workers.harbor.runner", "oddish.costs"),
    ("oddish.workers.harbor.runner", "oddish.costs.modal_cost"),
    ("oddish.workers.harbor.runner", "oddish.runtime.sandbox_lifecycle"),
    ("oddish.workers.harbor.runner", "oddish.schemas"),
    ("oddish.workers.harbor.runner", "oddish.worker.probe_staging"),
}


def _import_edges() -> tuple[set[tuple[str, str]], set[str]]:
    import json
    import subprocess
    import sys

    script = Path(__file__).with_name("_import_edges.py")
    out = subprocess.run(
        [sys.executable, str(script)], check=True, capture_output=True, text=True
    ).stdout
    data = json.loads(out)
    return {tuple(edge) for edge in data["edges"]}, set(data["loaded"])


def _path_to(target: str, parents: dict[str, str]) -> str:
    chain = [target]
    while chain[-1] in parents:
        chain.append(parents[chain[-1]])
    return " <- ".join(chain)


def test_mon_reaches_the_database_only_through_the_listed_edges():
    """Walk the executed import edges from oddish.mon (and the driver it
    loads), skipping the listed debt. Nothing reached may be oddish.db,
    sqlalchemy or asyncpg: a new edge into them fails here, with its path."""
    edges, loaded = _import_edges()
    assert loaded & {"oddish.db", "sqlalchemy"}  # the walk saw the debt
    stale = sorted(edge for edge in _DB_EDGE_DEBT if edge not in edges)
    assert stale == [], f"paid down, remove from _DB_EDGE_DEBT: {stale}"

    outgoing: dict[str, set[str]] = {}
    for source, target in edges - _DB_EDGE_DEBT:
        outgoing.setdefault(source, set()).add(target)
    roots = [name for name in loaded if name.startswith("oddish.mon")]
    parents: dict[str, str] = {}
    queue = list(roots)
    seen = set(roots)
    while queue:
        source = queue.pop()
        for target in sorted(outgoing.get(source, ())):
            if target not in seen:
                seen.add(target)
                parents[target] = source
                queue.append(target)
    reached = sorted(name for name in seen if name.startswith(_FORBIDDEN))
    assert reached == [], "\n".join(_path_to(name, parents) for name in reached)


# --- Package rules ----------------------------------------------------------


def _imports_in(package_dir: Path) -> list[tuple[str, str]]:
    """Every dotted name imported anywhere in the package, nested ones too."""
    found = []
    for path in sorted(package_dir.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                found += [(path.name, alias.name) for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.append((path.name, node.module))
                found += [(path.name, f"{node.module}.{a.name}") for a in node.names]
    return found


def test_mon_never_imports_the_worker_side():
    import oddish.mon

    worker_side = ("oddish.workers.queue", "oddish.workers.launcher")
    offenders = [
        f"{file}: {name}"
        for file, name in _imports_in(Path(oddish.mon.__file__).parent)
        if name.startswith(worker_side)
    ]
    assert offenders == []


def test_launcher_imports_only_the_contract_from_mon():
    import oddish.workers.launcher

    offenders = [
        f"{file}: {name}"
        for file, name in _imports_in(Path(oddish.workers.launcher.__file__).parent)
        if name.startswith("oddish.mon")
        and not name.startswith("oddish.mon.contract")
        and name != "oddish.mon"
    ]
    assert offenders == []
