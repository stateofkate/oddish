"""QA is shown only when the owner enables it for the current share link."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from oddish.core.sharing import helpers, public
from oddish.schemas import TaskStatusResponse, TrialResponse


def _trial_response():
    return TrialResponse.model_construct(
        id="trial-1",
        analysis={"classification": "BAD_FAILURE", "evidence": "QA evidence"},
        analysis_status="SUCCESS",
        analysis_error="QA error",
        analysis_started_at=datetime.now(timezone.utc),
        analysis_finished_at=datetime.now(timezone.utc),
        pre_trial_findings=[{"title": "Source finding"}],
        pre_trial_status="SUCCESS",
        pre_trial_error="Source error",
        pre_trial_cost_usd=0.1,
        qa_cost_usd=0.2,
        jobs=[],
    )


def _task_response():
    return TaskStatusResponse.model_construct(
        run_analysis=True,
        verdict={"summary": "QA verdict"},
        verdict_status="SUCCESS",
        verdict_error="Verdict error",
        trials=[_trial_response()],
        jobs=[],
    )


@pytest.mark.parametrize("show_qa", [False, True])
def test_share_setting_covers_task_and_nested_trial_qa(show_qa):
    response = _task_response()
    helpers.apply_public_task_qa_visibility(response, show_qa=show_qa)

    assert response.run_analysis is show_qa
    assert bool(response.verdict) is show_qa
    assert bool(response.verdict_status) is show_qa
    assert bool(response.verdict_error) is show_qa
    trial = response.trials[0]
    for field in (
        "analysis", "analysis_status", "analysis_error", "analysis_started_at",
        "analysis_finished_at", "pre_trial_findings", "pre_trial_status",
        "pre_trial_error", "pre_trial_cost_usd", "qa_cost_usd",
    ):
        assert bool(getattr(trial, field)) is show_qa, field


def _session(monkeypatch, row=None):
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: row))
    )

    @asynccontextmanager
    async def get_session():
        yield session

    monkeypatch.setattr(public, "get_read_session", get_session)
    return session


def _resolved(*, show_qa=True):
    experiment = SimpleNamespace(id="shared-exp", show_qa=show_qa)
    task = SimpleNamespace(
        id="task-1",
        current_version_id="task-1-v2",
        trials=[
            SimpleNamespace(
                id="live-trial", task_version_id="task-1-v1", is_probe=False,
                superseded_by_trial_id=None, experiment_id="shared-exp",
            ),
            SimpleNamespace(
                id="old-trial", task_version_id="task-1-v3", is_probe=False,
                superseded_by_trial_id="replacement", experiment_id="shared-exp",
            ),
            SimpleNamespace(
                id="probe-trial", task_version_id="task-1-v4", is_probe=True,
                superseded_by_trial_id=None, experiment_id="shared-exp",
            ),
        ],
    )
    return experiment, task, set()


@pytest.mark.asyncio
@pytest.mark.parametrize("resolved", [None, _resolved(show_qa=False)])
async def test_public_qa_refuses_missing_task_or_disabled_link(monkeypatch, resolved):
    session = _session(monkeypatch)
    resolve = AsyncMock(return_value=resolved)
    monkeypatch.setattr(public, "get_public_task_for_experiment", resolve)

    with pytest.raises(HTTPException) as exc:
        await public.get_public_task_qa("token", "task-1", version=None)
    assert exc.value.status_code == 404
    resolve.assert_awaited_once_with(session, "token", "task-1")
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_public_qa_uses_the_shared_trial_version_not_private_default(monkeypatch):
    row = SimpleNamespace(
        id="task-1-v1", version=1,
        pre_trial={"items": [{"title": "Check source"}], "cost_usd": 123},
        reported_findings=[{"finding": {"title": "Retained QA finding"}}],
        pre_trial_status="SUCCESS", pre_trial_error=None,
    )
    session = _session(monkeypatch, row)
    monkeypatch.setattr(
        public, "get_public_task_for_experiment", AsyncMock(return_value=_resolved())
    )

    response = await public.get_public_task_qa("token", "task-1", version=None)
    query = session.execute.call_args.args[0]
    params = query.compile().params
    assert "task-1-v1" in params.values()
    assert "task-1-v2" not in params.values()
    assert response.model_dump() == {
        "version": 1, "version_id": "task-1-v1",
        "pre_trial_findings": [{"title": "Retained QA finding"}, {"title": "Check source"}],
        "pre_trial_status": "SUCCESS", "pre_trial_error": None,
    }


@pytest.mark.asyncio
async def test_explicit_qa_version_is_limited_to_live_shared_trials(monkeypatch):
    session = _session(monkeypatch)
    monkeypatch.setattr(
        public, "get_public_task_for_experiment", AsyncMock(return_value=_resolved())
    )

    with pytest.raises(HTTPException) as exc:
        await public.get_public_task_qa("token", "task-1", version=2)
    assert exc.value.status_code == 404
    params = session.execute.call_args.args[0].compile().params
    visible_ids = next(value for value in params.values() if isinstance(value, list))
    assert visible_ids == ["task-1-v1"]


@pytest.mark.asyncio
async def test_source_checks_work_before_any_trials_exist(monkeypatch):
    row = SimpleNamespace(
        id="task-1-v2", version=2,
        pre_trial={"items": [{"title": "Check source"}]},
        pre_trial_status="SUCCESS", pre_trial_error=None,
    )
    session = _session(monkeypatch, row)
    resolved = _resolved()
    resolved[1].trials = []
    monkeypatch.setattr(
        public, "get_public_task_for_experiment", AsyncMock(return_value=resolved)
    )

    response = await public.get_public_task_qa("token", "task-1", version=2)
    params = session.execute.call_args.args[0].compile().params
    visible_ids = next(value for value in params.values() if isinstance(value, list))
    assert visible_ids == ["task-1-v2"]
    assert response.pre_trial_findings == [{"title": "Check source"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("show_qa", [False, True])
async def test_separate_public_trial_list_obeys_share_setting(monkeypatch, show_qa):
    trial = SimpleNamespace(id="trial-1")
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        all=lambda: [(trial, "task-path")]
    )))
    monkeypatch.setattr(helpers, "get_public_task_for_experiment", AsyncMock(
        return_value=_resolved(show_qa=show_qa)
    ))
    monkeypatch.setattr(helpers, "fetch_trial_queue_info", AsyncMock(return_value={}))
    monkeypatch.setattr(helpers, "build_trial_response", lambda *a, **k: _trial_response())

    trials = await helpers.list_task_trials_for_public_experiment(session, "token", "task-1")
    assert bool(trials[0].analysis) is show_qa
    assert bool(trials[0].pre_trial_findings) is show_qa


@pytest.mark.asyncio
@pytest.mark.parametrize("show_qa", [False, True])
async def test_public_spend_hides_qa_cost_and_pending_counts_when_disabled(
    monkeypatch, show_qa
):
    from oddish.schemas import ExperimentCostTotals

    _session(monkeypatch)
    monkeypatch.setattr(
        public,
        "get_public_experiment",
        AsyncMock(return_value=SimpleNamespace(
            id="shared-exp", org_id="org-1", show_qa=show_qa
        )),
    )
    monkeypatch.setattr(
        public,
        "get_experiment_cost_totals",
        AsyncMock(return_value=ExperimentCostTotals(
            cost_usd=10,
            qa_cost_usd=2,
            owned_qa_cost_usd=1,
            qa_has_estimated=True,
            qa_cost_complete=False,
            qa_unpriced_count=3,
            qa_pending_count=4,
            owned_qa_cost_complete=False,
            owned_qa_unpriced_count=1,
            owned_qa_pending_count=2,
            verifier_cost_usd=5,
            owned_verifier_cost_usd=3,
            verifier_has_estimated=True,
        )),
    )

    response = await public.get_public_experiment_cost_totals("token")

    assert response.cost_usd == 10
    for field in (
        "qa_cost_usd", "owned_qa_cost_usd", "qa_has_estimated",
        "qa_unpriced_count", "qa_pending_count", "owned_qa_unpriced_count",
        "owned_qa_pending_count",
    ):
        assert bool(getattr(response, field)) is show_qa, field
    assert response.qa_cost_complete is not show_qa
    assert response.owned_qa_cost_complete is not show_qa
    assert response.verifier_cost_usd == 0
    assert response.owned_verifier_cost_usd == 0
    assert response.verifier_has_estimated is False
