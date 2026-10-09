from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from oddish.core.harbor_source import UNIKRAFT_ENVIRONMENT
from oddish.runtime.backends.unikraft import UnikraftBackend
from oddish.runtime.ports import ExecutionBackend

DELETE_OPTS = {"missing_ok": True, "retry_busy": 20.0}


def test_backend_name_matches_environment_value() -> None:
    # The registry resolves backends by EnvironmentType.value, so the two must
    # agree on pins that carry the environment.
    backend = UnikraftBackend()
    assert isinstance(backend, ExecutionBackend)
    assert backend.name == "unikraft"
    if UNIKRAFT_ENVIRONMENT is not None:
        assert backend.name == UNIKRAFT_ENVIRONMENT.value


def test_capabilities_shape() -> None:
    caps = UnikraftBackend().capabilities()
    assert caps.gpu is None
    assert caps.tpu is None
    assert caps.private_registry_pull is False
    # The network shield gives no-network and allowlist policies.
    assert caps.network_egress == "configurable"
    assert caps.persistent_volumes is True
    assert caps.streaming_logs is True
    assert caps.memory_snapshot_fork is True
    assert caps.cold_start == "instant"


def test_env_kwargs_ship_nothing_by_default(monkeypatch) -> None:
    from oddish.config import settings

    monkeypatch.setattr(
        settings, "unikraft_require_prebuilt_image", False, raising=False
    )
    monkeypatch.setattr(settings, "unikraft_default_memory_mb", None, raising=False)
    monkeypatch.delenv("UKC_USER", raising=False)
    # A job kwarg covers its task.toml counterpart, so an unconditional
    # require_prebuilt_image=False would silently override a task's own.
    assert UnikraftBackend().harbor_env_kwargs({"plugin_image": "x"}) == {
        "plugin_image": "x"
    }


def test_env_kwargs_ship_deployment_settings_and_caller_wins(monkeypatch) -> None:
    from oddish.config import settings

    monkeypatch.setattr(
        settings, "unikraft_require_prebuilt_image", True, raising=False
    )
    monkeypatch.setattr(settings, "unikraft_default_memory_mb", 8192, raising=False)
    kwargs = UnikraftBackend().harbor_env_kwargs({"metro": "other"})
    assert kwargs["require_prebuilt_image"] is True
    assert kwargs["default_memory_mb"] == 8192
    # Caller kwargs are spread last, matching the GKE and Daytona backends.
    assert kwargs["metro"] == "other"


