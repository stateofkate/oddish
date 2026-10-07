from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from harbor.models.environment_subphase import EnvironmentSubphase, SubphaseStatus
from harbor.trial.hooks import TrialEvent

from live_tail_fakes import FakeEnv
from oddish.config import settings
from oddish.workers.harbor import live_tail
from oddish.workers.harbor.live_tail import PersistResult
from oddish.workers.harbor.setup_events import (
    EnvInfoPayload,
    EnvSubphasePayload,
    SetupEventEmitter,
    SetupStepStatus,
    setup_event_from_hook,
)

STARTED_AT = datetime(2026, 10, 5, 23, 8, 2, tzinfo=timezone.utc)


def subphase_event(
    phase: str = "kubernetes.helm_install",
    status: SubphaseStatus = SubphaseStatus.STARTED,
    duration_seconds: float | None = None,
) -> Any:
    return SimpleNamespace(
        event=TrialEvent.ENVIRONMENT_SUBPHASE,
        subphase=EnvironmentSubphase(
            phase=phase,
            status=status,
            provider="ec2",
            started_at=STARTED_AT,
            duration_seconds=duration_seconds,
        ),
    )


def provisioned_event(environment: Any = None, provider: str | None = "ec2") -> Any:
    return SimpleNamespace(
        event=TrialEvent.ENVIRONMENT_PROVISIONED,
        environment=environment,
        environment_provider=provider,
        environment_external_id="i-0a3f9c1",
    )


class RecordingPersist:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    async def __call__(self, **kwargs: Any) -> PersistResult:
        if self.fail:
            raise RuntimeError("database down")
        self.calls.append(kwargs)
        return PersistResult(flushed=len(kwargs["events"]))

    @property
    def rows(self) -> list[dict[str, Any]]:
        return [row for call in self.calls for row in call["events"]]


@pytest.fixture(autouse=True)
def _live_tail_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "live_tail_enabled", True)
    live_tail._allocated_seqs.clear()


def emitter(persist: RecordingPersist, trial_id: str = "t1", attempt: int = 1):
    return SetupEventEmitter(
        trial_id=trial_id,
        attempt=attempt,
        persist=persist,
        org_id="org",
        billed_user_id=None,
    )


def test_subphase_maps_to_typed_payload() -> None:
    payload = setup_event_from_hook(
        subphase_event(status=SubphaseStatus.SUCCESS, duration_seconds=19.2)
    )

    assert payload == EnvSubphasePayload(
        provider="ec2",
        completed_activities=(
            [] if "completed_activities" in EnvironmentSubphase.model_fields else None
        ),
        phase="kubernetes.helm_install",
        status=SetupStepStatus.SUCCESS,
        started_at=STARTED_AT,
        duration_seconds=19.2,
    )


def test_provisioned_reads_instance_from_environment_object() -> None:
    environment = SimpleNamespace(instance_type="m7i-flex.2xlarge", region="us-west-2")

    payload = setup_event_from_hook(provisioned_event(environment))

    assert payload == EnvInfoPayload(
        provider="ec2",
        external_id="i-0a3f9c1",
        instance_type="m7i-flex.2xlarge",
        region="us-west-2",
    )


def test_provisioned_without_instance_attributes_leaves_them_empty() -> None:
    payload = setup_event_from_hook(provisioned_event(SimpleNamespace()))

    assert isinstance(payload, EnvInfoPayload)
    assert payload.instance_type is None
    assert payload.region is None


@pytest.mark.parametrize(
    "hook_event",
    [
        SimpleNamespace(event=TrialEvent.AGENT_START),
        provisioned_event(provider=None),
        SimpleNamespace(event=TrialEvent.ENVIRONMENT_SUBPHASE, subphase=None),
    ],
)
def test_non_setup_events_map_to_none(hook_event: Any) -> None:
    assert setup_event_from_hook(hook_event) is None


@pytest.mark.asyncio
async def test_emitter_writes_rows_in_seq_order_with_kind_split_out() -> None:
    persist = RecordingPersist()
    setup = emitter(persist)

    await setup.observe(provisioned_event(SimpleNamespace()))
    await setup.observe(subphase_event())

    assert [row["seq"] for row in persist.rows] == [1, 2]
    assert [row["kind"] for row in persist.rows] == ["env_info", "env_subphase"]
    assert "kind" not in persist.rows[1]["payload"]
    assert persist.rows[1]["payload"]["status"] == "started"
    assert persist.calls[0]["attempt"] == 1
    assert persist.calls[0]["checkpoint_values"] is None


