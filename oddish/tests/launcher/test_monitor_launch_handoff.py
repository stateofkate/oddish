"""The launcher hands off as soon as the sandbox exists.

It never reads the monitor's check-in: from the hand-off the reconciler owns
the attempt (stale heartbeat, provider probe, launch deadline). What the
launcher still owns is everything before and at create.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace

import pytest

from oddish.costs.modal_cost import SpanResources
from oddish.workers.launcher import costs as monitor_costs
from oddish.workers.launcher import launch as ml


def _job():
    return SimpleNamespace(
        id="job-1", attempts=1, subject_id="trial-1", worker_id="w-1",
        queue_slot=0, modal_function_call_id=None, reroute_from_environment=None,
    )  # fmt: skip


def _resources() -> SpanResources:
    return SpanResources(
        cpu_request=1.0, cpu_limit=4.0, mem_request_mb=1024, mem_limit_mb=4096,
        gpu_type=None, gpu_count=0, price_multiplier=Decimal(1),
        container_class="sandbox", spec_source="pinned",
        cpu_enforcement_mode="limit", mem_enforcement_mode="request",
    )  # fmt: skip


async def _val(v):
    return v


async def _noop(*a, **k):
    return None


class _Starter:
    provider = "modal"

    def __init__(self, fail_start=False):
        self.fail_start = fail_start
        self.started: list = []
        self.stopped: list = []

    def provisioned(self, size):
        return size

    async def stop(self, external_id):
        self.stopped.append(external_id)


def _stub_launch(
    monkeypatch, starter, *, beat=None, resolve=None, persists=True, owns=True
):
    """Stub every collaborator of ``launch_monitor_attempt``; returns the calls."""
    calls = SimpleNamespace(
        settled=[], revoked=[], persisted=[], spans=[], ownership_reads=0
    )

    async def _beat(attempt):
        return True

    async def _resolve(*a, **k):
        return "img", None

    async def _start(_starter, job, image, env, size):
        starter.started.append(image)
        if starter.fail_start:
            raise ml.monitor_sandbox.MonitorSandboxError("provider refused")
        return "ext-1"

    async def _settle(**k):
        calls.settled.append(k)

    async def _revoke(job_id, *, attempt):
        calls.revoked.append((job_id, attempt))

    async def _persist(job, provider, external_id):
        calls.persisted.append(external_id)
        if isinstance(persists, Exception):
            raise persists
        return persists

    async def _still_owns(job):
        calls.ownership_reads += 1
        return owns

    async def _span(**k):
        calls.spans.append(k["external_id"])

    @asynccontextmanager
    async def _no_reads():
        raise AssertionError("the launcher reads nothing after create")
        yield  # pragma: no cover

    prepared = SimpleNamespace(
        trial_environment="modal", task_id="task-1", task_version=1,
        task_s3_key=None, task_path=None, trial_agent="oracle",
        trial_model="nop_oracle", trial_harbor_config={},
    )  # fmt: skip
    monkeypatch.setattr(ml, "_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(ml, "control_plane_url", lambda: "https://api.test")
    monkeypatch.setattr(ml, "record_monitor_heartbeat", beat or _beat)
    monkeypatch.setattr(ml, "_prepare_trial_run", lambda **k: _val(prepared))
    monkeypatch.setattr(ml.monitor_sandbox, "provider_for_environment", lambda e: "modal")
    monkeypatch.setattr(ml.monitor_sandbox, "get_starter", lambda p: starter)
    monkeypatch.setattr(
        ml.monitor_sandbox.SandboxSize,
        "from_span_resources",
        staticmethod(lambda r, **kw: None),
    )
    monkeypatch.setattr(ml, "_resolve_image", _noop)
    monkeypatch.setattr(ml, "_claim_funding_key", lambda **k: _val((True, {})))
    monkeypatch.setattr(ml, "_task_facts", lambda *a, **k: _val((None, None, {})))
    monkeypatch.setattr(ml.monitor_token, "mint_monitor_credential", lambda *a: _val("tok"))
    monkeypatch.setattr(ml.monitor_token, "revoke_monitor_credential", _revoke)
    monkeypatch.setattr(ml.job_tokens, "scoped_model_env", lambda **k: {})
    monkeypatch.setattr(ml.byok, "merge_byok_env", lambda *a: {})
    monkeypatch.setattr(ml.monitor_image_cache, "resolve", resolve or _resolve)
    monkeypatch.setattr(ml, "warm_source_for_trial", _noop)
    monkeypatch.setattr(ml, "_start", _start)
    monkeypatch.setattr(ml, "_persist_handle", _persist)
    monkeypatch.setattr(ml, "_still_owns", _still_owns)
    monkeypatch.setattr(ml.monitor_costs, "open_sandbox_span", _span)
    monkeypatch.setattr(ml, "_settle_trial_attempt", _settle)
    # Any row read after create (a check-in poll) fails the test.
    monkeypatch.setattr(ml, "get_session", _no_reads)
    return calls


@pytest.mark.asyncio
async def test_a_created_sandbox_is_handed_off_without_a_check_in_read(monkeypatch):
    starter = _Starter()
    calls = _stub_launch(monkeypatch, starter)

    outcome = await ml.launch_monitor_attempt(_job())

    assert outcome is not None and outcome.handoff is not None
    assert starter.started == ["img"]
    assert calls.persisted == ["ext-1"] and calls.spans == ["ext-1"]
    # Nothing torn down, nothing settled or revoked: the reconciler owns it now.
    assert starter.stopped == [] and calls.settled == [] and calls.revoked == []


@pytest.mark.asyncio
async def test_a_create_failure_settles_retryable_and_stops_nothing(monkeypatch):
    starter = _Starter(fail_start=True)
    calls = _stub_launch(monkeypatch, starter)

    outcome = await ml.launch_monitor_attempt(_job())

    assert outcome is None
    assert starter.stopped == []  # no sandbox was created
    assert calls.persisted == [] and calls.spans == []
    assert calls.revoked == [("job-1", 1)]
    assert len(calls.settled) == 1
    execution = calls.settled[0]["execution"]
    assert execution.retryable is True
    assert "Monitor launch failed" in execution.execution_error
    assert calls.ownership_reads == 1


@pytest.mark.asyncio
async def test_a_failure_after_a_reclaim_stops_our_sandbox_and_settles_nothing(
    monkeypatch,
):
    """The launch outlived the reap: the failure path's one ownership read
    finds a newer attempt, so our sandbox stops but the token and the trial,
    now the newer attempt's, are left alone."""
    starter = _Starter()
    calls = _stub_launch(
        monkeypatch, starter, persists=ConnectionError("db dropped"), owns=False
    )

    outcome = await ml.launch_monitor_attempt(_job())

    assert outcome is None
    assert starter.stopped == ["ext-1"]
    assert calls.ownership_reads == 1
    assert calls.settled == [] and calls.revoked == []


