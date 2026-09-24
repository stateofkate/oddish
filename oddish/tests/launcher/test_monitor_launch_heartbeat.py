"""The launcher must heartbeat while the image resolves and the sandbox boots.

Regression: the worker-job row was left at its claim-time heartbeat until the
in-loop beats began, so a warm-image build or a slow image pull that ran past
the reaper's stale threshold (15 min) got a healthy launch killed. Seven QA40
tasks stalled this way on 21 Sep 2026.

Wired through the launch's public seams: ``prepare_trial_run``,
``claim_funding_key``, ``build_sandbox_env``, ``settle_trial_attempt``, the
sandbox starter, the task-facts functions and the session. The only private
name touched is the beat interval, so the tests take milliseconds.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from oddish.db import WorkerJobStatus
from oddish.workers.launcher import launch as ml
from oddish.workers.launcher.image_cache import Resolved


def _job():
    return SimpleNamespace(
        id="job-1",
        attempts=1,
        subject_id="trial-1",
        worker_id="w-1",
        queue_slot=0,
        modal_function_call_id=None,
        reroute_from_environment=None,
        capacity_provider=None,
        capacity_slot=None,
    )


def _prepared():
    return SimpleNamespace(
        trial_environment="modal",
        task_id="task-1",
        task_version=1,
        task_s3_key=None,
        task_path=None,
        trial_agent="oracle",
        trial_model="nop_oracle",
        trial_harbor_config={},
    )


class _Starter:
    provider = "modal"

    def __init__(self):
        self.started: list[dict] = []
        self.stopped: list[str] = []

    def provisioned(self, size):
        return size

    async def start(self, *, name, image, env, size, bundle=None):
        self.started.append({"name": name, "image": image, "env": env, "size": size})
        return "ext-1"

    async def stop(self, external_id):
        self.stopped.append(external_id)


def _session_factory(row_attempts: int):
    """A session whose fenced update reports one row written and whose
    ownership read finds the row RUNNING at ``row_attempts`` under this
    worker (``_job()`` is attempt 1; 2 means reaped and re-claimed)."""

    @asynccontextmanager
    async def _session():
        async def _execute(_stmt):
            row = SimpleNamespace(
                status=WorkerJobStatus.RUNNING,
                attempts=row_attempts,
                current_worker_id="w-1",
            )
            return SimpleNamespace(rowcount=1, one_or_none=lambda: row)

        yield SimpleNamespace(execute=_execute)

    return _session


class _Launch:
    """One launch under test: what it started, settled and revoked."""

    def __init__(self, monkeypatch, *, beat, resolve_seconds, row_attempts=1):
        self.starter = _Starter()
        self.settled: list[dict] = []
        self.revoked: list[tuple[str, int]] = []
        self.beats: list[float] = []

        async def _beat(attempt):
            self.beats.append(asyncio.get_event_loop().time())
            return await beat(attempt)

        async def _settle(**kwargs):
            self.settled.append(kwargs)

        async def _revoke(job_id, *, attempt):
            self.revoked.append((job_id, attempt))

        async def _resolve(*args, **kwargs):
            # A warm-snapshot build or cold image pull spanning several beats.
            await asyncio.sleep(resolve_seconds)
            return Resolved("img", None, "cold")

        async def _none(*args, **kwargs):
            return None

        monkeypatch.setattr(ml, "_HEARTBEAT_SECONDS", 0.01)
        monkeypatch.setattr(ml, "control_plane_url", lambda: "https://api.test")
        monkeypatch.setattr(ml, "record_monitor_heartbeat", _beat)
        monkeypatch.setattr(ml, "prepare_trial_run", lambda **k: _val(_prepared()))
        monkeypatch.setattr(
            ml.monitor_sandbox, "provider_for_environment", lambda e: "modal"
        )
        monkeypatch.setattr(ml.monitor_sandbox, "get_starter", lambda p: self.starter)
        monkeypatch.setattr(
            ml.monitor_sandbox.SandboxSize,
            "from_span_resources",
            staticmethod(lambda r, **kw: None),
        )
        monkeypatch.setattr(ml, "claim_funding_key", lambda **k: _val((True, {})))
        # The task facts, from their public pieces: a task directory that needs
        # no cleanup, no judge costs, no resources and no judge keys.
        monkeypatch.setattr(
            ml, "resolve_task_directory", lambda **k: _val((Path("/task"), None, None))
        )
        monkeypatch.setattr(ml, "begin_judge_costs_if_enabled", _none)
        monkeypatch.setattr(ml, "capture_sandbox_resources", lambda *a: None)
        monkeypatch.setattr(ml, "capture_verifier_resources", lambda *a: None)
        monkeypatch.setattr(ml.verifier_secrets, "judge_env_for_task", lambda *a: {})
        monkeypatch.setattr(
            ml.monitor_token, "mint_monitor_credential", lambda *a: _val("tok")
        )
        monkeypatch.setattr(ml.monitor_token, "revoke_monitor_credential", _revoke)
        monkeypatch.setattr(
            ml.launch_env, "build_sandbox_env", lambda prepared, **k: {"TOK": "tok"}
        )
        monkeypatch.setattr(ml.monitor_image_cache, "resolve", _resolve)
        monkeypatch.setattr(ml, "warm_source_for_trial", _none)
        monkeypatch.setattr(ml, "get_session", _session_factory(row_attempts))
        monkeypatch.setattr(ml.monitor_costs, "open_sandbox_span", _none)
        monkeypatch.setattr(ml, "settle_trial_attempt", _settle)


async def _owned(attempt):
    return True


async def _not_owned(attempt):
    return False


async def _lease_lost(attempt):
    from oddish.workers.queue.worker_job_single_job import (
        SandboxCapacityLeaseLostError,
    )

    raise SandboxCapacityLeaseLostError("lease gone")


async def _val(v):
    return v


@pytest.mark.asyncio
async def test_launch_heartbeats_while_the_image_resolves(monkeypatch):
    launch = _Launch(monkeypatch, beat=_owned, resolve_seconds=0.08)

    outcome = await ml.launch_monitor_attempt(_job())

    assert outcome is not None and outcome.handoff is not None
    # The resolve alone spans several beat intervals; without beating from
    # the claim there would be none before the hand-off.
    assert len(launch.beats) >= 2, f"expected beats during launch, got {launch.beats}"
    assert [s["env"] for s in launch.starter.started] == [{"TOK": "tok"}]
    assert launch.settled == [] and launch.revoked == []


@pytest.mark.asyncio
async def test_a_lost_lease_during_image_resolve_fails_the_launch_of_a_job_we_own(
    monkeypatch,
):
    """A lost lease is not a lost job: the row is still RUNNING under this
    worker, so ``fail_launch`` settles it retryable and revokes its token."""
    launch = _Launch(monkeypatch, beat=_lease_lost, resolve_seconds=0.05)

    outcome = await ml.launch_monitor_attempt(_job())

    assert outcome is None
    assert launch.starter.started == []
    assert launch.revoked == [("job-1", 1)]
    assert len(launch.settled) == 1
    execution = launch.settled[0]["execution"]
    assert execution.retryable is True
    assert execution.outcome is None
    assert "SandboxCapacityLeaseLostError" in execution.execution_error
    assert "lease gone" in execution.execution_error


@pytest.mark.asyncio
async def test_a_lost_lease_after_the_job_was_reclaimed_settles_and_revokes_nothing(
    monkeypatch,
):
    """The beater's error is raised only before create, so the launch can
    outlive the stale-heartbeat reap. ``fail_launch``'s one ownership read
    finds attempt 2 on the row: the token and trial are that attempt's."""
    launch = _Launch(
        monkeypatch, beat=_lease_lost, resolve_seconds=0.05, row_attempts=2
    )

    outcome = await ml.launch_monitor_attempt(_job())

    assert outcome is None
    assert launch.starter.started == []
    assert launch.settled == [] and launch.revoked == []


