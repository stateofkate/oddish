from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from api.routers import trials
from api.trial_cache import TrialCacheIdentity
from auth import APIKeyScope


@pytest.mark.asyncio
async def test_rebuild_requires_write_scope_and_passes_org(monkeypatch):
    prepare = AsyncMock(return_value={"index_status": "ready"})
    monkeypatch.setattr(trials, "rebuild_trial_playback", prepare)
    auth = SimpleNamespace(org_id="org", require_scope=Mock())
    assert await trials.rebuild_playback("t-0", auth) == {"index_status": "ready"}
    auth.require_scope.assert_called_once_with(APIKeyScope.TASKS)
    prepare.assert_awaited_once_with("t-0", org_id="org")
    prepare.reset_mock()
    auth.require_scope.side_effect = HTTPException(403, "Read-only key")
    with pytest.raises(HTTPException):
        await trials.rebuild_playback("t-0", auth)
    prepare.assert_not_awaited()


def test_playback_rebuild_changes_detail_cache_validator():
    original = TrialCacheIdentity("t-0", 1, "success", None, "success", None)
    ready = replace(
        original,
        playback={
            "manifest_path": "verifier/artifacts/playback.json",
            "index_status": "ready",
            "index_revision": "revision-1",
        },
    )
    assert ready.etag != original.etag
    assert (
        replace(ready, playback={**ready.playback, "index_revision": "revision-2"}).etag
        != ready.etag
    )
    assert (
        replace(ready, playback={**ready.playback, "index_error": None}).etag
        == ready.etag
    )
