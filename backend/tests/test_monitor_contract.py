"""The /monitor wire contract: a deploy must not break monitors still running.

A trial's monitor keeps the code it started with for up to 12 hours, across
API deploys. Only 401 and 410 stop it. Any other 4xx from our API is a refusal:
telemetry is dropped, and a refused result is retried for about half an hour,
then the monitor exits failed and the finished trial re-runs. So every /monitor
format may only grow (oddish/src/oddish/mon/contract.py). The recorded contract
lives in tests/fixtures/monitor_contract.json and scripts/monitor_contract.py
builds, compares and records it.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
from pathlib import Path
from typing import Any, get_args

import httpx
import pytest
from api.routers import monitor
from oddish.mon import artifacts, hooks, loops, seams, status
from oddish.mon.client import MonitorClient
from oddish.mon.contract import AttemptSpec, summarize_hook_event
from oddish.mon.env import MonitorEnv
from oddish.workers.harbor.live_tail import PersistResult
from oddish.workers.harbor.outcome import HarborOutcome
from oddish.workers.launcher.hook_events import hook_event_from_summary
from oddish.workers.launcher.settle import _outcome_from_payload
from pydantic import BaseModel

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "monitor_contract.py"
_spec = importlib.util.spec_from_file_location("monitor_contract", _SCRIPT)
contract = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(contract)


def test_monitor_formats_only_grow():
    """What a monitor built against the recorded contract sends and reads
    still works against the current API."""
    recorded = json.loads(contract.SNAPSHOT.read_text())
    current = json.loads(contract.render(contract.current_contract()))
    broken = contract.breaking_changes(recorded, current)
    if broken:
        pytest.fail(
            contract.BREAKING_HELP + "\n".join(f"- {line}" for line in broken),
            pytrace=False,
        )
    if recorded != current:
        changed = "\n".join(
            f"- {line}" for line in contract.differences(recorded, current)
        )
        pytest.fail(
            "The /monitor contract grew without breaking running monitors. Record "
            f"the change so the next one is checked against it:\n    "
            f"{contract.REGENERATE}\n{changed}",
            pytrace=False,
        )


def _models(annotation: Any, seen: set[type[BaseModel]]) -> set[type[BaseModel]]:
    is_model = isinstance(annotation, type) and issubclass(annotation, BaseModel)
    if is_model and annotation not in seen:
        seen.add(annotation)
        for field in annotation.model_fields.values():
            _models(field.annotation, seen)
    for arg in get_args(annotation):
        _models(arg, seen)
    return seen


def test_no_model_on_the_monitor_boundary_forbids_unknown_fields():
    """An added optional field is the one change allowed, so no request or
    reply model may refuse a field it has not heard of yet."""
    found: set[type[BaseModel]] = set()
    for route in monitor.router.routes:
        if route.body_field is not None:
            _models(route.body_field.type_, found)
        _models(route.response_model, found)
    names = {model.__name__ for model in found}
    assert {"AttemptSpec", "ResultPayload", "MonitorStats", "LiveEvent"} <= names
    assert "UsageCheckpoint" in names
    forbidding = [m.__name__ for m in found if m.model_config.get("extra") == "forbid"]
    assert not forbidding, f"{forbidding} refuse unknown fields"


SPEC = AttemptSpec(
    trial_id="trial-1",
    worker_job_id="job-1",
    worker_job_attempt=2,
    trial_attempt=1,
    trial_kind="agent",
    task_id="task-1",
    task_name="hello",
    agent="oracle",
    model="anthropic/claude",
    harbor_config={"variant_id": "variant-1"},
    org_id="org-1",
    billed_user_id="user-1",
    task_files={"task.toml": "https://storage.test/task.toml"},
)
UPLOAD_URL = "https://storage.test/archive"


def _dropped(sent: Any, parsed: Any, path: str = "") -> list[str]:
    """Keys the monitor sent that the API's parse did not keep."""
    if isinstance(sent, dict) and isinstance(parsed, dict):
        lost = []
        for key, value in sent.items():
            if key not in parsed:
                lost.append(path + key)
            else:
                lost += _dropped(value, parsed[key], f"{path}{key}.")
        return lost
    if isinstance(sent, list) and isinstance(parsed, list):
        return [p for a, b in zip(sent, parsed) for p in _dropped(a, b, path + "[].")]
    return []


