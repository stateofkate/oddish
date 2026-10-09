"""Operator-only sharing of experiments, tasks, and deliveries with customer
orgs (``org_shares``).

Grantee members open shared experiments in the app by id and shared tasks under
``DIRECT_TASK_KEY`` (see ``get_public_task_for_experiment``), and list them
through ``GET /org-shares/incoming``. A delivery share counts once the delivery
is finalized, and covers its visible tasks at their shipped versions. Shares
never touch an experiment's ``/share/<token>`` link.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from auth import AuthContext, authorized_read_session, get_auth_context, require_admin
from auth.permissions import require_operator_org
from fastapi import APIRouter, Depends, HTTPException, Request
from models import OrganizationModel
from oddish.core.sharing.helpers import shared_tasks_query
from oddish.db import (
    CustomerModel,
    DeliveryModel,
    ExperimentModel,
    OrgShareModel,
    TaskModel,
    TaskVersionModel,
    get_session,
    task_experiments,
    utcnow,
)
from oddish.core.experiment_membership import experiment_trial_scope
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/org-shares", tags=["Sharing"])


class ShareableOrg(BaseModel):
    id: str
    name: str
    slug: str


class OrgShareResponse(BaseModel):
    id: str
    grantee_org_id: str
    grantee_org_name: str
    resource_type: str
    resource_id: str
    show_costs: bool
    created_at: datetime


ResourceType = Literal["experiment", "task", "delivery"]
SHARE_MODELS = {
    "experiment": ExperimentModel,
    "task": TaskModel,
    "delivery": DeliveryModel,
}


class CreateOrgShareRequest(BaseModel):
    grantee_org_id: str
    resource_type: ResourceType = "experiment"
    resource_id: str


class UpdateOrgShareRequest(BaseModel):
    show_costs: bool


class SharedExperiment(BaseModel):
    id: str
    name: str
    description: str | None
    shared_at: datetime
    task_count: int = 0
    run_count: int = 0


class SharedTask(BaseModel):
    id: str
    name: str
    # The shipped version for a delivered task; None means the current one.
    version: int | None
    delivery_id: str | None
    delivery_name: str | None
    shared_at: datetime | None


async def incoming_experiments(
    session: AsyncSession, org_id: str | None
) -> list[SharedExperiment]:
    """Experiments shared with ``org_id``, newest share first."""
    tasks = (
        select(func.count())
        .select_from(task_experiments)
        .where(
            task_experiments.c.experiment_id == ExperimentModel.id,
            task_experiments.c.deleted_at.is_(None),
        )
        .scalar_subquery()
    )
    rows = await session.execute(
        select(ExperimentModel, OrgShareModel.created_at, tasks)
        .join(OrgShareModel, OrgShareModel.resource_id == ExperimentModel.id)
        .where(
            OrgShareModel.grantee_org_id == org_id,
            OrgShareModel.resource_type == "experiment",
            OrgShareModel.deleted_at.is_(None),
        )
        .order_by(OrgShareModel.created_at.desc())
    )
    result = []
    for experiment, at, task_count in rows.all():
        # The same runs the sample's page shows: gathered ones included,
        # combine copies and QA/oracle trials left out.
        # ponytail: one count per shared sample; a customer has a handful.
        # The indexed UNION ALL the experiment page uses, not an OR over trials.
        scope = experiment_trial_scope(experiment.id, org_id=experiment.org_id)
        run_count = await session.scalar(
            select(func.count())
            .select_from(scope.trials)
            .where(*scope.visible_predicates())
        )
        result.append(
            SharedExperiment(
                id=experiment.id,
                name=experiment.name,
                description=experiment.description,
                shared_at=at,
                task_count=task_count,
                run_count=run_count or 0,
            )
        )
    return result


async def incoming_tasks(session: AsyncSession, org_id: str) -> list[SharedTask]:
    """Every task the org can open, newest grant first, once per task. A task
    shipped in a delivery shows its newest delivery and that pinned version."""
    grants = shared_tasks_query(org_id).subquery()
    rows = await session.execute(
        select(
            TaskModel.id,
            TaskModel.name,
            TaskVersionModel.version,
            DeliveryModel.id,
            DeliveryModel.name,
            grants.c.shared_at,
        )
        .join(grants, grants.c.task_id == TaskModel.id)
        .outerjoin(TaskVersionModel, TaskVersionModel.id == grants.c.task_version_id)
        .outerjoin(DeliveryModel, DeliveryModel.id == grants.c.delivery_id)
        .order_by(
            TaskModel.id,
            (grants.c.delivery_id.is_(None)),
            grants.c.shared_at.desc().nulls_last(),
        )
    )
    tasks: dict[str, SharedTask] = {}
    for task_id, name, version, delivery_id, delivery_name, at in rows.all():
        tasks.setdefault(
            task_id,
            SharedTask(
                id=task_id,
                name=name,
                version=version,
                delivery_id=delivery_id,
                delivery_name=delivery_name,
                shared_at=at,
            ),
        )
    return sorted(
        tasks.values(),
        key=lambda t: t.shared_at.timestamp() if t.shared_at else 0,
        reverse=True,
    )


async def _shares_for(
    session: AsyncSession, resource_type: str, resource_id: str
) -> list[OrgShareResponse]:
    rows = await session.execute(
        select(OrgShareModel, OrganizationModel.name)
        .join(OrganizationModel, OrganizationModel.id == OrgShareModel.grantee_org_id)
        .where(
            OrgShareModel.resource_type == resource_type,
            OrgShareModel.resource_id == resource_id,
            OrgShareModel.deleted_at.is_(None),
        )
        .order_by(OrganizationModel.name)
    )
    return [
        OrgShareResponse(
            id=share.id,
            grantee_org_id=share.grantee_org_id,
            grantee_org_name=name,
            resource_type=share.resource_type,
            resource_id=share.resource_id,
            show_costs=share.show_costs,
            created_at=share.created_at,
        )
        for share, name in rows.all()
    ]


async def _own(
    session: AsyncSession,
    resource_type: str,
    resource_id: str,
    org_id: str | None,
    *,
    include_deleted: bool = False,
) -> None:
    """404 unless the operator org owns the resource. Revokes pass
    ``include_deleted``: deleted experiments still have shares to remove."""
    model = SHARE_MODELS.get(resource_type)
    owner = None
    if model is not None:
        owner = await session.scalar(
            select(model.org_id)
            .where(model.id == resource_id)
            .execution_options(include_deleted=include_deleted)
        )
    if owner is None or owner != org_id:
        raise HTTPException(status_code=404, detail="Not found")


@router.get("/orgs", response_model=list[ShareableOrg])
async def list_shareable_orgs(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> list[ShareableOrg]:
    """Customer orgs, the only ones anything is shared with."""
    require_operator_org(auth)
    async with get_session() as session:
        orgs = await session.scalars(
            select(OrganizationModel)
            .where(
                OrganizationModel.is_active.is_(True),
                OrganizationModel.id != auth.org_id,
                OrganizationModel.org_type == "customer",
            )
            .order_by(OrganizationModel.name)
        )
        return [ShareableOrg(id=o.id, name=o.name, slug=o.slug) for o in orgs]


@router.get("", response_model=list[OrgShareResponse])
async def list_org_shares(
    resource_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
    resource_type: ResourceType = "experiment",
) -> list[OrgShareResponse]:
    require_operator_org(auth)
    async with get_session() as session:
        await _own(session, resource_type, resource_id, auth.org_id)
        return await _shares_for(session, resource_type, resource_id)


@router.post("", response_model=list[OrgShareResponse])
async def create_org_share(
    payload: CreateOrgShareRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> list[OrgShareResponse]:
    """Share an experiment, task, or delivery with an org."""
    require_operator_org(auth)
    async with get_session() as session:
        await _own(session, payload.resource_type, payload.resource_id, auth.org_id)
        org = await session.get(OrganizationModel, payload.grantee_org_id)
        if (
            org is None
            or not org.is_active
            or org.org_type != "customer"
            or org.id == auth.org_id
        ):
            raise HTTPException(status_code=404, detail="Organization not found")
        await add_org_share(
            session,
            grantee_org_id=org.id,
            resource_type=payload.resource_type,
            resource_id=payload.resource_id,
            user_id=auth.user_id,
        )
        await session.commit()
        return await _shares_for(session, payload.resource_type, payload.resource_id)


async def add_org_share(
    session: AsyncSession,
    *,
    grantee_org_id: str,
    resource_type: str,
    resource_id: str,
    user_id: str | None,
) -> None:
    """Insert a live share unless one exists. The caller commits."""
    existing = await session.scalar(
        select(OrgShareModel.id).where(
            OrgShareModel.grantee_org_id == grantee_org_id,
            OrgShareModel.resource_type == resource_type,
            OrgShareModel.resource_id == resource_id,
            OrgShareModel.deleted_at.is_(None),
        )
    )
    if existing is None:
        session.add(
            OrgShareModel(
                grantee_org_id=grantee_org_id,
                resource_type=resource_type,
                resource_id=resource_id,
                granted_by_user_id=user_id,
            )
        )


async def share_with_customer_org(
    session: AsyncSession,
    customer_id: str | None,
    delivery_id: str,
    user_id: str | None,
) -> None:
    """On finalize: share the delivery with its customer's org, if mapped."""
    customer_org_id = await session.scalar(
        select(CustomerModel.customer_org_id).where(CustomerModel.id == customer_id)
    )
    if customer_org_id:
        await add_org_share(
            session,
            grantee_org_id=customer_org_id,
            resource_type="delivery",
            resource_id=delivery_id,
            user_id=user_id,
        )


