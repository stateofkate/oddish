"""Unikraft Cloud execution backend.

Each trial runs in its own microVM. Harbor (``harbor.environments.unikraft``)
owns the sandbox lifecycle; this backend adds the oddish-side settings only.
Hosted workers have no BuildKit, so they run with ``require_prebuilt_image``
and the submitting CLI builds and pushes each task image first
(``oddish.cli.unikraft_prebuild``).

Configuration: ``UKC_TOKEN``, ``UKC_METRO`` and ``UKC_USER`` for the SDK and
Harbor (``UKC_USER`` also names the network shield images);
``HARBOR_UNIKRAFT_SHIELD_CA`` for tasks with a non-public network policy;
``ODDISH_UNIKRAFT_ENABLED`` registers the backend.
``ODDISH_UNIKRAFT_DEFAULT_MEMORY_MB`` sizes a trial whose task sets no memory.
"""

from __future__ import annotations

import contextlib
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from oddish.config import settings
from oddish.runtime.ports import Capabilities, ExecutionBackend

logger = logging.getLogger(__name__)

#: The tag Harbor puts on a trial's task instance and on its network shield.
_SESSION_TAG = "harbor.session="
#: The plain tag Harbor puts on every instance it creates.
_HARBOR_TAG = "harbor"
#: How long a delete keeps retrying while the platform reports an instance busy.
_DELETE_BUSY_RETRY_SEC = 20.0
#: A lone shield younger than this can still wait for its task instance.
_ORPHAN_SHIELD_GRACE = timedelta(minutes=15)


def _session_tag(instance: Any) -> str | None:
    """The instance's ``harbor.session=`` tag, or ``None``."""
    return next(
        (tag for tag in instance.tags or [] if tag.startswith(_SESSION_TAG)), None
    )


def _is_shield(instance: Any) -> bool:
    """Whether the instance is a network shield. Harbor appends "-shield"."""
    return (instance.name or "").endswith("-shield")


async def _sweep_orphaned_shields(client: Any) -> None:
    """Delete each session that holds only shields, in any instance state.
    A grace period keeps a shield that still waits for its task instance."""
    try:
        sessions: dict[str, list[Any]] = {}
        async for instance in client.instances.list(tags=[_HARBOR_TAG], details=True):
            session = _session_tag(instance)
            if session:
                sessions.setdefault(session, []).append(instance)
        cutoff = datetime.now(timezone.utc) - _ORPHAN_SHIELD_GRACE
        for session, members in sessions.items():
            if not all(_is_shield(member) for member in members):
                continue
            if any(m.created_at is None or m.created_at > cutoff for m in members):
                continue
            await client.instances.each(tags=[session]).delete(
                missing_ok=True, retry_busy=_DELETE_BUSY_RETRY_SEC
            )
            logger.info(
                "UnikraftBackend.teardown: deleted orphaned shield session %s",
                session,
            )
    except Exception:
        logger.warning(
            "UnikraftBackend.teardown: orphaned shield sweep failed", exc_info=True
        )


def unikraft_org() -> str | None:
    """The registry namespace: ``UKC_USER``, else the CLI profile's organization."""
    return os.environ.get("UKC_USER") or cli_profile_organization()


def cli_profile_organization() -> str | None:
    """The ``organization`` of the active ``unikraft`` CLI profile, or ``None``."""
    # UNIKRAFT_PROFILE names the profile and UNIKRAFT_CONFIG the file. Never raises.
    path = Path(
        os.environ.get("UNIKRAFT_CONFIG")
        or Path.home() / ".config" / "unikraft" / "config.yaml"
    )
    try:
        import yaml

        config = yaml.safe_load(path.read_text()) or {}
        profiles = config.get("profiles") or {}
        name = os.environ.get("UNIKRAFT_PROFILE") or config.get("profile")
        organization = (profiles.get(name) or {}).get("organization") if name else None
    except Exception:
        return None
    return str(organization) if organization else None


class UnikraftBackend:
    name = "unikraft"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            gpu=None,
            tpu=None,
            # A private base image works through the build host's credentials.
            private_registry_pull=False,
            network_egress="configurable",
            persistent_volumes=True,
            streaming_logs=True,
            memory_snapshot_fork=True,
            # An existing image creates in 0.4s; a new one pays a build first.
            cold_start="instant",
        )

    def harbor_env_kwargs(self, base_kwargs: dict[str, Any]) -> dict[str, Any]:
        # Deployment defaults first, caller kwargs last (caller wins), matching
        # the GKE and Daytona spreads. Names mirror ``UnikraftEnvironment``.
        kwargs: dict[str, Any] = {}
        # Shipped only when set: a job kwarg covers its task.toml counterpart,
        # so an unconditional False would override a task that asks for it.
        if settings.unikraft_require_prebuilt_image:
            kwargs["require_prebuilt_image"] = True
        if settings.unikraft_default_memory_mb:
            kwargs["default_memory_mb"] = settings.unikraft_default_memory_mb
        kwargs.update(base_kwargs)
        return kwargs

    async def teardown(self, external_id: str) -> bool:
        """Delete a hung trial's instances, given its task instance. Never raises.

        Harbor tags the task instance and its shield with the session, so both go.
        """
        if not external_id:
            return False
        metro = os.environ.get("UKC_METRO")
        if not metro:
            logger.warning("UnikraftBackend.teardown: no metro configured")
            return False
        try:
            from unikraft_cloud import NotFoundError, UnikraftCloud

            async with UnikraftCloud(metro=metro) as client:
                handle = client.instances.get(uuid=external_id)
                try:
                    instance = await handle
                except NotFoundError:
                    logger.info(
                        "UnikraftBackend.teardown: %s is already gone", external_id
                    )
                    # Its session tag went with it, so the trial's shield is
                    # only reachable through the orphan sweep.
                    await _sweep_orphaned_shields(client)
                    return True
                session = _session_tag(instance)
                if session is None:
                    await handle.delete(
                        missing_ok=True, retry_busy=_DELETE_BUSY_RETRY_SEC
                    )
                    deleted = [external_id]
                else:
                    gone = await client.instances.each(tags=[session]).delete(
                        missing_ok=True, retry_busy=_DELETE_BUSY_RETRY_SEC
                    )
                    deleted = [item.uuid for item in gone if item.uuid]
        except Exception:
            logger.warning(
                "UnikraftBackend.teardown: failed to delete %s",
                external_id,
                exc_info=True,
            )
            return False
        logger.info(
            "UnikraftBackend.teardown: deleted %s", ", ".join(deleted) or "nothing"
        )
        return True

    @contextlib.contextmanager
    def capture_diagnostics(self, job_dir: Path) -> Iterator[Path | None]:
        # Harbor saves the instance console and stop reason to the trial itself.
        yield None


_: ExecutionBackend = UnikraftBackend()  # structural conformance check at import
