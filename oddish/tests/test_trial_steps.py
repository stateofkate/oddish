"""``trial_steps``: the attempt start split into ``start_trial_attempt`` (the
writes, on a row the caller locked) and ``load_trial_context`` (the reads),
and the property settlement relies on: building the context of an observed
attempt changes nothing. No database: rows are namespaces, the session a fake."""

from __future__ import annotations

import copy
import inspect
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from oddish.db import TaskStatus, TrialStatus
from oddish.workers.launcher import settle
from oddish.workers.queue import trial_steps


def _trial(**overrides):
    fields = dict(
        id="task-1-2",
        task_id="task-1",
        task_version_id="tv-7",
        experiment_id="exp-1",
        org_id="org-1",
        billed_user_id="bill-1",
        agent="codex",
        model="gpt-5",
        kind="agent",
        queue_key="stale-queue",
        environment="modal",
        harbor_config=None,
        status=TrialStatus.RETRYING,
        attempts=1,
        max_attempts=6,
        idempotency_key="idem-1",
        superseded_by_trial_id=None,
        started_at=None,
        finished_at="earlier",
        next_retry_at="soon",
        harbor_stage="verifier",
        reward=0.5,
        error_message="old error",
        harbor_result_path="old/result.json",
        trial_s3_key="old/key",
        result={"old": True},
        input_tokens=1,
        cache_tokens=1,
        cache_write_tokens=1,
        output_tokens=1,
        total_steps=1,
        trajectory_duration_seconds=1.0,
        total_tool_calls=1,
        tool_counts={"x": 1},
        cost_usd=1.0,
        phase_timing={"agent": {}},
        has_trajectory=True,
        current_worker_id=None,
        current_queue_slot=None,
        claimed_at=None,
        heartbeat_at=None,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _rows():
    from oddish.db import ExperimentModel, TaskModel, TaskVersionModel

    return {
        TaskModel: SimpleNamespace(
            id="task-1",
            name="hello",
            tags={"repo": "r"},
            status=TaskStatus.PENDING,
            started_at=None,
            task_path="/head/task",
            task_s3_key="tasks/head/",
            created_by_user_id=None,
        ),
        ExperimentModel: SimpleNamespace(
            id="exp-1", name="exp", owner_user_id="owner-9"
        ),
        TaskVersionModel: SimpleNamespace(
            id="tv-7", version=7, task_path="/pinned/task", task_s3_key="tasks/v7/"
        ),
    }


class _Session:
    """``get(model, id)`` from a row table; records every lookup."""

    def __init__(self, rows):
        self.rows = rows
        self.lookups: list = []

    async def get(self, model, key, **_kwargs):
        self.lookups.append((model.__name__, key))
        return self.rows.get(model)


@pytest.fixture
def steps_settings(monkeypatch):
    monkeypatch.setattr(
        trial_steps,
        "settings",
        SimpleNamespace(
            harbor_environment="modal",
            model_for_stored_trial=lambda agent, model, provider: model,
            normalize_trial_model=lambda agent, model: f"{agent}/{model}",
            get_queue_key_for_trial=lambda agent, model: f"queue:{agent}",
        ),
    )


# --- load_trial_context: reads only --------------------------------------


@pytest.mark.asyncio
async def test_load_trial_context_reads_the_pinned_version_not_the_head():
    rows = _rows()
    session = _Session(rows)

    prepared = await trial_steps.load_trial_context(
        session, _trial(), fallback_from_environment="archil"
    )

    assert (prepared.task_path, prepared.task_s3_key, prepared.task_version) == (
        "/pinned/task",
        "tasks/v7/",
        7,
    )
    assert prepared.task_version_id == "tv-7"
    assert prepared.task_id == "task-1" and prepared.task_name == "hello"
    assert prepared.task_tags == {"repo": "r"}
    assert prepared.experiment_id == "exp-1" and prepared.experiment_name == "exp"
    # The task submitter is unset, so the experiment owner funds BYOK.
    assert prepared.created_by_user_id == "owner-9"
    assert prepared.attempt_number == 3  # "task-1-2" is the third trial
    assert prepared.trial_attempt == 1  # the attempt as observed, not started
    assert prepared.trial_model == "gpt-5"  # the row's model, as stored
    assert prepared.fallback_from_environment == "archil"
    assert prepared.org_id == "org-1" and prepared.billed_user_id == "bill-1"


@pytest.mark.asyncio
async def test_load_trial_context_falls_back_to_the_head_without_a_pin():
    rows = _rows()
    session = _Session(rows)

    prepared = await trial_steps.load_trial_context(
        session, _trial(task_version_id=None, experiment_id=None)
    )

    assert (prepared.task_path, prepared.task_s3_key, prepared.task_version) == (
        "/head/task",
        "tasks/head/",
        None,
    )
    assert prepared.experiment_id == "" and prepared.experiment_name is None
    assert prepared.created_by_user_id is None
    assert ("ExperimentModel", None) not in session.lookups


@pytest.mark.asyncio
async def test_load_trial_context_changes_no_row():
    """The settlement property: building the context of an observed attempt
    writes nothing, on the trial or on any row it reads."""
    rows = _rows()
    trial = _trial()
    before = (
        copy.deepcopy(vars(trial)),
        {m: copy.deepcopy(vars(r)) for m, r in rows.items()},
    )

    await trial_steps.load_trial_context(_Session(rows), trial)

    assert vars(trial) == before[0]
    assert {m: vars(r) for m, r in rows.items()} == before[1]
    assert rows[next(iter(rows))].status == TaskStatus.PENDING  # the task too


# --- start_trial_attempt: the writes ----------------------------------------


@pytest.mark.asyncio
async def test_start_trial_attempt_resets_the_row_and_counts_the_attempt(
    steps_settings,
):
    rows = _rows()
    trial = _trial()

    await trial_steps.start_trial_attempt(
        _Session(rows), trial, worker_id="w-1", queue_slot=3
    )

    assert trial.status == TrialStatus.RUNNING
    assert trial.attempts == 2
    assert trial.started_at is not None and trial.finished_at is None
    assert trial.next_retry_at is None and trial.harbor_stage == "starting"
    for name in (
        "reward",
        "error_message",
        "harbor_result_path",
        "trial_s3_key",
        "result",
        "input_tokens",
        "cache_tokens",
        "cache_write_tokens",
        "output_tokens",
        "total_steps",
        "trajectory_duration_seconds",
        "total_tool_calls",
        "tool_counts",
        "cost_usd",
        "phase_timing",
    ):
        assert getattr(trial, name) is None, name
    assert trial.has_trajectory is False
    assert trial.idempotency_key == "idem-1"  # kept when set
    # The worker claims the row and the model and queue key are normalised.
    assert (trial.current_worker_id, trial.current_queue_slot) == ("w-1", 3)
    assert trial.claimed_at is not None and trial.heartbeat_at == trial.claimed_at
    assert trial.model == "codex/gpt-5" and trial.queue_key == "queue:codex"
    # A pending task starts with its first attempt.
    task = rows[next(iter(rows))]
    assert task.status == TaskStatus.RUNNING and task.started_at is not None


@pytest.mark.asyncio
async def test_start_trial_attempt_mints_an_idempotency_key_when_missing(
    steps_settings,
):
    trial = _trial(idempotency_key=None)

    await trial_steps.start_trial_attempt(
        _Session(_rows()), trial, worker_id=None, queue_slot=None
    )

    assert trial.idempotency_key


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "attempts", "environment", "expected"),
    [
        (TrialStatus.RETRYING, 2, "archil", "modal"),  # second retry falls back
        (TrialStatus.RETRYING, 1, "archil", "archil"),  # first retry stays
        (TrialStatus.RETRYING, 6, "archil", "archil"),  # last attempt stays
        (TrialStatus.PENDING, 2, "archil", "archil"),  # only retries fall back
        (TrialStatus.RETRYING, 2, "modal", "modal"),
    ],
)
async def test_start_trial_attempt_applies_the_archil_fallback_at_pickup(
    steps_settings, status, attempts, environment, expected
):
    trial = _trial(status=status, attempts=attempts, environment=environment)

    await trial_steps.start_trial_attempt(
        _Session(_rows()), trial, worker_id=None, queue_slot=None
    )

    assert trial.environment == expected


