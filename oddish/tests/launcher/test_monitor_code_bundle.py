"""A snapshot boot runs the launcher's own monitor code: the bundle is the same
bytes for the same files, is uploaded once, reaches the sandbox through its
environment, and only a cached snapshot's start gets one."""

from __future__ import annotations

import hashlib
import io
import logging
import os
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from oddish.config import settings
from oddish.mon.contract import BUNDLE_SHA256_VAR, BUNDLE_URL_VAR
from oddish.workers.launcher import code_bundle, image_cache
from oddish.workers.launcher import launch as ml
from oddish.workers.launcher.image_cache import Resolved
from oddish.workers.launcher.providers.base import CodeBundle

ODDISH = Path(__file__).resolve().parents[2]  # the folder holding mon.Dockerfile
SIGNED = "X-Amz-Signature=do-not-log"


@pytest.fixture
def root(write_tree) -> Path:
    """A stand-in oddish folder, as the worker image holds it."""
    root = write_tree(
        {
            "mon.Dockerfile": "FROM python:3.13-slim-bookworm\n",
            "pyproject.toml": "[project]\nname = 'oddish'\n",
            "uv.lock": "version = 1\n",
            "README.md": "not the monitor's code\n",
            "scripts/mon-entrypoint.sh": "#!/bin/sh\nexec python -m oddish.mon run\n",
            "scripts/mon-boot.sh": "#!/bin/sh\necho boot\n",
            "src/oddish/__init__.py": "",
            "src/oddish/mon/runner.py": "x = 1\n",
            "src/oddish/harbor-pin.toml": "rev = 'a'\n",
            "src/oddish/assets/tool": "#!/bin/sh\n",
        }
    )
    (root / "src/oddish/assets/tool").chmod(0o755)
    return root


