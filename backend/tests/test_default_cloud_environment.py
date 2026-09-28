"""The API's default-environment choice must mirror the CLI's TPU routing for
everything expressible in the request body: a submission that requests a TPU
(override_tpu) can only be served by GKE, so it must default there rather than
to the GPU/CPU cloud default.
"""

from __future__ import annotations

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
    assert get_default_cloud_environment(submission) == EnvironmentType.GKE


def test_override_gpus_does_not_default_to_gke() -> None:
    submission = _submission(override_gpus=1)
    assert get_default_cloud_environment(submission) != EnvironmentType.GKE


def test_plain_submission_does_not_default_to_gke() -> None:
    assert get_default_cloud_environment(_submission()) != EnvironmentType.GKE


def test_no_submission_does_not_default_to_gke() -> None:
    assert get_default_cloud_environment(None) != EnvironmentType.GKE


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
    selected = get_default_cloud_environment(submission)

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
    selected = get_default_cloud_environment(submission)

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
    get_default_cloud_environment(flagged)
    get_default_cloud_environment(_submission(override_gpus=1))
    get_default_cloud_environment(_submission())
    assert requirements == [True, True, False]
