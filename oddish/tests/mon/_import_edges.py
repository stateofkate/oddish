"""Record which module imported which while the monitor's modules load.

Run as a script in a fresh interpreter (pytest's own process has already
loaded the database through conftest). Prints one JSON object: the executed
import edges out of ``oddish`` modules, parent packages included, and every
module loaded. Edges are what ``__import__`` saw at import time; imports made
later, inside functions, are not recorded.
"""

from __future__ import annotations

import builtins
import importlib
import json
import pkgutil
import sys

# What the monitor process loads before it drives a trial: every module of
# the package, and the driver ``mon.runner`` imports once the task is fetched.
ROOTS = ("oddish.mon", "oddish.workers.harbor.runner")


def main() -> None:
    calls: list[tuple[str, str, tuple[str, ...]]] = []
    real_import = builtins.__import__

    def record(name, globals=None, locals=None, fromlist=(), level=0):
        scope = globals or {}
        importer = scope.get("__name__", "")
        target = name
        if level:
            package = scope.get("__package__") or ""
            base = package.rsplit(".", level - 1)[0] if level > 1 else package
            target = f"{base}.{name}" if name else base
        calls.append((importer, target, tuple(fromlist or ())))
        return real_import(name, globals, locals, fromlist, level)

    builtins.__import__ = record
    try:
        package = importlib.import_module(ROOTS[0])
        for info in pkgutil.walk_packages(package.__path__, f"{ROOTS[0]}."):
            importlib.import_module(info.name)
        for root in ROOTS[1:]:
            importlib.import_module(root)
    finally:
        builtins.__import__ = real_import

    edges: set[tuple[str, str]] = set()
    for importer, target, fromlist in calls:
        if not importer.startswith("oddish"):
            continue
        parts = target.split(".")
        for depth in range(1, len(parts) + 1):
            edges.add((importer, ".".join(parts[:depth])))
        for item in fromlist:
            if f"{target}.{item}" in sys.modules:
                edges.add((importer, f"{target}.{item}"))
    json.dump(
        {"edges": sorted(edges), "loaded": sorted(sys.modules)},
        sys.stdout,
    )


if __name__ == "__main__":
    main()
