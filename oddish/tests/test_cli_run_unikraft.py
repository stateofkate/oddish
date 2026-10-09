from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harbor.models.environment_type import EnvironmentType

run_module = importlib.import_module("oddish.cli.run")

UNIKRAFT = getattr(EnvironmentType, "UNIKRAFT", None)
requires_unikraft = pytest.mark.skipif(
    UNIKRAFT is None, reason="the pinned Harbor has no unikraft environment"
)


def test_hosted_passthrough_adds_unikraft_when_harbor_exposes_it() -> None:
    class ForkEnvironmentType:
        MODAL = object()
        DAYTONA = object()
        EC2 = object()
        GKE = object()
        UNIKRAFT = object()

    environments = run_module._hosted_passthrough_environments(ForkEnvironmentType)
    assert ForkEnvironmentType.UNIKRAFT in environments


@requires_unikraft
def test_unikraft_is_hosted_passthrough() -> None:
    assert UNIKRAFT in run_module._HOSTED_PASSTHROUGH_ENVIRONMENTS


@requires_unikraft
def test_hosted_normalization_preserves_explicit_unikraft() -> None:
    assert (
        run_module._normalize_hosted_environment(UNIKRAFT, is_modal_api=True)
        is UNIKRAFT
    )
