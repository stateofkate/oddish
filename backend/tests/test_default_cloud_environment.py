"""The API's default-environment choice must mirror the CLI's TPU routing for
everything expressible in the request body: a submission that requests a TPU
(override_tpu) can only be served by GKE, so it must default there rather than
to the GPU/CPU cloud default.
"""

from __future__ import annotations

import pytest

import cloud_policy
from cloud_policy import get_default_cloud_environment
from harbor.models.environment_type import EnvironmentType
from oddish.core.sweeps import build_trial_specs_from_sweep
from oddish.schemas import AgentModelPair, HarborConfig, TaskSweepSubmission


def _submission(**environment_fields) -> TaskSweepSubmission:
    return TaskSweepSubmission(
        task_id="t1",
        configs=[AgentModelPair(agent="oracle")],
        harbor=HarborConfig.model_validate({"environment": environment_fields}),
    )


def test_override_tpu_defaults_to_gke() -> None:
    submission = _submission(override_tpu={"type": "v5e", "topology": "2x2"})
    assert (
        get_default_cloud_environment(submission, request_hash="0" * 64)
        == EnvironmentType.GKE
    )


def test_override_gpus_does_not_default_to_gke() -> None:
    submission = _submission(override_gpus=1)
    assert (
        get_default_cloud_environment(submission, request_hash="0" * 64)
        != EnvironmentType.GKE
    )


def test_plain_submission_does_not_default_to_gke() -> None:
    assert (
        get_default_cloud_environment(_submission(), request_hash="0" * 64)
        != EnvironmentType.GKE
    )


def test_no_submission_does_not_default_to_gke() -> None:
    assert (
        get_default_cloud_environment(None, request_hash="0" * 64)
        != EnvironmentType.GKE
    )


def test_selected_default_reaches_every_unset_trial_in_the_sweep() -> None:
    submission = TaskSweepSubmission(
        task_id="t1",
        configs=[
            AgentModelPair(agent="oracle", n_trials=2),
            AgentModelPair(
                agent="claude-code",
                model="anthropic/claude-sonnet-4-6",
                n_trials=3,
            ),
        ],
    )
    selected = get_default_cloud_environment(submission, request_hash="0" * 64)

    specs = build_trial_specs_from_sweep(
        submission,
        default_environment=selected,
        allowed_environments=cloud_policy.ALLOWED_CLOUD_ENVIRONMENTS,
    )

    assert len(specs) == 5
    assert {spec.environment for spec in specs} == {EnvironmentType.MODAL}


def test_explicit_archil_wins_over_selected_modal_default() -> None:
    submission = TaskSweepSubmission(
        task_id="t1",
        configs=[
            AgentModelPair(
                agent="oracle",
                n_trials=1,
                environment=EnvironmentType.ARCHIL,
            )
        ],
    )
    selected = get_default_cloud_environment(submission, request_hash="0" * 64)

    specs = build_trial_specs_from_sweep(
        submission,
        default_environment=selected,
        allowed_environments=cloud_policy.ALLOWED_CLOUD_ENVIRONMENTS,
    )

    assert specs[0].environment == EnvironmentType.ARCHIL


def test_task_declared_gpu_need_is_negotiated_like_an_override(monkeypatch) -> None:
    requirements = []

    def select(**kwargs):
        requirements.append(kwargs["requires_gpu"])
        return EnvironmentType.MODAL

    monkeypatch.setattr(cloud_policy, "default_cloud_environment", select)
    flagged = TaskSweepSubmission(
        task_id="t1", configs=[AgentModelPair(agent="oracle")], requires_gpu=True
    )
    get_default_cloud_environment(flagged, request_hash="0" * 64)
    get_default_cloud_environment(_submission(override_gpus=1), request_hash="0" * 64)
    get_default_cloud_environment(_submission(), request_hash="0" * 64)
    assert requirements == [True, True, False]


