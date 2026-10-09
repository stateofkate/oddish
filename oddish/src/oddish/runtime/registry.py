"""Provider lookup for launches, cleanup, and historical records.

Daytona stays registered for teardown only. Routing excludes it from launch
choices and selects Modal for CPU tasks, Thunder (when compatible) or Modal
for GPU tasks. Explicit Archil and other enabled providers remain available.
"""

from __future__ import annotations

import logging

from oddish.config import settings
from oddish.core.harbor_source import UNIKRAFT_ENVIRONMENT
from oddish.runtime.backends.archil import ArchilBackend
from oddish.runtime.backends.daytona import DaytonaBackend
from oddish.runtime.backends.ec2 import Ec2Backend
from oddish.runtime.backends.gke import GkeBackend
from oddish.runtime.backends.modal import ModalBackend
from oddish.runtime.backends.numinous import NuminousBackend
from oddish.runtime.backends.thunder import ThunderBackend
from oddish.runtime.backends.unikraft import UnikraftBackend
from oddish.runtime.ports import ExecutionBackend

logger = logging.getLogger(__name__)

# Singleton instances; backends are stateless w.r.t. trial dispatch.
_MODAL = ModalBackend()
_DAYTONA = DaytonaBackend()
_ARCHIL = ArchilBackend()

REGISTERED_BACKENDS: dict[str, ExecutionBackend] = {}

# Numinous keeps its opt-in accelerator precedence; CPU selection uses Modal.
if settings.numinous_enabled:
    _NUMINOUS = NuminousBackend()
    REGISTERED_BACKENDS[_NUMINOUS.name] = _NUMINOUS

# Retained for cancellation and orphan cleanup; never a launch candidate.
REGISTERED_BACKENDS[_DAYTONA.name] = _DAYTONA

# Unikraft is an explicit choice: --env unikraft selects it, and routing
# never hands it a trial that did not ask for it.
if settings.unikraft_enabled:
    if UNIKRAFT_ENVIRONMENT is None:
        logger.warning(
            "ODDISH_UNIKRAFT_ENABLED is set but the pinned Harbor has no "
            "unikraft environment; the backend was not registered"
        )
    else:
        _UNIKRAFT = UnikraftBackend()
        REGISTERED_BACKENDS[_UNIKRAFT.name] = _UNIKRAFT

# Thunder precedes Modal for compatible GPU requests. CPU selection uses Modal.
if settings.thunder_enabled:
    _THUNDER = ThunderBackend()
    REGISTERED_BACKENDS[_THUNDER.name] = _THUNDER

if settings.ec2_enabled:
    _EC2 = Ec2Backend()
    REGISTERED_BACKENDS[_EC2.name] = _EC2

REGISTERED_BACKENDS[_MODAL.name] = _MODAL
REGISTERED_BACKENDS[_ARCHIL.name] = _ARCHIL

# GKE joins only when a cluster is configured, and always after the other
# backends so cheap-first negotiation never hands non-TPU work to it.
if settings.gke_cluster_name:
    _GKE = GkeBackend()
    REGISTERED_BACKENDS[_GKE.name] = _GKE


def get_backend(name: str | None) -> ExecutionBackend | None:
    """Resolve a backend by provider name (case-insensitive); None if unknown."""
    if not name:
        return None
    return REGISTERED_BACKENDS.get(name.lower())


def ordered_backends() -> list[ExecutionBackend]:
    """All registered backends in stable policy/display order."""
    return list(REGISTERED_BACKENDS.values())
