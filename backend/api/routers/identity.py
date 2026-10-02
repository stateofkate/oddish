"""A small identity contract for services that use Oddish sign-in."""

from typing import Annotated, Literal

from auth import APIKeyScope, AuthContext, require_auth
from fastapi import APIRouter, Depends, Response
from models import APIKeyModel, UserModel
from oddish.db import get_read_session
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

router = APIRouter(prefix="/auth", tags=["Authentication"])


class IdentityResponse(BaseModel):
    auth_method: Literal["api_key", "clerk_jwt"]
    org_id: str
    org_slug: str | None
    user_id: str | None
    user_role: str | None
    api_key_id: str | None
    api_key_created_by_role: str | None
    scope: Literal["read", "tasks", "full"]


ActorId = Annotated[str, Field(min_length=1, max_length=68)]


class ActorLookup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[ActorId] = Field(max_length=500)


class ActorDisplay(BaseModel):
    id: ActorId
    display_name: str | None = Field(max_length=256)
    kind: Literal["user", "api_key"]


class ActorsResponse(BaseModel):
    actors: list[ActorDisplay] = Field(max_length=500)


@router.post("/actors/resolve", response_model=ActorsResponse)
async def resolve_actors(
    body: ActorLookup,
    response: Response,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> ActorsResponse:
    """Resolve saved attribution with two bounded reads in the caller's org."""
    auth.require_scope(APIKeyScope.READ)
    response.headers["Cache-Control"] = "no-store"
    actor_ids = list(dict.fromkeys(body.ids))
    user_ids = [actor_id for actor_id in actor_ids if not actor_id.startswith("key:")]
    key_ids = [actor_id[4:] for actor_id in actor_ids if actor_id.startswith("key:")]
    names: dict[str, str | None] = {}
    async with get_read_session() as session:
        if user_ids:
            rows = await session.execute(
                select(UserModel.id, UserModel.name, UserModel.github_username).where(
                    UserModel.org_id == auth.org_id,
                    UserModel.id.in_(user_ids),
                )
            )
            # Inactive members can still own saved work. Keep normal soft-delete
            # filters; deleted or other-org actors receive no display name.
            for user_id, name, handle in rows.all():
                name = (name or "").strip()
                handle = (handle or "").strip().lstrip("@")
                names[user_id] = name or (f"@{handle}" if handle else None)
        if key_ids:
            rows = await session.execute(
                select(APIKeyModel.id).where(
                    APIKeyModel.org_id == auth.org_id,
                    APIKeyModel.id.in_(key_ids),
                    APIKeyModel.is_internal.is_(False),
                )
            )
            names.update((f"key:{key_id}", "API key") for key_id in rows.scalars())
    return ActorsResponse(
        actors=[
            ActorDisplay(
                id=actor_id,
                display_name=names.get(actor_id),
                kind="api_key" if actor_id.startswith("key:") else "user",
            )
            for actor_id in actor_ids
        ]
    )


@router.get("/identity", response_model=IdentityResponse)
async def get_identity(
    response: Response,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> IdentityResponse:
    """Return verified identity and scope after normal organization approval."""
    response.headers["Cache-Control"] = "no-store"
    return IdentityResponse(
        auth_method=auth.method.value,
        org_id=auth.org_id,
        org_slug=auth.org.slug,
        user_id=auth.user_id,
        user_role=auth.user_role.value if auth.user_role else None,
        api_key_id=auth.api_key_id,
        api_key_created_by_role=auth.api_key_created_by_role,
        scope=auth.scope.value,
    )
