"""Playback indexes are built once, identically, from stored trial evidence."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from oddish.core import playback as pb
from oddish.db import TrialStatus
from oddish.workers.queue import trial_handler as th
from test_scoreless_trial_no_retry import _outcome, _patch_session, _trial

FIXTURES = Path(__file__).resolve().parents[2] / "frontend" / "e2e" / "fixtures"
MANIFEST = json.loads((FIXTURES / "robot-embedded-playback.json").read_text())
FRAMES = MANIFEST["observations"]
PREFIX = "trials/robot-0/"
ROOT = f"{PREFIX}robot/"
MOVE = "python /opt/robot/robot_cli.py step --delta 0 0 -0.04 --gripper open"
CLOSE = "python /opt/robot/robot_cli.py step --delta 0 0 0 --gripper close"


def _calls(call_id: str, command: str) -> list[dict]:
    return [
        {
            "tool_call_id": call_id,
            "function_name": "shell",
            "arguments": {"command": command},
        }
    ]


def _result(call_id: str, frame: dict, **changes) -> dict:
    body = {
        "id": frame["id"],
        "time": frame["time"],
        "image": f"/work/{frame['image']}",
    }
    return {"source_call_id": call_id, "content": json.dumps(body | changes)}


def _step(step_id: int, calls: list | None = None, results: list | None = None) -> dict:
    step: dict = {"step_id": step_id, "source": "agent"}
    if calls:
        step["tool_calls"] = calls
    if results:
        step["observation"] = {"results": results}
    return step


# A plain image read, the producing move, a later command naming the image,
# and a second producing call.
TRAJECTORY = {
    "agent": {"name": "codex"},
    "steps": [
        _step(
            7,
            _calls("inspect", "cat /work/o1.jpg"),
            [{"source_call_id": "inspect", "content": "not JSON"}],
        ),
        _step(8, _calls("move", MOVE)),
        _step(9, results=[_result("move", FRAMES[1])]),
        _step(
            10,
            _calls("crop", "crop /work/o1.jpg"),
            [{"source_call_id": "crop", "content": "done"}],
        ),
        _step(12, _calls("close", CLOSE), [_result("close", FRAMES[2])]),
    ],
}


class MemoryStorage:
    """The StorageClient calls made by the indexer and layout resolver."""

    def __init__(
        self,
        objects: dict[str, bytes],
        *,
        fail_read: str | None = None,
        fail_writes: bool = False,
    ):
        self.objects = dict(objects)
        self.fail_read = fail_read
        self.fail_writes = fail_writes
        self.writes: list[str] = []

    @staticmethod
    def _error(code: str, status: int) -> ClientError:
        return ClientError(
            {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
            "GetObject",
        )

    async def download_bytes(self, key: str, max_bytes: int | None = None) -> bytes:
        if self.fail_read and key.endswith(self.fail_read):
            raise self._error("InternalError", 500)
        if key not in self.objects:
            raise self._error("NoSuchKey", 404)
        return self.objects[key]

    async def download_text(self, key: str) -> str:
        return (await self.download_bytes(key)).decode()

    async def object_exists(self, key: str) -> bool:
        return key in self.objects

    async def list_keys(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))

    async def upload_bytes(
        self, data: bytes, key: str, *, content_type: str | None = None
    ) -> None:
        if self.fail_writes:
            raise self._error("AccessDenied", 403)
        self.writes.append(key)
        self.objects[key] = data


def _evidence(manifest: dict | None = None) -> dict[str, bytes]:
    """An uploaded robot trial: verifier evidence, its collected copy, and trace."""
    manifest = manifest or MANIFEST
    files = {
        "playback.json": json.dumps(manifest, indent=2).encode(),
        "recording.rrd": (FIXTURES / "robot-embedded-recording.rrd").read_bytes(),
        "outcome.json": json.dumps({"success": True}).encode(),
    } | {
        o["image"]: (FIXTURES / o["image"]).read_bytes()
        for o in manifest.get("observations", [])
    }
    collection = [
        {
            "source": "/episode",
            "destination": "artifacts/episode",
            "type": "directory",
            "status": "ok",
        }
    ]
    return {
        f"{PREFIX}result.json": json.dumps({"oddish_trial_name": "robot"}).encode(),
        f"{ROOT}artifacts/manifest.json": json.dumps(collection).encode(),
        f"{ROOT}agent/trajectory.json": json.dumps(TRAJECTORY).encode(),
        **{f"{ROOT}verifier/artifacts/{name}": data for name, data in files.items()},
        **{f"{ROOT}artifacts/episode/{name}": data for name, data in files.items()},
    }


def _stored_trial() -> SimpleNamespace:
    return SimpleNamespace(
        id="robot-0",
        name="robot",
        trial_s3_key=PREFIX,
        model="test-model",
        has_trajectory=True,
        kind="agent",
        attempts=1,
        finished_at="done",
    )


async def _index(objects: dict[str, bytes]) -> tuple[dict, dict | None, MemoryStorage]:
    storage = MemoryStorage(objects)
    descriptor = await pb.index_stored_trial(_stored_trial(), storage)
    path = descriptor["index_path"]
    written = storage.objects.get(f"{ROOT}{path}") if path else None
    return descriptor, json.loads(written) if written else None, storage


@pytest.mark.asyncio
async def test_worker_and_import_write_the_same_index(monkeypatch):
    sources = _evidence()
    worker = MemoryStorage(sources)
    monkeypatch.setattr(th, "get_storage_client", lambda: worker)
    from_worker = await th._index_uploaded_playback(
        "robot-0",
        PREFIX,
        SimpleNamespace(has_trajectory=True),
        SimpleNamespace(trial_model="test-model"),
    )
    from_import, index, imported = await _index(sources)

    assert (
        from_worker
        == from_import
        == pb.ready_descriptor(pb.MANIFEST_PATH, index["revision"])
    )
    assert worker.objects == imported.objects
    # Only the derived index is added; source evidence is untouched.
    assert {
        k: v for k, v in imported.objects.items() if k not in imported.writes
    } == sources
    # The browser tests serve this file as the indexed trial's index.
    assert index == json.loads((FIXTURES / "robot-playback-index.json").read_text())


@pytest.mark.asyncio
async def test_index_links_commands_collected_copies_and_hashes():
    _, index, _ = await _index(_evidence())
    assert index["manifest"] == MANIFEST
    assert index["commands"] == {
        "o0": None,
        "o1": {
            "step_id": 8,
            "tool_call_id": "move",
            "function_name": "shell",
            "arguments": {"command": MOVE},
        },
        "o2": {
            "step_id": 12,
            "tool_call_id": "close",
            "function_name": "shell",
            "arguments": {"command": CLOSE},
        },
        "o3": None,
    }
    assert index["observation_paths"] == {
        f"{directory}{o['image']}": o["id"]
        for directory in ("verifier/artifacts/", "artifacts/episode/")
        for o in FRAMES
    }
    names = [
        "playback.json",
        "recording.rrd",
        "outcome.json",
        *(o["image"] for o in FRAMES),
    ]
    assert sorted(index["asset_hashes"]) == sorted(
        f"verifier/artifacts/{n}" for n in names
    )


@pytest.mark.asyncio
async def test_a_later_revision_keeps_the_published_index_readable():
    first, index, storage = await _index(_evidence())
    # A rebuild that read a different trace builds another revision, then
    # loses the race to publish its descriptor.
    storage.objects[f"{ROOT}agent/trajectory.json"] = b"{}"
    second = await pb.index_stored_trial(_stored_trial(), storage)

    assert second["index_revision"] != first["index_revision"]
    assert json.loads(storage.objects[ROOT + first["index_path"]]) == index


@pytest.mark.asyncio
async def test_collected_copies_need_identical_bytes():
    objects = _evidence()
    objects[f"{ROOT}artifacts/episode/o2.jpg"] = b"different pixels"
    _, index, _ = await _index(objects)
    assert "artifacts/episode/o1.jpg" in index["observation_paths"]
    assert "artifacts/episode/o2.jpg" not in index["observation_paths"]

    objects[f"{ROOT}artifacts/episode/playback.json"] = b"{}"
    _, index, _ = await _index(objects)
    assert all(path.startswith("verifier/") for path in index["observation_paths"])


@pytest.mark.asyncio
async def test_absent_manifest_writes_nothing():
    objects = {k: v for k, v in _evidence().items() if "/artifacts/" not in k}
    descriptor, index, storage = await _index(objects)
    assert (
        descriptor == pb.absent_descriptor() and index is None and storage.writes == []
    )


def _changed(change) -> dict:
    manifest = copy.deepcopy(MANIFEST)
    change(manifest)
    return manifest


def _without(name: str) -> dict[str, bytes]:
    return {
        k: v
        for k, v in _evidence().items()
        if not k.endswith(f"verifier/artifacts/{name}")
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("objects", "code"),
    [
        (_evidence(_changed(lambda m: m.update(version=1))), "invalid_manifest"),
        (
            _evidence(_changed(lambda m: m.update(recording="../../outside.rrd"))),
            "invalid_manifest",
        ),
        (
            _evidence(
                _changed(lambda m: m.update(recording="https://example.com/x.rrd"))
            ),
            "invalid_manifest",
        ),
        (
            _evidence(
                _changed(lambda m: m["actions"][0].update(observation_id="missing"))
            ),
            "invalid_manifest",
        ),
        (
            _evidence(
                _changed(
                    lambda m: m["observations"].append(dict(m["observations"][-1]))
                )
            ),
            "invalid_manifest",
        ),
        (
            _evidence(_changed(lambda m: m["observations"][1].update(time="1.1"))),
            "invalid_manifest",
        ),
        (
            _evidence(_changed(lambda m: m.update(viewer_layout="custom"))),
            "invalid_manifest",
        ),
        (
            {**_evidence(), f"{ROOT}verifier/artifacts/playback.json": b"not json"},
            "invalid_manifest",
        ),
        (_without("o2.jpg"), "missing_reference"),
        (_without("recording.rrd"), "missing_reference"),
    ],
)
async def test_invalid_evidence_is_recorded_without_an_index(objects, code):
    descriptor, index, _ = await _index(objects)
    assert descriptor == pb.invalid_descriptor(pb.MANIFEST_PATH, code)
    assert index is None


@pytest.mark.asyncio
async def test_storage_failures_are_recorded():
    storage = MemoryStorage(_evidence(), fail_read="o1.jpg")
    descriptor = await pb.index_stored_trial(_stored_trial(), storage)
    assert descriptor == pb.invalid_descriptor(pb.MANIFEST_PATH, "unreadable_artifacts")

    storage = MemoryStorage(_evidence(), fail_writes=True)
    descriptor = await pb.index_stored_trial(_stored_trial(), storage)
    assert descriptor == pb.invalid_descriptor(pb.MANIFEST_PATH, "index_write_failed")


@pytest.mark.asyncio
async def test_worker_indexes_uploaded_storage_and_survives_bugs(
    monkeypatch,
):
    storage = MemoryStorage(_evidence())
    monkeypatch.setattr(th, "get_storage_client", lambda: storage)
    outcome = SimpleNamespace(has_trajectory=False)
    prepared = SimpleNamespace(trial_model=None)
    descriptor = await th._index_uploaded_playback("t", PREFIX, outcome, prepared)
    assert descriptor["index_status"] == "ready"
    assert ROOT + descriptor["index_path"] in storage.objects

    storage.objects = {PREFIX + "result.json": b"{}"}
    assert (
        await th._index_uploaded_playback("t", PREFIX, outcome, prepared)
        == pb.absent_descriptor()
    )

    async def broken(*_args):
        raise RuntimeError("indexer bug")

    monkeypatch.setattr(th, "index_stored_trial", broken)
    descriptor = await th._index_uploaded_playback("t", PREFIX, outcome, prepared)
    assert descriptor == pb.invalid_descriptor(None, "unreadable_artifacts")


@pytest.mark.asyncio
async def test_index_errors_keep_the_score_and_replaced_attempts_publish_nothing(
    monkeypatch,
):
    invalid = pb.invalid_descriptor(pb.MANIFEST_PATH, "missing_reference")
    trial = _trial(result=None)
    _patch_session(monkeypatch, trial)
    await th._store_trial_results(
        trial_id="trial-1",
        outcome=_outcome(reward=1.0),
        trial_s3_key=PREFIX,
        execution_error=None,
        trial_attempt=1,
        playback=invalid,
    )
    assert (trial.status, trial.reward, trial.result["_playback"]) == (
        TrialStatus.SUCCESS,
        1.0,
        invalid,
    )

    trial = _trial(attempts=2, result={"kept": True})
    _patch_session(monkeypatch, trial)
    await th._store_trial_results(
        trial_id="trial-1",
        outcome=_outcome(reward=1.0),
        trial_s3_key=PREFIX,
        execution_error=None,
        trial_attempt=1,
        playback=invalid,
    )
    assert trial.result == {"kept": True}


@pytest.mark.parametrize(
    ("steps", "expected"),
    [
        # One call returning the frame twice is one match.
        (
            [
                _step(1, _calls("a", "x"), [_result("a", FRAMES[1])]),
                _step(2, results=[_result("a", FRAMES[1])]),
            ],
            "a",
        ),
        # Two calls returning the same frame conflict.
        (
            [
                _step(1, _calls("a", "x"), [_result("a", FRAMES[1])]),
                _step(2, _calls("b", "y"), [_result("b", FRAMES[1])]),
            ],
            None,
        ),
        # The call is recorded only after the result.
        (
            [_step(1, results=[_result("a", FRAMES[1])]), _step(2, _calls("a", "x"))],
            None,
        ),
        # A matching result whose call was never recorded.
        (
            [
                _step(
                    1,
                    _calls("a", "x"),
                    [_result("a", FRAMES[1]), _result("gone", FRAMES[1])],
                )
            ],
            None,
        ),
        # Time, basename and a single JSON object must all match.
        ([_step(1, _calls("a", "x"), [_result("a", FRAMES[1], time=1.2)])], None),
        ([_step(1, _calls("a", "x"), [_result("a", FRAMES[1], image="o2.jpg")])], None),
        (
            [
                _step(
                    1,
                    _calls("a", "x"),
                    [{"source_call_id": "a", "content": json.dumps([FRAMES[1]])}],
                )
            ],
            None,
        ),
        (
            [
                _step(
                    1, _calls("a", "x"), [{"source_call_id": "a", "content": "{broken"}]
                )
            ],
            None,
        ),
        # Text parts are joined as the trace viewer joins them.
        (
            [
                _step(
                    1,
                    _calls("a", "x"),
                    [
                        {
                            "source_call_id": "a",
                            "content": [
                                {"type": "text", "text": json.dumps(FRAMES[1])}
                            ],
                        }
                    ],
                )
            ],
            "a",
        ),
        # Oversized outputs are not parsed.
        (
            [
                _step(
                    1,
                    _calls("a", "x"),
                    [_result("a", FRAMES[1], pad="x" * pb.COMMAND_OUTPUT_LIMIT)],
                )
            ],
            None,
        ),
    ],
)
def test_command_attribution(steps, expected):
    commands = pb.match_commands({"steps": steps}, FRAMES)
    assert (commands["o1"] or {}).get("tool_call_id") == expected
    assert commands["o0"] is None
    assert pb.match_commands(None, FRAMES) == {frame["id"]: None for frame in FRAMES}


def test_codex_code_mode_result_blocks_link_to_the_producing_call():
    results = []
    for frame in FRAMES[:2]:
        output = _result("exec", frame)["content"]
        results.append({"type": "text", "text": json.dumps({
            "chunk_id": "test", "exit_code": 0, "output": output,
        })})
    trace = {"steps": [_step(6, _calls("exec", MOVE), [{
        "source_call_id": "exec", "content": [
            {"type": "text", "text": "Script completed\nWall time 0.2 seconds\nOutput:\n"},
            *results,
        ],
    }])]}
    commands = pb.match_commands(trace, FRAMES)
    assert commands[FRAMES[0]["id"]]["step_id"] == 6
    assert commands[FRAMES[1]["id"]]["tool_call_id"] == "exec"
    assert commands[FRAMES[2]["id"]] is None


def test_unrecognized_nested_json_is_not_observation_evidence():
    observation = _result("a", FRAMES[0])["content"]
    trace = {"steps": [_step(1, _calls("a", "cat notes"), [{
        "source_call_id": "a", "content": json.dumps({"message": observation}),
    }])]}
    assert all(v is None for v in pb.match_commands(trace, FRAMES).values())
