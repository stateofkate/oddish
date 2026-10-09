"""Admin > Customers: everything about a customer org, from Oddish.

Operator-org admins only. Name, logo, members, invitations, and domains live in
Clerk and go through its Backend API, so no Abundant admin has to join the
org. Access and budget call the same functions as the ``provision_org`` CLI;
shares are ``org_shares`` rows (see ``org_shares.py``).
"""

from __future__ import annotations

import base64
import binascii
import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from oddish.core.quotas import start_of_month_utc, sum_org_cost_usd
from oddish.db import (
    CustomerModel,
    OrgShareModel,
    TaskModel,
    get_session,
    utcnow,
)
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import provision_org
from auth import AuthContext, require_admin
from auth.permissions import require_operator_org
from api.routers.org_shares import (
    SHARE_MODELS,
    SharedExperiment,
    incoming_experiments,
)
from auth.provisioning import (
    add_clerk_org_domain,
    clerk_api,
    create_clerk_org,
    sync_clerk_org,
)
from models import OrganizationModel, OrgQuotaModel

router = APIRouter(prefix="/admin/customer-orgs", tags=["Admin"])


_DOMAIN = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}$")


def _clean_domain(value: str) -> str:
    value = value.strip().lower().removeprefix("@")
    if not _DOMAIN.match(value):
        raise ValueError("domain must look like cursive.ai")
    return value


Role = Literal["org:admin", "org:member"]


class CustomerOrg(BaseModel):
    id: str
    name: str
    slug: str
    # viewer: signs in read-only. execution: runs jobs within a monthly budget.
    access: Literal["viewer", "execution", "none"]
    share_count: int
    image_url: str | None = None
    members_count: int | None = None


class CreateCustomerOrgRequest(BaseModel):
    name: str
    domain: str

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be empty")
        return value.strip()

    @field_validator("domain")
    @classmethod
    def _domain(cls, value: str) -> str:
        return _clean_domain(value)


class DeliveryCustomer(BaseModel):
    id: str
    name: str
    # The org finalized deliveries to this customer are shared with.
    customer_org_id: str | None


class CustomerOrgDetail(CustomerOrg):
    monthly_limit_usd: float | None = None
    month_spend_usd: float = 0
    # Every delivery customer of the operator org, to map onto this org.
    delivery_customers: list[DeliveryCustomer] = []


class LinkDeliveryCustomerRequest(BaseModel):
    linked: bool


class UpdateOrgRequest(BaseModel):
    name: str | None = None
    slug: str | None = None


class SetAccessRequest(BaseModel):
    access: Literal["viewer", "execution", "none"]
    # Required for execution: approval always comes with a budget.
    monthly_limit_usd: Decimal | None = None


class Member(BaseModel):
    user_id: str
    email: str | None
    name: str | None
    image_url: str | None
    role: str
    joined_at: int | None


class Invitation(BaseModel):
    id: str
    email: str
    role: str
    created_at: int | None


class Domain(BaseModel):
    id: str
    name: str
    enrollment_mode: str


class RoleRequest(BaseModel):
    role: Role


class DomainRequest(BaseModel):
    domain: str

    @field_validator("domain")
    @classmethod
    def _domain(cls, value: str) -> str:
        return _clean_domain(value)


class LogoRequest(BaseModel):
    # Sent as JSON so the frontend reuses its JSON proxy.
    filename: str
    content_type: str
    data_base64: str


class InviteRequest(BaseModel):
    email: str
    role: Role = "org:member"

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        value = value.strip()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value):
            raise ValueError("enter an email address")
        return value


class CustomerShare(BaseModel):
    id: str
    resource_type: Literal["experiment", "task", "delivery"]
    resource_id: str
    label: str
    # The resource was deleted (or the delivery is not finalized yet), so the
    # share grants nothing for now.
    inactive: bool
    show_costs: bool
    shared_at: datetime


