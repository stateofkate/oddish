"""Hosted completion and repair prepare the same durable playback evidence."""

from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from oddish.core import playback as pb
from oddish.db import TrialStatus, WorkerJobModel, WorkerJobStatus
from oddish.workers.launcher import settle as monitor
from oddish.workers.queue import trial_handler as th
from test_playback_index import MemoryStorage, PREFIX, ROOT, _evidence
from test_scoreless_trial_no_retry import _patch_session, _trial


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["oracle", "codex"])
@pytest.mark.parametrize("missing_image", [False, True])
@pytest.mark.parametrize("local_manifest", [None, pb.MANIFEST_PATH])
async def test_hosted_completion_publishes_playback_without_changing_reward(
    monkeypatch, tmp_path, agent, missing_image, local_manifest
):
    evidence = _evidence()
    if agent == "oracle":
        del evidence[ROOT + "agent/trajectory.json"]
    if missing_image:
        del evidence[ROOT + "verifier/artifacts/o1.jpg"]
    storage = MemoryStorage(evidence)
    storage.upload_trial_results = AsyncMock(return_value=PREFIX)
    monkeypatch.setattr(monitor, "get_storage_client", lambda: storage)
    monkeypatch.setattr(th, "get_storage_client", lambda: storage)
    trial = _trial(id="robot-0", agent=agent, result={})
    _patch_session(monkeypatch, trial)
    job = SimpleNamespace(
        id="j",
        kind="trial",
        queue_key="q",
        status=WorkerJobStatus.RUNNING,
        attempts=1,
        max_attempts=1,
        subject_table="trials",
        subject_id=trial.id,
        payload={},
        org_id="org",
        parent_job_id=None,
        current_worker_id="w-1",
        current_queue_slot=None,
        provider=None,
        external_id=None,
        monitor_result_accepted_at="accepted",
        monitor_result={
            "attempt": 1,
            "archive_key": "archive.tar.gz",
            "outcome": {
                "reward": 1.0,
                "error": None,
                "exit_code": 0,
                "duration_sec": 1,
                "has_trajectory": agent == "codex",
                "playback": {"manifest_path": local_manifest},
            },
        },
    )

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(
            get=AsyncMock(
                side_effect=lambda model, _: job if model is WorkerJobModel else trial
            )
        )

    monkeypatch.setattr(monitor, "get_session", session)
    monkeypatch.setattr(monitor, "_unpack_archive", AsyncMock(return_value=tmp_path))
    monkeypatch.setattr(
        monitor,
        "load_trial_context",
        AsyncMock(
            return_value=SimpleNamespace(
                trial_kind="agent",
                trial_attempt=1,
                trial_model="test-model",
                org_id="org",
                billed_user_id=None,
            )
        ),
    )
    monkeypatch.setattr(monitor, "mirror_to_sauron", AsyncMock())
    monkeypatch.setattr(
        monitor.monitor_costs, "cost_state_for_settle", lambda **_: None
    )
    monkeypatch.setattr(monitor, "ensure_builtin_handlers_registered", lambda: None)
    monkeypatch.setattr(
        monitor,
        "get_handler",
        lambda _: SimpleNamespace(
            outcome_for_settled_trial=AsyncMock(
                return_value=SimpleNamespace(success=True)
            )
        ),
    )
    monkeypatch.setattr(
        monitor, "record_outcome", AsyncMock(return_value=WorkerJobStatus.SUCCESS)
    )
    monkeypatch.setattr(monitor, "release_after_attempt", AsyncMock())
    monkeypatch.setattr(monitor, "_post_success_hook", None)
    monkeypatch.setattr(th, "_finish_trial_settlement", AsyncMock())
    monkeypatch.setattr(th, "settle_judge_costs", AsyncMock(return_value=None))
    monkeypatch.setattr(th, "_worker_still_owns_trial", AsyncMock(return_value=True))
    assert await monitor.settle_monitor_result("j", attempt=1) == "settled"
    assert trial.status == TrialStatus.SUCCESS
    assert trial.reward == 1
    assert trial.result["_playback"]["index_status"] == (
        "invalid" if missing_image else "ready"
    )
    written = [k for k in storage.objects if k.startswith(ROOT + "_oddish/")]
    assert bool(written) == (not missing_image)
    if agent == "oracle" and not missing_image:
        index_key = ROOT + trial.result["_playback"]["index_path"]
        assert written == [index_key]
        index = json.loads(storage.objects[index_key])
        assert all(command is None for command in index["commands"].values())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", [None, "attempts", "trial_s3_key", "superseded_by_trial_id", "status"]
)
async def test_rebuild_checks_attempt_and_preserves_scores(monkeypatch, change):
    from oddish.db import storage as storage_module
    from oddish.core.endpoints import _common
    import oddish.db

    trial = _trial(
        id="robot-0",
        org_id="org",
        status=TrialStatus.SUCCESS,
        reward=1,
        name="robot",
        trial_s3_key=PREFIX,
        model="test-model",
        result={"metric": 42},
    )
    current = SimpleNamespace(**vars(trial))
    storage = MemoryStorage(_evidence())
    monkeypatch.setattr(storage_module, "get_storage_client", lambda: storage)
    commits = []

    @asynccontextmanager
    async def session():
        async def commit():
            commits.append(True)

        yield SimpleNamespace(
            get=AsyncMock(return_value=current), expunge=lambda _: None, commit=commit
        )

    async def authorized(session, *, trial_id, org_id):
        assert (trial_id, org_id) == ("robot-0", "org")
        return trial

    monkeypatch.setattr(oddish.db, "get_session", session)
    monkeypatch.setattr(_common, "get_trial_for_org_core", authorized)
    if change:
        setattr(
            current,
            change,
            {
                "attempts": 2,
                "trial_s3_key": "new/",
                "superseded_by_trial_id": "replacement",
                "status": TrialStatus.RUNNING,
            }[change],
        )
        with pytest.raises(HTTPException) as err:
            await pb.rebuild_trial_playback(trial.id, org_id="org")
        assert err.value.status_code == 409
        assert not commits
        assert current.result == {"metric": 42}
    else:
        first = await pb.rebuild_trial_playback(trial.id, org_id="org")
        assert first["index_status"] == "ready"
        assert await pb.rebuild_trial_playback(trial.id, org_id="org") == first
        assert current.result == {"metric": 42, "_playback": first}
        monkeypatch.setattr(
            pb, "index_stored_trial", AsyncMock(side_effect=RuntimeError("flaky"))
        )
        with pytest.raises(HTTPException) as err:
            await pb.rebuild_trial_playback(trial.id, org_id="org")
        assert err.value.status_code == 503
        assert current.result == {"metric": 42, "_playback": first}
        for code in ("unreadable_artifacts", "index_write_failed"):
            monkeypatch.setattr(
                pb,
                "index_stored_trial",
                AsyncMock(return_value=pb.invalid_descriptor(pb.MANIFEST_PATH, code)),
            )
            with pytest.raises(HTTPException) as err:
                await pb.rebuild_trial_playback(trial.id, org_id="org")
            assert err.value.status_code == 503
            assert current.result == {"metric": 42, "_playback": first}
    assert current.reward == 1


@pytest.mark.asyncio
async def test_rebuild_rejects_running_trial_before_reading_storage(monkeypatch):
    from oddish.core.endpoints import _common
    import oddish.db

    @asynccontextmanager
    async def session():
        yield object()

    monkeypatch.setattr(oddish.db, "get_session", session)
    monkeypatch.setattr(
        _common, "get_trial_for_org_core", AsyncMock(return_value=_trial())
    )
    monkeypatch.setattr(
        pb,
        "index_stored_trial",
        AsyncMock(side_effect=AssertionError("must not read artifacts")),
    )
    with pytest.raises(HTTPException) as err:
        await pb.rebuild_trial_playback("trial-1")
    assert err.value.status_code == 409
