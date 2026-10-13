from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import settings  # noqa: E402
from oddish.core.quotas import (  # noqa: E402
    _inflight_predicates,
    _org_inflight_predicates,
    effective_limits_by_org_user_all_orgs,
    quota_window_start,
    start_of_month_utc,
    sum_cost_usd,
    sum_cost_usd_by_org_user_all_orgs,
    sum_cost_usd_by_user,
    sum_org_cost_usd,
    to_money_decimal,
)
from oddish.db import (  # noqa: E402
    AnalysisCostModel,
    ModalCostSpanModel,
    TaskModel,
    TrialModel,
    WorkerJobModel,
    get_session,
)
from oddish.queue import create_task  # noqa: E402
from oddish.schemas import TaskSubmission, TrialSpec  # noqa: E402

_RUN = uuid.uuid4().hex[:8]
_ORG_IDS: set[str] = set()
_USER_IDS: set[str] = set()
_ANALYSIS_IDS: list[str] = []
_MODAL_IDS: list[str] = []


def _submission(name: str, *, n_trials: int) -> TaskSubmission:
    return TaskSubmission(
        name=name,
        task_path="s3://test-bucket/quota-composite-fake-task",
        trials=[TrialSpec(agent="nop", model=None) for _ in range(n_trials)],
    )


async def _ensure_org(org_id: str) -> None:
    _ORG_IDS.add(org_id)
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO organizations "
                "(id, name, slug, plan, settings, is_active, created_at, updated_at) "
                "VALUES (:id, :id, :id, 'free', '{}'::jsonb, true, NOW(), NOW()) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": org_id},
        )


async def _ensure_user(org_id: str, user_id: str) -> None:
    await _ensure_org(org_id)
    _USER_IDS.add(user_id)
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO users "
                "(id, org_id, email, role, is_active, created_at, updated_at) "
                "VALUES (:id, :org_id, :email, 'member'::userrole, true, NOW(), NOW()) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": user_id,
                "org_id": org_id,
                "email": f"{user_id}@example.com",
            },
        )


@pytest_asyncio.fixture
async def cleanup_task_ids():
    task_ids: list[str] = []
    yield task_ids
    async with get_session() as session:
        for task_id in task_ids:
            await session.execute(
                WorkerJobModel.__table__.delete().where(
                    WorkerJobModel.subject_id.like(f"{task_id}%")
                )
            )
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id == task_id)
            )
        if _ANALYSIS_IDS:
            await session.execute(
                AnalysisCostModel.__table__.delete().where(
                    AnalysisCostModel.id.in_(_ANALYSIS_IDS)
                )
            )
        if _MODAL_IDS:
            await session.execute(
                ModalCostSpanModel.__table__.delete().where(
                    ModalCostSpanModel.id.in_(_MODAL_IDS)
                )
            )
        for user_id in list(_USER_IDS):
            await session.execute(
                text("DELETE FROM quota_bumps WHERE user_id = :u"), {"u": user_id}
            )
            await session.execute(
                text("DELETE FROM quotas WHERE user_id = :u"), {"u": user_id}
            )
            await session.execute(
                text("DELETE FROM users WHERE id = :u"), {"u": user_id}
            )
        for org_id in list(_ORG_IDS):
            await session.execute(
                text("DELETE FROM quota_bumps WHERE org_id = :o"), {"o": org_id}
            )
            await session.execute(
                text("DELETE FROM quotas WHERE org_id = :o"), {"o": org_id}
            )
            await session.execute(
                text("DELETE FROM organizations WHERE id = :o"), {"o": org_id}
            )
    _ORG_IDS.clear()
    _USER_IDS.clear()
    _ANALYSIS_IDS.clear()
    _MODAL_IDS.clear()