def test_start_trial_attempt_routes_the_pin_through_the_refresh():
    """Worker-runtime invariant 7: the claim rewrites the recorded harbor pin.
    Pin the wiring, not just the helper."""
    source = inspect.getsource(trial_steps.start_trial_attempt)
    assert "refresh_stable_variant_pin(trial)" in source


# --- prepare_trial_run: both steps under one lock ---------------------------


@pytest.mark.asyncio
async def test_prepare_trial_run_starts_then_reads_inside_the_locked_session(
    monkeypatch, steps_settings
):
    rows = _rows()
    trial = _trial()
    events: list = []
    session = _Session(rows)

    @asynccontextmanager
    async def trial_session(trial_id, *, with_for_update=False, **_kwargs):
        events.append(("lock", trial_id, with_for_update))
        yield session, trial
        events.append(("commit", trial.status, trial.attempts))

    async def refresh(sess, version_ids):
        events.append(("browse", sess is session, list(version_ids)))

    monkeypatch.setattr(trial_steps, "_trial_session", trial_session)
    monkeypatch.setattr(trial_steps, "refresh_task_browse_summaries", refresh)

    prepared = await trial_steps.prepare_trial_run(
        trial_id="task-1-2",
        worker_id="w-1",
        queue_slot=0,
        modal_function_call_id=None,
        fallback_from_environment="archil",
    )

    assert events == [
        ("lock", "task-1-2", True),
        ("browse", True, ["tv-7"]),
        ("commit", TrialStatus.RUNNING, 2),
    ]
    # The context sees the started attempt: the incremented count and the
    # normalised model, from the same transaction.
    assert prepared.trial_attempt == 2
    assert prepared.trial_model == "codex/gpt-5"
    assert prepared.task_path == "/pinned/task"
    assert prepared.fallback_from_environment == "archil"


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["absent", "superseded"])
async def test_prepare_trial_run_leaves_a_missing_or_superseded_trial_alone(
    monkeypatch, missing
):
    trial = None if missing == "absent" else _trial(superseded_by_trial_id="task-1-3")
    snapshot = copy.deepcopy(vars(trial)) if trial else None

    @asynccontextmanager
    async def trial_session(trial_id, **_kwargs):
        yield _Session(_rows()), trial

    monkeypatch.setattr(trial_steps, "_trial_session", trial_session)

    prepared = await trial_steps.prepare_trial_run(
        trial_id="task-1-2", worker_id="w-1", queue_slot=0, modal_function_call_id=None
    )

    assert prepared is None
    if trial is not None:
        assert vars(trial) == snapshot


