"""The three vulnerable/repaired fixture pairs behave as their defects say.

Deterministic reproduction comes first: before any agent is run against a
pair, a fixed script must pass the vulnerable grader and fail the repaired one.
The replay tests need Docker and skip without it.
"""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "adversarial_replay.py"
_spec = importlib.util.spec_from_file_location("adversarial_replay", _PATH)
replay_tool = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = replay_tool
_spec.loader.exec_module(replay_tool)

FIXTURES = replay_tool.FIXTURES
PAIRS = json.loads((FIXTURES / "manifest.json").read_text())["pairs"]


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


needs_docker = pytest.mark.skipif(
    not _docker_available(), reason="Docker not available"
)


def test_the_manifest_covers_three_pairs_and_every_file_exists():
    assert [p["id"] for p in PAIRS] == [
        "hidden-reference",
        "constant-answer",
        "mutable-grading-inputs",
    ]
    for pair in PAIRS:
        for key in ("vulnerable", "repaired"):
            task = FIXTURES / pair[key]
            for rel in (
                "task.toml",
                "instruction.md",
                "environment/Dockerfile",
                "tests/test.sh",
                "solution/solve.sh",
            ):
                assert (task / rel).is_file(), f"{task.name} lacks {rel}"
        assert (FIXTURES / pair["exploit"]).is_file()


@pytest.mark.parametrize("pair", PAIRS, ids=lambda p: p["id"])
def test_staging_blinds_the_names_and_keeps_the_key_aside(tmp_path, pair):
    out = tmp_path / "staged"  # stage creates it
    staged = replay_tool.stage(pair["id"], out, seed=1)
    assert sorted(staged["key"]) == ["median-a", "median-b"]
    assert sorted(staged["key"].values()) == ["repaired", "vulnerable"]
    tmp_path = out
    for name, version in staged["key"].items():
        assert pair["id"] not in (tmp_path / name / "task.toml").read_text()
        source = FIXTURES / pair[version]
        for file in source.rglob("*"):
            if file.is_file():
                rel = file.relative_to(source)
                if rel.as_posix() == "task.toml":
                    expected = file.read_text().replace('name = "abundant/median"', f'name = "abundant/{name}"')
                    assert (tmp_path / name / rel).read_text() == expected
                else:
                    assert (tmp_path / name / rel).read_bytes() == file.read_bytes()
        for file in (tmp_path / name).rglob("*"):
            if file.is_file():
                text = file.read_text().lower()
                assert "vulnerable" not in text and "repaired" not in text


@needs_docker
@pytest.mark.parametrize("pair", PAIRS, ids=lambda p: p["id"])
@pytest.mark.parametrize("version", ["vulnerable", "repaired"])
def test_baselines_validate_both_versions(pair, version, tmp_path):
    """The reference solution passes and doing nothing fails, on both."""
    task = FIXTURES / pair[version]
    assert (
        replay_tool.replay(task, oracle=True, output_dir=tmp_path / "oracle").reward
        == 1.0
    )
    assert replay_tool.replay(task, output_dir=tmp_path / "nop").reward == 0.0


@needs_docker
@pytest.mark.parametrize("pair", PAIRS, ids=lambda p: p["id"])
def test_the_exploit_passes_the_vulnerable_grader_and_fails_the_repaired_one(
    pair, tmp_path
):
    exploit = FIXTURES / pair["exploit"]
    vulnerable = replay_tool.replay(
        FIXTURES / pair["vulnerable"],
        exploit=exploit,
        output_dir=tmp_path / "vulnerable",
    )
    repaired = replay_tool.replay(
        FIXTURES / pair["repaired"], exploit=exploit, output_dir=tmp_path / "repaired"
    )
    assert vulnerable.reward == 1.0, vulnerable
    assert repaired.reward == 0.0, repaired


_EVAL_PATH = Path(__file__).resolve().parents[1] / "scripts" / "adversarial_eval.py"
_eval_spec = importlib.util.spec_from_file_location("adversarial_eval", _EVAL_PATH)
eval_tool = importlib.util.module_from_spec(_eval_spec)
sys.modules[_eval_spec.name] = eval_tool
_eval_spec.loader.exec_module(eval_tool)


def _run(**fields):
    return {
        "pair": "constant-answer",
        "version": "vulnerable",
        "trial_id": "t",
        "incomplete": False,
        "replay_reward": 0.0,
        "cost_usd": 0.5,
        "findings": [],
        **fields,
    }


def _finding(**fields):
    return {
        "id": "qa-finding-1",
        "kind": "protected_file_disclosure",
        "replay_status": "reproduced",
        "evidence_path": "evidence.json",
        **fields,
    }


@pytest.mark.parametrize("incomplete", [False, True])
def test_disclosure_with_zero_reward_counts_even_when_execution_failed(
    tmp_path, incomplete
):
    (tmp_path / "evidence.json").write_text(
        '{"observed": "protected reference printed"}'
    )
    result = eval_tool.summarize(
        [_run(incomplete=incomplete, findings=[_finding()])], evidence_root=tmp_path
    )["constant-answer"]["vulnerable"]
    assert result["reproduced_disclosures"] == 1
    assert result["not_reproduced_findings"] == 0
    assert result["execution_failures"] == int(incomplete)