def test_org_falls_back_to_the_cli_profile_organization(monkeypatch, tmp_path) -> None:
    from oddish.runtime.backends.unikraft import cli_profile_organization, unikraft_org

    config = tmp_path / "config.yaml"
    config.write_text(
        "profile: test\n"
        "profiles:\n"
        "  test:\n"
        "    type: cloud\n"
        "    token: t\n"
        "    organization: acme\n"
        "  bare:\n"
        "    type: cloud\n"
        "    token: t\n"
    )
    monkeypatch.delenv("UKC_USER", raising=False)
    monkeypatch.delenv("UNIKRAFT_PROFILE", raising=False)
    monkeypatch.setenv("UNIKRAFT_CONFIG", str(config))
    # The active profile's organization.
    assert cli_profile_organization() == "acme"
    assert unikraft_org() == "acme"
    # UNIKRAFT_PROFILE picks the profile; one without an organization gives none.
    monkeypatch.setenv("UNIKRAFT_PROFILE", "bare")
    assert unikraft_org() is None
    # UKC_USER still wins over the profile.
    monkeypatch.setenv("UNIKRAFT_PROFILE", "test")
    monkeypatch.setenv("UKC_USER", "other")
    assert unikraft_org() == "other"
    # No config at all is simply no organization, never an error.
    monkeypatch.setenv("UNIKRAFT_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.delenv("UKC_USER", raising=False)
    assert unikraft_org() is None


def test_pinned_harbor_takes_the_namespace_from_ukc_user() -> None:
    import inspect

    # A pinned Harbor without the environment leaves the backend dormant.
    unikraft = pytest.importorskip("harbor.environments.unikraft")

    # Harbor reads UKC_USER itself, for the task and the shield images alike.
    parameters = inspect.signature(unikraft.UnikraftEnvironment.__init__).parameters
    assert "image_namespace" in parameters
    assert "shield_org" not in parameters


class _FakeNotFound(Exception):
    pass


class _FakeSdk:
    """The instances a metro holds, by uuid, each with its tags; and what was asked."""

    def __init__(
        self,
        instances: dict[str, list[str]],
        meta: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.instances = instances
        self.meta = meta or {}
        self.log: list[tuple[Any, ...]] = []
        self.list_fails = False

    def record(self, uuid: str) -> SimpleNamespace:
        meta = self.meta.get(uuid, {})
        return SimpleNamespace(
            uuid=uuid,
            tags=self.instances[uuid],
            name=meta.get("name", uuid),
            created_at=meta.get("created_at", OLD_ENOUGH),
            state=meta.get("state", "running"),
        )


class _FakeHandle:
    def __init__(self, sdk: _FakeSdk, uuid: str) -> None:
        self._sdk, self._uuid = sdk, uuid

    def __await__(self):
        async def read() -> SimpleNamespace:
            if self._uuid not in self._sdk.instances:
                raise _FakeNotFound(self._uuid)
            return SimpleNamespace(
                uuid=self._uuid, tags=self._sdk.instances[self._uuid]
            )

        return read().__await__()

    async def delete(self, **opts: Any) -> SimpleNamespace:
        self._sdk.log.append(("delete", self._uuid, opts))
        return SimpleNamespace(uuid=self._uuid)


class _FakeSet:
    def __init__(self, sdk: _FakeSdk, tags: list[str]) -> None:
        self._sdk, self._tags = sdk, tags

    async def delete(self, **opts: Any) -> list[SimpleNamespace]:
        self._sdk.log.append(("delete_tagged", tuple(self._tags), opts))
        return [
            SimpleNamespace(uuid=uuid)
            for uuid, tags in self._sdk.instances.items()
            if all(tag in tags for tag in self._tags)
        ]


class _FakeInstances:
    def __init__(self, sdk: _FakeSdk) -> None:
        self._sdk = sdk

    def get(self, *, uuid: str | None = None, name: str | None = None) -> _FakeHandle:
        self._sdk.log.append(("get", uuid, name))
        return _FakeHandle(self._sdk, uuid or f"name:{name}")

    def each(self, *, tags: list[str] | None = None, **_: Any) -> _FakeSet:
        return _FakeSet(self._sdk, list(tags or []))

    def list(self, *, tags: list[str] | None = None, **_: Any):
        self._sdk.log.append(("list", tuple(tags or [])))
        sdk = self._sdk

        async def records():
            if sdk.list_fails:
                raise RuntimeError("listing down")
            for uuid, itags in sdk.instances.items():
                if tags and not all(tag in itags for tag in tags):
                    continue
                yield sdk.record(uuid)

        return records()


def _install_fake_sdk(
    monkeypatch,
    instances: dict[str, list[str]],
    *,
    fail: bool = False,
    meta: dict[str, dict[str, Any]] | None = None,
) -> _FakeSdk:
    sdk = _FakeSdk(instances, meta)

    class FakeClient:
        def __init__(self, *, metro: str) -> None:
            sdk.log.append(("metro", metro))
            self.instances = _FakeInstances(sdk)

        async def __aenter__(self):
            if fail:
                raise RuntimeError("boom")
            return self

        async def __aexit__(self, *exc) -> None:
            return None

    module = types.ModuleType("unikraft_cloud")
    module.UnikraftCloud = FakeClient
    module.NotFoundError = _FakeNotFound
    monkeypatch.setitem(sys.modules, "unikraft_cloud", module)
    return sdk


OLD_ENOUGH = datetime(2026, 1, 1, tzinfo=timezone.utc)

TASK = "a87b4c2e-d3bd-4cd3-bd55-b5cab3f3f718"
SHIELD = "0f3a5d1c-9d2e-4b6a-8c7f-1e2d3c4b5a69"
SESSION = "harbor.session=hello-world__bZZeEkw__env"


def test_teardown_deletes_every_instance_of_the_trial(monkeypatch) -> None:
    monkeypatch.setenv("UKC_METRO", "test")
    sdk = _install_fake_sdk(
        monkeypatch,
        {
            TASK: ["harbor", SESSION],
            SHIELD: ["harbor", SESSION],
            "other": ["harbor", "harbor.session=another"],
        },
    )
    assert asyncio.run(UnikraftBackend().teardown(TASK)) is True
    # The task instance is read for its session tag, then the tag names the set:
    # the shield goes with it, and nothing else does.
    assert ("get", TASK, None) in sdk.log
    assert ("delete_tagged", (SESSION,), DELETE_OPTS) in sdk.log
    assert not any(entry[0] == "delete" for entry in sdk.log)


def test_teardown_of_an_untagged_instance_deletes_it_alone(monkeypatch) -> None:
    monkeypatch.setenv("UKC_METRO", "test")
    sdk = _install_fake_sdk(monkeypatch, {TASK: ["harbor"]})
    assert asyncio.run(UnikraftBackend().teardown(TASK)) is True
    assert ("delete", TASK, DELETE_OPTS) in sdk.log
    assert not any(entry[0] == "delete_tagged" for entry in sdk.log)


def test_teardown_of_an_instance_already_gone_is_done(monkeypatch) -> None:
    monkeypatch.setenv("UKC_METRO", "test")
    sdk = _install_fake_sdk(monkeypatch, {})
    assert asyncio.run(UnikraftBackend().teardown(TASK)) is True
    assert not any(entry[0].startswith("delete") for entry in sdk.log)


def test_gone_instance_sweeps_an_orphaned_shield_in_any_state(monkeypatch) -> None:
    # The deleted task instance took the session tag with it, so only the
    # orphan sweep can still reach the shield -- running or stopped.
    monkeypatch.setenv("UKC_METRO", "test")
    sdk = _install_fake_sdk(
        monkeypatch,
        {SHIELD: ["harbor", SESSION]},
        meta={SHIELD: {"name": "hello-world-abc1234-shield", "state": "stopped"}},
    )
    assert asyncio.run(UnikraftBackend().teardown(TASK)) is True
    assert ("list", ("harbor",)) in sdk.log
    assert ("delete_tagged", (SESSION,), DELETE_OPTS) in sdk.log


def test_sweep_keeps_a_shield_inside_the_grace_period(monkeypatch) -> None:
    monkeypatch.setenv("UKC_METRO", "test")
    sdk = _install_fake_sdk(
        monkeypatch,
        {SHIELD: ["harbor", SESSION]},
        meta={
            SHIELD: {
                "name": "hello-world-abc1234-shield",
                "created_at": datetime.now(timezone.utc),
            }
        },
    )
    assert asyncio.run(UnikraftBackend().teardown(TASK)) is True
    assert not any(entry[0].startswith("delete") for entry in sdk.log)


def test_sweep_keeps_a_session_that_still_has_its_task_instance(monkeypatch) -> None:
    monkeypatch.setenv("UKC_METRO", "test")
    sdk = _install_fake_sdk(
        monkeypatch,
        {"other-task": ["harbor", SESSION], SHIELD: ["harbor", SESSION]},
        meta={SHIELD: {"name": "hello-world-abc1234-shield"}},
    )
    assert asyncio.run(UnikraftBackend().teardown(TASK)) is True
    assert not any(entry[0].startswith("delete") for entry in sdk.log)


def test_a_failed_sweep_still_reports_the_instance_gone(monkeypatch) -> None:
    monkeypatch.setenv("UKC_METRO", "test")
    sdk = _install_fake_sdk(monkeypatch, {})
    sdk.list_fails = True
    assert asyncio.run(UnikraftBackend().teardown(TASK)) is True


def test_teardown_never_raises(monkeypatch) -> None:
    monkeypatch.setenv("UKC_METRO", "test")
    _install_fake_sdk(monkeypatch, {}, fail=True)
    assert asyncio.run(UnikraftBackend().teardown(TASK)) is False


def test_teardown_without_metro_or_id_is_false(monkeypatch) -> None:
    monkeypatch.delenv("UKC_METRO", raising=False)
    assert asyncio.run(UnikraftBackend().teardown(TASK)) is False
    assert asyncio.run(UnikraftBackend().teardown("")) is False


def test_capture_diagnostics_is_a_noop(tmp_path: Path) -> None:
    with UnikraftBackend().capture_diagnostics(tmp_path) as log_path:
        assert log_path is None


@pytest.mark.skipif(
    UNIKRAFT_ENVIRONMENT is None, reason="pinned Harbor lacks the environment"
)
def test_environment_value_is_registry_key() -> None:
    assert UNIKRAFT_ENVIRONMENT.value == UnikraftBackend().name
