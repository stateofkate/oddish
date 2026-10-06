"""An archive member the ``data`` filter rejects is skipped, not fatal: the
rest of the trial's artifacts still unpack."""

import io
import tarfile

import pytest

from oddish.workers.launcher import settle as monitor_settle


def _add(tar, name, *, data=b"", link=None):
    info = tarfile.TarInfo(name)
    if link is not None:
        info.type, info.linkname = tarfile.SYMTYPE, link
    else:
        info.size = len(data)
    tar.addfile(info, io.BytesIO(data) if link is None else None)


@pytest.mark.asyncio
async def test_unpack_skips_absolute_links_and_keeps_the_rest(monkeypatch, tmp_path):
    class _Storage:
        async def download_file(self, _key, path):
            with tarfile.open(path, "w:gz") as tar:
                _add(tar, "./result.json", data=b"{}")
                _add(tar, "./app/.lake/packages", link="/root/.elan/packages")
                _add(tar, "./escape", link="../../outside")
                _add(tar, "./app/main.lean", data=b"def x := 1")

    monkeypatch.setattr(monitor_settle, "get_storage_client", lambda: _Storage())

    job_dir = await monitor_settle._unpack_archive("key", tmp_path)

    assert (job_dir / "result.json").read_bytes() == b"{}"
    assert (job_dir / "app/main.lean").read_bytes() == b"def x := 1"
    assert not (job_dir / "app/.lake/packages").is_symlink()
    assert not (job_dir / "escape").is_symlink()