@pytest.mark.asyncio
async def test_emitter_stops_at_agent_start() -> None:
    persist = RecordingPersist()
    setup = emitter(persist)

    await setup.observe(subphase_event())
    await setup.observe(SimpleNamespace(event=TrialEvent.AGENT_START))
    await setup.observe(subphase_event(phase="kubernetes.helm_uninstall"))

    assert [row["payload"]["phase"] for row in persist.rows] == [
        "kubernetes.helm_install"
    ]


@pytest.mark.asyncio
async def test_emitter_never_raises() -> None:
    setup = emitter(RecordingPersist(fail=True))

    assert await setup.observe(subphase_event()) is None


@pytest.mark.asyncio
async def test_emitter_respects_live_tail_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "live_tail_enabled", False)
    persist = RecordingPersist()

    await emitter(persist).observe(subphase_event())

    assert persist.rows == []


def test_allocated_seqs_restart_for_a_new_attempt() -> None:
    assert live_tail.allocate_seq("t1", 1) == 1
    assert live_tail.allocate_seq("t1", 1) == 2
    assert live_tail.allocate_seq("t1", 2) == 1


@pytest.mark.asyncio
async def test_tailer_continues_after_setup_rows() -> None:
    persist = RecordingPersist()
    setup = emitter(persist, trial_id="t-seq", attempt=1)
    for _ in range(3):
        await setup.observe(subphase_event())

    live_tail.start(
        trial_id="t-seq",
        environment=FakeEnv([]),
        attempt=1,
        agent="claude-code",
        model=None,
        persist=persist,
    )
    tailer, _task = live_tail._tailers["t-seq"]
    assert tailer.seq == 3

    assert await live_tail.shutdown("t-seq", timeout_sec=1) == 1
    assert "t-seq" not in live_tail._allocated_seqs


@pytest.mark.asyncio
async def test_shutdown_without_tailer_still_reports_attempt_for_purge() -> None:
    await emitter(RecordingPersist(), trial_id="t-oracle", attempt=2).observe(
        subphase_event()
    )

    assert await live_tail.shutdown("t-oracle") == 2
    assert await live_tail.shutdown("t-oracle") is None