@pytest.mark.parametrize("percent", [0, 20, 40, 100])
def test_archil_rollout_selects_exactly_the_configured_request_groups(
    monkeypatch, percent
):
    monkeypatch.setattr(cloud_policy.settings, "archil_traffic_percent", percent)
    submission = _submission()
    selected = [
        get_default_cloud_environment(submission, request_hash=f"{bucket:064x}")
        for bucket in range(100)
    ]
    assert selected == [EnvironmentType.ARCHIL] * percent + [EnvironmentType.MODAL] * (
        100 - percent
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"requires_gpu": True},
        {"harbor": {"environment": {"override_gpus": 1}}},
        {
            "registry_auth": [
                {"registry": "ghcr.io", "username": "user", "token": "test"}
            ]
        },
    ],
)
def test_archil_rollout_never_overrides_gpu_or_private_registry_modal_route(
    monkeypatch, fields
):
    monkeypatch.setattr(cloud_policy.settings, "archil_traffic_percent", 100)
    monkeypatch.setattr(
        cloud_policy,
        "default_cloud_environment",
        lambda **kwargs: EnvironmentType.MODAL,
    )
    submission = TaskSweepSubmission.model_validate(
        {
            "task_id": "t1",
            "configs": [{"agent": "oracle"}],
            **fields,
        }
    )
    assert (
        get_default_cloud_environment(submission, request_hash="0" * 64)
        == EnvironmentType.MODAL
    )


def test_archil_rollout_preserves_tpu_route(monkeypatch):
    monkeypatch.setattr(cloud_policy.settings, "archil_traffic_percent", 100)
    submission = _submission(override_tpu={"type": "v5e", "topology": "2x2"})
    assert (
        get_default_cloud_environment(submission, request_hash="0" * 64)
        == EnvironmentType.GKE
    )


def test_archil_rollout_applies_to_unset_trials_and_preserves_explicit_modal(
    monkeypatch,
):
    monkeypatch.setattr(cloud_policy.settings, "archil_traffic_percent", 20)
    submission = TaskSweepSubmission(
        task_id="t1",
        configs=[
            AgentModelPair(agent="oracle", n_trials=2),
            AgentModelPair(agent="nop", environment=EnvironmentType.MODAL),
        ],
    )
    selected = get_default_cloud_environment(submission, request_hash="0" * 64)
    specs = build_trial_specs_from_sweep(
        submission,
        default_environment=selected,
        allowed_environments=cloud_policy.ALLOWED_CLOUD_ENVIRONMENTS,
    )
    assert [spec.environment for spec in specs] == [
        EnvironmentType.ARCHIL,
        EnvironmentType.ARCHIL,
        EnvironmentType.MODAL,
    ]
    submission.environment = EnvironmentType.MODAL
    specs = build_trial_specs_from_sweep(
        submission,
        default_environment=selected,
        allowed_environments=cloud_policy.ALLOWED_CLOUD_ENVIRONMENTS,
    )
    assert {spec.environment for spec in specs} == {EnvironmentType.MODAL}


@pytest.mark.parametrize("percent", [-1, 101])
def test_archil_rollout_rejects_out_of_range_percentages(percent):
    from oddish.config import Settings
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="archil_traffic_percent"):
        Settings(archil_traffic_percent=percent, _env_file=None)


def _routed_environments(submission, *, request_hash):
    cloud_policy.apply_archil_rollout_to_explicit_modal(
        submission, request_hash=request_hash
    )
    selected = get_default_cloud_environment(submission, request_hash=request_hash)
    return [
        spec.environment
        for spec in build_trial_specs_from_sweep(
            submission,
            default_environment=selected,
            # EC2 is registered only where its credentials are configured.
            allowed_environments={
                *cloud_policy.ALLOWED_CLOUD_ENVIRONMENTS,
                EnvironmentType.EC2,
            },
        )
    ]


@pytest.mark.parametrize("bucket, expected", [(0, "archil"), (99, "modal")])
def test_explicit_modal_cpu_sweep_joins_the_archil_rollout(
    monkeypatch, bucket, expected
):
    monkeypatch.setattr(cloud_policy.settings, "archil_traffic_percent", 40)
    submission = TaskSweepSubmission(
        task_id="t1",
        environment=EnvironmentType.MODAL,
        configs=[
            AgentModelPair(agent="oracle", n_trials=2),
            AgentModelPair(agent="nop", environment=EnvironmentType.MODAL),
        ],
    )
    assert (
        _routed_environments(submission, request_hash=f"{bucket:064x}")
        == [EnvironmentType(expected)] * 3
    )


@pytest.mark.parametrize("percent", [0, 40, 100])
def test_explicit_modal_rollout_selects_the_configured_request_groups(
    monkeypatch, percent
):
    monkeypatch.setattr(cloud_policy.settings, "archil_traffic_percent", percent)
    selected = [
        _routed_environments(
            TaskSweepSubmission(
                task_id="t1",
                environment=EnvironmentType.MODAL,
                configs=[AgentModelPair(agent="oracle")],
            ),
            request_hash=f"{bucket:064x}",
        )[0]
        for bucket in range(100)
    ]
    assert selected == [EnvironmentType.ARCHIL] * percent + [EnvironmentType.MODAL] * (
        100 - percent
    )


