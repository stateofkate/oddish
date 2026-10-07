"""Environment setup progress as live trial events.

Harbor reports each environment setup step (``ENVIRONMENT_SUBPHASE``) and the
provisioned environment (``ENVIRONMENT_PROVISIONED``) through trial hooks. This
module turns those into ``trial_events`` rows, written through the same persist
callback as the live transcript, so the trial page can show setup before the
agent starts. Rows stop at ``AGENT_START``; teardown steps are not shown.
"""

from __future__ import annotations

from typing import Any

from harbor.trial.hooks import TrialEvent, TrialHookEvent
from pydantic import BaseModel

from oddish.progress import (
    EnvInfoPayload,
    EnvSubphasePayload,
    SetupEventKind as SetupEventKind,
    SetupStepStatus,
    SetupPayload,
)
from oddish.config import settings
from oddish.workers.console import console
from oddish.workers.harbor import live_tail

# Only the abundant-ai Harbor fork defines these; resolve them the same way as
# trial_handler so an upstream Harbor makes this module a no-op.
_ENVIRONMENT_SUBPHASE = getattr(TrialEvent, "ENVIRONMENT_SUBPHASE", None)
_ENVIRONMENT_PROVISIONED = getattr(TrialEvent, "ENVIRONMENT_PROVISIONED", None)


def is_setup_only_event(event: TrialEvent) -> bool:
    """True for events that only carry setup progress and need no other handling."""
    return _ENVIRONMENT_SUBPHASE is not None and event == _ENVIRONMENT_SUBPHASE


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _environment_value(hook_event: TrialHookEvent, name: str) -> str | None:
    """Read from the live environment, or from the ephemeral bridge's fields."""
    value = getattr(getattr(hook_event, "environment", None), name, None)
    return _optional_str(value) or _optional_str(
        getattr(hook_event, f"environment_{name}", None)
    )


def setup_event_from_hook(hook_event: TrialHookEvent) -> SetupPayload | None:
    """Map one Harbor hook event to a setup payload, or None if it is not one."""
    event = hook_event.event
    if _ENVIRONMENT_SUBPHASE is not None and event == _ENVIRONMENT_SUBPHASE:
        subphase = getattr(hook_event, "subphase", None)
        if subphase is None:
            return None
        activities = getattr(subphase, "completed_activities", None)
        health = getattr(subphase, "ec2_health", None)
        return EnvSubphasePayload(
            ec2_health=health.model_dump(mode="json")
            if isinstance(health, BaseModel)
            else health,
            completed_activities=(
                [
                    item.model_dump(mode="json")
                    if isinstance(item, BaseModel)
                    else item
                    for item in activities
                ]
                if activities is not None
                else None
            ),
            **{
                name: getattr(subphase, name)
                for name in (
                    "schema_version",
                    "operation_id",
                    "parent_operation_id",
                    "category",
                    "resource_layer",
                    "strategy",
                    "provider",
                    "operation_attempt",
                    "finished_at",
                    "activity_kind",
                )
                if hasattr(subphase, name)
            },
            phase=subphase.phase,
            # An enum in-process; a plain string from the ephemeral bridge.
            status=SetupStepStatus(getattr(subphase.status, "value", subphase.status)),
            started_at=subphase.started_at,
            duration_seconds=subphase.duration_seconds,
            error_type=subphase.error_type,
        )
    if _ENVIRONMENT_PROVISIONED is not None and event == _ENVIRONMENT_PROVISIONED:
        provider = _optional_str(hook_event.environment_provider)
        if provider is None:
            return None
        return EnvInfoPayload(
            provider=provider,
            external_id=_optional_str(hook_event.environment_external_id),
            instance_type=_environment_value(hook_event, "instance_type"),
            region=_environment_value(hook_event, "region"),
        )
    return None


class SetupEventEmitter:
    """Writes one trial attempt's setup events as live rows.

    Created per Harbor run and handed to the hook callback. Never raises: a
    hook exception would fail the trial, and these rows are display only.
    """

    def __init__(
        self,
        *,
        trial_id: str,
        attempt: int,
        persist: live_tail.PersistTick,
        org_id: str | None,
        billed_user_id: str | None,
    ) -> None:
        self._trial_id = trial_id
        self._attempt = attempt
        self._persist = persist
        self._org_id = org_id
        self._billed_user_id = billed_user_id
        self._agent_started = False

    async def observe(
        self, hook_event: TrialHookEvent
    ) -> live_tail.PersistResult | None:
        """Record ``hook_event`` if it is a setup event; returns what was persisted."""
        if hook_event.event == TrialEvent.AGENT_START:
            self._agent_started = True
            return None
        if self._agent_started or not settings.live_tail_enabled:
            return None
        try:
            payload = setup_event_from_hook(hook_event)
            if payload is None:
                return None
            return await self._write(payload)
        except Exception as exc:
            console.print(
                f"[yellow]Trial {self._trial_id} setup event not written: "
                f"{type(exc).__name__}[/yellow]"
            )
            return None

    async def _write(self, payload: SetupPayload) -> live_tail.PersistResult | None:
        seq = live_tail.allocate_seq(self._trial_id, self._attempt)
        if seq > live_tail.MAX_TRIAL_EVENTS:
            return None
        row = {
            "seq": seq,
            "kind": payload.kind.value,
            "payload": payload.model_dump(mode="json", exclude={"kind"}),
        }
        return await self._persist(
            trial_id=self._trial_id,
            attempt=self._attempt,
            events=[row],
            checkpoint_values=None,
            org_id=self._org_id,
            billed_user_id=self._billed_user_id,
        )
