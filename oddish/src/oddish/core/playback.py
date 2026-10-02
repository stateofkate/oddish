"""Validate a trial's robot playback evidence and write its display index.

A task's verifier may publish ``verifier/artifacts/playback.json``: a manifest
naming a Rerun recording, the physical outcome, and the camera images
returned to the agent with the actions between them. After a trial's artifacts
reach storage, worker settlement and CLI import completion call
``index_stored_trial``. It checks every referenced object directly, attributes
each observation to the recorded tool call that returned it, and writes
``_oddish/playback-index/<revision>.json`` under the trial's artifact prefix.
Each revision has its own object, so a rebuild that loses the race to publish
its descriptor never replaces the index the stored descriptor names. Source
artifacts are never changed.

``trials.result._playback`` stores the small descriptor: ``index_status`` is
``ready``, ``absent`` (no manifest) or ``invalid`` with a stable ``index_error``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from oddish.db.storage import StorageClient, is_missing_object

logger = logging.getLogger(__name__)

MANIFEST_PATH = "verifier/artifacts/playback.json"
# Tool results longer than this are not parsed for attribution.
COMMAND_OUTPUT_LIMIT = 1_000_000
_SIBLING = re.compile(r"^[\w.-]+$", re.ASCII)
_STORAGE_ERRORS = (BotoCoreError, ClientError, OSError)


class PlaybackIndexError(Exception):
    """Indexing stopped. ``code`` is the stored ``index_error``."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


def _descriptor(status: str, manifest: str | None = None, **fields: Any) -> dict:
    return {
        "manifest_path": manifest,
        "index_status": status,
        "index_path": fields.get("index_path"),
        "index_revision": fields.get("index_revision"),
        "index_error": fields.get("index_error"),
    }


def absent_descriptor() -> dict:
    return _descriptor("absent")


def index_path(revision: str) -> str:
    return f"_oddish/playback-index/{revision}.json"


def ready_descriptor(manifest_path: str, revision: str) -> dict:
    return _descriptor(
        "ready",
        manifest_path,
        index_path=index_path(revision),
        index_revision=revision,
    )


# Index errors caused by storage reads or writes rather than by the evidence.
_TRANSIENT_INDEX_ERRORS = frozenset({"unreadable_artifacts", "index_write_failed"})


def clobbers_ready_index(previous: object, playback: dict) -> bool:
    """Whether publishing ``playback`` would replace a working index with a
    transient storage failure for the same artifacts."""
    return (
        isinstance(previous, dict)
        and previous.get("index_status") == "ready"
        and playback.get("index_error") in _TRANSIENT_INDEX_ERRORS
    )


def invalid_descriptor(manifest_path: str | None, code: str) -> dict:
    return _descriptor("invalid", manifest_path, index_error=code)


