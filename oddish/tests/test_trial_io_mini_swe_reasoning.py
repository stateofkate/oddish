"""Restore saved mini-swe reasoning without changing the selected trial steps."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from botocore.exceptions import ClientError

from oddish.core import trial_io
from oddish.core.trial_artifacts import TrialArtifactLayout, TrialArtifactMode


@pytest.fixture
def stored_reasoning(monkeypatch):
    prefix = "tasks/task-1/trials/task-1-0/attempt-1/"
    selected = prefix + "selected/"
    trajectory_key = selected + "agent/trajectory.json"
    raw_key = selected + "agent/mini-swe-agent.trajectory.json"
    trajectory = {
        "agent": {"name": "mini-swe-agent", "version": "2", "extra": {}},
        "steps": [
            {"step_id": 1, "source": "system", "message": "System prompt"},
            {"step_id": 2, "source": "user", "message": "Task prompt"},
            {
                "step_id": 3,
                "source": "agent",
                "message": "I will read the file.",
                "reasoning_content": "I will read the file.",
                "tool_calls": [
                    {
                        "tool_call_id": "call-1",
                        "function_name": "bash",
                        "arguments": {"command": "cat app.py"},
                    }
                ],
                "metrics": {"prompt_tokens": 20},
                "observation": {"results": [{"content": "Saved output"}]},
            },
        ],
    }
    raw = {
        "messages": [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Task prompt"},
            {
                "role": "assistant",
                "content": "I will read the file.",
                "reasoning_content": "The saved full reasoning explains this choice.",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "bash",
                            "arguments": '{"command":"cat app.py"}',
                        },
                    }
                ],
            },
        ]
    }
    objects = {
        prefix + "result.json": json.dumps({"oddish_trial_name": "selected"}),
        trajectory_key: json.dumps(trajectory),
        raw_key: json.dumps(raw),
        prefix + "other/agent/mini-swe-agent.trajectory.json": json.dumps(raw),
    }

    async def download(key):
        value = objects.get(key)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return value

    storage = SimpleNamespace(
        download_text=AsyncMock(side_effect=download),
        object_exists=AsyncMock(side_effect=AssertionError("Unexpected HEAD")),
        list_keys=AsyncMock(side_effect=AssertionError("Unexpected LIST")),
    )
    trial = SimpleNamespace(
        id="task-1-0",
        name=None,
        attempts=1,
        trial_s3_key=prefix,
        finished_at=datetime.now(UTC),
        model="test-model",
        has_trajectory=True,
    )
    monkeypatch.setattr(trial_io, "get_storage_client", lambda: storage)
    trial_io._TRAJECTORY_CACHE.clear()
    trial_io._TRAJECTORY_LOCKS.clear()
    yield trial, storage, objects, trajectory_key, raw_key, trajectory
    trial_io._TRAJECTORY_CACHE.clear()
    trial_io._TRAJECTORY_LOCKS.clear()


@pytest.mark.asyncio
async def test_restores_same_attempt_reasoning_and_caches_the_result(stored_reasoning):
    trial, storage, _, trajectory_key, raw_key, trajectory = stored_reasoning
    restored = await trial_io.read_trial_trajectory(trial)
    assert restored["steps"][2]["reasoning_content"] == (
        "The saved full reasoning explains this choice."
    )
    for key in ("step_id", "message", "tool_calls", "metrics", "observation"):
        assert restored["steps"][2][key] == trajectory["steps"][2][key]
    assert restored["agent"]["extra"]["oddish_reasoning_version"] == 1
    assert await trial_io.read_trial_trajectory(trial) == restored
    assert [call.args[0] for call in storage.download_text.await_args_list] == [
        trial.trial_s3_key + "result.json",
        trajectory_key,
        raw_key,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_content", [None, "{", "null", "[]", '"bad"'])
async def test_missing_or_bad_source_keeps_saved_steps(stored_reasoning, raw_content):
    trial, storage, objects, _, raw_key, trajectory = stored_reasoning
    objects[raw_key] = raw_content
    assert await trial_io.read_trial_trajectory(trial) == trajectory
    storage.list_keys.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_storage_error_is_not_cached(stored_reasoning):
    trial, _, objects, _, raw_key, _ = stored_reasoning
    objects[raw_key] = ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
    with pytest.raises(ClientError):
        await trial_io.read_trial_trajectory(trial)
    assert not trial_io._TRAJECTORY_CACHE


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["candidate", "listed", "step"])
async def test_legacy_source_error_does_not_skip_the_selected_trace(
    stored_reasoning, location
):
    trial, storage, objects, trajectory_key, raw_key, _ = stored_reasoning
    trial.has_trajectory = False
    if location == "candidate":
        target = trial.trial_s3_key + "agent/trajectory.json"
        source = trial.trial_s3_key + "agent/mini-swe-agent.trajectory.json"
    elif location == "step":
        target = trajectory_key.replace("/agent/", "/steps/solve/agent/")
        source = raw_key.replace("/agent/", "/steps/solve/agent/")
    else:
        target, source = trajectory_key, raw_key
    objects[target] = objects[trajectory_key]
    objects[source] = ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
    layout = TrialArtifactLayout(
        mode=TrialArtifactMode.LEGACY,
        attempt_prefix=trial.trial_s3_key,
        artifact_prefix=None,
        listed_keys=(target,),
    )
    with pytest.raises(ClientError):
        await trial_io._read_trial_trajectory_from_s3(trial, storage, layout)
    assert storage.download_text.await_args.args[0] == source


@pytest.mark.asyncio
async def test_new_capture_needs_no_source_download(stored_reasoning):
    trial, storage, objects, trajectory_key, _, trajectory = stored_reasoning
    trajectory["agent"]["extra"]["oddish_reasoning_version"] = 1
    objects[trajectory_key] = json.dumps(trajectory)
    assert await trial_io.read_trial_trajectory(trial) == trajectory
    assert storage.download_text.await_count == 2


@pytest.mark.asyncio
async def test_per_step_recovery_reads_only_its_own_source(stored_reasoning):
    _, storage, objects, trajectory_key, raw_key, _ = stored_reasoning
    step_key = trajectory_key.replace("/agent/", "/steps/solve/agent/")
    step_raw_key = raw_key.replace("/agent/", "/steps/solve/agent/")
    objects[step_key] = objects[trajectory_key]
    objects[step_raw_key] = objects[raw_key]
    restored = await trial_io._read_step_trajectories(storage, [step_key])
    assert restored["steps"][2]["reasoning_content"] == (
        "The saved full reasoning explains this choice."
    )
    assert restored["steps"][2]["extra"]["harbor_step"] == "solve"
    assert [call.args[0] for call in storage.download_text.await_args_list] == [
        step_key,
        step_raw_key,
    ]


def test_local_recovery_keeps_source_files(stored_reasoning, tmp_path, monkeypatch):
    trial, _, objects, trajectory_key, raw_key, trajectory = stored_reasoning
    directory = tmp_path / "job" / "selected" / "agent"
    directory.mkdir(parents=True)
    result_path = tmp_path / "job" / "result.json"
    result_path.write_text('{"oddish_trial_name":"selected"}')
    atif_path = directory / "trajectory.json"
    atif_path.write_text(objects[trajectory_key])
    source_path = directory / "mini-swe-agent.trajectory.json"
    source_path.write_text(objects[raw_key])
    before = copy.deepcopy(objects)
    trial.harbor_result_path = str(result_path)
    monkeypatch.setattr(trial_io.settings, "harbor_jobs_dir", str(tmp_path))
    restored = trial_io._read_local_trial_trajectory(trial)
    assert (
        restored["steps"][2]["reasoning_content"]
        != trajectory["steps"][2]["reasoning_content"]
    )
    assert atif_path.read_text() == before[trajectory_key]
    assert source_path.read_text() == before[raw_key]