# --- settlement never starts an attempt ------------------------------------


def test_settlement_never_calls_the_starter():
    """``settle_monitor_result`` rebuilds the context of the attempt it
    observed; the starter would restart it (status RUNNING, attempts + 1)."""
    source = inspect.getsource(settle)
    assert "load_trial_context" in source
    assert "start_trial_attempt" not in source
    assert "prepare_trial_run" not in source
    assert not hasattr(settle, "start_trial_attempt")
    assert not hasattr(settle, "prepare_trial_run")


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("provider", ["anthropic", "bedrock"])
async def test_legacy_claude_attempt_preserves_row_and_uses_stored_provider(monkeypatch, flag, provider):
    from oddish.config import settings
    monkeypatch.setattr(settings, "claude_code_force_direct_api", flag)
    trial = _trial(agent="claude-code", model="claude-opus-4-8", provider=provider, queue_key="historical-queue")
    session = _Session(_rows())
    await trial_steps.start_trial_attempt(session, trial, worker_id="w", queue_slot=0)
    prepared = await trial_steps.load_trial_context(session, trial)
    assert trial.model == "claude-opus-4-8"
    assert trial.provider == provider
    assert trial.queue_key == "historical-queue"
    assert settings.get_provider_for_trial(prepared.trial_agent, prepared.trial_model) == provider
    from oddish.workers.queue.byok import uses_direct_anthropic
    assert uses_direct_anthropic(prepared.trial_agent, prepared.trial_model, settings=settings) == (provider == "anthropic")