def _analysis(
    *,
    org_id: str,
    billed_user_id: str,
    cost_usd: float,
    created_at: datetime,
    deleted_at: datetime | None = None,
) -> AnalysisCostModel:
    row = AnalysisCostModel(
        id=f"acomp-{_RUN}-{uuid.uuid4().hex[:8]}",
        job_kind="trial_classifier",
        org_id=org_id,
        billed_user_id=billed_user_id,
        model="claude-haiku-4-5",
        cost_usd=cost_usd,
        cost_source="native",
        created_at=created_at,
        deleted_at=deleted_at,
    )
    _ANALYSIS_IDS.append(row.id)
    return row


def _modal(
    *,
    org_id: str,
    billed_user_id: str,
    cost_usd: str | None,
    finished_at: datetime | None,
    deleted_at: datetime | None = None,
) -> ModalCostSpanModel:
    started = (finished_at or datetime.now(timezone.utc)) - timedelta(minutes=1)
    row = ModalCostSpanModel(
        id=f"mcomp-{_RUN}-{uuid.uuid4().hex[:8]}",
        org_id=org_id,
        billed_user_id=billed_user_id,
        worker_job_id=f"mcomp-job-{_RUN}-{uuid.uuid4().hex[:6]}",
        worker_job_attempt=1,
        component_role="agent_sandbox",
        span_ordinal=0,
        provider="daytona",
        started_at=started,
        finished_at=finished_at,
        basis="hooks",
        spec_source="pinned",
        cost_usd=Decimal(cost_usd) if cost_usd is not None else None,
        cost_source="estimated",
        estimator_version=1,
        deleted_at=deleted_at,
    )
    _MODAL_IDS.append(row.id)
    return row


async def _make_billed_task(cleanup_task_ids, *, n_trials, billed_user, org_id):
    task_id = f"quota-comp-{_RUN}-{uuid.uuid4().hex[:6]}"
    cleanup_task_ids.append(task_id)
    await _ensure_org(org_id)
    async with get_session() as session:
        await create_task(
            session,
            _submission("quota-comp", n_trials=n_trials),
            task_id=task_id,
            org_id=org_id,
            billed_user_id=billed_user,
        )
        await session.flush()
    return task_id


def test_inflight_analysis_reservations_follow_composite_setting(monkeypatch):
    monkeypatch.setattr(settings, "quota_counts_analysis_and_compute", False)
    predicate_sets = (
        _inflight_predicates("org-1", "user-1"),
        _org_inflight_predicates("org-1"),
    )

    for predicates in predicate_sets:
        sql = str(
            select(TrialModel.id)
            .where(*predicates)
            .compile(compile_kwargs={"literal_binds": True})
        )
        assert "trials.kind = 'agent'" in sql

    monkeypatch.setattr(settings, "quota_counts_analysis_and_compute", True)
    for predicates in (
        _inflight_predicates("org-1", "user-1"),
        _org_inflight_predicates("org-1"),
    ):
        sql = str(
            select(TrialModel.id)
            .where(*predicates)
            .compile(compile_kwargs={"literal_binds": True})
        )
        assert "trials.kind = 'agent'" not in sql


@pytest.mark.asyncio
async def test_composite_spend_ignored_when_setting_false(
    cleanup_task_ids, monkeypatch
):
    monkeypatch.setattr(
        settings, "quota_counts_analysis_and_compute", False, raising=False
    )
    org_id = f"org-comp-{_RUN}-off"
    billed_user = f"user-comp-{_RUN}-off"
    now = datetime.now(timezone.utc)
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=billed_user, org_id=org_id
    )
    async with get_session() as session:
        trial = await session.get(TrialModel, f"{task_id}-0")
        trial.finished_at = now
        trial.cost_usd = 0.10
        session.add_all(
            [
                _analysis(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd=1.25,
                    created_at=now,
                ),
                _modal(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd="2.50",
                    finished_at=now,
                ),
            ]
        )
        await session.flush()

        user_total = await sum_cost_usd(
            session, org_id, billed_user, quota_window_start(now)
        )
        org_total = await sum_org_cost_usd(session, org_id, start_of_month_utc(now))
        by_user = await sum_cost_usd_by_user(session, org_id, quota_window_start(now))
        by_org_user = await sum_cost_usd_by_org_user_all_orgs(
            session, quota_window_start(now)
        )

    assert user_total == Decimal("0.1000")
    assert org_total == Decimal("0.1000")
    assert by_user[billed_user] == Decimal("0.1000")
    assert by_org_user[(org_id, billed_user)] == Decimal("0.1000")