async def _live_share(session: AsyncSession, share_id: str) -> OrgShareModel:
    share = await session.get(OrgShareModel, share_id)
    if share is None or share.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Share not found")
    return share


@router.patch("/{share_id}", response_model=OrgShareResponse)
async def update_org_share(
    share_id: str,
    payload: UpdateOrgShareRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> OrgShareResponse:
    """Show or hide experiment costs from the grantee."""
    require_operator_org(auth)
    async with get_session() as session:
        share = await _live_share(session, share_id)
        share.show_costs = payload.show_costs
        await session.commit()
        org = await session.get(OrganizationModel, share.grantee_org_id)
        return OrgShareResponse(
            id=share.id,
            grantee_org_id=share.grantee_org_id,
            grantee_org_name=org.name if org else share.grantee_org_id,
            resource_type=share.resource_type,
            resource_id=share.resource_id,
            show_costs=share.show_costs,
            created_at=share.created_at,
        )


@router.delete("/{share_id}", response_model=list[OrgShareResponse])
async def revoke_org_share(
    share_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> list[OrgShareResponse]:
    """Revoke one share. The experiment's public link, if any, is untouched."""
    require_operator_org(auth)
    async with get_session() as session:
        share = await _live_share(session, share_id)
        await _own(
            session,
            share.resource_type,
            share.resource_id,
            auth.org_id,
            include_deleted=True,
        )
        share.deleted_at = utcnow()
        await session.commit()
        return await _shares_for(session, share.resource_type, share.resource_id)


@router.get("/incoming", response_model=list[SharedExperiment])
async def list_incoming_shares(
    request: Request,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
) -> list[SharedExperiment]:
    """Experiments other orgs shared with the caller's org, newest share first."""
    async with authorized_read_session(request, auth) as session:
        return await incoming_experiments(session, auth.org_id)


@router.get("/incoming/tasks", response_model=list[SharedTask])
async def list_incoming_tasks(
    request: Request,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
) -> list[SharedTask]:
    """Tasks shared with the caller's org directly, by delivery, or by experiment."""
    async with authorized_read_session(request, auth) as session:
        return await incoming_tasks(session, auth.org_id)