def _response(
    org: OrganizationModel, share_count: int, clerk_org: dict | None = None
) -> CustomerOrg:
    if org.execution_enabled:
        access = "execution"
    else:
        access = "viewer" if org.viewer_access else "none"
    clerk_org = clerk_org or {}
    return CustomerOrg(
        id=org.id,
        name=org.name,
        slug=org.slug,
        access=access,
        share_count=share_count,
        image_url=clerk_org.get("image_url") if clerk_org.get("has_image") else None,
        members_count=clerk_org.get("members_count"),
    )


async def _clerk_orgs_by_id() -> dict[str, dict]:
    """Logos and member counts; the list still loads if Clerk is unreachable."""
    try:
        payload = await clerk_api(
            "GET",
            "/organizations",
            params={"limit": 500, "include_members_count": "true"},
        )
    except HTTPException:
        return {}
    return {org["id"]: org for org in payload.get("data", [])}


async def _customer_org(
    session: AsyncSession, org_id: str, auth: AuthContext
) -> OrganizationModel:
    """404 for unknown, inactive, or the operator's own org."""
    org = await session.get(OrganizationModel, org_id)
    if org is None or not org.is_active or org.id == auth.org_id:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org


async def _share_count(session: AsyncSession, org_id: str) -> int:
    count = await session.scalar(
        select(func.count()).where(
            OrgShareModel.grantee_org_id == org_id,
            OrgShareModel.deleted_at.is_(None),
        )
    )
    return count or 0