@pytest.mark.asyncio
async def test_composite_spend_adds_analysis_and_compute_to_user_24h(
    cleanup_task_ids, monkeypatch
):
    monkeypatch.setattr(
        settings, "quota_counts_analysis_and_compute", True, raising=False
    )
    org_id = f"org-comp-{_RUN}-user"
    billed_user = f"user-comp-{_RUN}-user"
    other_user = f"user-comp-{_RUN}-other"
    now = datetime.now(timezone.utc)
    aged_out = now - timedelta(hours=25)
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=billed_user, org_id=org_id
    )
    async with get_session() as session:
        trial = await session.get(TrialModel, f"{task_id}-0")
        trial.finished_at = now
        trial.cost_usd = 0.10
        session.add_all(
            [
                _analysis(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd=1.25,
                    created_at=now,
                ),
                _modal(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd="2.50",
                    finished_at=now,
                ),
                _analysis(
                    org_id=org_id,
                    billed_user_id=other_user,
                    cost_usd=9.99,
                    created_at=now,
                ),
                _analysis(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd=8.00,
                    created_at=aged_out,
                ),
                _modal(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd="7.00",
                    finished_at=aged_out,
                ),
            ]
        )
        await session.flush()

        user_total = await sum_cost_usd(
            session, org_id, billed_user, quota_window_start(now)
        )
        by_user = await sum_cost_usd_by_user(session, org_id, quota_window_start(now))
        by_org_user = await sum_cost_usd_by_org_user_all_orgs(
            session, quota_window_start(now)
        )

    assert user_total == Decimal("3.8500")
    assert by_user[billed_user] == Decimal("3.8500")
    assert by_user[other_user] == Decimal("9.9900")
    assert by_org_user[(org_id, billed_user)] == Decimal("3.8500")


@pytest.mark.asyncio
async def test_composite_spend_adds_analysis_and_compute_to_org_month(
    cleanup_task_ids, monkeypatch
):
    monkeypatch.setattr(
        settings, "quota_counts_analysis_and_compute", True, raising=False
    )
    org_id = f"org-comp-{_RUN}-org"
    billed_user = f"user-comp-{_RUN}-org"
    now = datetime.now(timezone.utc)
    before_month = start_of_month_utc(now) - timedelta(seconds=1)
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=billed_user, org_id=org_id
    )
    async with get_session() as session:
        trial = await session.get(TrialModel, f"{task_id}-0")
        trial.finished_at = now
        trial.cost_usd = 0.10
        session.add_all(
            [
                _analysis(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd=1.25,
                    created_at=now,
                ),
                _modal(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd="2.50",
                    finished_at=now,
                ),
                _analysis(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd=4.00,
                    created_at=before_month,
                ),
                _modal(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd="5.00",
                    finished_at=before_month,
                ),
            ]
        )
        await session.flush()

        org_total = await sum_org_cost_usd(session, org_id, start_of_month_utc(now))

    assert org_total == Decimal("3.8500")