@pytest.mark.asyncio
async def test_worker_hook_writes_setup_progress_without_touching_the_trial_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from oddish.workers.queue import trial_handler

    def no_session(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("setup progress must not open a trial session")

    monkeypatch.setattr(trial_handler, "_trial_session", no_session)
    persist = RecordingPersist()

    await trial_handler.handle_harbor_event(
        subphase_event(),
        trial_id="t-worker",
        setup_events=emitter(persist, trial_id="t-worker"),
    )

    assert [row["kind"] for row in persist.rows] == ["env_subphase"]


class RecordingJob:
    def __init__(self, with_fork_events: bool = True) -> None:
        self.registered: list[str] = []
        if with_fork_events:
            self.on_environment_provisioned = self._register("environment_provisioned")
            self.on_environment_subphase = self._register("environment_subphase")
        for name in (
            "trial_started",
            "environment_started",
            "agent_started",
            "agent_ended",
            "verification_started",
            "trial_ended",
            "trial_cancelled",
        ):
            setattr(self, f"on_{name}", self._register(name))

    def _register(self, name: str):
        return lambda _callback: self.registered.append(name)


def test_runner_subscribes_to_setup_progress_when_harbor_offers_it() -> None:
    from oddish.workers.harbor.runner import _register_lifecycle_hooks

    async def callback(_event: Any) -> None:
        return None

    job = RecordingJob()
    _register_lifecycle_hooks(job, callback)
    assert "environment_subphase" in job.registered
    assert len(job.registered) == 9

    upstream = RecordingJob(with_fork_events=False)
    _register_lifecycle_hooks(upstream, callback)
    assert "environment_subphase" not in upstream.registered
    assert len(upstream.registered) == 7


def test_ephemeral_child_and_bridge_carry_setup_progress() -> None:
    from oddish.workers.harbor._entry import _serialize_setup
    from oddish.workers.harbor.ephemeral import _bridge_event

    child_subphase = subphase_event(
        status=SubphaseStatus.SUCCESS, duration_seconds=19.2
    )
    child_provisioned = provisioned_event(
        SimpleNamespace(instance_type="m7i-flex.2xlarge", region="us-west-2")
    )
    lines = [
        {"event": "environment-subphase", **_serialize_setup(child_subphase)},
        {
            "event": "environment-provisioned",
            "environment_provider": "ec2",
            "environment_external_id": "i-0a3f9c1",
            **_serialize_setup(child_provisioned),
        },
    ]

    bridged = [_bridge_event(line, trial_id="t-eph") for line in lines]

    assert bridged[1].environment is None
    assert setup_event_from_hook(bridged[0]) == EnvSubphasePayload(
        provider="ec2",
        completed_activities=(
            [] if "completed_activities" in EnvironmentSubphase.model_fields else None
        ),
        phase="kubernetes.helm_install",
        status=SetupStepStatus.SUCCESS,
        started_at=STARTED_AT,
        duration_seconds=19.2,
    )
    assert setup_event_from_hook(bridged[1]) == EnvInfoPayload(
        provider="ec2",
        external_id="i-0a3f9c1",
        instance_type="m7i-flex.2xlarge",
        region="us-west-2",
    )


@pytest.mark.parametrize("status", ["started", "success", "error", "cancelled"])
def test_v2_survives_actual_child_serializer_and_bridge(status):
    from oddish.workers.harbor._entry import _serialize_setup
    from oddish.workers.harbor.ephemeral import _bridge_event

    original = EnvSubphasePayload(
        schema_version=2, phase="compose.build", status=status,
        provider="docker", operation_id="build-2", parent_operation_id="setup-1",
        category="build", resource_layer="task_environment", strategy="compose",
        operation_attempt=2, started_at=STARTED_AT,
        finished_at=STARTED_AT if status != "started" else None,
        duration_seconds=0 if status != "started" else None,
    )
    child = SimpleNamespace(subphase=original, environment=None)
    bridged = _bridge_event(
        {"event": "environment-subphase", **_serialize_setup(child)}, trial_id="t"
    )
    assert setup_event_from_hook(bridged) == original


def test_old_hook_without_identity_keeps_v1_compatible():
    event = SimpleNamespace(
        event=TrialEvent.ENVIRONMENT_SUBPHASE,
        subphase=SimpleNamespace(phase="build", status="success", started_at=STARTED_AT,
                                 duration_seconds=1, error_type=None),
    )
    payload = setup_event_from_hook(event)
    assert payload.schema_version == 1
    assert payload.operation_id is None
    assert payload.provider is None


@pytest.mark.asyncio
async def test_completed_measurements_piggyback_one_existing_notification():
    from oddish.progress import CompletedEnvironmentActivity
    from oddish.workers.harbor._entry import _serialize_setup
    from oddish.workers.harbor.ephemeral import _bridge_event

    activity = CompletedEnvironmentActivity(
        operation_id="wrapper",
        phase="environment.build",
        provider="docker",
        category="build",
        status="success",
        started_at=STARTED_AT,
        finished_at=STARTED_AT,
        duration_seconds=0.2,
    )
    delivery = activity.model_copy(
        update={
            "operation_id": "delivery",
            "parent_operation_id": "wrapper",
            "phase": "progress.delivery.finished",
            "activity_kind": "supporting",
        }
    )
    original = EnvSubphasePayload(
        schema_version=2,
        operation_id="root",
        phase="environment.setup",
        status="success",
        started_at=STARTED_AT,
        finished_at=STARTED_AT,
        duration_seconds=1,
        completed_activities=[activity, delivery],
    )
    child = SimpleNamespace(
        event=TrialEvent.ENVIRONMENT_SUBPHASE,
        subphase=original,
        environment=None,
    )
    bridged = _bridge_event(
        {"event": "environment-subphase", **_serialize_setup(child)}, trial_id="t1"
    )
    assert setup_event_from_hook(child) == setup_event_from_hook(bridged) == original
    persist = RecordingPersist()
    await emitter(persist).observe(child)
    assert len(persist.calls) == len(persist.rows) == 1
    assert (
        persist.rows[0]["payload"]["completed_activities"][1]["activity_kind"]
        == "supporting"
    )


def test_real_harbor_completed_activity_model_crosses_bridge():
    import harbor.models.environment_subphase as harbor_progress
    from oddish.workers.harbor._entry import _serialize_setup
    from oddish.workers.harbor.ephemeral import _bridge_event

    model = getattr(harbor_progress, "CompletedEnvironmentActivity", None)
    if model is None:
        pytest.skip("Legacy Harbor has no completed activity producer")
    activity = model(
        operation_id="body", parent_operation_id="root", provider="docker",
        phase="environment.setup.operation", status="success", started_at=STARTED_AT,
        finished_at=STARTED_AT, duration_seconds=0, activity_kind="operation",
    )
    subphase = EnvironmentSubphase(
        schema_version=2, operation_id="root", provider="docker",
        phase="environment.setup", status="success", started_at=STARTED_AT,
        finished_at=STARTED_AT, duration_seconds=0, completed_activities=[activity],
    )
    hook = SimpleNamespace(event=TrialEvent.ENVIRONMENT_SUBPHASE,
                           subphase=subphase, environment=None)
    bridged = _bridge_event(
        {"event": "environment-subphase", **_serialize_setup(hook)}, trial_id="t"
    )
    actual = setup_event_from_hook(hook)
    assert actual == setup_event_from_hook(bridged)
    assert actual.completed_activities[0].operation_id == "body"


@pytest.mark.asyncio
async def test_setup_does_not_consume_transcript_budget_across_restart_and_retry() -> None:
    persist = RecordingPersist()
    trial_id = "t-full-transcript"

    def start(attempt: int):
        live_tail.start(
            trial_id=trial_id, environment=FakeEnv([]), attempt=attempt,
            agent="claude-code", model=None, persist=persist,
        )
        return live_tail._tailers[trial_id][0]

    message = {"kind": "message", "payload": {"text": "output"}}
    try:
        setup = emitter(persist, trial_id=trial_id, attempt=1)
        for _ in range(3):
            await setup.observe(subphase_event())
        tailer = start(1)
        tailer._buffer_events([message] * 2500)
        await tailer._persist_tick()
        tailer = start(1)
        tailer._buffer_events([message] * 2500)
        await tailer._persist_tick()
        assert not tailer.capped
        assert len([row for row in persist.rows if row["kind"] == "message"]) == 5000
        assert [row["seq"] for row in persist.rows] == list(range(1, 5004))
        tailer._buffer_events([message, message])
        await tailer._persist_tick()
        assert tailer.capped
        assert persist.rows[-1]["kind"] == "summary"
        assert persist.rows[-1]["seq"] == 5004

        await emitter(persist, trial_id=trial_id, attempt=2).observe(subphase_event())
        tailer = start(2)
        tailer._buffer_events([message] * 5000)
        assert not tailer.capped
        assert tailer.seq == 5001
        assert len(tailer.pending_events) == 5000
    finally:
        await live_tail.shutdown(trial_id, timeout_sec=1)


@pytest.mark.asyncio
async def test_setup_bound_does_not_exhaust_transcript_budget(monkeypatch) -> None:
    monkeypatch.setattr(live_tail, "MAX_TRIAL_EVENTS", 3)
    persist = RecordingPersist()
    trial_id = "t-separate-bounds"
    setup = emitter(persist, trial_id=trial_id, attempt=1)
    for _ in range(5):
        await setup.observe(subphase_event())
    assert len(persist.rows) == 3
    try:
        live_tail.start(
            trial_id=trial_id, environment=FakeEnv([]), attempt=1,
            agent="claude-code", model=None, persist=persist,
        )
        tailer = live_tail._tailers[trial_id][0]
        tailer._buffer_events([{"kind": "message", "payload": {"text": "output"}}] * 4)
        await tailer._persist_tick()
        assert len([row for row in persist.rows if row["kind"] == "message"]) == 3
        assert len({row["seq"] for row in persist.rows}) == len(persist.rows)
        assert persist.rows[-1]["kind"] == "summary"
    finally:
        await live_tail.shutdown(trial_id, timeout_sec=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("bridge", [False, True])
async def test_ec2_health_is_typed_and_retained_for_native_and_ephemeral_hooks(bridge):
    from harbor.models.ec2_health import EC2Health
    from oddish.progress import EnvSubphasePayload, ProgressEvent

    event = subphase_event(phase="ec2.instance_status_ok")
    event.subphase.schema_version = 2
    event.subphase.operation_id = "health-wait"
    event.subphase.ec2_health = EC2Health(
        observed_at=STARTED_AT,
        instance_state="running",
        instance_status="initializing",
        system_status="ok",
    )
    if bridge:
        from oddish.workers.harbor._entry import _serialize_setup
        from oddish.workers.harbor.ephemeral import _bridge_event

        event = _bridge_event(
            {"event": "environment-subphase", **_serialize_setup(event)}, trial_id="t1"
        )
    persist = RecordingPersist()
    writer = emitter(persist)
    await writer.observe(event)
    payload = EnvSubphasePayload.model_validate(persist.rows[0]["payload"])
    assert payload.ec2_health.instance_status == "initializing"
    assert payload.ec2_health.system_status == "ok"
    assert payload.ec2_health.observed_at == STARTED_AT
    retained = ProgressEvent(
        attempt=1,
        event_key="live:1",
        kind="env_subphase",
        source_seq=1,
        schema_version=2,
        recorded_at=STARTED_AT,
        payload=payload,
    )
    assert (
        ProgressEvent.model_validate_json(retained.model_dump_json()).payload == payload
    )
    assert payload.operation_id == "health-wait"
    assert payload.started_at == STARTED_AT


@pytest.mark.asyncio
async def test_invalid_health_payload_does_not_interrupt_trial():
    event = subphase_event(phase="ec2.instance_status_ok")
    event.subphase = SimpleNamespace(**event.subphase.model_dump(mode="json"))
    event.subphase.ec2_health = {
        "observed_at": STARTED_AT.isoformat(),
        "instance_status": "invented",
    }
    persist = RecordingPersist()
    assert await emitter(persist).observe(event) is None
    assert persist.rows == []
