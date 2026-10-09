"""Experiment averages use scoped measurements and filter before pagination."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update

from oddish.core.dashboard import (
    _build_aggregates_for_experiment_ids,
    dashboard_experiment_rows,
    load_dashboard_experiments,
    rebuild_dashboard_experiments,
)
from oddish.db import ExperimentModel, TaskModel, TrialModel, TrialStatus
from oddish.db.models import ExperimentSummaryModel, experiment_trials
from oddish.filters.experiment_effort import ExperimentEffortFilter, TokenMetric
from test_dashboard_collection_aggregates import _task, _trial, _version


@pytest.mark.asyncio
async def test_experiment_cohort_versions_memberships_and_independent_coverage(session):
    org = uuid4().hex
    task, exp = _task("effort", org_id=org), ExperimentModel(name="effort", org_id=org)
    session.add_all([task, exp])
    await session.flush()
    versions = [_version(task, v) for v in (1, 2, 3)]
    session.add_all(versions)
    await session.flush()
    await session.execute(update(TaskModel).where(TaskModel.id == task.id).values(current_version_id=versions[1].id))
    cases = [
        {"total_steps": 10, "input_tokens": 5_714_326_425, "output_tokens": 10, "cache_tokens": 100, "cost_usd": 4},
        {"reward": 0, "total_steps": 0, "input_tokens": 0, "output_tokens": 0, "cache_tokens": 0, "cost_usd": 0},
        {"total_steps": None, "input_tokens": None, "output_tokens": 4, "cache_tokens": None, "cost_usd": None},
        {"agent": "oracle"}, {"agent": "nop"},
        {"error_message": "SandboxError"}, {"kind": "qa"},
        {"deleted_at": datetime.now(UTC)}, {"idempotency_key": "combine:copy"},
        {"task_version_id": versions[0].id}, {"task_version_id": versions[2].id},
        {"org_id": "other"},
    ]
    trials = []
    for overrides in cases:
        trial = _trial(task, exp, org_id=org, reward=1, task_version_id=versions[1].id)
        for key, value in {"total_steps": 9999, "input_tokens": 9999, "output_tokens": 9999, "cache_tokens": 9999, "cost_usd": 9999, **overrides}.items():
            setattr(trial, key, value)
        session.add(trial)
        trials.append(trial)
    await session.flush()
    # An extra membership of the canonical home must not double count.
    await session.execute(experiment_trials.insert().values(experiment_id=exp.id, trial_id=trials[0].id))
    _, aggregate, _ = _build_aggregates_for_experiment_ids([exp.id], org_id=org)
    row = (await session.execute(select(aggregate))).mappings().one()
    assert row["effort_completed_trials"] == 3
    assert (row["steps_sum"], row["steps_trial_count"]) == (10, 2)
    assert (row["tokens_sum"], row["tokens_trial_count"]) == (5_714_326_435, 2)
    assert (row["cached_input_tokens_sum"], row["cached_input_tokens_trial_count"]) == (100, 2)
    assert (row["uncached_input_tokens_sum"], row["uncached_input_tokens_trial_count"]) == (5_714_326_325, 2)
    assert (row["output_tokens_sum"], row["output_tokens_trial_count"]) == (14, 3)
    assert (row["cost_sum"], row["cost_trial_count"]) == (4, 2)
    # A gathered collection measures its own membership, not the entire task.
    collection = ExperimentModel(name="collected", org_id=org)
    session.add(collection)
    await session.flush()
    await session.execute(experiment_trials.insert().values(experiment_id=collection.id, trial_id=trials[1].id))
    _, aggregate, _ = _build_aggregates_for_experiment_ids([collection.id], org_id=org)
    row = (await session.execute(select(aggregate))).mappings().one()
    assert row["effort_completed_trials"] == 1
    assert row["cost_sum"] == 0
    assert row["cost_trial_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("metric", ["steps", "tokens", "cost"])
async def test_prepared_average_order_ranges_pagination_and_unknowns(session, metric):
    org = uuid4().hex
    ids = []
    for value in (0, 30.5, None, 10):
        exp = ExperimentModel(name=f"effort-{value}", org_id=org)
        session.add(exp)
        await session.flush()
        ids.append(exp.id)
        fields = {f"{field}_{suffix}": (value * 2 if suffix == "sum" and value is not None else 2 if value is not None else 0)
                  for field in ("steps", "tokens", "cached_input_tokens", "cost") for suffix in ("sum", "trial_count")}
        session.add(ExperimentSummaryModel(experiment_id=exp.id, payload={"effort": fields}, built_revision=1, revision=1))
    session.add(ExperimentModel(name="other-org", org_id="other"))
    await session.flush()
    async def page(filters, offset=0, limit=1):
        return await load_dashboard_experiments(session, org_id=org, experiments_limit=limit, experiments_offset=offset,
            experiments_query=None, experiments_status="all", experiments_effort=filters)
    for direction, order in (("asc", [ids[0], ids[3], ids[1], ids[2]]), ("desc", [ids[1], ids[3], ids[0], ids[2]])):
        seen = []
        for offset in range(4):
            rows, more = await page(ExperimentEffortFilter(sort=f"avg_{metric}_{direction}"), offset)
            seen.append(rows[0]["id"])
            assert more == (offset < 3)
        assert seen == order
    rows, more = await page(ExperimentEffortFilter(**{f"min_{metric}": 10, f"max_{metric}": 30}), limit=25)
    assert [row["id"] for row in rows] == [ids[3]]
    assert more is False
    # Fractional boundaries and zero are kept; unknowns never match as zero.
    rows, _ = await page(ExperimentEffortFilter(**{f"max_{metric}": 0}), limit=25)
    assert [row["id"] for row in rows] == [ids[0]]
    if metric == "tokens":
        rows, _ = await page(ExperimentEffortFilter(token_metric=TokenMetric.CACHED_INPUT, min_tokens=30))
        assert rows[0]["id"] == ids[1]


@pytest.mark.asyncio
async def test_rebuild_publishes_recorded_effort_without_cost_estimates(session):
    org = uuid4().hex
    task, exp = _task("rebuild", org_id=org), ExperimentModel(name="rebuild", org_id=org)
    session.add_all([task, exp])
    await session.flush()
    trial = _trial(task, exp, org_id=org, reward=0)
    trial.total_steps, trial.input_tokens, trial.output_tokens = 5, 100, 20
    trial.cost_usd = None
    session.add(trial)
    await session.flush()
    rows = (await session.execute(dashboard_experiment_rows().where(ExperimentModel.id == exp.id))).mappings().all()
    response = await rebuild_dashboard_experiments(session, page_rows=rows, org_id=org)
    effort = response[0]["effort"]
    assert effort["completed_trials"] == 1
    assert effort["steps_sum"] == 5
    assert effort["tokens_sum"] == 120
    assert effort["cost_trial_count"] == 0  # not a token-price estimate or free run


@pytest.mark.parametrize("values", [{"min_cost": -1}, {"min_tokens": float("nan")}, {"max_steps": float("inf")}, {"min_steps": 2, "max_steps": 1}, {"sort": "drop_table"}, {"token_metric": "invalid"}])
def test_invalid_average_filter_rejected(values):
    with pytest.raises(ValidationError):
        ExperimentEffortFilter(**values)


from test_prepared_dashboard import experiment, fold  # noqa: E402,F401


@pytest.mark.asyncio
async def test_legacy_payload_rebuild_and_late_measurement_invalidation(experiment):
    from oddish.core.experiment_summaries import refresh_experiment_summaries
    from oddish.db import get_session

    org, eid, tid = experiment
    async with get_session() as session:
        await session.execute(update(TrialModel).where(TrialModel.id == tid).values(status=TrialStatus.SUCCESS, reward=0, total_steps=5))
    await fold()
    await refresh_experiment_summaries()
    async with get_session() as session:
        summary = await session.get(ExperimentSummaryModel, eid)
        old_payload = dict(summary.payload)
        old_payload.pop("effort")
        await session.execute(update(ExperimentSummaryModel).where(ExperimentSummaryModel.experiment_id == eid).values(payload=old_payload, refreshed_at=datetime.now(UTC)))
    # Missing fields are repaired by the existing bounded maintainer even when
    # a summary is current; no dashboard read scans history to repair it.
    await refresh_experiment_summaries()
    async with get_session() as session:
        summary = await session.get(ExperimentSummaryModel, eid)
        assert summary.payload["effort"]["steps_sum"] == 5
        revision = summary.revision
        await session.execute(update(TrialModel).where(TrialModel.id == tid).values(total_steps=9, cost_usd=2))
    await fold()
    async with get_session() as session:
        summary = await session.get(ExperimentSummaryModel, eid)
        assert summary.revision > revision
    await refresh_experiment_summaries()
    async with get_session() as session:
        summary = await session.get(ExperimentSummaryModel, eid)
        assert summary.payload["effort"]["steps_sum"] == 9
        assert summary.payload["effort"]["cost_sum"] == 2
        assert summary.payload["effort"]["cost_trial_count"] == 1


@pytest.mark.asyncio
async def test_running_checkpoints_do_not_dirty_completed_model_averages(experiment):
    from oddish.core.experiment_summaries import refresh_experiment_summaries
    from oddish.db import get_session

    _, eid, tid = experiment
    await refresh_experiment_summaries()
    async with get_session() as session:
        summary = await session.get(ExperimentSummaryModel, eid)
        revision = summary.revision
        await session.execute(update(TrialModel).where(TrialModel.id == tid).values(
            total_steps=7, input_tokens=100, output_tokens=20, cost_usd=1,
        ))
    await fold()
    async with get_session() as session:
        summary = await session.get(ExperimentSummaryModel, eid)
        assert summary.revision == revision
        assert summary.payload["effort"]["completed_trials"] == 0
        # Settlement still invalidates through the existing status trigger.
        await session.execute(update(TrialModel).where(TrialModel.id == tid).values(status=TrialStatus.SUCCESS, reward=1))
    await fold()
    await refresh_experiment_summaries()
    async with get_session() as session:
        summary = await session.get(ExperimentSummaryModel, eid)
        assert summary.payload["effort"]["steps_sum"] == 7
        assert summary.payload["effort"]["cost_sum"] == 1
