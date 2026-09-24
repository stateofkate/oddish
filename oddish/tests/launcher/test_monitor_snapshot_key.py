"""The snapshot key covers what a snapshot holds and nothing else: a code-only
deploy keeps every snapshot (each boot gets the launcher's code instead), and a
change to how snapshots are made warms them again."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from oddish.workers.launcher import image_cache as cache

ODDISH = Path(__file__).resolve().parents[2]  # the folder holding mon.Dockerfile

# Monitor code a snapshot boot replaces, and files no snapshot holds.
_NOT_IN_THE_KEY = (
    "src/oddish/mon/runner.py",
    "src/oddish/mon/client.py",
    "src/oddish/workers/launcher/launch.py",
    "src/oddish/harbor-pin.toml",
    "scripts/mon-boot.sh",
    "README.md",
)


@pytest.fixture
def oddish(write_tree):
    """A stand-in oddish folder with every key input and code outside the key."""
    inputs = ("mon.Dockerfile", *cache.SNAPSHOT_BASE, *cache.WARM_RECIPE)
    root = write_tree({name: f"# {name}\n" for name in (*inputs, *_NOT_IN_THE_KEY)})
    cache.snapshot_base_identity.cache_clear()
    yield root
    cache.snapshot_base_identity.cache_clear()


def _key(oddish: Path, provider: str = "modal", task: str = "task|v1|") -> str:
    cache.snapshot_base_identity.cache_clear()
    return cache.cache_key(provider, str(oddish / "mon.Dockerfile"), task)


def test_a_code_only_deploy_keeps_every_snapshot(oddish):
    before = _key(oddish)
    for relative in _NOT_IN_THE_KEY:
        (oddish / relative).write_text("changed by a deploy\n")
    (oddish / "src/oddish/mon/new_module.py").write_text("x = 1\n")
    assert _key(oddish) == before


@pytest.mark.parametrize(
    "relative", ["mon.Dockerfile", *cache.SNAPSHOT_BASE, *cache.WARM_RECIPE]
)
def test_what_a_snapshot_holds_is_in_the_key(oddish, relative):
    before = _key(oddish)
    (oddish / relative).write_text("changed\n")
    assert _key(oddish) != before


def test_a_listed_file_the_tree_lacks_counts_once_it_appears(oddish):
    listed = oddish / cache.WARM_RECIPE[0]
    listed.unlink()
    before = _key(oddish)
    listed.write_text("added by a later change\n")
    assert _key(oddish) != before


def test_bumping_the_epoch_warms_every_snapshot_again(oddish, monkeypatch):
    before = _key(oddish)
    monkeypatch.setattr(cache, "SNAPSHOT_EPOCH", cache.SNAPSHOT_EPOCH + 1)
    assert _key(oddish) != before


def test_the_provider_and_the_task_are_in_the_key(oddish):
    base = _key(oddish)
    assert base == _key(oddish)
    assert base != _key(oddish, provider="daytona")
    assert base != _key(oddish, task="task|v2|")


def test_an_image_reference_is_keyed_by_its_name_as_before():
    cache.snapshot_base_identity.cache_clear()
    one = cache.cache_key("modal", "ghcr.io/x/oddish-mon:1", "task|v1|")
    assert one == cache.cache_key("modal", "ghcr.io/x/oddish-mon:1", "task|v1|")
    assert one != cache.cache_key("modal", "ghcr.io/x/oddish-mon:2", "task|v1|")


# --- the warm recipe is every file the warm-up imports ------------------------


def _module_file(src: Path, module: str) -> Path | None:
    base = src.joinpath(*module.split("."))
    for candidate in (base / "__init__.py", base.with_suffix(".py")):
        if candidate.is_file():
            return candidate
    return None


def _imports(path: Path, module: str, *, top_level_only: bool = False) -> set[str]:
    """The oddish names one file imports: nested imports too, unless
    ``top_level_only``; relative imports resolved."""
    tree = ast.parse(path.read_text())
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    names: set[str] = set()
    for node in tree.body if top_level_only else ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                parts = parts[: len(parts) - node.level + 1]
                base = ".".join([*parts, *([node.module] if node.module else [])])
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return {name for name in names if name.split(".")[0] == "oddish"}


def warm_up_imports(src: Path) -> set[str]:
    """Files ``python -m oddish.mon warm`` imports, relative to ``src``'s parent:
    ``oddish.mon.__main__`` and its module-level imports, then ``oddish.mon.warm``
    (its warm command's import) and everything that imports, at any depth, with
    the parent packages Python runs first. The imports ``__main__`` makes inside
    its other commands (the monitor's run) are not the warm-up's."""
    main = "oddish.mon.__main__"
    queue = [main, "oddish.mon.warm"]
    queue += _imports(_module_file(src, main), main, top_level_only=True)
    seen: set[str] = set()
    while queue:
        parts = queue.pop().split(".")
        for depth in range(1, len(parts) + 1):
            module = ".".join(parts[:depth])
            path = _module_file(src, module)
            if path is None or module in seen:
                continue
            seen.add(module)
            if module != main:
                queue += _imports(path, module)
    return {
        _module_file(src, module).relative_to(src.parent).as_posix() for module in seen
    }


def test_the_warm_recipe_is_every_file_the_warm_up_imports():
    imported = warm_up_imports(ODDISH / "src")
    listed = set(cache.WARM_RECIPE)
    assert sorted(imported - listed) == [], (
        "The warm-up imports files the snapshot key does not cover: add them to "
        "image_cache.WARM_RECIPE, or snapshots made the old way keep being served."
    )
    # A listed file this tree does not have hashes as nothing (one a stacked
    # change adds); one it has that the warm-up does not import is churn.
    stale = sorted(f for f in listed - imported if (ODDISH / f).exists())
    assert stale == [], (
        "No longer imported by the warm-up: remove from image_cache.WARM_RECIPE, "
        "or every change to them warms every snapshot again for nothing."
    )


def test_a_module_the_warm_up_starts_importing_is_found(write_tree):
    """The check itself: a module the warm-up imports lazily, through a relative
    import, is found; the monitor's run path, which the warm-up never loads, is
    not."""
    files = {
        "oddish/__init__.py": "",
        "oddish/mon/__init__.py": "",
        "oddish/mon/__main__.py": (
            "from oddish.mon import env\n"
            "def main(command):\n"
            "    if command == 'run':\n"
            "        from oddish.mon.runner import run\n"
            "    from oddish.mon.warm import run as warm\n"
        ),
        "oddish/mon/env.py": "",
        "oddish/mon/runner.py": "from oddish.mon import driver\n",
        "oddish/mon/driver.py": "",
        "oddish/mon/warm.py": "from . import task_image\nimport harbor\n",
        "oddish/mon/task_image.py": (
            "def build():\n    from oddish.mon.compose_build import build_all\n"
        ),
        "oddish/mon/compose_build.py": "from ..mon import builder\n",
        "oddish/mon/builder.py": "",
    }
    root = write_tree({f"src/{relative}": text for relative, text in files.items()})

    assert warm_up_imports(root / "src") == {
        f"src/oddish/{name}"
        for name in (
            "__init__.py",
            "mon/__init__.py",
            "mon/__main__.py",
            "mon/env.py",
            "mon/warm.py",
            "mon/task_image.py",
            "mon/compose_build.py",
            "mon/builder.py",
        )
    }
