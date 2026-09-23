"""Monitor endpoint tests; the database functions they call are faked."""

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from api.routers import monitor
from fastapi import FastAPI, HTTPException
from oddish.db.storage import StorageClient
from oddish.workers.launcher import token as monitor_token
from oddish.workers.launcher.token import MonitorAttempt
from oddish.workers.queue.worker_job_single_job import SandboxCapacityLeaseLostError

ATTEMPT = MonitorAttempt(
    worker_job_id="job-1",
    attempt=2,
    worker_id="worker-1",
    trial_id="trial-1",
    org_id="org-1",
    queue_slot=4,
)


@pytest.fixture
def client(monkeypatch):
    seen = {}

    async def authorize(credential):
        seen["credential"] = credential
        if credential == "job-1.2.good":
            return ATTEMPT
        if credential == "job-1.1.old":
            raise monitor_token.MonitorGone("A newer attempt owns this job")
        raise monitor_token.MonitorUnauthorized("Unknown monitor credential")

    monkeypatch.setattr(monitor_token, "authorize_monitor", authorize)
    app = FastAPI()
    app.include_router(monitor.router)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test"), seen


def _auth(token="job-1.2.good"):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_heartbeat_writes_through_the_workers_heartbeat(client, monkeypatch):
    http, _ = client
    calls = []

    async def record(attempt, **kwargs):
        calls.append((attempt, kwargs))
        return True

    monkeypatch.setattr(monitor.monitor_heartbeat, "record_monitor_heartbeat", record)
    response = await http.post(
        "/monitor/heartbeat",
        headers=_auth(),
        json={"pending_failure_count": 2, "pending_last_error": "timeout"},
    )
    assert response.status_code == 200
    assert calls == [
        (ATTEMPT, {"pending_failure_count": 2, "pending_last_error": "timeout"})
    ]


@pytest.mark.asyncio
async def test_bad_token_is_401_and_stale_attempt_is_410(client):
    http, _ = client
    assert (await http.post("/monitor/heartbeat", json={})).status_code == 401
    bad = await http.post("/monitor/heartbeat", headers=_auth("nope"), json={})
    assert bad.status_code == 401
    old = await http.post("/monitor/heartbeat", headers=_auth("job-1.1.old"), json={})
    assert old.status_code == 410


@pytest.mark.asyncio
async def test_lost_ownership_or_capacity_lease_is_410(client, monkeypatch):
    http, _ = client

    async def not_owned(attempt, **kwargs):
        return False

    monkeypatch.setattr(
        monitor.monitor_heartbeat, "record_monitor_heartbeat", not_owned
    )
    response = await http.post("/monitor/heartbeat", headers=_auth(), json={})
    assert response.status_code == 410

    async def lease_lost(attempt, **kwargs):
        raise SandboxCapacityLeaseLostError("lease gone")

    monkeypatch.setattr(
        monitor.monitor_heartbeat, "record_monitor_heartbeat", lease_lost
    )
    response = await http.post("/monitor/heartbeat", headers=_auth(), json={})
    assert response.status_code == 410


@pytest.mark.asyncio
async def test_control_reports_approval_and_quota_pause(client, monkeypatch):
    http, _ = client
    asked = {}

    async def approved(org_id):
        asked["org_id"] = org_id

    async def pause_requested(session, *, org_id, billed_user_id):
        asked["pause"] = (org_id, billed_user_id)
        return True

    @asynccontextmanager
    async def read_session():
        async def get(model, trial_id):
            return SimpleNamespace(billed_user_id="user-1")

        yield SimpleNamespace(get=get)

    monkeypatch.setattr(monitor, "require_execution_org", approved)
    monkeypatch.setattr(monitor, "quota_pause_requested", pause_requested)
    monkeypatch.setattr(monitor, "get_read_session", read_session)

    response = await http.get("/monitor/control", headers=_auth())
    assert response.status_code == 200
    assert response.json() == {
        "authorized": True,
        "detail": None,
        "pause_requested": True,
    }
    assert asked == {"org_id": "org-1", "pause": ("org-1", "user-1")}