@pytest.mark.asyncio
async def test_composite_spend_excludes_soft_deleted_analysis_and_compute(
    cleanup_task_ids, monkeypatch
):
    monkeypatch.setattr(
        settings, "quota_counts_analysis_and_compute", True, raising=False
    )
    org_id = f"org-comp-{_RUN}-del"
    billed_user = f"user-comp-{_RUN}-del"
    now = datetime.now(timezone.utc)
    task_id = await _make_billed_task(
        cleanup_task_ids, n_trials=1, billed_user=billed_user, org_id=org_id
    )
    async with get_session() as session:
        trial = await session.get(TrialModel, f"{task_id}-0")
        trial.finished_at = now
        trial.cost_usd = 0.10
        session.add_all(
            [
                _analysis(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd=1.25,
                    created_at=now,
                    deleted_at=now,
                ),
                _modal(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd="2.50",
                    finished_at=now,
                    deleted_at=now,
                ),
                _modal(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd=None,
                    finished_at=now,
                ),
                _modal(
                    org_id=org_id,
                    billed_user_id=billed_user,
                    cost_usd="3.00",
                    finished_at=None,
                ),
            ]
        )
        await session.flush()

        user_total = await sum_cost_usd(
            session, org_id, billed_user, quota_window_start(now)
        )
        org_total = await sum_org_cost_usd(session, org_id, start_of_month_utc(now))

    assert user_total == Decimal("0.1000")
    assert org_total == Decimal("0.1000")


@pytest.mark.asyncio
async def test_effective_limits_include_live_bump_without_quota_row(
    cleanup_task_ids, monkeypatch
):
    monkeypatch.setattr(settings, "default_daily_quota_usd", Decimal("200.00"))
    org_id = f"org-comp-{_RUN}-bump"
    user_id = f"user-comp-{_RUN}-bump"
    await _ensure_user(org_id, user_id)
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO quota_bumps "
                "(id, org_id, user_id, amount_usd, expires_at, created_at, updated_at) "
                "VALUES (:id, :org_id, :user_id, :amount, NOW() + INTERVAL '6 hours', "
                "NOW(), NOW())"
            ),
            {
                "id": f"bump-{_RUN}",
                "org_id": org_id,
                "user_id": user_id,
                "amount": Decimal("40.0000"),
            },
        )
        await session.flush()
        limits = await effective_limits_by_org_user_all_orgs(session)

    assert limits[(org_id, user_id)] == to_money_decimal(Decimal("240.00"))


@pytest.mark.asyncio
async def test_effective_limits_propagates_non_missing_table_sql_errors(monkeypatch):
    """A broken query must NOT degrade to base-only limits.

    The fallback exists so a missing ``quota_bumps`` (the last of the three quota
    migrations) does not 500 the admin dashboard. If it also swallowed generic SQL
    faults it would silently drop bumps -- exactly the bug it was added to fix.
    """
    from sqlalchemy.exc import ProgrammingError

    from oddish.core import quotas as quotas_mod

    async def _boom(_session):
        raise ProgrammingError("SELECT bad", {}, Exception("syntax error"))

    monkeypatch.setattr(quotas_mod, "_bump_aware_limits_by_org_user_all_orgs", _boom)
    async with get_session() as session:
        with pytest.raises(ProgrammingError):
            await quotas_mod.effective_limits_by_org_user_all_orgs(session)