def _number(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _sibling(value: object, suffix: str = "") -> bool:
    """A plain file name beside the manifest: no paths, parents or URLs."""
    return (
        isinstance(value, str)
        and bool(_SIBLING.fullmatch(value))
        and value not in (".", "..")
        and value.endswith(suffix)
    )


def validate_manifest(m: object) -> dict:
    """Return a task-authored manifest, or raise ``invalid_manifest``."""

    def fail(detail: str) -> PlaybackIndexError:
        return PlaybackIndexError("invalid_manifest", detail)

    if not isinstance(m, dict) or type(m.get("version")) is not int:
        raise fail("not a versioned manifest")
    duration = m.get("duration_seconds")
    checks = m.get("checks")
    if (
        m["version"] != 2
        or m.get("producer") != "verifier"
        or not isinstance(m.get("title"), str)
        or not isinstance(m.get("goal"), str)
        or not isinstance(m.get("timeline"), str)
        or not m["timeline"]
        or not _sibling(m.get("recording"), ".rrd")
        or not _sibling(m.get("outcome"))
        or not _number(duration)
        or duration < 0
        or not isinstance(checks, list)
        or not all(
            isinstance(c, dict)
            and isinstance(c.get("key"), str)
            and isinstance(c.get("label"), str)
            for c in checks
        )
        or m.get("viewer_layout", "embedded_scene") != "embedded_scene"
    ):
        raise fail("unsupported or invalid playback description")

    def time(t: object) -> bool:
        return _number(t) and 0 <= t <= duration  # type: ignore[operator]

    def ordered(items: list, start: str, end: str) -> bool:
        return all(b[start] >= a[end] for a, b in zip(items, items[1:]))

    observations, actions = m.get("observations"), m.get("actions")
    if not (
        time(m.get("start_time"))
        and isinstance(observations, list)
        and observations
        and all(
            isinstance(o, dict)
            and isinstance(o.get("id"), str)
            and o["id"]
            and time(o.get("time"))
            and _sibling(o.get("image"), ".jpg")
            for o in observations
        )
        and observations[0]["time"] == m["start_time"]
        and ordered(observations, "time", "time")
        and len({o["id"] for o in observations}) == len(observations)
    ):
        raise fail("invalid observations")
    times = {o["id"]: o["time"] for o in observations}
    if not (
        isinstance(actions, list)
        and all(
            isinstance(a, dict)
            and isinstance(a.get("id"), str)
            and a["id"]
            and isinstance(a.get("label"), str)
            and time(a.get("time"))
            and time(a.get("end_time"))
            and a["end_time"] > a["time"]
            and times.get(a.get("observation_id")) == a["time"]
            and times.get(a.get("result_observation_id")) == a["end_time"]
            for a in actions
        )
        and ordered(actions, "time", "end_time")
        and len({a["id"] for a in actions}) == len(actions)
    ):
        raise fail("invalid action and observation links")
    return m


def _returned_observations(content: object) -> Iterator[dict]:
    """Read JSON tool results, including Codex's exec_command output envelope."""
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = [
            p["text"]
            for p in content
            if isinstance(p, dict)
            and p.get("type") == "text"
            and isinstance(p.get("text"), str)
        ]
    else:
        return
    if sum(len(p) for p in parts) > COMMAND_OUTPUT_LIMIT:
        return
    for part in parts:
        try:
            body = json.loads(part)
            # Codex code-mode emits a status text block followed by one JSON
            # exec_command result per call. Only unwrap that known envelope.
            if (
                isinstance(body, dict)
                and isinstance(body.get("chunk_id"), str)
                and isinstance(body.get("output"), str)
            ):
                body = json.loads(body["output"])
        except ValueError:
            continue
        if isinstance(body, dict):
            yield body


def match_commands(
    trajectory: Mapping[str, Any] | None, observations: list[dict]
) -> dict[str, dict | None]:
    """Attribute each observation to the recorded tool call that returned it.

    A tool result must return a JSON object naming the observation's ID, its
    simulation time and its image basename. Its ``source_call_id`` must name a
    call recorded at or before that result. An observation matched by an
    unattributable result, or by two different calls, stays null.
    """
    commands: dict[str, dict | None] = {o["id"]: None for o in observations}
    steps = trajectory.get("steps") if isinstance(trajectory, Mapping) else None
    if not isinstance(steps, list):
        return commands
    steps = [s if isinstance(s, dict) else {} for s in steps]
    calls: dict[str, list[tuple[int, dict]]] = {}
    for index, step in enumerate(steps):
        for call in step.get("tool_calls") or []:
            if isinstance(call, dict) and isinstance(call.get("tool_call_id"), str):
                calls.setdefault(call["tool_call_id"], []).append((index, call))
    frames = {o["id"]: o for o in observations}
    found: dict[str, set[tuple[int, str] | None]] = {}
    for index, step in enumerate(steps):
        results = (step.get("observation") or {}).get("results")
        for result in results if isinstance(results, list) else []:
            if not isinstance(result, dict):
                continue
            for body in _returned_observations(result.get("content")):
                if not isinstance(body.get("id"), str):
                    continue
                frame = frames.get(body["id"])
                image = body.get("image")
                if (
                    frame is None
                    or not (_number(body.get("time")) and body["time"] == frame["time"])
                    or not isinstance(image, str)
                    or image.split("/")[-1] != frame["image"]
                ):
                    continue
                source = result.get("source_call_id")
                recorded = calls.get(source, []) if isinstance(source, str) else []
                earlier = [i for i, _ in recorded if i <= index]
                found.setdefault(frame["id"], set()).add(
                    (earlier[-1], source) if earlier else None
                )
    for observation_id, matches in found.items():
        if len(matches) != 1 or None in matches:
            continue
        step_index, call_id = next(iter(matches))  # type: ignore[misc]
        call = next(c for i, c in calls[call_id] if i == step_index)
        commands[observation_id] = {
            "step_id": steps[step_index].get("step_id"),
            "tool_call_id": call_id,
            "function_name": call.get("function_name"),
            "arguments": call.get("arguments"),
        }
    return commands


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


class _Reader:
    """Direct, bounded reads of one trial's stored objects."""

    def __init__(self, storage: StorageClient, prefix: str):
        self.storage, self.prefix = storage, prefix
        self.limit = asyncio.Semaphore(8)

    async def read(self, path: str) -> bytes | None:
        """Object bytes, or None only when storage reports it missing."""
        async with self.limit:
            try:
                return await self.storage.download_bytes(f"{self.prefix}{path}")
            except ClientError as exc:
                if is_missing_object(exc):
                    return None
                raise


def _collected_dirs(record: bytes | None) -> list[str]:
    """Directories Harbor collected for the task's ``[[artifacts]]`` entries."""
    try:
        entries = json.loads(record) if record else []
    except ValueError:
        return []
    dirs = []
    for entry in entries if isinstance(entries, list) else []:
        destination = entry.get("destination") if isinstance(entry, dict) else None
        parts = PurePosixPath(destination).parts if isinstance(destination, str) else ()
        if (
            entry.get("status") == "ok"
            and entry.get("type") == "directory"
            and parts[:1] == ("artifacts",)
            and ".." not in parts
        ):
            dirs.append("/".join(parts) + "/")
    return dirs


TrajectoryLoader = Callable[[], Awaitable[Mapping[str, Any] | None]]


async def _build_index(
    reader: _Reader, manifest_bytes: bytes, load_trajectory: TrajectoryLoader
) -> dict:
    try:
        manifest = validate_manifest(json.loads(manifest_bytes))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PlaybackIndexError("invalid_manifest", "manifest is not JSON") from exc
    root = MANIFEST_PATH.removesuffix("playback.json")
    observations = manifest["observations"]
    names = [
        manifest["recording"],
        manifest["outcome"],
        *(o["image"] for o in observations),
    ]
    contents = await asyncio.gather(*(reader.read(root + n) for n in names))
    missing = sorted({n for n, data in zip(names, contents) if data is None})
    if missing:
        raise PlaybackIndexError("missing_reference", f"missing {missing}")
    hashes = {MANIFEST_PATH: hashlib.sha256(manifest_bytes).hexdigest()}
    hashes |= {
        root + n: hashlib.sha256(data).hexdigest() for n, data in zip(names, contents)
    }  # type: ignore[arg-type]

    # Collected copies of the episode are aliases only when the manifest and
    # each image are byte-identical to the verifier's copies.
    paths = {root + o["image"]: o["id"] for o in observations}
    for directory in _collected_dirs(await reader.read("artifacts/manifest.json")):
        if (
            observations
            and await reader.read(directory + "playback.json") == manifest_bytes
        ):
            copies = await asyncio.gather(
                *(reader.read(directory + o["image"]) for o in observations)
            )
            for o, data in zip(observations, copies):
                if (
                    data is not None
                    and hashlib.sha256(data).hexdigest() == hashes[root + o["image"]]
                ):
                    paths[directory + o["image"]] = o["id"]

    fields = {
        "version": 1,
        "manifest_path": MANIFEST_PATH,
        "manifest": manifest,
        "commands": (
            match_commands(await load_trajectory(), observations)
            if observations
            else {}
        ),
        "observation_paths": paths,
        "asset_hashes": hashes,
    }
    try:
        return {**fields, "revision": hashlib.sha256(_canonical(fields)).hexdigest()}
    except ValueError as exc:  # NaN or Infinity copied from a tool call
        raise PlaybackIndexError("index_write_failed", str(exc)) from exc


async def index_trial_playback(
    storage: StorageClient, artifact_prefix: str, load_trajectory: TrajectoryLoader
) -> dict:
    """Build and write one trial's index; return its descriptor, never raising
    for invalid evidence or unreadable storage."""
    reader = _Reader(storage, artifact_prefix)
    try:
        manifest_bytes = await reader.read(MANIFEST_PATH)
    except _STORAGE_ERRORS as exc:
        # No manifest was seen, so this may not be a robot trial at all; a
        # null manifest_path keeps the drawer from showing a Scene tab.
        logger.warning(
            "Playback manifest at %s is unreadable: %s", artifact_prefix, exc
        )
        return invalid_descriptor(None, "unreadable_artifacts")
    if manifest_bytes is None:
        return absent_descriptor()
    try:
        index = await _build_index(reader, manifest_bytes, load_trajectory)
        key = f"{artifact_prefix}{index_path(index['revision'])}"
        try:
            await storage.upload_bytes(
                _canonical(index), key, content_type="application/json"
            )
            readable = await storage.object_exists(key)
        except _STORAGE_ERRORS as exc:
            raise PlaybackIndexError("index_write_failed", str(exc)) from exc
        if not readable:
            raise PlaybackIndexError("index_write_failed", f"{key} is not readable")
        return ready_descriptor(MANIFEST_PATH, index["revision"])
    except PlaybackIndexError as exc:
        logger.warning("Playback index at %s is invalid: %s", artifact_prefix, exc)
        return invalid_descriptor(MANIFEST_PATH, exc.code)
    except _STORAGE_ERRORS as exc:
        logger.warning(
            "Playback evidence at %s is unreadable: %s", artifact_prefix, exc
        )
        return invalid_descriptor(MANIFEST_PATH, "unreadable_artifacts")


@dataclass(frozen=True, slots=True)
class UploadedTrial:
    """The fields the layout resolver and trace reader need after upload."""

    id: str
    trial_s3_key: str | None
    name: str | None = None
    model: str | None = None
    has_trajectory: bool = False
    kind: str = "agent"
    attempts: int = 0
    finished_at: object = None


async def index_stored_trial(trial: Any, storage: StorageClient) -> dict:
    """Resolve a stored trial's artifact prefix and trace, then index it."""
    from oddish.core.trial_artifacts import (
        TrialArtifactMode,
        resolve_trial_artifact_layout,
    )
    from oddish.core.trial_io import read_layout_trajectory

    try:
        layout = await resolve_trial_artifact_layout(trial, storage)
    except _STORAGE_ERRORS as exc:
        logger.warning("Trial %s layout is unreadable: %s", trial.id, exc)
        return invalid_descriptor(None, "unreadable_artifacts")
    if layout.mode is TrialArtifactMode.UNAVAILABLE or layout.artifact_prefix is None:
        logger.warning(
            "Trial %s artifacts are unavailable: %s", trial.id, layout.failure_reason
        )
        return invalid_descriptor(None, "unreadable_artifacts")
    return await index_trial_playback(
        storage,
        layout.artifact_prefix,
        lambda: read_layout_trajectory(trial, storage, layout),
    )


async def rebuild_trial_playback(trial_id: str, *, org_id: str | None = None) -> dict:
    """Prepare a completed trial's playback without executing or grading it."""
    from fastapi import HTTPException

    from oddish.core.endpoints._common import get_trial_for_org_core
    from oddish.db import TrialModel, TrialStatus, get_session
    from oddish.db.storage import get_storage_client

    async with get_session() as session:
        trial = await get_trial_for_org_core(session, trial_id=trial_id, org_id=org_id)
        if trial.status not in (TrialStatus.SUCCESS, TrialStatus.FAILED):
            raise HTTPException(
                status_code=409, detail="Playback requires a completed trial"
            )
        identity = (trial.attempts, trial.trial_s3_key, trial.superseded_by_trial_id)
        session.expunge(trial)

    try:
        playback = await index_stored_trial(trial, get_storage_client())
    except Exception:
        logger.exception("Playback rebuild failed for trial %s", trial_id)
        playback = invalid_descriptor(None, "unreadable_artifacts")

    async with get_session() as session:
        current = await session.get(TrialModel, trial_id, with_for_update=True)
        if current is None:
            raise HTTPException(status_code=404, detail=f"Trial {trial_id} not found")
        if (
            current.status not in (TrialStatus.SUCCESS, TrialStatus.FAILED)
            or (current.attempts, current.trial_s3_key, current.superseded_by_trial_id)
            != identity
        ):
            raise HTTPException(
                status_code=409,
                detail="Trial changed while preparing playback; try again",
            )
        if clobbers_ready_index((current.result or {}).get("_playback"), playback):
            raise HTTPException(
                status_code=503, detail="Playback rebuild failed; try again"
            )
        current.result = {**(current.result or {}), "_playback": playback}
        await session.commit()
    return playback
