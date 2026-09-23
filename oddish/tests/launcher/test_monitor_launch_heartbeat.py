"""The launcher must heartbeat while the image resolves and the sandbox boots.

Regression: the worker-job row was left at its claim-time heartbeat until the
in-loop beats began, so a warm-image build or a slow image pull that ran past
the reaper's stale threshold (15 min) got a healthy launch killed. Seven QA40
tasks stalled this way on 21 Sep 2026.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from oddish.db import WorkerJobStatus
from oddish.workers.jobs.registry import JobOutcome
from oddish.workers.launcher import launch as ml


def _job():
    return SimpleNamespace(
        id="job-1",
        attempts=1,
        subject_id="trial-1",
        worker_id="w-1",
        queue_slot=0,
        modal_function_call_id=None,
        reroute_from_environment=None,
    )


@pytest.mark.asyncio
async def test_launch_heartbeats_while_the_image_resolves(monkeypatch):
    beats: list[float] = []

    async def _record(attempt):
        beats.append(asyncio.get_event_loop().time())
        return True

    async def _slow_resolve(*args, **kwargs):
        # Stand in for a warm-snapshot build / cold image pull that outlasts a
        # couple of heartbeat intervals.
        await asyncio.sleep(0.12)
        return "img", None

    prepared = SimpleNamespace(
        trial_environment="modal",
        task_id="task-1",
        task_version=1,
        task_s3_key=None,
        task_path=None,
        trial_agent="oracle",
        trial_model="nop_oracle",
        trial_harbor_config={},
    )
    starter = SimpleNamespace(
        provider="modal",
        provisioned=lambda size: size,
        start=None,
        stop=None,
    )

    monkeypatch.setattr(ml, "_HEARTBEAT_SECONDS", 0.02)
    monkeypatch.setattr(ml, "control_plane_url", lambda: "https://api.test")
    monkeypatch.setattr(ml, "record_monitor_heartbeat", _record)

    async def _prepared(*a, **k):
        return prepared

    monkeypatch.setattr(ml, "_prepare_trial_run", _prepared)
    monkeypatch.setattr(ml.monitor_sandbox, "provider_for_environment", lambda e: "modal")
    monkeypatch.setattr(ml.monitor_sandbox, "get_starter", lambda p: starter)
    monkeypatch.setattr(
        ml.monitor_sandbox.SandboxSize,
        "from_span_resources",
        staticmethod(lambda r, **kw: None),
    )

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(ml, "_resolve_image", _noop)
    monkeypatch.setattr(ml, "_claim_funding_key", lambda **k: _true_empty())
    monkeypatch.setattr(ml, "_task_facts", lambda *a, **k: _two_none())
    monkeypatch.setattr(ml.monitor_token, "mint_monitor_credential", lambda *a: _val("tok"))
    monkeypatch.setattr(ml.job_tokens, "scoped_model_env", lambda **k: {})
    monkeypatch.setattr(ml.byok, "merge_byok_env", lambda *a: {})
    monkeypatch.setattr(ml.monitor_image_cache, "resolve", _slow_resolve)
    monkeypatch.setattr(ml, "_start", lambda *a, **k: _val("ext-1"))
    monkeypatch.setattr(ml, "_persist_handle", lambda *a, **k: _val(True))
    monkeypatch.setattr(ml.monitor_costs, "open_sandbox_span", _noop)

    async def _warm(*a, **k):
        return None

    monkeypatch.setattr(ml, "warm_source_for_trial", _warm)

    outcome = await ml.launch_monitor_attempt(_job())

    assert outcome is not None and outcome.handoff is not None
    # The resolve alone spans ~6 heartbeat intervals; without beating from the
    # claim there would be none before the hand-off.
    assert len(beats) >= 2, f"expected heartbeats during launch, got {len(beats)}"


def _patch_launch(monkeypatch, beat, started, settled, revoked, row_attempts=1):
    """``row_attempts`` is the job row's attempt when the failure path reads it
    (``_job()`` is attempt 1; 2 means reaped and re-claimed meanwhile)."""

    @asynccontextmanager
    async def _session():
        async def _execute(_stmt):
            row = SimpleNamespace(
                status=WorkerJobStatus.RUNNING,
                attempts=row_attempts,
                current_worker_id="w-1",
            )
            return SimpleNamespace(one_or_none=lambda: row)

        yield SimpleNamespace(execute=_execute)

    async def _slow_resolve(*args, **kwargs):
        await asyncio.sleep(0.05)  # the beater runs, and fails, meanwhile
        return "img", None

    async def _start(*a, **k):
        started.append(a)
        return "ext-1"

    async def _settle(**k):
        settled.append(k)

    async def _revoke(job_id, *, attempt):
        revoked.append((job_id, attempt))

    async def _noop(*a, **k):
        return None

    async def _prepared(*a, **k):
        return SimpleNamespace(
            trial_environment="modal", task_id="task-1", task_version=1,
            task_s3_key=None, task_path=None, trial_agent="oracle",
            trial_model="nop_oracle", trial_harbor_config={},
        )  # fmt: skip

    starter = SimpleNamespace(provider="modal", provisioned=lambda s: s, stop=_noop)
    monkeypatch.setattr(ml, "_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(ml, "control_plane_url", lambda: "https://api.test")
    monkeypatch.setattr(ml, "record_monitor_heartbeat", beat)
    monkeypatch.setattr(ml, "_prepare_trial_run", _prepared)
    monkeypatch.setattr(ml.monitor_sandbox, "provider_for_environment", lambda e: "modal")
    monkeypatch.setattr(ml.monitor_sandbox, "get_starter", lambda p: starter)
    monkeypatch.setattr(
        ml.monitor_sandbox.SandboxSize,
        "from_span_resources",
        staticmethod(lambda r, **kw: None),
    )
    monkeypatch.setattr(ml, "_resolve_image", _noop)
    monkeypatch.setattr(ml, "_claim_funding_key", lambda **k: _true_empty())
    monkeypatch.setattr(ml, "_task_facts", lambda *a, **k: _two_none())
    monkeypatch.setattr(ml.monitor_token, "mint_monitor_credential", lambda *a: _val("tok"))
    monkeypatch.setattr(ml.monitor_token, "revoke_monitor_credential", _revoke)
    monkeypatch.setattr(ml.job_tokens, "scoped_model_env", lambda **k: {})
    monkeypatch.setattr(ml.byok, "merge_byok_env", lambda *a: {})
    monkeypatch.setattr(ml.monitor_image_cache, "resolve", _slow_resolve)
    monkeypatch.setattr(ml, "warm_source_for_trial", _noop)
    monkeypatch.setattr(ml, "_start", _start)
    monkeypatch.setattr(ml, "_settle_trial_attempt", _settle)
    monkeypatch.setattr(ml, "get_session", _session)


@pytest.mark.asyncio
async def test_a_lost_lease_during_image_resolve_fails_the_launch_of_a_job_we_own(
    monkeypatch,
):
    """A lost lease is not a lost job: the row is still RUNNING under this
    worker, so the launch settles it retryable and revokes its token."""
    from oddish.workers.queue.worker_job_single_job import (
        SandboxCapacityLeaseLostError,
    )

    started: list = []
    settled: list = []
    revoked: list = []

    async def _lease_lost(attempt):
        raise SandboxCapacityLeaseLostError("lease gone")

    _patch_launch(monkeypatch, _lease_lost, started, settled, revoked)

    outcome = await ml.launch_monitor_attempt(_job())

    assert outcome is None
    assert started == []
    assert revoked == [("job-1", 1)]
    assert len(settled) == 1
    execution = settled[0]["execution"]
    assert execution.retryable is True
    assert execution.outcome is None
    assert "SandboxCapacityLeaseLostError" in execution.execution_error
    assert "lease gone" in execution.execution_error


@pytest.mark.asyncio
async def test_a_lost_lease_after_the_job_was_reclaimed_settles_and_revokes_nothing(
    monkeypatch,
):
    """The beater's error is raised only before create, so the launch can
    outlive the stale-heartbeat reap. The failure path's one ownership read
    finds attempt 2 on the row: the token and trial are that attempt's."""
    from oddish.workers.queue.worker_job_single_job import (
        SandboxCapacityLeaseLostError,
    )

    started: list = []
    settled: list = []
    revoked: list = []

    async def _lease_lost(attempt):
        raise SandboxCapacityLeaseLostError("lease gone")

    _patch_launch(monkeypatch, _lease_lost, started, settled, revoked, row_attempts=2)

    outcome = await ml.launch_monitor_attempt(_job())

    assert outcome is None
    assert started == []
    assert settled == [] and revoked == []


@pytest.mark.asyncio
async def test_a_false_beat_during_image_resolve_ends_the_launch_settling_nothing(
    monkeypatch,
):
    """A False beat is the one case where the job is gone: its token and trial
    belong to the newer attempt, so nothing is settled or revoked."""
    started: list = []
    settled: list = []
    revoked: list = []

    async def _not_owned(attempt):
        return False

    _patch_launch(monkeypatch, _not_owned, started, settled, revoked)

    outcome = await ml.launch_monitor_attempt(_job())

    assert outcome is None
    assert started == []
    assert settled == []
    assert revoked == []


async def _true_empty():
    return True, {}


async def _two_none():
    return None, None, {}


async def _val(v):
    return v
