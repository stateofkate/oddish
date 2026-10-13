"""Authenticated live capacity reads: the load snapshot and configured models."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from auth import AuthContext, require_auth
from auth.permissions import is_operator_org, require_operator_org
from oddish.core.admin import (
    ConfiguredModelsResponse,
    LoadSnapshot,
    get_cached_load_snapshot,
    get_configured_models_core,
)
from oddish.db import get_read_session

router = APIRouter(tags=["Load"])


@router.get("/load", response_model=LoadSnapshot)
async def get_load(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> LoadSnapshot:
    """Live submission-load snapshot (auth-gated, ~5s cached, single-flighted).

    Queue keys name every organization's models, so only the operator org
    sees ``queues``; other orgs get the pacing fields and totals.
    """
    snapshot = await get_cached_load_snapshot()
    if is_operator_org(auth):
        return snapshot
    return snapshot.model_copy(update={"queues": []})


@router.get("/models", response_model=ConfiguredModelsResponse)
async def get_models(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> ConfiguredModelsResponse:
    """Configured models with concurrency limits, live load, and monitor health.

    Any key scope works, but only in the operator org: the list names
    vendors' private models.
    """
    from endpoint_health import monitor_health, platform_target_index

    require_operator_org(auth)
    snapshot = await get_cached_load_snapshot()
    async with get_read_session() as session:
        monitors = await monitor_health(session)
        by_name = {monitor.name: monitor for monitor in monitors.values()}
        # Join on the probed connection first: aliases that share one have a
        # single monitor named after only one of them. Explicit
        # ODDISH_ENDPOINT_MONITORS entries match by their queue-key name.
        _, target_ids = await platform_target_index(session)
        health = {
            queue_key: (monitor.health, monitor.last_checked_at)
            for queue_key, target_id in target_ids.items()
            if (monitor := monitors.get(target_id) or by_name.get(queue_key))
        }
        return await get_configured_models_core(
            session, snapshot=snapshot, health=health
        )