def test_explicit_ec2_is_never_moved_by_the_archil_rollout(monkeypatch):
    monkeypatch.setattr(cloud_policy.settings, "archil_traffic_percent", 100)
    submission = TaskSweepSubmission(
        task_id="t1",
        environment=EnvironmentType.EC2,
        configs=[
            AgentModelPair(agent="oracle"),
            AgentModelPair(agent="nop", environment=EnvironmentType.MODAL),
        ],
    )
    assert _routed_environments(submission, request_hash="0" * 64) == [
        EnvironmentType.EC2,
        EnvironmentType.MODAL,
    ]


def test_per_config_ec2_is_kept_when_modal_configs_join_the_rollout(monkeypatch):
    monkeypatch.setattr(cloud_policy.settings, "archil_traffic_percent", 100)
    submission = TaskSweepSubmission(
        task_id="t1",
        configs=[
            AgentModelPair(agent="oracle", environment=EnvironmentType.EC2),
            AgentModelPair(agent="nop", environment=EnvironmentType.MODAL),
        ],
    )
    assert _routed_environments(submission, request_hash="0" * 64) == [
        EnvironmentType.EC2,
        EnvironmentType.ARCHIL,
    ]


@pytest.mark.parametrize(
    "fields",
    [
        {"requires_gpu": True},
        {"harbor": {"environment": {"override_gpus": 1}}},
        {
            "registry_auth": [
                {"registry": "ghcr.io", "username": "user", "token": "test"}
            ]
        },
    ],
)
def test_explicit_modal_stays_where_archil_cannot_serve_the_run(monkeypatch, fields):
    monkeypatch.setattr(cloud_policy.settings, "archil_traffic_percent", 100)
    submission = TaskSweepSubmission.model_validate(
        {
            "task_id": "t1",
            "environment": "modal",
            "configs": [{"agent": "oracle", "environment": "modal"}],
            **fields,
        }
    )
    cloud_policy.apply_archil_rollout_to_explicit_modal(
        submission, request_hash="0" * 64
    )
    assert submission.environment == EnvironmentType.MODAL
    assert submission.configs[0].environment == EnvironmentType.MODAL


@pytest.mark.parametrize("bucket, expected", [(0, "archil"), (99, "modal")])
def test_explicit_modal_still_overrides_an_inherited_daytona_provider(
    monkeypatch, bucket, expected
):
    # Re-running a saved Daytona task with --env modal is the documented way
    # off Daytona; the rollout pick must stay explicit so it beats inheritance.
    from oddish.core.endpoints.sweep import _effective_sweep_environment

    monkeypatch.setattr(cloud_policy.settings, "archil_traffic_percent", 40)
    submission = TaskSweepSubmission(
        task_id="t1",
        environment=EnvironmentType.MODAL,
        configs=[AgentModelPair(agent="oracle")],
    )
    request_hash = f"{bucket:064x}"
    cloud_policy.apply_archil_rollout_to_explicit_modal(
        submission, request_hash=request_hash
    )
    assert _effective_sweep_environment(
        submission.environment,
        EnvironmentType.DAYTONA,
        get_default_cloud_environment(submission, request_hash=request_hash),
    ) == EnvironmentType(expected)


def test_unset_submission_environment_stays_unset_for_append_inheritance(
    monkeypatch,
):
    monkeypatch.setattr(cloud_policy.settings, "archil_traffic_percent", 100)
    submission = TaskSweepSubmission(
        task_id="t1",
        configs=[AgentModelPair(agent="nop", environment=EnvironmentType.MODAL)],
    )
    cloud_policy.apply_archil_rollout_to_explicit_modal(
        submission, request_hash="0" * 64
    )
    assert submission.environment is None
    assert submission.configs[0].environment == EnvironmentType.ARCHIL


def test_skip_archil_keeps_explicit_modal_on_modal(monkeypatch):
    monkeypatch.setattr(cloud_policy.settings, "archil_traffic_percent", 100)
    submission = TaskSweepSubmission(
        task_id="t1",
        environment=EnvironmentType.MODAL,
        skip_archil=True,
        configs=[AgentModelPair(agent="oracle")],
    )
    assert _routed_environments(submission, request_hash="0" * 64) == [
        EnvironmentType.MODAL
    ]