@router.get("", response_model=list[CustomerOrg])
async def list_customer_orgs(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> list[CustomerOrg]:
    """Active customer orgs, with how many shares each has."""
    require_operator_org(auth)
    share_counts = (
        select(OrgShareModel.grantee_org_id, func.count().label("n"))
        .where(OrgShareModel.deleted_at.is_(None))
        .group_by(OrgShareModel.grantee_org_id)
        .subquery()
    )
    async with get_session() as session:
        rows = await session.execute(
            select(OrganizationModel, func.coalesce(share_counts.c.n, 0))
            .outerjoin(
                share_counts, share_counts.c.grantee_org_id == OrganizationModel.id
            )
            .where(
                OrganizationModel.is_active.is_(True),
                OrganizationModel.id != auth.org_id,
                OrganizationModel.org_type == "customer",
            )
            .order_by(OrganizationModel.name)
        )
        rows = rows.all()
    clerk_orgs = await _clerk_orgs_by_id()
    return [
        _response(org, count, clerk_orgs.get(org.clerk_org_id or ""))
        for org, count in rows
    ]


@router.post("", response_model=CustomerOrg)
async def create_customer_org(
    payload: CreateCustomerOrgRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> CustomerOrg:
    """Create the Clerk org with view-only sign-in, then invite its email domain.

    Nobody is added as a member; people at ``domain`` join on first sign-in.
    """
    require_operator_org(auth)
    clerk_org = await create_clerk_org(payload.name)
    async with get_session() as session:
        org = await sync_clerk_org(
            session, clerk_org["id"], clerk_org.get("name"), clerk_org.get("slug")
        )
        org.viewer_access = True
        org.org_type = "customer"
        await session.commit()
        response = _response(org, 0)
    try:
        await add_clerk_org_domain(clerk_org["id"], payload.domain)
    except HTTPException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Created {payload.name}, but adding {payload.domain} failed; "
            f"add the domain in Clerk. {exc.detail}",
        ) from exc
    return response


@router.put("/{org_id}/access", response_model=CustomerOrgDetail)
async def set_customer_access(
    org_id: str,
    payload: SetAccessRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> CustomerOrgDetail:
    """View-only, no access, or run jobs within a monthly budget.

    Leaving execution cancels the org's queued and running work.
    """
    require_operator_org(auth)
    clerk_org_id = await _clerk_id(org_id, auth)
    try:
        if payload.access == "execution":
            await provision_org.provision(
                clerk_org_id, enable=True, monthly_limit=payload.monthly_limit_usd
            )
        elif payload.access == "viewer":
            await provision_org.grant_viewer(clerk_org_id)
        else:
            await provision_org.provision(
                clerk_org_id, enable=False, monthly_limit=None
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await get_customer_org(org_id, auth)


@router.get("/{org_id}", response_model=CustomerOrgDetail)
async def get_customer_org(
    org_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> CustomerOrgDetail:
    require_operator_org(auth)
    async with get_session() as session:
        org = await _customer_org(session, org_id, auth)
        limit = await session.scalar(
            select(OrgQuotaModel.limit_usd).where(
                OrgQuotaModel.org_id == org_id, OrgQuotaModel.deleted_at.is_(None)
            )
        )
        spend = await sum_org_cost_usd(session, org_id, start_of_month_utc())
        base = _response(org, await _share_count(session, org_id))
        clerk_org_id = org.clerk_org_id
        delivery_customers = [
            DeliveryCustomer(id=c.id, name=c.name, customer_org_id=c.customer_org_id)
            for c in await session.scalars(
                select(CustomerModel)
                .where(CustomerModel.org_id == auth.org_id)
                .order_by(CustomerModel.name)
            )
        ]
    clerk_org = {}
    if clerk_org_id:
        try:
            clerk_org = await clerk_api(
                "GET",
                f"/organizations/{clerk_org_id}",
                params={"include_members_count": "true"},
            )
        except HTTPException:
            pass
    return CustomerOrgDetail(
        **_response(org, base.share_count, clerk_org).model_dump(),
        monthly_limit_usd=float(limit) if limit is not None else None,
        month_spend_usd=float(spend),
        delivery_customers=delivery_customers,
    )


@router.put("/{org_id}/delivery-customers/{customer_id}", response_model=dict)
async def link_delivery_customer(
    org_id: str,
    customer_id: str,
    payload: LinkDeliveryCustomerRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    """Finalized deliveries to this customer are shared with this org."""
    require_operator_org(auth)
    async with get_session() as session:
        await _customer_org(session, org_id, auth)
        customer = await session.scalar(
            select(CustomerModel).where(
                CustomerModel.id == customer_id, CustomerModel.org_id == auth.org_id
            )
        )
        if customer is None:
            raise HTTPException(status_code=404, detail="Customer not found")
        if payload.linked:
            customer.customer_org_id = org_id
        elif customer.customer_org_id == org_id:
            customer.customer_org_id = None
        await session.commit()
    return {"status": "ok"}


@router.patch("/{org_id}", response_model=CustomerOrgDetail)
async def update_customer_org(
    org_id: str,
    payload: UpdateOrgRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> CustomerOrgDetail:
    """Rename the org or change its slug in Clerk, then mirror it here."""
    require_operator_org(auth)
    clerk_org_id = await _clerk_id(org_id, auth)
    body = {k: v.strip() for k, v in payload.model_dump().items() if v and v.strip()}
    if body:
        updated = await clerk_api("PATCH", f"/organizations/{clerk_org_id}", json=body)
        async with get_session() as session:
            await sync_clerk_org(
                session, clerk_org_id, updated.get("name"), updated.get("slug")
            )
            await session.commit()
    return await get_customer_org(org_id, auth)


@router.get("/{org_id}/members", response_model=list[Member])
async def list_customer_members(
    org_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> list[Member]:
    require_operator_org(auth)
    clerk_org_id = await _clerk_id(org_id, auth)
    payload = await clerk_api(
        "GET",
        f"/organizations/{clerk_org_id}/memberships",
        params={"limit": 500, "order_by": "created_at"},
    )
    members = []
    for row in payload.get("data", []):
        user = row.get("public_user_data") or {}
        name = " ".join(filter(None, (user.get("first_name"), user.get("last_name"))))
        members.append(
            Member(
                user_id=user.get("user_id", ""),
                email=user.get("identifier"),
                name=name or None,
                image_url=user.get("image_url"),
                role=row.get("role", ""),
                joined_at=row.get("created_at"),
            )
        )
    return members


@router.patch("/{org_id}/members/{user_id}", response_model=dict)
async def set_customer_member_role(
    org_id: str,
    user_id: str,
    payload: RoleRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    require_operator_org(auth)
    clerk_org_id = await _clerk_id(org_id, auth)
    await clerk_api(
        "PATCH",
        f"/organizations/{clerk_org_id}/memberships/{user_id}",
        json={"role": payload.role},
    )
    return {"status": "ok"}


@router.delete("/{org_id}/members/{user_id}", response_model=dict)
async def remove_customer_member(
    org_id: str,
    user_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    require_operator_org(auth)
    clerk_org_id = await _clerk_id(org_id, auth)
    await clerk_api("DELETE", f"/organizations/{clerk_org_id}/memberships/{user_id}")
    return {"status": "ok"}


@router.get("/{org_id}/invitations", response_model=list[Invitation])
async def list_customer_invitations(
    org_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> list[Invitation]:
    """Pending invitations only."""
    require_operator_org(auth)
    clerk_org_id = await _clerk_id(org_id, auth)
    payload = await clerk_api(
        "GET",
        f"/organizations/{clerk_org_id}/invitations",
        params={"status": "pending", "limit": 500},
    )
    return [
        Invitation(
            id=row["id"],
            email=row.get("email_address", ""),
            role=row.get("role", ""),
            created_at=row.get("created_at"),
        )
        for row in payload.get("data", [])
    ]


@router.delete("/{org_id}/invitations/{invitation_id}", response_model=dict)
async def revoke_customer_invitation(
    org_id: str,
    invitation_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    require_operator_org(auth)
    clerk_org_id = await _clerk_id(org_id, auth)
    # No requesting_user_id: Clerk wants an admin of *that* org, and Abundant
    # staff are not members of customer orgs.
    await clerk_api(
        "POST",
        f"/organizations/{clerk_org_id}/invitations/{invitation_id}/revoke",
        json={},
    )
    return {"status": "ok"}


@router.get("/{org_id}/domains", response_model=list[Domain])
async def list_customer_domains(
    org_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> list[Domain]:
    require_operator_org(auth)
    clerk_org_id = await _clerk_id(org_id, auth)
    payload = await clerk_api("GET", f"/organizations/{clerk_org_id}/domains")
    return [
        Domain(
            id=row["id"],
            name=row.get("name", ""),
            enrollment_mode=row.get("enrollment_mode", ""),
        )
        for row in payload.get("data", [])
    ]


@router.post("/{org_id}/domains", response_model=dict)
async def add_customer_domain(
    org_id: str,
    payload: DomainRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    """People signing in with an email at this domain are invited automatically."""
    require_operator_org(auth)
    await add_clerk_org_domain(await _clerk_id(org_id, auth), payload.domain)
    return {"status": "ok"}


@router.delete("/{org_id}/domains/{domain_id}", response_model=dict)
async def remove_customer_domain(
    org_id: str,
    domain_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    require_operator_org(auth)
    clerk_org_id = await _clerk_id(org_id, auth)
    await clerk_api("DELETE", f"/organizations/{clerk_org_id}/domains/{domain_id}")
    return {"status": "ok"}


@router.get("/{org_id}/shares", response_model=list[CustomerShare])
async def list_customer_shares(
    org_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> list[CustomerShare]:
    """This org's experiment, task, and delivery shares, newest first."""
    require_operator_org(auth)
    async with get_session() as session:
        await _customer_org(session, org_id, auth)
        shares = (
            await session.scalars(
                select(OrgShareModel)
                .where(
                    OrgShareModel.grantee_org_id == org_id,
                    OrgShareModel.resource_type.in_(SHARE_MODELS),
                    OrgShareModel.deleted_at.is_(None),
                )
                .order_by(OrgShareModel.created_at.desc())
            )
        ).all()
        resources = {}
        for kind, model in SHARE_MODELS.items():
            ids = [s.resource_id for s in shares if s.resource_type == kind]
            for row in await session.scalars(
                select(model)
                .where(model.id.in_(ids), model.org_id == auth.org_id)
                .execution_options(include_deleted=True)
            ):
                resources[kind, row.id] = row
    result = []
    for share in shares:
        resource = resources.get((share.resource_type, share.resource_id))
        if resource is None:
            continue
        inactive = getattr(resource, "deleted_at", None) is not None or (
            share.resource_type == "delivery" and resource.status != "finalized"
        )
        result.append(
            CustomerShare(
                id=share.id,
                resource_type=share.resource_type,
                resource_id=share.resource_id,
                label=resource.name,
                inactive=inactive,
                show_costs=share.show_costs,
                shared_at=share.created_at,
            )
        )
    return result


@router.get("/{org_id}/experiments", response_model=list[SharedExperiment])
async def list_customer_experiments(
    org_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> list[SharedExperiment]:
    """Exactly what this org sees on its Shared page."""
    require_operator_org(auth)
    async with get_session() as session:
        await _customer_org(session, org_id, auth)
        return await incoming_experiments(session, org_id)


async def _clerk_id(org_id: str, auth: AuthContext) -> str:
    async with get_session() as session:
        org = await _customer_org(session, org_id, auth)
        if not org.clerk_org_id:
            raise HTTPException(status_code=409, detail="This org has no Clerk org")
        return org.clerk_org_id


@router.post("/{org_id}/logo")
async def upload_customer_logo(
    org_id: str,
    payload: LogoRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    """Set the org's logo in Clerk; it shows in their org switcher."""
    require_operator_org(auth)
    if not payload.content_type.startswith("image/"):
        raise HTTPException(status_code=422, detail="Logo must be an image")
    try:
        content = base64.b64decode(payload.data_base64, validate=True)
    except binascii.Error as exc:
        raise HTTPException(status_code=422, detail="Logo is not base64") from exc
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Logo must be under 10 MB")
    clerk_org_id = await _clerk_id(org_id, auth)
    await clerk_api(
        "PUT",
        f"/organizations/{clerk_org_id}/logo",
        files={"file": (payload.filename, content, payload.content_type)},
    )
    return {"status": "ok"}


@router.post("/{org_id}/invitations")
async def invite_customer_admin(
    org_id: str,
    payload: InviteRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    """Invite someone into the org as an admin or member."""
    require_operator_org(auth)
    clerk_org_id = await _clerk_id(org_id, auth)
    await clerk_api(
        "POST",
        f"/organizations/{clerk_org_id}/invitations",
        json={"email_address": payload.email, "role": payload.role},
    )
    return {"status": "ok"}


@router.delete("/{org_id}")
async def delete_customer_org(
    org_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    """Delete the Clerk org. Orgs that run or ever ran jobs are left to the CLI;
    Clerk deletion can't be undone."""
    require_operator_org(auth)
    async with get_session() as session:
        org = await _customer_org(session, org_id, auth)
        ran_jobs = await session.scalar(
            select(
                select(TaskModel.id).where(TaskModel.org_id == org.id).exists()
            ).execution_options(include_deleted=True)
        )
        if org.execution_enabled or ran_jobs:
            raise HTTPException(
                status_code=409,
                detail="This org has run jobs; it can't be deleted here.",
            )
        clerk_org_id = org.clerk_org_id
    if clerk_org_id:
        await clerk_api("DELETE", f"/organizations/{clerk_org_id}")
    # The organization.deleted webhook does the same; don't wait for it.
    async with get_session() as session:
        org = await session.get(OrganizationModel, org_id)
        if org is not None:  # the webhook may have soft-deleted it already
            org.viewer_access = False
            org.is_active = False
            org.deleted_at = utcnow()
            await session.commit()
    return {"status": "ok"}
