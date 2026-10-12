"""The CLI-side Unikraft image prebuild: name parity with Harbor, the
exists-then-build flow, and the hosted passthrough."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import typer
from harbor.environments.definition import SNAPSHOT_HASH_LEN, environment_content_hash

from oddish.cli import unikraft_prebuild as prebuild


def _write_task(
    tmp_path: Path, *, name: str = "acme/hello-world", docker_image: str | None = None
) -> Path:
    task = tmp_path / "task"
    (task / "environment").mkdir(parents=True)
    env_lines = [f'docker_image = "{docker_image}"'] if docker_image else []
    (task / "task.toml").write_text(
        "[task]\n" f'name = "{name}"\n' "[environment]\n" + "\n".join(env_lines) + "\n"
    )
    if not docker_image:
        (task / "environment" / "Dockerfile").write_text(
            "FROM alpine:3.20\nRUN echo hi\n"
        )
    return task


def test_image_ref_is_namespace_and_content_hash(tmp_path: Path):
    task = _write_task(tmp_path)
    ref = prebuild.task_image_ref(task, namespace="acme")
    expected_tag = environment_content_hash(task / "environment", docker_image=None)[
        :SNAPSHOT_HASH_LEN
    ]
    assert ref == f"acme/harbor-task:{expected_tag}"
    # The tag follows the content: touching the Dockerfile changes it.
    (task / "environment" / "Dockerfile").write_text(
        "FROM alpine:3.20\nRUN echo changed\n"
    )
    assert prebuild.task_image_ref(task, namespace="acme") != ref


def test_image_ref_is_independent_of_the_task_directory_name(tmp_path: Path):
    # The ref is content-addressed, so the same environment under two directory
    # names yields one ref: the local prebuild matches the hosted worker lookup.
    def _make(dir_name: str) -> Path:
        task = tmp_path / dir_name
        (task / "environment").mkdir(parents=True)
        (task / "task.toml").write_text("[environment]\n")
        (task / "environment" / "Dockerfile").write_text("FROM alpine:3.20\n")
        return task

    local = _make("files")
    hosted = _make("task-files-bb70c114")
    assert prebuild.task_image_ref(local, namespace="acme") == prebuild.task_image_ref(
        hosted, namespace="acme"
    )


def test_image_ref_matches_harbor_unikraft_environment(tmp_path: Path):
    # Parity check against Harbor's own derivation, on pins that carry the
    # environment. Built via __new__ so the check needs neither the SDK nor
    # a metro, only the naming code.
    unikraft = pytest.importorskip("harbor.environments.unikraft")
    from harbor.models.task.config import EnvironmentConfig

    task = _write_task(tmp_path)
    env = object.__new__(unikraft.UnikraftEnvironment)
    env.environment_dir = task / "environment"
    # A differing environment_name proves the ref no longer depends on the name.
    env.environment_name = "a-different-name"
    env.task_env_config = EnvironmentConfig()
    env._image_namespace = "acme"
    env._registry = None
    env._compose_mode = False
    assert prebuild.task_image_ref(task, namespace="acme") == env._image_ref()


class _Runs:
    """Records subprocess.run calls; ``images get`` answers with ``exists``."""

    def __init__(self, *, exists: bool) -> None:
        self.exists, self.calls, self.dockerfiles = exists, [], []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[1] == "build":
            # The build dir can be temporary, so read it during the call.
            self.dockerfiles.append((Path(argv[2]) / "Dockerfile").read_text())
        rc = (
            0
            if (argv[1:3] == ["images", "get"] and self.exists)
            else (1 if argv[1:3] == ["images", "get"] else 0)
        )
        return subprocess.CompletedProcess(argv, rc, "", "")


def test_prebuild_builds_a_missing_image_from_the_dockerfile(
    tmp_path: Path, monkeypatch
):
    task = _write_task(tmp_path)
    monkeypatch.setenv("UKC_USER", "acme")
    monkeypatch.setenv(prebuild.UNIKRAFT_CLI_ENV, "/opt/unikraft")
    runs = _Runs(exists=False)
    monkeypatch.setattr(prebuild.subprocess, "run", runs)
    prebuild.prebuild_task_images([task], quiet=True)
    ref = prebuild.task_image_ref(task, namespace="acme")
    assert ["/opt/unikraft", "images", "get", ref] in runs.calls
    # Same argv Harbor's _run_build uses: the Dockerfile's directory is the
    # build input. The task image builds after the fixed analysis set.
    assert [c for c in runs.calls if c[1] == "build"][-1] == [
        "/opt/unikraft",
        "build",
        str(task / "environment"),
        "--arch",
        "x86_64",
        "-o",
        ref,
    ]
    assert runs.dockerfiles[-1] == (task / "environment" / "Dockerfile").read_text()


def test_prebuild_skips_existing_images_and_checks_the_analysis_set(
    tmp_path: Path, monkeypatch
):
    task = _write_task(tmp_path)
    monkeypatch.setenv("UKC_USER", "acme")
    monkeypatch.setenv(prebuild.UNIKRAFT_CLI_ENV, "/opt/unikraft")
    runs = _Runs(exists=True)
    monkeypatch.setattr(prebuild.subprocess, "run", runs)
    prebuild.prebuild_task_images([task], quiet=True)
    # Nothing builds; every submission also keeps the analysis images pushed.
    assert [c[1] for c in runs.calls] == ["images"] * 3
    assert {c[3] for c in runs.calls} == {
        prebuild.task_image_ref(task, namespace="acme"),
        *prebuild.analysis_image_refs("acme"),
    }


def test_prebuild_wraps_a_prebuilt_docker_image_in_a_dockerfile(
    tmp_path: Path, monkeypatch
):
    task = _write_task(tmp_path, docker_image="python:3.12-slim")
    monkeypatch.setenv("UKC_USER", "acme")
    monkeypatch.setenv(prebuild.UNIKRAFT_CLI_ENV, "/opt/unikraft")
    runs = _Runs(exists=False)
    monkeypatch.setattr(prebuild.subprocess, "run", runs)
    prebuild.prebuild_task_images([task], quiet=True)
    # The prebuilt image is imported through a one-line Dockerfile, exactly as
    # Harbor does. The task image builds after the fixed analysis set.
    assert runs.dockerfiles[-1] == "FROM python:3.12-slim\n"


def test_prebuild_requires_ukc_user_even_with_a_cli_profile(
    tmp_path: Path, monkeypatch
):
    # The worker reads its namespace only from its own UKC_USER, so a name
    # taken from the local CLI profile could push where no worker looks.
    config = tmp_path / "config.yaml"
    config.write_text(
        "profile: test\nprofiles:\n  test:\n    token: t\n    organization: acme\n"
    )
    monkeypatch.setenv("UNIKRAFT_CONFIG", str(config))
    monkeypatch.delenv("UKC_USER", raising=False)
    monkeypatch.delenv("UNIKRAFT_PROFILE", raising=False)
    monkeypatch.setenv(prebuild.UNIKRAFT_CLI_ENV, "/opt/unikraft")
    task = _write_task(tmp_path)
    with pytest.raises(typer.Exit):
        prebuild.prebuild_task_images([task], quiet=True)


def test_prebuild_requires_namespace_and_cli(tmp_path: Path, monkeypatch):
    task = _write_task(tmp_path)
    monkeypatch.delenv("UKC_USER", raising=False)
    with pytest.raises(typer.Exit):
        prebuild.prebuild_task_images([task], quiet=True)
    monkeypatch.setenv("UKC_USER", "acme")
    monkeypatch.delenv(prebuild.UNIKRAFT_CLI_ENV, raising=False)
    monkeypatch.setattr(prebuild.shutil, "which", lambda _name: None)
    with pytest.raises(typer.Exit):
        prebuild.prebuild_task_images([task], quiet=True)


def test_prebuild_fails_loudly_when_the_build_fails(tmp_path: Path, monkeypatch):
    task = _write_task(tmp_path)
    monkeypatch.setenv("UKC_USER", "acme")
    monkeypatch.setenv(prebuild.UNIKRAFT_CLI_ENV, "/opt/unikraft")

    def failing(argv, **kwargs):
        rc = 1 if argv[1] in ("images", "build") else 0
        return subprocess.CompletedProcess(argv, rc, "", "buildkit not found")

    monkeypatch.setattr(prebuild.subprocess, "run", failing)
    with pytest.raises(typer.Exit):
        prebuild.prebuild_task_images([task], quiet=True)


def test_force_build_rebuilds_an_existing_image(tmp_path: Path, monkeypatch):
    # The hosted worker never builds, so this is the only path that can
    # refresh a pushed image. The content-addressed ref stays the same.
    task = _write_task(tmp_path)
    monkeypatch.setenv("UKC_USER", "acme")
    monkeypatch.setenv(prebuild.UNIKRAFT_CLI_ENV, "/opt/unikraft")
    runs = _Runs(exists=True)
    monkeypatch.setattr(prebuild.subprocess, "run", runs)
    prebuild.prebuild_task_images([task], quiet=True, force_build=True)
    ref = prebuild.task_image_ref(task, namespace="acme")
    assert [c for c in runs.calls if c[1] == "build" and c[-1] == ref] == [
        [
            "/opt/unikraft",
            "build",
            str(task / "environment"),
            "--arch",
            "x86_64",
            "-o",
            ref,
        ]
    ]


def test_force_build_prefers_the_dockerfile_over_a_prebuilt_image(
    tmp_path: Path, monkeypatch
):
    task = _write_task(tmp_path, docker_image="python:3.12-slim")
    dockerfile = "FROM python:3.12-slim\nRUN echo hi\n"
    (task / "environment" / "Dockerfile").write_text(dockerfile)
    monkeypatch.setenv("UKC_USER", "acme")
    monkeypatch.setenv(prebuild.UNIKRAFT_CLI_ENV, "/opt/unikraft")
    runs = _Runs(exists=False)
    monkeypatch.setattr(prebuild.subprocess, "run", runs)
    prebuild.prebuild_task_images([task], quiet=True, force_build=True)
    # Forced: the task's own Dockerfile builds, not the one-line import.
    assert runs.dockerfiles[-1] == dockerfile


def test_analysis_image_refs_match_the_worker_overlay(tmp_path: Path):
    # Parity check: the refs hash the same environment/ directory the worker
    # stages with apply_analysis_overlay, for both Dockerfile variants.
    from oddish.worker.probe_staging import ANALYSIS_DOCKERFILES, apply_analysis_overlay

    refs = prebuild.analysis_image_refs("acme")
    assert len(refs) == len(ANALYSIS_DOCKERFILES) == 2
    for needs_query_cli in (True, False):
        work = tmp_path / f"overlay-{needs_query_cli}"
        work.mkdir()
        apply_analysis_overlay(
            work,
            brief="brief",
            artifact="qa_result.json",
            check_payload={},
            needs_query_cli=needs_query_cli,
        )
        tag = environment_content_hash(work / "environment", docker_image=None)[
            :SNAPSHOT_HASH_LEN
        ]
        assert f"acme/harbor-task:{tag}" in refs


def test_prebuild_builds_missing_analysis_images(monkeypatch):
    from oddish.worker.probe_staging import ANALYSIS_DOCKERFILES

    monkeypatch.setenv("UKC_USER", "acme")
    monkeypatch.setenv(prebuild.UNIKRAFT_CLI_ENV, "/opt/unikraft")
    runs = _Runs(exists=False)
    monkeypatch.setattr(prebuild.subprocess, "run", runs)
    # No task paths: only the fixed analysis set is checked and pushed.
    prebuild.prebuild_task_images([], quiet=True)
    assert sorted(runs.dockerfiles) == sorted(ANALYSIS_DOCKERFILES)
    output_refs = {c[-1] for c in runs.calls if c[1] == "build"}
    assert output_refs == set(prebuild.analysis_image_refs("acme"))


def _write_compose_task(tmp_path: Path, name: str = "compose") -> Path:
    task = _write_task(tmp_path / name)
    (task / "environment" / "docker-compose.yaml").write_text(
        "services:\n  web:\n    image: nginx:1.27-alpine\n"
    )
    return task


def test_dind_image_ref_matches_harbor_unikraft_environment():
    unikraft = pytest.importorskip("harbor.environments.unikraft")
    if not hasattr(unikraft, "dind_image_tag"):
        pytest.skip("the pinned Harbor runs no Compose tasks on Unikraft")
    ref, dockerfile_text = prebuild.dind_image_ref("acme")
    assert prebuild.DEFAULT_DIND_IMAGE == unikraft.DEFAULT_DIND_IMAGE
    assert dockerfile_text == unikraft.dind_dockerfile(unikraft.DEFAULT_DIND_IMAGE)
    tag = unikraft.dind_image_tag(unikraft.DEFAULT_DIND_IMAGE)
    assert ref == f"acme/{unikraft.DIND_IMAGE_NAME}:{tag}"


def test_prebuild_pushes_the_dind_image_instead_of_a_compose_task_image(
    tmp_path: Path, monkeypatch
):
    compose = _write_compose_task(tmp_path)
    monkeypatch.setenv("UKC_USER", "acme")
    monkeypatch.setenv(prebuild.UNIKRAFT_CLI_ENV, "/opt/unikraft")
    runs = _Runs(exists=False)
    monkeypatch.setattr(prebuild.subprocess, "run", runs)
    prebuild.prebuild_task_images([compose], quiet=True)

    dind_ref, dind_dockerfile = prebuild.dind_image_ref("acme")
    built = {c[-1] for c in runs.calls if c[1] == "build"}
    assert built == {*prebuild.analysis_image_refs("acme"), dind_ref}
    assert dind_dockerfile in runs.dockerfiles
    # Harbor builds the Compose services in the VM, so no task image is pushed.
    assert prebuild.task_image_ref(compose, namespace="acme") not in built


def test_prebuild_checks_the_dind_image_once_for_many_compose_tasks(
    tmp_path: Path, monkeypatch
):
    tasks = [_write_compose_task(tmp_path, f"compose-{i}") for i in range(2)]
    tasks.append(_write_task(tmp_path / "single"))
    monkeypatch.setenv("UKC_USER", "acme")
    monkeypatch.setenv(prebuild.UNIKRAFT_CLI_ENV, "/opt/unikraft")
    runs = _Runs(exists=True)
    monkeypatch.setattr(prebuild.subprocess, "run", runs)
    prebuild.prebuild_task_images(tasks, quiet=True)

    checked = [c[3] for c in runs.calls if c[1:3] == ["images", "get"]]
    assert checked.count(prebuild.dind_image_ref("acme")[0]) == 1
    assert prebuild.task_image_ref(tasks[2], namespace="acme") in checked
    assert not [c for c in runs.calls if c[1] == "build"]


def _write_separate_verifier(task: Path, *, compose_env: bool = False) -> Path:
    """Add a separate-verifier environment (its own tests/Dockerfile) to a task."""
    toml = task / "task.toml"
    toml.write_text(toml.read_text() + '\n[verifier]\nenvironment_mode = "separate"\n')
    tests = task / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "Dockerfile").write_text("FROM alpine:3.20\nRUN echo verifier\n")
    (tests / "test.sh").write_text("#!/bin/sh\necho 1 > /logs/verifier/reward.txt\n")
    if compose_env:
        (task / "environment" / "docker-compose.yaml").write_text(
            "services:\n  web:\n    image: nginx:1.27-alpine\n"
        )
    return task


def test_prebuild_pushes_the_separate_verifier_image(tmp_path: Path, monkeypatch):
    task = _write_separate_verifier(_write_task(tmp_path))
    monkeypatch.setenv("UKC_USER", "acme")
    monkeypatch.setenv(prebuild.UNIKRAFT_CLI_ENV, "/opt/unikraft")
    runs = _Runs(exists=False)
    monkeypatch.setattr(prebuild.subprocess, "run", runs)
    prebuild.prebuild_task_images([task], quiet=True)

    verifier_ref = prebuild._env_image_ref(task / "tests", None, "acme")
    built = {c[-1] for c in runs.calls if c[1] == "build"}
    # Both the agent environment and the separate verifier environment are pushed.
    assert prebuild.task_image_ref(task, namespace="acme") in built
    assert verifier_ref in built
    # The verifier ref hashes the tests/ directory, as Harbor derives it.
    from harbor.environments.definition import (
        SNAPSHOT_HASH_LEN,
        environment_content_hash,
    )

    tag = environment_content_hash(task / "tests", docker_image=None)[
        :SNAPSHOT_HASH_LEN
    ]
    assert verifier_ref == f"acme/harbor-task:{tag}"


def test_shared_verifier_task_pushes_no_verifier_image(tmp_path: Path, monkeypatch):
    # A plain task (no [verifier] environment) runs its verifier in the agent
    # environment, so only the one environment image is pushed.
    task = _write_task(tmp_path)
    monkeypatch.setenv("UKC_USER", "acme")
    monkeypatch.setenv(prebuild.UNIKRAFT_CLI_ENV, "/opt/unikraft")
    runs = _Runs(exists=False)
    monkeypatch.setattr(prebuild.subprocess, "run", runs)
    prebuild.prebuild_task_images([task], quiet=True)

    built = {c[-1] for c in runs.calls if c[1] == "build"}
    env_ref = prebuild.task_image_ref(task, namespace="acme")
    assert built == {*prebuild.analysis_image_refs("acme"), env_ref}


def test_compose_task_with_separate_verifier_pushes_dind_and_verifier(
    tmp_path: Path, monkeypatch
):
    task = _write_separate_verifier(_write_task(tmp_path), compose_env=True)
    monkeypatch.setenv("UKC_USER", "acme")
    monkeypatch.setenv(prebuild.UNIKRAFT_CLI_ENV, "/opt/unikraft")
    runs = _Runs(exists=False)
    monkeypatch.setattr(prebuild.subprocess, "run", runs)
    prebuild.prebuild_task_images([task], quiet=True)

    built = {c[-1] for c in runs.calls if c[1] == "build"}
    dind_ref, _ = prebuild.dind_image_ref("acme")
    verifier_ref = prebuild._env_image_ref(task / "tests", None, "acme")
    # The compose agent env builds in the VM (DinD), but the single-container
    # separate verifier still needs its own prebuilt image.
    assert dind_ref in built
    assert verifier_ref in built
    assert prebuild.task_image_ref(task, namespace="acme") not in built


def test_steps_sharing_tests_dir_push_one_verifier_image_per_base_image(
    tmp_path: Path, monkeypatch
):
    # Both steps read tests/, but their verifier base images differ, so the
    # content hashes differ too and each one needs its own image.
    task = _write_task(tmp_path)
    toml = task / "task.toml"
    toml.write_text(
        toml.read_text()
        + '\n[verifier]\nenvironment_mode = "separate"\n'
        + "".join(
            f'\n[[steps]]\nname = "{name}"\n'
            f'[steps.verifier.environment]\ndocker_image = "{image}"\n'
            for name, image in (("one", "alpine:3.20"), ("two", "alpine:3.21"))
        )
    )
    (task / "tests").mkdir()
    (task / "tests" / "test.sh").write_text("#!/bin/sh\n")
    monkeypatch.setenv("UKC_USER", "acme")
    monkeypatch.setenv(prebuild.UNIKRAFT_CLI_ENV, "/opt/unikraft")
    runs = _Runs(exists=False)
    monkeypatch.setattr(prebuild.subprocess, "run", runs)
    prebuild.prebuild_task_images([task], quiet=True)

    built = {c[-1] for c in runs.calls if c[1] == "build"}
    for image in ("alpine:3.20", "alpine:3.21"):
        assert prebuild._env_image_ref(task / "tests", image, "acme") in built
        assert f"FROM {image}\n" in runs.dockerfiles