@pytest.mark.asyncio
async def test_the_monitor_and_the_api_parse_each_other(tmp_path, monkeypatch):
    """Today's monitor code against today's API models, through every route:
    each reply carries a field the monitor has never heard of (except the
    closed one), and each request must parse without the API dropping a key."""
    monkeypatch.setattr(status, "STATUS_PATH", tmp_path / "status.json")
    replies: dict[str, list[BaseModel]] = {
        "GET /monitor/attempt": [SPEC],
        "POST /monitor/heartbeat": [monitor.HeartbeatResponse()],
        "GET /monitor/control": [
            monitor.ControlResponse(authorized=True, pause_requested=True),
            monitor.ControlResponse(authorized=False, detail="execution revoked"),
        ],
        "POST /monitor/events": [
            monitor.EventsResponse(
                flushed=1,
                checkpoint_written=True,
                trial_finished=False,
                quota_checked=True,
                pause_requested=None,
            )
        ],
        "POST /monitor/lifecycle": [monitor.HeartbeatResponse()],
        "POST /monitor/artifacts": [
            monitor.ArtifactUploadResponse(
                upload_url=UPLOAD_URL,
                upload_headers={"Content-Type": "application/gzip"},
                archive_key="trials/trial-1/monitor/job-1-2.tar.gz",
            )
        ],
        "POST /monitor/result": [monitor.ResultResponse()],
    }
    called: set[str] = set()
    sent: dict[str, list[dict]] = {}

    def api(request: httpx.Request) -> httpx.Response:
        route = f"{request.method} {request.url.path}"
        called.add(route)
        if request.content:
            sent.setdefault(route, []).append(json.loads(request.content))
        reply = replies[route].pop(0).model_dump(mode="json")
        if route not in contract.CLOSED_REPLIES:
            reply["added_later"] = {"by": "a newer API"}
        return httpx.Response(200, json=reply)

    uploads: list[httpx.Request] = []

    def storage(request: httpx.Request) -> httpx.Response:
        uploads.append(request)
        return httpx.Response(200)

    client = MonitorClient(
        MonitorEnv(api_url="https://api.test", token="job-1.2.token"),
        transport=httpx.MockTransport(api),
    )
    assert await client.attempt() == SPEC
    await client.heartbeat(pending_failure_count=2, pending_last_error="timeout")
    state = seams.ControlState()
    with pytest.raises(loops.StopTrial, match="execution revoked"):
        await loops.control_loop(client, state, interval=0)
    assert state.pause_requested is True
    persisted = await seams.build(client, state).persist(
        attempt=1,
        events=[{"seq": 1, "kind": "text", "payload": {"text": "hello"}}],
        checkpoint_values={
            "input_tokens": 10,
            "cache_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 5,
            "cost_usd": 0.01,
        },
    )
    assert persisted == PersistResult(flushed=1, checkpoint_written=True)
    await hooks.LifecycleReporter(client).report(
        summarize_hook_event(contract.finished_trial_event())
    )
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "result.json").write_text("{}")
    outcome = HarborOutcome(
        reward=1.0,
        error=None,
        exit_code=0,
        duration_sec=12.5,
        job_result_path=job_dir / "result.json",
        job_dir=job_dir,
        tool_counts={"bash": 3},
        metrics={"accuracy": 1.0},
    )
    # As runner._deliver_result does: upload, then post the result.
    uploaded = await artifacts.upload(
        client, outcome, transport=httpx.MockTransport(storage)
    )
    await artifacts.post_result(
        client,
        outcome,
        archive_uploaded=uploaded,
        monitor_stats=loops.LoopStats(heartbeats_sent=3).as_dict(),
    )
    await client.aclose()

    assert [(str(r.url), r.headers.get("content-type")) for r in uploads] == [
        (UPLOAD_URL, "application/gzip")
    ], "the archive went where the reply said, with its headers"
    assert not any(replies.values()), "every reply was read"
    routes = {f"{m} {r.path}" for r in monitor.router.routes for m in r.methods}
    assert called == routes, "the monitor calls exactly the API's /monitor routes"
    request_models = {
        f"{method} {route.path}": route.body_field.type_
        for route in monitor.router.routes
        for method in route.methods
        if route.body_field is not None
    }
    assert set(sent) == set(request_models), "bodies the API parses == bodies sent"
    for route, bodies in sent.items():
        for body in bodies:
            parsed = request_models[route].model_validate(body)
            dropped = _dropped(body, parsed.model_dump(mode="json"))
            assert not dropped, f"{route}: the API drops {dropped}"
    [lifecycle] = sent["POST /monitor/lifecycle"]
    assert hook_event_from_summary(lifecycle["summary"]).result is not None
    [result] = sent["POST /monitor/result"]
    kept = {field.name for field in dataclasses.fields(HarborOutcome)}
    assert set(result["outcome"]) <= kept, "settlement keeps the whole outcome"
    assert _outcome_from_payload(result["outcome"], None).reward == 1.0


@pytest.mark.asyncio
async def test_a_field_the_events_reply_adds_stops_running_monitors_live_tail():
    """Why POST /monitor/events is a closed reply. When seams.persist learns to
    ignore unknown fields, delete this test, but keep the CLOSED_REPLIES entry
    until no monitor older than that fix can still run (12 hours after it
    deploys)."""
    added = {
        **monitor.EventsResponse(
            flushed=1,
            checkpoint_written=False,
            trial_finished=False,
            quota_checked=True,
            pause_requested=None,
        ).model_dump(mode="json"),
        "added_later": True,
    }
    client = MonitorClient(
        MonitorEnv(api_url="https://api.test", token="job-1.2.token"),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=added)),
    )
    persist = seams.build(client, seams.ControlState()).persist
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        await persist(attempt=1, events=[], checkpoint_values=None)
    await client.aclose()
