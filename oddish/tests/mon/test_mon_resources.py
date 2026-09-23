"""On the monitor path a task's memory_mb does not cap the container, as on the
worker's Daytona path.

Regression: QA40 task 11 (4096 MB, Erlang compile) passed on the old Daytona
path and died on the new one. Harbor's Daytona environment wrote the task's
memory as a compose reservation (no cap); the Docker environment the monitor
runs writes it as a hard limit, and the compile thrashed at 4096 of 4096 MiB
for eleven minutes before the monitor was killed. Harbor's Docker environment
refuses ``request`` ("does not support memory resource requests"), so the
uncapped policy is ``ignore``.
"""

from __future__ import annotations

from oddish.mon.resources import with_reservation_semantics


def test_unset_memory_policy_becomes_ignore_and_cpu_is_untouched():
    out = with_reservation_semantics({"environment": {"kwargs": {}}})
    assert out["environment"]["memory_enforcement_policy"] == "ignore"
    assert "cpu_enforcement_policy" not in out["environment"]
    assert out["environment"]["kwargs"] == {}


def test_auto_memory_policy_becomes_ignore():
    out = with_reservation_semantics({"environment": {"memory_enforcement_policy": "AUTO"}})
    assert out["environment"]["memory_enforcement_policy"] == "ignore"


def test_explicit_policies_are_respected():
    out = with_reservation_semantics(
        {"environment": {"memory_enforcement_policy": "limit", "cpu_enforcement_policy": "ignore"}}
    )
    assert out["environment"]["memory_enforcement_policy"] == "limit"
    assert out["environment"]["cpu_enforcement_policy"] == "ignore"


def test_empty_config_and_input_untouched():
    src = {"docker_image": "img", "environment": {"override_memory_mb": 8192}}
    out = with_reservation_semantics(src)
    assert out["docker_image"] == "img"
    assert out["environment"]["override_memory_mb"] == 8192
    assert "memory_enforcement_policy" not in src["environment"]
    assert with_reservation_semantics(None)["environment"] == {"memory_enforcement_policy": "ignore"}


def test_harbor_docker_environment_accepts_the_policy():
    # The first proof of this fix pinned "request", which the pinned Harbor's
    # Docker environment refuses (capabilities: limits only).
    from harbor.environments.docker.docker import DockerEnvironment
    from harbor.environments.resource_policies import validate_resource_capabilities
    from harbor.models.trial.config import ResourceMode

    validate_resource_capabilities(
        environment_label="docker",
        resource_capabilities=DockerEnvironment.resource_capabilities(),
        cpu_enforcement_policy=ResourceMode.AUTO,
        memory_enforcement_policy=ResourceMode(with_reservation_semantics(None)["environment"]["memory_enforcement_policy"]),
    )


def test_runner_binds_the_module_it_calls():
    # The first deploy of this fix called resources.with_reservation_semantics
    # from runner.run() without importing it: NameError on every trial, and
    # no unit test exercised the call site.
    from oddish.mon import resources, runner

    assert runner.resources is resources