def _members(data: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        return {member.name: member for member in tar.getmembers()}


# --- the bundle ---------------------------------------------------------------


def test_the_bundle_is_the_monitor_code_and_nothing_else(root):
    members = _members(code_bundle.build(root))
    assert sorted(members) == [
        "pyproject.toml",
        "scripts/mon-entrypoint.sh",
        "src/oddish/__init__.py",
        "src/oddish/assets/tool",
        "src/oddish/harbor-pin.toml",
        "src/oddish/mon/runner.py",
        "uv.lock",
    ]  # the boot script travels as the start command, not in the bundle
    assert members["src/oddish/assets/tool"].mode == 0o755
    assert members["src/oddish/mon/runner.py"].mode == 0o644


def test_the_same_files_make_the_same_bytes_whatever_the_bytecode_or_timestamps(
    root,
):
    first = code_bundle.build(root)
    # What differs between two launcher containers of one deploy: bytecode the
    # launcher wrote into its own src, and the files' timestamps.
    (root / "src/oddish/__pycache__").mkdir()
    (root / "src/oddish/__pycache__/__init__.cpython-313.pyc").write_bytes(b"\0")
    (root / "src/oddish/mon/stale.pyc").write_bytes(b"\0")
    for path in root.rglob("*"):
        os.utime(path, (1_700_000_000, 1_700_000_000))
    assert code_bundle.build(root) == first
    member = _members(first)["uv.lock"]
    assert (member.mtime, member.uid, member.gid, member.uname) == (0, 0, 0, "")


def test_a_change_to_any_file_is_a_new_bundle_non_python_files_included(root):
    before = hashlib.sha256(code_bundle.build(root)).hexdigest()
    (root / "src/oddish/harbor-pin.toml").write_text("rev = 'b'\n")
    assert hashlib.sha256(code_bundle.build(root)).hexdigest() != before


def test_the_real_bundle_holds_the_monitor_without_bytecode():
    data = code_bundle.build(ODDISH)
    members = _members(data)
    for name in (
        "src/oddish/mon/__main__.py",
        "src/oddish/harbor-pin.toml",
        "scripts/mon-entrypoint.sh",
        "pyproject.toml",
        "uv.lock",
    ):
        assert name in members
    assert [n for n in members if "__pycache__" in n or n.endswith(".pyc")] == []
    assert code_bundle.build(ODDISH) == data


def test_only_a_dockerfile_the_launcher_has_is_a_code_root(root):
    assert image_cache.source_root(str(root / "mon.Dockerfile")) == root
    assert image_cache.source_root("ghcr.io/abundant-ai/oddish-mon:1") is None
    assert image_cache.source_root(str(root / "gone" / "mon.Dockerfile")) is None


def test_the_link_outlives_the_longest_create():
    # The sandbox fetches as it boots, after a create that may take this long.
    assert code_bundle.URL_SECONDS > settings.monitor_daytona_create_timeout_seconds


# --- upload once, presign per boot ---------------------------------------------


class _Storage:
    """The storage client calls ``prepare`` makes, against a dict."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.uploads: list[tuple[str, str | None]] = []
        self.presigned: list[tuple[str, int]] = []

    async def object_exists(self, key):
        return key in self.objects

    async def upload_bytes(self, data, key, *, content_type=None):
        self.uploads.append((key, content_type))
        self.objects[key] = data

    async def get_presigned_url(self, key, expiration=3600):
        self.presigned.append((key, expiration))
        return f"https://bucket.test/{key}?{SIGNED}"


@pytest.fixture
def storage(monkeypatch):
    fake = _Storage()
    monkeypatch.setattr(code_bundle, "get_storage_client", lambda: fake)
    code_bundle._built.cache_clear()
    yield fake
    code_bundle._built.cache_clear()


@pytest.mark.asyncio
async def test_a_bundle_is_uploaded_once_and_presigned_for_every_boot(root, storage):
    first = await code_bundle.prepare(root)
    second = await code_bundle.prepare(root)

    key = code_bundle.object_key(first.sha256)
    assert key == f"monitor-image-cache/bundles/{first.sha256}.tar.gz"
    assert storage.uploads == [(key, "application/gzip")]
    assert storage.objects[key] == code_bundle.build(root)
    assert hashlib.sha256(storage.objects[key]).hexdigest() == first.sha256
    assert storage.presigned == [(key, code_bundle.URL_SECONDS)] * 2
    assert second.sha256 == first.sha256
    assert first.env() == {BUNDLE_URL_VAR: first.url, BUNDLE_SHA256_VAR: first.sha256}
    assert first.boot_script == (root / "scripts/mon-boot.sh").read_text()
    assert SIGNED not in repr(first)  # a logged bundle never shows the signature


@pytest.mark.asyncio
async def test_a_bundle_already_in_the_bucket_is_not_uploaded_again(root, storage):
    sha256 = hashlib.sha256(code_bundle.build(root)).hexdigest()
    storage.objects[code_bundle.object_key(sha256)] = b"uploaded by another launch"
    bundle = await code_bundle.prepare(root)
    assert bundle.sha256 == sha256 and storage.uploads == []


# --- only a cached snapshot's start gets one -----------------------------------


class _Starter:
    provider = "daytona"

    def __init__(self, *, exists=True, fail_first=False):
        self.exists, self.fail_first = exists, fail_first
        self.starts: list[tuple[str, CodeBundle | None, dict]] = []

    async def start(self, *, name, image, env, size=None, **code):
        self.starts.append((image, code.get("bundle"), env))
        if self.fail_first and len(self.starts) == 1:
            raise RuntimeError("snapshot not found")
        return f"sbx-{len(self.starts)}"

    async def warm_image_exists(self, image):
        return self.exists


_JOB = SimpleNamespace(id="wj-1", attempts=1)


async def _never_warmed_again():
    raise AssertionError("warmed again")


@pytest.fixture
def launcher(root, monkeypatch, storage):
    """The launcher with its own oddish folder on disk, as in the worker image."""
    monkeypatch.setattr(ml.settings, "monitor_image", str(root / "mon.Dockerfile"))
    forgotten: list[str] = []

    async def _forget(starter, key):
        forgotten.append(key)

    monkeypatch.setattr(ml.monitor_image_cache, "forget", _forget)
    return SimpleNamespace(root=root, forgotten=forgotten, storage=storage)


@pytest.mark.asyncio
async def test_a_cached_snapshot_boots_with_the_launchers_code(launcher, caplog):
    starter = _Starter()
    with caplog.at_level(logging.INFO):
        external_id = await ml._start_from_cache(
            starter,
            _JOB,
            "snapshot:s1",
            "key-1",
            {"ODDISH_MON_TOKEN": "t"},
            None,
            rewarm=_never_warmed_again,
        )

    ((image, bundle, env),) = starter.starts
    assert (external_id, image) == ("sbx-1", "snapshot:s1")
    assert bundle.sha256 == hashlib.sha256(code_bundle.build(launcher.root)).hexdigest()
    assert env == {"ODDISH_MON_TOKEN": "t"}  # the provider adds bundle.env() itself
    assert "code_source=bundle" in caplog.text
    assert SIGNED not in caplog.text


@pytest.mark.asyncio
async def test_a_plain_image_start_is_unchanged(launcher, caplog):
    starter = _Starter()
    with caplog.at_level(logging.INFO):
        await ml._start_from_cache(
            starter,
            _JOB,
            ml.settings.monitor_image,
            None,
            {},
            None,
            rewarm=_never_warmed_again,
        )
    assert [(image, bundle) for image, bundle, _ in starter.starts] == [
        (ml.settings.monitor_image, None)
    ]
    assert launcher.storage.uploads == launcher.storage.presigned == []
    assert "code_source=image" in caplog.text


@pytest.mark.asyncio
async def test_a_gone_snapshot_is_warmed_again_and_boots_with_the_bundle(launcher):
    starter = _Starter(exists=False, fail_first=True)

    async def _rewarm():
        return Resolved("snapshot:s2", "key-1", "warmed")

    await ml._start_from_cache(
        starter, _JOB, "snapshot:s1", "key-1", {}, None, rewarm=_rewarm
    )
    sha256 = hashlib.sha256(code_bundle.build(launcher.root)).hexdigest()
    assert [(image, bundle.sha256) for image, bundle, _ in starter.starts] == [
        ("snapshot:s1", sha256),
        ("snapshot:s2", sha256),  # the replacement gets the current code too
    ]
    assert launcher.forgotten == ["key-1"]


@pytest.mark.asyncio
async def test_a_bundle_that_cannot_be_prepared_never_boots_the_older_code(
    launcher, monkeypatch
):
    async def _bucket_down(key):
        raise ConnectionError("bucket unreachable")

    monkeypatch.setattr(launcher.storage, "object_exists", _bucket_down)
    starter = _Starter(exists=True)
    with pytest.raises(ConnectionError, match="bucket unreachable"):
        await ml._start_from_cache(
            starter, _JOB, "snapshot:s1", "key-1", {}, None, rewarm=_never_warmed_again
        )
    assert starter.starts == []  # the launch fails; the snapshot stays
    assert launcher.forgotten == []


@pytest.mark.asyncio
async def test_a_snapshot_of_an_image_reference_boots_as_before(monkeypatch, storage):
    monkeypatch.setattr(ml.settings, "monitor_image", "ghcr.io/abundant-ai/mon:1")
    starter = _Starter()
    await ml._start_from_cache(
        starter, _JOB, "im-snap", "key-1", {}, None, rewarm=_never_warmed_again
    )
    assert [(image, bundle) for image, bundle, _ in starter.starts] == [
        ("im-snap", None)
    ]
    assert storage.uploads == []