@pytest.mark.asyncio
async def test_control_reports_revoked_approval(client, monkeypatch):
    http, _ = client

    async def denied(org_id):
        raise HTTPException(403, "Organization is not approved for execution")

    monkeypatch.setattr(monitor, "require_execution_org", denied)
    response = await http.get("/monitor/control", headers=_auth())
    assert response.status_code == 200
    assert response.json()["authorized"] is False
    assert "not approved" in response.json()["detail"]


@pytest.mark.asyncio
async def test_events_run_the_tailers_persist_tick_for_the_credentials_trial(
    client, monkeypatch
):
    http, _ = client
    calls = []

    async def persist(**kwargs):
        calls.append(kwargs)
        return monitor.live_tail.PersistResult(
            flushed=1, checkpoint_written=True, pause_requested=True
        )

    @asynccontextmanager
    async def read_session():
        async def get(model, trial_id):
            return SimpleNamespace(billed_user_id="user-1")

        yield SimpleNamespace(get=get)

    monkeypatch.setattr(monitor.live_tail, "persist_tick_to_database", persist)
    monkeypatch.setattr(monitor, "get_read_session", read_session)

    response = await http.post(
        "/monitor/events",
        headers=_auth(),
        json={
            "trial_attempt": 1,
            "events": [{"seq": 1, "kind": "text", "payload": {"text": "hi"}}],
            "checkpoint": {"input_tokens": 10, "cost_usd": 0.5},
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "flushed": 1,
        "checkpoint_written": True,
        "trial_finished": False,
        "quota_checked": True,
        "pause_requested": True,
    }
    (call,) = calls
    # Identity comes from the credential and the trial row, not the body.
    assert (call["trial_id"], call["org_id"], call["billed_user_id"]) == (
        "trial-1",
        "org-1",
        "user-1",
    )
    assert call["attempt"] == 1
    assert call["events"] == [{"seq": 1, "kind": "text", "payload": {"text": "hi"}}]
    assert call["checkpoint_values"]["cost_usd"] == 0.5


@pytest.mark.asyncio
async def test_events_reject_a_stale_attempt_and_malformed_rows(client):
    http, _ = client
    stale = await http.post(
        "/monitor/events", headers=_auth("job-1.1.old"), json={"trial_attempt": 1}
    )
    assert stale.status_code == 410
    malformed = await http.post(
        "/monitor/events",
        headers=_auth(),
        json={"trial_attempt": 1, "events": [{"seq": 0, "kind": "text"}]},
    )
    assert malformed.status_code == 422


class _Storage:
    def __init__(self, exists: bool):
        self.exists = exists
        self.asked: list[str] = []

    async def get_presigned_upload_url(self, key, *, content_type=None):
        self.asked.append(key)
        return f"https://s3.test/{key}?signed"

    async def object_exists(self, key):
        self.asked.append(key)
        return self.exists


@pytest.mark.asyncio
async def test_artifact_upload_url_is_for_the_credentials_trial(client, monkeypatch):
    http, _ = client
    storage = _Storage(exists=True)
    monkeypatch.setattr(monitor, "get_storage_client", lambda: storage)
    response = await http.post("/monitor/artifacts", headers=_auth())
    assert response.status_code == 200
    body = response.json()
    assert "trial-1" in body["archive_key"]
    assert body["upload_url"].startswith("https://s3.test/")
    assert body["upload_headers"] == {"Content-Type": "application/gzip"}


@pytest.mark.asyncio
async def test_each_attempt_of_a_trial_gets_its_own_archive_key(client, monkeypatch):
    http, _ = client
    retried = replace(ATTEMPT, attempt=3)
    accepted = []

    async def authorize(credential):
        return retried if credential == "job-1.3.good" else ATTEMPT

    async def accept(attempt, *, archive_key, **_):
        accepted.append(archive_key)
        return "accepted"

    monkeypatch.setattr(monitor_token, "authorize_monitor", authorize)
    monkeypatch.setattr(monitor.monitor_result, "accept_monitor_result", accept)
    monkeypatch.setattr(monitor.settings, "monitor_settle_inline", False)
    monkeypatch.setattr(monitor, "get_storage_client", lambda: _Storage(exists=True))
    issued = []
    for token in ("job-1.2.good", "job-1.3.good"):
        upload = await http.post("/monitor/artifacts", headers=_auth(token))
        issued.append(upload.json()["archive_key"])
        body = {"outcome": None, "archive_uploaded": True}
        await http.post("/monitor/result", headers=_auth(token), json=body)
    # A late upload from the old attempt cannot replace the retry's archive.
    assert issued[0] != issued[1]
    prefix = StorageClient.trial_write_prefix("trial-1")
    assert all(key.startswith(prefix) for key in issued)
    # Each result stores the key its own attempt was issued.
    assert accepted == issued


@pytest.mark.asyncio
async def test_result_is_accepted_only_when_the_archive_is_in_storage(
    client, monkeypatch
):
    http, _ = client
    accepted = []

    async def accept(attempt, *, outcome, archive_key, **_):
        accepted.append((attempt, outcome, archive_key))
        return "accepted"

    monkeypatch.setattr(monitor.monitor_result, "accept_monitor_result", accept)
    monkeypatch.setattr(monitor.settings, "monitor_settle_inline", False)
    monkeypatch.setattr(monitor, "get_storage_client", lambda: _Storage(exists=False))
    body = {"outcome": {"reward": 1.0, "exit_code": 0}, "archive_uploaded": True}
    missing = await http.post("/monitor/result", headers=_auth(), json=body)
    assert missing.status_code == 409
    assert accepted == []

    monkeypatch.setattr(monitor, "get_storage_client", lambda: _Storage(exists=True))
    ok = await http.post("/monitor/result", headers=_auth(), json=body)
    assert ok.status_code == 200
    assert ok.json() == {"accepted": True, "duplicate": False}
    ((attempt, outcome, archive_key),) = accepted
    assert attempt == ATTEMPT and outcome["reward"] == 1.0
    assert "trial-1" in archive_key


@pytest.mark.asyncio
async def test_duplicate_result_is_a_no_op_and_a_dead_attempt_is_410(
    client, monkeypatch
):
    http, _ = client
    body = {"outcome": {"reward": 0.0, "exit_code": 0}}

    async def duplicate(attempt, **kwargs):
        return "duplicate"

    monkeypatch.setattr(monitor.monitor_result, "accept_monitor_result", duplicate)
    again = await http.post("/monitor/result", headers=_auth(), json=body)
    assert again.json() == {"accepted": True, "duplicate": True}

    async def gone(attempt, **kwargs):
        raise monitor_token.MonitorGone("This attempt can no longer report a result")

    monkeypatch.setattr(monitor.monitor_result, "accept_monitor_result", gone)
    dead = await http.post("/monitor/result", headers=_auth(), json=body)
    assert dead.status_code == 410


@pytest.mark.asyncio
async def test_lifecycle_event_goes_to_the_workers_hook_handler(client, monkeypatch):
    http, _ = client
    applied = []

    async def apply(attempt, summary):
        applied.append((attempt, summary))
        if summary.get("event") == "bogus":
            raise ValueError("'bogus' is not a valid TrialEvent")

    monkeypatch.setattr(monitor.monitor_lifecycle, "apply_lifecycle_event", apply)
    ok = await http.post(
        "/monitor/lifecycle", headers=_auth(), json={"summary": {"event": "end"}}
    )
    assert ok.status_code == 200
    assert applied == [(ATTEMPT, {"event": "end"})]

    bad = await http.post(
        "/monitor/lifecycle", headers=_auth(), json={"summary": {"event": "bogus"}}
    )
    assert bad.status_code == 422
    stale = await http.post(
        "/monitor/lifecycle", headers=_auth("job-1.1.old"), json={"summary": {}}
    )
    assert stale.status_code == 410


@pytest.mark.asyncio
async def test_attempt_spec_is_served_to_the_owning_attempt_only(client, monkeypatch):
    http, _ = client
    from oddish.mon.contract import AttemptSpec

    async def build(attempt):
        assert attempt == ATTEMPT
        return AttemptSpec(
            trial_id="trial-1",
            worker_job_id="job-1",
            worker_job_attempt=2,
            trial_attempt=1,
            trial_kind="agent",
            task_id="task-1",
            task_name="hello",
            agent="oracle",
            model=None,
            harbor_config=None,
            org_id="org-1",
            billed_user_id=None,
            task_archive_url="https://s3.test/task.tar.gz?signed",
        )

    acked = []

    async def ack(attempt):
        acked.append(attempt)

    monkeypatch.setattr(monitor.monitor_attempt, "build_attempt_spec", build)
    monkeypatch.setattr(monitor.monitor_attempt, "acknowledge_monitor", ack)
    ok = await http.get("/monitor/attempt", headers=_auth())
    assert ok.status_code == 200
    assert ok.json()["agent"] == "oracle"
    # Fetching the spec is the monitor's check-in.
    assert acked == [ATTEMPT]
    assert (await http.get("/monitor/attempt")).status_code == 401
    stale = await http.get("/monitor/attempt", headers=_auth("job-1.1.old"))
    assert stale.status_code == 410


@pytest.mark.asyncio
async def test_inline_settle_is_fenced_to_its_attempt_and_notifies_github(
    client, monkeypatch
):
    from oddish.workers.launcher import settle as monitor_settle
    from worker.github import notify_github_trial

    http, _ = client
    settled = []

    async def accept(attempt, **_):
        return "accepted"

    async def settle(worker_job_id, *, attempt):
        settled.append((worker_job_id, attempt))

    monkeypatch.setattr(monitor.monitor_result, "accept_monitor_result", accept)
    monkeypatch.setattr(monitor_settle, "settle_monitor_result", settle)
    monkeypatch.setattr(monitor.settings, "monitor_settle_inline", True)
    body = {"outcome": {"reward": 1.0, "exit_code": 0}, "archive_uploaded": False}
    ok = await http.post("/monitor/result", headers=_auth(), json=body)
    assert ok.status_code == 200
    assert settled == [(ATTEMPT.worker_job_id, ATTEMPT.attempt)]
    # The API process registered the worker's own hook for settlement to fire.
    assert monitor_settle._post_success_hook is notify_github_trial


@pytest.mark.asyncio
async def test_skills_are_the_trials_selected_ones_read_as_the_worker_reads_them(
    client, monkeypatch
):
    from oddish.worker import probe_staging

    http, _ = client
    trial = SimpleNamespace(org_id="org-1", harbor_config={"skill_ids": ["skill-2"]})
    listed = []

    @asynccontextmanager
    async def read_session():
        async def get(model, trial_id):
            assert trial_id == "trial-1"
            return trial

        yield SimpleNamespace(get=get)

    @asynccontextmanager
    async def session():
        yield SimpleNamespace()

    def skill(skill_id, name):
        files = [SimpleNamespace(relative_path="SKILL.md", content=f"name: {name}")]
        return SimpleNamespace(id=skill_id, name=name, files=files)

    async def list_skills(session, *, org_id=None):
        listed.append(org_id)
        return [skill("skill-1", "alpha"), skill("skill-2", "beta")]

    monkeypatch.setattr(monitor, "get_read_session", read_session)
    # The worker's own loader, down to the query it runs.
    monkeypatch.setattr(probe_staging, "get_session", session)
    monkeypatch.setattr(probe_staging, "list_skills_core", list_skills)

    selected = await http.get("/monitor/skills", headers=_auth())
    assert selected.status_code == 200
    assert selected.json() == {
        "skills": [
            {
                "name": "beta",
                "files": [{"relative_path": "SKILL.md", "content": "name: beta"}],
            }
        ]
    }
    assert listed == ["org-1"]

    trial.harbor_config = {"mode": "probe"}
    none = await http.get("/monitor/skills", headers=_auth())
    assert none.json() == {"skills": []}
    assert listed == ["org-1"]  # nothing selected, nothing read
    stale = await http.get("/monitor/skills", headers=_auth("job-1.1.old"))
    assert stale.status_code == 410
