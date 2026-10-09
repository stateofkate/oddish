"""The QA/audit rerun sandbox gate: only registered providers are accepted,
mirroring sweep submit, so a bad request fails fast instead of at launch."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from harbor.models.environment_type import EnvironmentType

from oddish.core.endpoints.qa import require_allowed_analysis_environment

UNIKRAFT = getattr(EnvironmentType, "UNIKRAFT", None)


def test_none_keeps_the_worker_default():
    require_allowed_analysis_environment(None)


def test_modal_is_always_registered():
    require_allowed_analysis_environment("modal")


def test_an_unregistered_environment_is_rejected_with_400():
    # Unikraft is opt-in and the test settings never enable it, so the gate
    # must refuse it on this deployment (whatever the Harbor pin carries).
    with pytest.raises(HTTPException) as exc:
        require_allowed_analysis_environment("unikraft")
    assert exc.value.status_code == 400
    assert "unikraft" in exc.value.detail


def test_daytona_is_not_a_launch_choice():
    # Registered for cleanup only; routing excludes it, so the gate does too.
    with pytest.raises(HTTPException) as exc:
        require_allowed_analysis_environment("daytona")
    assert exc.value.status_code == 400


@pytest.mark.skipif(
    UNIKRAFT is None, reason="the pinned Harbor has no unikraft environment"
)
def test_a_registered_unikraft_backend_is_accepted(monkeypatch):
    from oddish.runtime import registry
    from oddish.runtime.backends.unikraft import UnikraftBackend

    monkeypatch.setitem(registry.REGISTERED_BACKENDS, "unikraft", UnikraftBackend())
    require_allowed_analysis_environment("unikraft")
