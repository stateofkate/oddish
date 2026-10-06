from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harbor.models.environment_type import EnvironmentType  # noqa: E402
from oddish.cli import app  # noqa: E402


run_module = importlib.import_module("oddish.cli.run")


def test_ec2_is_a_hosted_passthrough_environment() -> None:
    assert EnvironmentType.EC2 in run_module._HOSTED_PASSTHROUGH_ENVIRONMENTS


def test_run_help_lists_explicit_providers_and_keeps_modal_as_cpu_default() -> None:
    result = CliRunner().invoke(app, ["run", "--help"], env={"COLUMNS": "240"})

    assert result.exit_code == 0
    output = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
    output = re.sub(r"\s+", " ", output)
    assert "ec2, e2b, modal" in output
    assert "modal, archil, runloop" in output
    assert "Defaults: modal" in output
    assert "for CPU-only" in output


def _task(tmp_path: Path, toml: str = "", chart_dir: str | None = "chart") -> Path:
    (tmp_path / "task.toml").write_text(toml)
    if chart_dir is not None:
        (tmp_path / "environment" / chart_dir).mkdir(parents=True)
        (tmp_path / "environment" / chart_dir / "Chart.yaml").write_text("name: t\n")
    return tmp_path


def _route(task_path: Path, environment: EnvironmentType | None = None):
    return run_module._hosted_task_routing(
        environment, task_path, override_gpus=None, is_modal_api=True
    )[0]


def test_helm_chart_task_routes_to_ec2(tmp_path: Path) -> None:
    assert _route(_task(tmp_path)) == EnvironmentType.EC2


def test_helm_chart_honors_chart_path_kwarg(tmp_path: Path) -> None:
    task = _task(
        tmp_path, '[environment.kwargs]\nchart_path = "deploy/helm"\n', "deploy/helm"
    )
    assert _route(task) == EnvironmentType.EC2


def test_task_without_chart_keeps_hosted_default(tmp_path: Path) -> None:
    assert _route(_task(tmp_path, chart_dir=None)) is None


def test_explicit_env_beats_helm_chart(tmp_path: Path) -> None:
    assert _route(_task(tmp_path), EnvironmentType.MODAL) == EnvironmentType.MODAL


def test_gpu_helm_task_keeps_gpu_routing(tmp_path: Path) -> None:
    assert _route(_task(tmp_path, "[environment]\ngpus = 1\n")) is None