@pytest.mark.asyncio
async def test_a_pre_start_ownership_loss_ends_the_launch_without_create(monkeypatch):
    starter = _Starter()

    async def _not_owned(attempt):
        return False

    async def _slow_resolve(*a, **k):
        await asyncio.sleep(0.05)  # the beater runs, and reports the loss
        return "img", None

    calls = _stub_launch(monkeypatch, starter, beat=_not_owned, resolve=_slow_resolve)

    outcome = await ml.launch_monitor_attempt(_job())

    assert outcome is None
    assert starter.started == [] and starter.stopped == []
    # The token and the trial belong to the newer attempt.
    assert calls.settled == [] and calls.revoked == []


@pytest.mark.asyncio
async def test_ownership_lost_at_create_stops_the_unrecorded_sandbox(monkeypatch):
    starter = _Starter()
    calls = _stub_launch(monkeypatch, starter, persists=False)

    outcome = await ml.launch_monitor_attempt(_job())

    assert outcome is None
    assert calls.persisted == ["ext-1"]
    # No row names the sandbox, so the launcher stops it; no span was opened.
    assert starter.stopped == ["ext-1"] and calls.spans == []
    # The token and the trial belong to the newer attempt.
    assert calls.settled == [] and calls.revoked == []


@pytest.mark.asyncio
@pytest.mark.parametrize("rowcount", [1, 0])
async def test_persist_handle_reports_whether_the_row_was_written(
    monkeypatch, rowcount
):
    @asynccontextmanager
    async def _session():
        async def _execute(_stmt):
            return SimpleNamespace(rowcount=rowcount)

        yield SimpleNamespace(execute=_execute)

    monkeypatch.setattr(ml, "get_session", _session)
    job = SimpleNamespace(
        id="job-1",
        attempts=1,
        worker_id="w-1",
        capacity_slot=None,
        capacity_provider=None,
    )

    assert await ml._persist_handle(job, "modal", "ext-1") is (rowcount == 1)


def _compiled(stmt):
    from sqlalchemy.dialects import postgresql

    return stmt.compile(dialect=postgresql.dialect())