@pytest.mark.asyncio
async def test_a_false_beat_during_image_resolve_ends_the_launch_settling_nothing(
    monkeypatch,
):
    """A False beat is the one case where the job is gone: its token and trial
    belong to the newer attempt, so nothing is settled or revoked."""
    launch = _Launch(monkeypatch, beat=_not_owned, resolve_seconds=0.05)

    outcome = await ml.launch_monitor_attempt(_job())

    assert outcome is None
    assert launch.starter.started == []
    assert launch.settled == [] and launch.revoked == []


@pytest.mark.asyncio
async def test_fail_launch_stops_the_sandbox_it_started_and_settles_the_attempt(
    monkeypatch,
):
    """Called directly: a provider exception after create stops that sandbox,
    revokes this attempt's token and settles a retryable failure."""
    launch = _Launch(monkeypatch, beat=_owned, resolve_seconds=0)

    outcome = await ml.fail_launch(
        RuntimeError("provider hiccup"),
        job=_job(),
        trial_id="trial-1",
        prepared=_prepared(),
        starter=launch.starter,
        external_id="ext-9",
        span_opened=False,
    )

    assert outcome == "failed"
    assert launch.starter.stopped == ["ext-9"]
    assert launch.revoked == [("job-1", 1)]
    assert [s["execution"].execution_error for s in launch.settled] == [
        "Monitor launch failed: RuntimeError: provider hiccup"
    ]
