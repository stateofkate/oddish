from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.routing import APIRoute

from api.routers import tasks
from api.routers.trials import router
from oddish.core.endpoints.sweep import _plan_append_trials
from oddish.core.idempotency import (
    EXTERNAL_REQUEST_IDEMPOTENCY_TTL,
    StoredIdempotencyRecord,
    compute_request_hash,
    reserve_idempotency_slot,
)
from oddish.schemas import TaskResponse, TaskSweepSubmission


class MemoryStore:
    def __init__(self) -> None:
        self.record: StoredIdempotencyRecord | None = None
        self.expires_at = None

    async def get(self, org_id, route, key_hash):
        return self.record

    async def begin(self, org_id, route, key_hash, request_hash, now, expires_at):
        self.expires_at = expires_at
        return True

    async def complete(self, org_id, route, key_hash, response_json):
        return None

    async def discard(self, org_id, route, key_hash, now):
        return None


def test_sweep_schema_exposes_exact_version_and_strict_external_identity():
    submission = TaskSweepSubmission(
        task_id="task-1",
        task_version_id="task-1-v3",
        external_request_id="sherpa-request-1",
        append_to_task=True,
        add_trials=True,
        configs=[{"agent": "codex", "model": "model-1", "n_trials": 2}],
    )

    assert submission.task_version_id == "task-1-v3"
    assert submission.external_request_id == "sherpa-request-1"
    assert submission.add_trials is True


def test_new_optional_fields_preserve_legacy_request_hash():
    legacy = TaskSweepSubmission(
        task_id="task-1",
        append_to_task=True,
        configs=[{"agent": "codex", "model": "model-1", "n_trials": 1}],
    )
    payload = legacy.model_dump(mode="json")
    payload.pop("task_version_id")
    payload.pop("external_request_id")
    without_new_fields = SimpleNamespace(
        model_dump=lambda mode: payload,
        registry_auth=legacy.registry_auth,
    )

    assert compute_request_hash(legacy) == compute_request_hash(without_new_fields)


def test_sweep_response_includes_trial_version_refs():
    response = TaskResponse(
        id="task-1",
        name="Task",
        status="running",
        priority="low",
        trials_count=1,
        providers={"openai": 1},
        created_at=datetime.now(UTC),
        new_trial_ids=["trial-1"],
        new_trials=[{"id": "trial-1", "task_version_id": "task-1-v3"}],
    )

    assert response.new_trials[0].task_version_id == "task-1-v3"


@pytest.mark.asyncio
async def test_external_identity_replays_exact_trials_even_after_failure(monkeypatch):
    saved = TaskResponse(
        id="task-1",
        name="Task",
        status="running",
        priority="low",
        trials_count=1,
        providers={"openai": 1},
        created_at=datetime.now(UTC),
        new_trial_ids=["trial-1"],
        new_trials=[{"id": "trial-1", "task_version_id": "task-1-v3"}],
    )

    @asynccontextmanager
    async def session():
        yield object()

    replay = AsyncMock(return_value=saved.model_dump(mode="json"))
    failed_leaf_probe = AsyncMock(return_value=True)
    create = AsyncMock()
    monkeypatch.setattr(tasks, "get_session", session)
    monkeypatch.setattr(tasks, "probe_completed_replay", replay)
    monkeypatch.setattr(tasks, "replay_has_retryable_failed_trials", failed_leaf_probe)
    monkeypatch.setattr(tasks, "create_task_sweep_core", create)

    response = await tasks.create_task_sweep(
        TaskSweepSubmission(
            task_id="task-1",
            task_version_id="task-1-v3",
            external_request_id="sherpa-request-1",
            append_to_task=True,
            add_trials=True,
            configs=[{"agent": "codex", "model": "model-1", "n_trials": 1}],
        ),
        SimpleNamespace(org_id="org-1", require_scope=lambda scope: None),
        idempotency_key="ignored-client-key",
    )

    assert response.new_trial_ids == ["trial-1"]
    assert replay.await_args.kwargs["raw_key"] == "external:sherpa-request-1"
    failed_leaf_probe.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_additive_sweep_does_not_reconcile_against_existing_trials():
    submission = TaskSweepSubmission(
        task_id="task-1",
        append_to_task=True,
        task_version_id="task-1-v3",
        add_trials=True,
        configs=[{"agent": "codex", "model": "model-1", "n_trials": 2}],
    )
    session = SimpleNamespace(execute=AsyncMock())

    trials, superseded = await _plan_append_trials(
        session,
        task=SimpleNamespace(id="task-1", current_version_id="task-1-v3"),
        submission=submission,
        target_experiment_id="experiment-1",
        append_version_id="task-1-v3",
        default_environment=None,
        allowed_environments=None,
    )

    assert len(trials) == 2
    assert superseded == [[], []]
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_request_ttl_can_cover_long_lived_workflows():
    store = MemoryStore()
    now = datetime(2026, 9, 20, tzinfo=UTC)

    await reserve_idempotency_slot(
        store,
        org_id="org-1",
        route="POST /tasks/sweep",
        raw_key="external:sherpa-request-1",
        request_hash="a" * 64,
        now=now,
        ttl=EXTERNAL_REQUEST_IDEMPOTENCY_TTL,
    )

    assert store.expires_at == now + EXTERNAL_REQUEST_IDEMPOTENCY_TTL


def test_batch_status_and_exact_cancel_routes_are_registered():
    routes = {
        (route.path, frozenset(route.methods or set()))
        for route in router.routes
        if isinstance(route, APIRoute)
    }
    assert ("/trials/status/query", frozenset({"POST"})) in routes
    assert ("/trials/cancel/batch", frozenset({"POST"})) in routes