@pytest.mark.asyncio
@pytest.mark.parametrize("rowcount", [1, 0])
async def test_resolve_image_is_one_write_fenced_on_this_attempt(
    monkeypatch, rowcount
):
    """The image lands with one UPDATE fenced on status, attempt and worker and
    merged into the payload; a row that is no longer ours (rowcount 0) is
    skipped without an error."""
    statements: list = []

    @asynccontextmanager
    async def _session():
        async def _execute(stmt):
            statements.append(stmt)
            return SimpleNamespace(rowcount=rowcount)

        async def _get(*a, **k):
            raise AssertionError("no read-modify-write of the payload")

        yield SimpleNamespace(execute=_execute, get=_get)

    async def _resolver(task_id, version):
        return "registry/img:1"

    monkeypatch.setattr(ml, "get_session", _session)
    job = SimpleNamespace(id="job-1", attempts=3, worker_id="w-1")

    assert await ml._resolve_image(job, "task-1", 1, _resolver) is None

    assert len(statements) == 1
    compiled = _compiled(statements[0])
    sql = str(compiled)
    assert sql.startswith("UPDATE worker_jobs SET payload=(worker_jobs.payload ||")
    for fence in ("id", "status", "attempts", "current_worker_id"):
        assert f"worker_jobs.{fence} = " in sql, sql
    values = list(compiled.params.values())
    assert "job-1" in values and 3 in values and "w-1" in values
    assert {"monitor_docker_image": "registry/img:1"} in values


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "owned"),
    [
        (SimpleNamespace(status="RUNNING", attempts=1, current_worker_id="w-1"), True),
        (SimpleNamespace(status="RUNNING", attempts=2, current_worker_id="w-1"), False),
        (SimpleNamespace(status="RUNNING", attempts=1, current_worker_id="w-2"), False),
        (SimpleNamespace(status="FAILED", attempts=1, current_worker_id="w-1"), False),
        (None, False),
    ],
)
async def test_still_owns_is_one_read_of_status_attempt_and_worker(
    monkeypatch, row, owned
):
    from oddish.db import WorkerJobStatus

    if row is not None:
        row.status = WorkerJobStatus(row.status)
    statements: list = []

    @asynccontextmanager
    async def _session():
        async def _execute(stmt):
            statements.append(stmt)
            return SimpleNamespace(one_or_none=lambda: row)

        yield SimpleNamespace(execute=_execute)

    monkeypatch.setattr(ml, "get_session", _session)

    assert await ml._still_owns(_job()) is owned
    assert len(statements) == 1


@pytest.mark.asyncio
async def test_a_cancel_after_create_stops_the_sandbox_and_closes_its_span(monkeypatch):
    starter = _Starter()
    span_open = asyncio.Event()
    calls = _stub_launch(monkeypatch, starter)
    closed: list = []

    async def _span_then_hang(**k):
        span_open.set()  # the span is open, then the worker is cancelled
        await asyncio.Event().wait()

    async def _close(job, trial_id, prepared):
        closed.append((job.id, trial_id))

    monkeypatch.setattr(ml.monitor_costs, "open_sandbox_span", _span_then_hang)
    monkeypatch.setattr(ml, "_close_sandbox_span_after_failed_launch", _close)

    task = asyncio.create_task(ml.launch_monitor_attempt(_job()))
    await span_open.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert starter.stopped == ["ext-1"]
    assert closed == [("job-1", "trial-1")]
    assert calls.revoked == [("job-1", 1)]


@pytest.mark.asyncio
async def test_the_cancel_teardown_closes_the_span_the_payload_records(monkeypatch):
    settled = []

    class _Session:
        async def get(self, model, job_id):
            return SimpleNamespace(
                payload={
                    monitor_costs.PAYLOAD_KEY: {
                        "provider": "modal",
                        "resources": monitor_costs._dump(_resources()),
                        "verifier_resources": None,
                    }
                }
            )

    @asynccontextmanager
    async def _get_session():
        yield _Session()

    async def _settle(state, outcome):
        settled.append((state.worker_job_id, state.worker_job_attempt, outcome))

    monkeypatch.setattr(ml, "get_session", _get_session)
    monkeypatch.setattr(ml, "_settle_compute_costs", _settle)
    monkeypatch.setattr(ml.monitor_token, "revoke_monitor_credential", _noop)
    prepared = SimpleNamespace(
        trial_attempt=1, experiment_id="", org_id=None, billed_user_id=None,
        task_id="task-1", task_version_id=None,
    )  # fmt: skip

    await ml._after_cancelled_launch(
        job=_job(), trial_id="trial-1", prepared=prepared,
        starter=_Starter(), external_id="ext-1",
    )  # fmt: skip

    assert settled == [("job-1", 1, None)]


@pytest.mark.asyncio
async def test_the_image_resolver_is_the_callers_not_a_global(monkeypatch):
    """The prebuilt-image lookup is passed in by the caller; without one the
    sandbox builds the task image itself."""
    starter = _Starter()
    _stub_launch(monkeypatch, starter)
    resolved = []

    async def _resolve_image(job, task_id, version, resolver):
        resolved.append(resolver)

    monkeypatch.setattr(ml, "_resolve_image", _resolve_image)

    async def _resolver(task_id, version):
        return "registry/img:1"

    assert (await ml.launch_monitor_attempt(_job(), image_resolver=_resolver)).handoff
    assert (await ml.launch_monitor_attempt(_job())).handoff
    assert resolved == [_resolver, None]
    assert not hasattr(ml, "_image_resolver")
