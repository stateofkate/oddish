"""Fakes the launcher tests share: Modal's SDK and a stand-in oddish folder."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


class _Aio:
    """Stands in for a Modal SDK method: only ``.aio`` is called."""

    def __init__(self, fn):
        self.aio = fn


@pytest.fixture
def fake_modal(monkeypatch):
    """Modal's SDK with no call leaving the process. ``Sandbox.create`` records
    ``(args, kwargs)`` in ``created`` and returns ``sandbox`` (``sb-1``), whose
    ``exec`` exits ``exit_code``, whose ``snapshot_filesystem`` records its
    arguments in ``snapshots`` and returns ``im-warm``, and whose ``terminate``
    counts in ``terminated``. ``Secret.from_dict`` records each env in
    ``secrets``. ``ModalStarter._image`` passes the image through."""
    import modal

    from oddish.workers.launcher.providers.modal import ModalStarter

    fake = SimpleNamespace(
        created=[], secrets=[], snapshots=[], terminated=0, exit_code=0
    )

    async def _wait():
        return fake.exit_code

    async def _exec(*args):
        return SimpleNamespace(wait=_Aio(_wait))

    async def _snapshot(**kwargs):
        fake.snapshots.append(kwargs)
        return SimpleNamespace(object_id="im-warm")

    async def _terminate():
        fake.terminated += 1

    fake.sandbox = SimpleNamespace(
        object_id="sb-1",
        exec=_Aio(_exec),
        snapshot_filesystem=_Aio(_snapshot),
        terminate=_Aio(_terminate),
    )

    async def _lookup(*args, **kwargs):
        return "app"

    async def _create(*args, **kwargs):
        fake.created.append((args, kwargs))
        return fake.sandbox

    def _secret(env):
        fake.secrets.append(dict(env))
        return "secret"

    monkeypatch.setattr(modal.App, "lookup", _Aio(_lookup))
    monkeypatch.setattr(modal.Sandbox, "create", _Aio(_create))
    monkeypatch.setattr(modal.Secret, "from_dict", _secret)
    monkeypatch.setattr(ModalStarter, "_image", staticmethod(lambda image: image))
    return fake


@pytest.fixture
def write_tree(tmp_path):
    """Writes ``{relative path: text}`` under ``tmp_path`` and returns that
    folder: a stand-in oddish folder, or the part of one a test needs."""

    def write(files: dict[str, str]) -> Path:
        for relative, text in files.items():
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return tmp_path

    return write