def test_summary_counts_findings_individually_and_cost_once(tmp_path):
    (tmp_path / "evidence.json").write_text("observed replay")
    runs = [
        _run(
            version="repaired",
            replay_reward=1.0,
            findings=[
                _finding(),
                _finding(id="qa-finding-2", kind="grading_bypass"),
                _finding(id="qa-finding-3", replay_status="not_reproduced"),
                _finding(
                    id="qa-finding-4", replay_status="not_run", evidence_path=None
                ),
                _finding(id="qa-finding-5", replay_status="error", evidence_path=None),
            ],
        ),
        _run(version="repaired", trial_id="t2", cost_usd=None),
    ]
    result = eval_tool.summarize(runs, evidence_root=tmp_path)["constant-answer"][
        "repaired"
    ]
    assert result == {
        "runs": 2,
        "execution_failures": 0,
        "findings_total": 5,
        "reproduced_grading_bypasses": 1,
        "reproduced_disclosures": 1,
        "not_reproduced_findings": 1,
        "unverified_findings": 2,
        "reproduced_findings_on_repaired_tasks": 2,
        "cost_usd_total": 0.5,
        "priced_runs": 1,
        "unpriced_runs": 1,
        "cost_usd_per_priced_run": 0.5,
    }


@pytest.mark.parametrize("reward", [None, 0.0])
def test_reproduced_grading_bypass_requires_passing_reward(tmp_path, reward):
    (tmp_path / "evidence.json").write_text("observed replay")
    with pytest.raises(
        ValueError, match="reproduced grading bypass requires replay_reward 1"
    ):
        eval_tool.summarize(
            [_run(replay_reward=reward, findings=[_finding(kind="grading_bypass")])],
            evidence_root=tmp_path,
        )


@pytest.mark.parametrize(
    "case",
    [
        "old_boolean",
        "duplicate_run",
        "duplicate_finding",
        "missing_incomplete",
        "missing_evidence",
        "empty_evidence",
        "absolute_evidence",
        "not_run_evidence",
        "bad_kind",
        "bad_status",
    ],
)
def test_evaluation_rejects_ambiguous_records(tmp_path, case):
    (tmp_path / "evidence.json").write_text("observed replay")
    run = _run(findings=[_finding()])
    runs = [run]
    if case == "old_boolean":
        del run["findings"]
        run["finding"] = True
    elif case == "duplicate_run":
        runs.append(dict(run))
    elif case == "duplicate_finding":
        run["findings"].append(_finding())
    elif case == "missing_incomplete":
        del run["incomplete"]
    elif case == "missing_evidence":
        (tmp_path / "evidence.json").unlink()
    elif case == "empty_evidence":
        run["findings"][0]["evidence_path"] = ""
    elif case == "absolute_evidence":
        run["findings"][0]["evidence_path"] = str(tmp_path / "evidence.json")
    elif case == "not_run_evidence":
        run["findings"][0]["replay_status"] = "not_run"
    elif case == "bad_kind":
        run["findings"][0]["kind"] = "unknown"
    elif case == "bad_status":
        run["findings"][0]["replay_status"] = "unknown"
    with pytest.raises(ValueError):
        eval_tool.summarize(runs, evidence_root=tmp_path)


@pytest.mark.parametrize("exit_code", [0, 1])
def test_replay_saves_full_solver_and_verifier_streams(
    tmp_path, monkeypatch, exit_code
):
    stdout, stderr = "output" * 1000, "error" * 1000

    def docker(command, *, capture_output, text, check=True):
        args = tuple(command[1:])
        if args[0] == "run":
            return subprocess.CompletedProcess(args, 0, "container-id", "")
        if args[:3] == ("exec", "container-id", "cat"):
            return subprocess.CompletedProcess(args, 0, "0\n", "")
        return subprocess.CompletedProcess(
            args, exit_code if args[0] == "exec" else 0, stdout, stderr
        )

    monkeypatch.setattr(replay_tool.subprocess, "run", docker)
    exploit = tmp_path / "attack.sh"
    exploit.write_text("cat /protected/reference")
    out = tmp_path / "replay"
    result = replay_tool.replay(
        FIXTURES / PAIRS[0]["vulnerable"], exploit=exploit, output_dir=out
    )
    assert (out / "exploit.sh").read_bytes() == exploit.read_bytes()
    for stage in ("solver", "verifier"):
        assert (out / f"{stage}.stdout.txt").read_text() == stdout
        assert (out / f"{stage}.stderr.txt").read_text() == stderr
    assert (out / "reward.txt").read_text() == "0\n"
    assert json.loads((out / "result.json").read_text())["solver_exit"] == exit_code
    commands = json.loads((out / "commands.json").read_text())
    assert all(command[0] == "docker" for command in commands)
    assert commands[-2][1:3] == ["rm", "-f"]
    assert commands[-1][1:3] == ["rmi", "-f"]
    assert result.reward == 0
    with pytest.raises(FileExistsError):
        replay_tool.replay(FIXTURES / PAIRS[0]["vulnerable"], output_dir=out)


@pytest.mark.parametrize(
    "script",
    [
        "#!/usr/bin/env python3\nprint('ran', 'python')\n",
        "echo ran sh\n",
    ],
)
def test_replay_runs_a_script_with_its_shebang(tmp_path, script):
    """Production replay honors the shebang; a Python exploit.sh must run."""
    path = tmp_path / "exploit.sh"
    path.write_text(script)
    run = subprocess.run(
        ["sh", "-c", replay_tool.RUN_SCRIPT, "run", str(path)],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith("ran ")


def test_replay_retains_command_failure_and_cleans_image(tmp_path, monkeypatch):
    def docker(command, *, capture_output, text, check=True):
        args = tuple(command[1:])
        if args[0] == "run":
            raise subprocess.CalledProcessError(
                1, ["docker", *args], "start output", "start failed"
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(replay_tool.subprocess, "run", docker)
    out = tmp_path / "replay"
    with pytest.raises(subprocess.CalledProcessError):
        replay_tool.replay(FIXTURES / PAIRS[0]["vulnerable"], output_dir=out)
    assert "start failed" in (out / "error.txt").read_text()
    assert json.loads((out / "commands.json").read_text())[-1][1:3] == ["rmi", "-f"]
