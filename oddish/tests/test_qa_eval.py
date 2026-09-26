"""Focused contracts for pointer-based historical QA replay."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from oddish.analyze.qa_stages import prompt_sha256
from oddish.core.endpoints.qa_eval import create_qa_eval_core
from oddish.core.idempotency import _canonical_digest, compute_request_hash
from oddish.db import AnalysisStatus, TaskModel, TaskVersionModel, TrialStatus
from oddish.db.models import TrialModel
from oddish.schemas import QAEvalCreateRequest
from oddish.worker.analysis_result_check import check_analysis_result
from oddish.workers.analysis_trials import (
    _import_qa_eval_result,
    analysis_check_payload,
    build_qa_brief,
    is_analysis_kind,
)


def _analysis() -> dict:
    return {
        "trial_name": "source-1",
        "classification": "GOOD_FAILURE",
        "subtype": "misdiagnosis",
        "evidence": "The agent changed the wrong file.",
        "root_cause": "The agent diagnosed the wrong component.",
        "recommendation": "N/A",
        "reward": 0.0,
        "action_items": [],
        "exploitation": [],
    }


def _qa_artifact(source_trial_id: str = "source-1") -> dict:
    return {
        "trials": [
            {
                "trial_id": source_trial_id,
                "analysis": _analysis(),
                "trajectory_summary": {
                    "summary": "The agent edited the wrong file.",
                    "highlights": [],
                    "components": [
                        {
                            "step_ids": [1],
                            "trajectory_component": "implementing",
                            "action": "edit",
                            "purpose": "build",
                            "summary": "One edit.",
                        }
                    ],
                },
            }
        ],
        "verdict": None,
    }


def test_request_strips_and_deduplicates_source_trial_ids():
    request = QAEvalCreateRequest(
        name=" replay ",
        source_trial_ids=[" source-1 ", "source-1", "source-2"],
        prompt_name=" candidate-1 ",
        prompt_text=" classify this ",
        model=" ",
    )
    assert request.name == "replay"
    assert request.source_trial_ids == ["source-1", "source-2"]
    assert request.prompt_name == "candidate-1"
    assert request.prompt_text == "classify this"
    assert request.model is None
    assert request.audit_context == "current"


def test_request_rejects_an_unknown_audit_context():
    with pytest.raises(ValueError, match="audit_context"):
        QAEvalCreateRequest(
            name="replay",
            source_trial_ids=["source-1"],
            prompt_name="candidate-1",
            prompt_text="classify this",
            audit_context="snapshot",
        )


def test_replay_reuses_the_production_qa_brief_and_contract():
    assert is_analysis_kind("qa_eval")
    brief = build_qa_brief(
        task_name="demo",
        trial_ids=["source-1"],
        pre_trial_items=[{"id": "finding-1"}],
        with_verdict=False,
        classification_prompt="Candidate rules go here.",
    )
    assert "source-1" in brief
    assert "Candidate rules go here." in brief
    assert "finding-1" in brief
    assert "qa_result.json" in brief
    assert '"verdict": null' in brief

    expected = analysis_check_payload(
        "qa_eval",
        {
            "analysis_payload": {
                "trial_ids": ["source-1"],
                "with_verdict": False,
            }
        },
    )
    assert expected["kind"] == "qa"
    assert check_analysis_result(_qa_artifact(), expected) == []


def _source(
    *,
    has_trajectory: bool = True,
    status: TrialStatus = TrialStatus.SUCCESS,
) -> TrialModel:
    return TrialModel(
        id="source-1",
        name="source-1",
        task_id="task-1",
        task_version_id="version-1",
        experiment_id="original-experiment",
        org_id="org-1",
        agent="codex",
        provider="openai",
        queue_key="codex:model",
        model="model",
        kind="agent",
        status=status,
        has_trajectory=has_trajectory,
        analysis={"classification": "BAD_FAILURE"},
    )


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _CreateSession:
    def __init__(self, *sources: TrialModel):
        self.sources = list(sources)
        self.added_experiment = None
        self.prompt_rows: list[dict] = []
        self.task = TaskModel(
            id="task-1",
            name="task-1",
            org_id="org-1",
            current_version_id="version-1",
        )
        self.version = TaskVersionModel(
            id="version-1",
            task_id="task-1",
            task_s3_key="tasks/task-1/version-1.tar.gz",
        )

    async def execute(self, statement):
        if getattr(statement, "is_insert", False):
            assert statement.table.name == "qa_prompts"
            self.prompt_rows.append(
                statement.compile(dialect=postgresql.dialect()).params
            )
            return None
        descriptions = getattr(statement, "column_descriptions", None)
        if not descriptions:
            raise AssertionError("replay must not write task_experiments membership")
        entity = descriptions[0].get("entity")
        return _Result(
            {
                TrialModel: self.sources,
                TaskModel: [self.task],
                TaskVersionModel: [self.version],
            }[entity]
        )

    def add(self, row):
        self.added_experiment = row

    async def flush(self):
        self.added_experiment.id = "replay-experiment"


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", [None, "modal"])
async def test_create_accepts_a_failed_source_without_a_trajectory(
    monkeypatch, environment
):
    source = _source(has_trajectory=False, status=TrialStatus.FAILED)
    session = _CreateSession(source)
    session.version.pre_trial = {
        "items": [
            {"id": "audit-1", "tier": "must_fix"},
            {"id": "audit-2", "tier": "must_fix"},
        ]
    }
    captured = {}
    admitted = []

    async def fake_admit_trials(_session, org_id, billed_user_id, count):
        admitted.append((org_id, billed_user_id, count))

    async def fake_create_analysis_trial(_session, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id="qa-eval-1")

    monkeypatch.setattr(
        "oddish.core.endpoints.qa_eval.create_analysis_trial",
        fake_create_analysis_trial,
    )
    monkeypatch.setattr(
        "oddish.core.endpoints.qa_eval.admit_trials",
        fake_admit_trials,
    )
    response = await create_qa_eval_core(
        session,
        request=QAEvalCreateRequest(
            name="candidate replay",
            source_trial_ids=["source-1"],
            prompt_name="candidate-1",
            prompt_text="Classify the stored solver evidence.",
            environment=environment,
        ),
        org_id="org-1",
        owner_user_id="user-1",
        billed_user_id="user-1",
    )

    assert response.experiment_id == "replay-experiment"
    assert response.trials[0].qa_eval_trial_id == "qa-eval-1"
    assert captured["experiment_id"] == "replay-experiment"
    assert captured["task_version_id"] == "version-1"
    assert captured["billed_user_id"] == "user-1"
    assert captured["environment"] == environment
    assert captured["payload"]["trial_ids"] == ["source-1"]
    assert captured["payload"]["trial_evidence"] == [
        {
            "trial_id": "source-1",
            "status": "failed",
            "reward": None,
            "has_trajectory": False,
            "agent": "codex",
            "baseline_kind": None,
        }
    ]
    assert captured["payload"]["pre_trial_item_ids"] == ["audit-1", "audit-2"]
    assert captured["payload"]["pre_trial_must_fix_ids"] == ["audit-1", "audit-2"]
    assert captured["payload"]["baseline_evidence"] == []
    assert "source_trial_id" not in captured["payload"]
    assert admitted == [("org-1", "user-1", 1)]
    assert source.experiment_id == "original-experiment"
    assert source.analysis == {"classification": "BAD_FAILURE"}

    # Picked trials: a subset run. The sent text is recorded, private to the
    # org, so the page can show and diff it.
    sha = prompt_sha256("Classify the stored solver evidence.")
    assert captured["payload"]["subset"] is True
    assert captured["payload"]["stage"] == "post_trial"
    assert (response.prompt_sha256, captured["payload"]["prompt_sha256"]) == (sha, sha)
    assert response.skipped == []
    [row] = session.prompt_rows
    assert {k: row[k] for k in ("org_id", "stage", "sha256", "name", "source")} == {
        "org_id": "org-1",
        "stage": "post_trial",
        "sha256": sha,
        "name": "candidate-1",
        "source": "replay",
    }
    assert row["text"] == "Classify the stored solver evidence."
    assert row["created_by_user_id"] == "user-1"


@pytest.mark.asyncio
async def test_create_can_omit_current_audit_context_for_historical_goldens(
    monkeypatch,
):
    source = _source(has_trajectory=False, status=TrialStatus.FAILED)
    session = _CreateSession(source)
    session.version.pre_trial = {
        "items": [{"id": "new-staging-finding", "tier": "must_fix"}]
    }
    captured = {}

    async def fake_admit_trials(*_args, **_kwargs):
        return None

    async def fake_create_analysis_trial(_session, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id="qa-eval-1")

    monkeypatch.setattr(
        "oddish.core.endpoints.qa_eval.create_analysis_trial",
        fake_create_analysis_trial,
    )
    monkeypatch.setattr(
        "oddish.core.endpoints.qa_eval.admit_trials",
        fake_admit_trials,
    )
    await create_qa_eval_core(
        session,
        request=QAEvalCreateRequest(
            name="historical replay",
            source_trial_ids=["source-1"],
            prompt_name="candidate-1",
            prompt_text="Classify the stored solver evidence.",
            audit_context="none",
        ),
        org_id="org-1",
        owner_user_id="user-1",
        billed_user_id="user-1",
    )

    assert captured["payload"]["audit_context"] == "none"
    assert captured["payload"]["pre_trial_item_ids"] == []
    assert captured["payload"]["pre_trial_must_fix_ids"] == []
    assert "Source-audit status: omitted for historical replay" in captured["brief"]
    assert "new-staging-finding" not in captured["brief"]


@pytest.mark.asyncio
async def test_create_rejects_the_whole_request_when_a_source_is_ineligible():
    session = _CreateSession(_source(status=TrialStatus.RUNNING))
    with pytest.raises(HTTPException, match=r"not terminal \(running\)"):
        await create_qa_eval_core(
            session,
            request=QAEvalCreateRequest(
                name="candidate replay",
                source_trial_ids=["source-1"],
                prompt_name="candidate-1",
                prompt_text="Classify the stored solver evidence.",
            ),
            org_id="org-1",
            owner_user_id="user-1",
            billed_user_id="user-1",
        )
    assert session.added_experiment is None


@pytest.mark.asyncio
async def test_importer_extracts_one_standard_qa_analysis(monkeypatch):
    eval_trial = TrialModel(
        id="eval-1",
        name="eval-1",
        task_id="task-1",
        agent="claude-code",
        provider="anthropic",
        queue_key="claude-code:model",
        model="model",
        kind="qa_eval",
        status=TrialStatus.SUCCESS,
        harbor_config={
            "analysis_payload": {
                "trial_ids": ["source-1"],
                "with_verdict": False,
            }
        },
    )

    class FakeSession:
        async def get(self, _model, trial_id):
            if trial_id == "eval-1":
                return eval_trial
            if trial_id == "source-1":
                return _source()
            return None

    @asynccontextmanager
    async def fake_get_session():
        yield FakeSession()

    async def fake_read_artifact(_trial, filename):
        assert filename == "qa_result.json"
        return _qa_artifact()

    monkeypatch.setattr("oddish.workers.analysis_trials.get_session", fake_get_session)
    monkeypatch.setattr(
        "oddish.workers.analysis_trials.read_analysis_artifact", fake_read_artifact
    )

    await _import_qa_eval_result(eval_trial)
    assert eval_trial.analysis == {
        **_analysis(),
        "trial_name": "source-1",
        "reward": None,
    }
    assert eval_trial.analysis_status == AnalysisStatus.SUCCESS


@pytest.mark.asyncio
async def test_importer_requires_exactly_one_source_trial(monkeypatch):
    eval_trial = TrialModel(
        id="eval-1",
        name="eval-1",
        task_id="task-1",
        agent="claude-code",
        provider="anthropic",
        queue_key="claude-code:model",
        model="model",
        kind="qa_eval",
        status=TrialStatus.SUCCESS,
        harbor_config={
            "analysis_payload": {
                "trial_ids": ["source-1", "source-2"],
                "with_verdict": False,
            }
        },
    )

    class FakeSession:
        async def get(self, _model, trial_id):
            return eval_trial if trial_id == "eval-1" else None

    @asynccontextmanager
    async def fake_get_session():
        yield FakeSession()

    async def fake_read_artifact(_trial, _filename):
        return _qa_artifact()

    monkeypatch.setattr("oddish.workers.analysis_trials.get_session", fake_get_session)
    monkeypatch.setattr(
        "oddish.workers.analysis_trials.read_analysis_artifact", fake_read_artifact
    )

    await _import_qa_eval_result(eval_trial)

    assert eval_trial.analysis is None
    assert eval_trial.analysis_status == AnalysisStatus.FAILED
    assert "exactly one" in eval_trial.analysis_error


def _patch_create(monkeypatch, *, goldens=(), version=None) -> dict:
    """Fake the golden and prompt lookups and the trial writes; returns what
    the endpoint admitted and created."""
    seen: dict = {"admitted": [], "payloads": []}

    async def fake_counted_goldens(_session, org_id, stage):
        seen["goldens_for"] = (org_id, stage)
        return {trial_id: object() for trial_id in goldens}

    async def fake_prompt_version(_session, org_id, stage, sha256):
        seen["version_for"] = (org_id, stage, sha256)
        return version

    async def fake_admit_trials(_session, _org_id, _billed_user_id, count):
        seen["admitted"].append(count)

    async def fake_create_analysis_trial(_session, **kwargs):
        seen["payloads"].append(kwargs["payload"])
        return SimpleNamespace(id=f"qa-{kwargs['payload']['trial_ids'][0]}")

    endpoint = "oddish.core.endpoints.qa_eval"
    monkeypatch.setattr(f"{endpoint}.counted_goldens", fake_counted_goldens)
    monkeypatch.setattr(f"{endpoint}.prompt_version", fake_prompt_version)
    monkeypatch.setattr(f"{endpoint}.admit_trials", fake_admit_trials)
    monkeypatch.setattr(f"{endpoint}.create_analysis_trial", fake_create_analysis_trial)
    return seen


async def _create(session, **body):
    return await create_qa_eval_core(
        session,
        request=QAEvalCreateRequest(name="goldens replay", **body),
        org_id="org-1",
        owner_user_id="user-1",
        billed_user_id="user-1",
    )


def _retried_source() -> TrialModel:
    retried = _source()
    retried.id = "source-2"
    retried.superseded_by_trial_id = "source-3"
    return retried


@pytest.mark.asyncio
async def test_create_replays_every_golden_with_a_recorded_prompt(monkeypatch):
    text = "Recorded candidate prompt."
    seen = _patch_create(monkeypatch, goldens=["source-1"], version=("rec-1", text))
    session = _CreateSession(_source())

    response = await _create(session, goldens=True, prompt_sha256=prompt_sha256(text))

    assert seen["goldens_for"] == ("org-1", "post_trial")
    assert seen["version_for"] == ("org-1", "post_trial", prompt_sha256(text))
    [payload] = seen["payloads"]
    assert payload["trial_ids"] == ["source-1"]
    # Every golden replayed: a full run, charted and compared.
    assert payload["subset"] is False
    assert (payload["prompt_name"], payload["prompt_sha256"]) == (
        "rec-1",
        prompt_sha256(text),
    )
    assert (response.prompt_name, response.skipped) == ("rec-1", [])
    # The version is already recorded; only sent text writes a row.
    assert session.prompt_rows == []


@pytest.mark.asyncio
async def test_an_unknown_prompt_hash_is_not_found(monkeypatch):
    _patch_create(monkeypatch, goldens=["source-1"], version=None)
    session = _CreateSession(_source())
    with pytest.raises(HTTPException) as caught:
        await _create(session, goldens=True, prompt_sha256="0" * 64)
    assert caught.value.status_code == 404
    assert session.added_experiment is None


@pytest.mark.asyncio
async def test_a_full_run_skips_golden_trials_that_can_no_longer_replay(monkeypatch):
    seen = _patch_create(monkeypatch, goldens=["source-1", "source-2"])
    body = {"prompt_name": "candidate-1", "prompt_text": "Classify."}

    # Golden trial source-2 was retried after it was labeled: the run replays
    # the rest and says what it left out.
    response = await _create(
        _CreateSession(_source(), _retried_source()), goldens=True, **body
    )
    assert [t.source_trial_id for t in response.trials] == ["source-1"]
    assert response.skipped == ["source-2: superseded"]
    assert seen["admitted"] == [1]

    # Named explicitly, the same trial is refused.
    session = _CreateSession(_source(), _retried_source())
    with pytest.raises(HTTPException) as caught:
        await _create(session, source_trial_ids=["source-2"], **body)
    assert caught.value.status_code == 409
    assert session.added_experiment is None

    # Nothing left to replay is refused rather than started empty.
    session = _CreateSession(_retried_source())
    with pytest.raises(HTTPException) as caught:
        await _create(session, goldens=True, **body)
    assert caught.value.status_code == 422
    assert "source-2: superseded" in caught.value.detail
    assert session.added_experiment is None


@pytest.mark.asyncio
async def test_a_goldens_run_with_no_counted_goldens_is_refused(monkeypatch):
    _patch_create(monkeypatch, goldens=[])
    session = _CreateSession(_source())
    with pytest.raises(HTTPException) as caught:
        await _create(session, goldens=True, prompt_name="c", prompt_text="Classify.")
    assert caught.value.status_code == 422
    assert "no counted post_trial goldens" in caught.value.detail
    assert session.added_experiment is None


def test_an_older_replay_body_keeps_its_idempotency_hash():
    request = QAEvalCreateRequest(
        name="replay",
        source_trial_ids=["source-1"],
        prompt_name="candidate-1",
        prompt_text="classify this",
    )
    legacy = request.model_dump(
        mode="json", exclude={"stage", "goldens", "prompt_sha256"}
    )
    assert compute_request_hash(request) == _canonical_digest(legacy)
    by_hash = QAEvalCreateRequest(name="replay", goldens=True, prompt_sha256="abc")
    assert compute_request_hash(by_hash) != compute_request_hash(
        by_hash.model_copy(update={"prompt_sha256": "abd"})
    )