@pytest.mark.asyncio
async def test_effective_limits_degrades_only_on_missing_table(monkeypatch):
    from sqlalchemy.exc import ProgrammingError

    from oddish.core import quotas as quotas_mod

    class _UndefinedTableError(Exception):
        sqlstate = "42P01"

    async def _missing(_session):
        raise ProgrammingError("SELECT x", {}, _UndefinedTableError("no such table"))

    monkeypatch.setattr(quotas_mod, "_bump_aware_limits_by_org_user_all_orgs", _missing)
    async with get_session() as session:
        # Falls back to the base-only read rather than raising.
        assert isinstance(
            await quotas_mod.effective_limits_by_org_user_all_orgs(session), dict
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("include_analysis", [False, True])
@pytest.mark.parametrize("status", ["success", "failed", "cancelled"])
async def test_adversarial_cost_counts_only_in_organization_qa_budget(
    session, monkeypatch, include_analysis, status
):
    from oddish.db import ExperimentModel, TrialStatus, utcnow
    from oddish.core.quotas import (
        inflight_reserved_usd,
        inflight_trial_count_by_org_user_all_orgs,
        org_inflight_reserved_usd,
    )

    monkeypatch.setattr(settings, "quota_counts_analysis_and_compute", include_analysis)
    monkeypatch.setattr(settings, "pending_trial_reservation_usd", Decimal("0.25"))
    key = "attack-cost-" + uuid.uuid4().hex[:12]
    session.add(ExperimentModel(id=key, name=key))
    session.add(TaskModel(id=key, name=key, user="test", task_path="p", org_id=key))
    await session.flush()
    trial = TrialModel(
        id=key + "-1",
        name=key,
        task_id=key,
        experiment_id=key,
        org_id=key,
        billed_user_id=key,
        agent="codex",
        model="gpt-5",
        provider="openai",
        queue_key="test",
        kind="adversarial",
        status=TrialStatus.QUEUED,
    )
    session.add(trial)
    await session.flush()
    assert await inflight_reserved_usd(session, key, key) == 0
    assert (key, key) not in await inflight_trial_count_by_org_user_all_orgs(session)
    assert await org_inflight_reserved_usd(session, key) == (
        Decimal("0.25") if include_analysis else 0
    )
    trial.cost_usd = 0.1
    await session.flush()
    start = utcnow() - timedelta(hours=1)
    assert await sum_org_cost_usd(session, key, start) == 0
    assert await org_inflight_reserved_usd(session, key) == (
        Decimal("0.25") if include_analysis else 0
    )
    trial.status = TrialStatus.FAILED if status == "cancelled" else TrialStatus(status)
    if status == "cancelled":
        trial.harbor_stage = "cancelled"
    trial.finished_at = utcnow()
    trial.cost_usd = 0.5
    await session.flush()
    start = utcnow() - timedelta(hours=1)
    assert await sum_cost_usd(session, key, key, start) == 0
    assert await sum_org_cost_usd(session, key, start) == (
        Decimal("0.5") if include_analysis else 0
    )
    assert key not in await sum_cost_usd_by_user(session, key, start)
    assert (key, key) not in await sum_cost_usd_by_org_user_all_orgs(session, start)
    assert await inflight_reserved_usd(session, key, key) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("include_qa", [False, True])
async def test_organization_qa_compute_has_no_personal_payer(
    session, monkeypatch, include_qa
):
    from oddish.db import ExperimentModel, TrialStatus, utcnow

    monkeypatch.setattr(settings, "quota_counts_analysis_and_compute", include_qa)
    key = "qa-compute-" + uuid.uuid4().hex[:12]
    now = utcnow()
    session.add(ExperimentModel(id=key, name=key))
    session.add(TaskModel(id=key, name=key, user="test", task_path="p", org_id=key))
    await session.flush()
    trial = TrialModel(
        id=key + "-attack",
        name=key,
        task_id=key,
        experiment_id=key,
        org_id=key,
        billed_user_id=None,
        agent="codex",
        provider="openai",
        queue_key="test",
        kind="adversarial",
        status=TrialStatus.SUCCESS,
        cost_usd=0.5,
        finished_at=now,
    )
    compute = _modal(org_id=key, billed_user_id=None, cost_usd="0.20", finished_at=now)
    compute.trial_id = trial.id
    # A payer copied onto the attack's compute still must not reach a person.
    paid = _modal(
        org_id=key, billed_user_id="requester", cost_usd="0.30", finished_at=now
    )
    paid.trial_id = trial.id
    session.add_all([trial, compute, paid])
    await session.flush()
    start = now - timedelta(hours=1)
    assert await sum_org_cost_usd(session, key, start) == (
        Decimal("1.00") if include_qa else 0
    )
    assert await sum_cost_usd(session, key, "requester", start) == 0
    assert "requester" not in await sum_cost_usd_by_user(session, key, start)
    assert (key, "requester") not in await sum_cost_usd_by_org_user_all_orgs(
        session, start
    )
