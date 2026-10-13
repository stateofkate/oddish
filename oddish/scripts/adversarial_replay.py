"""Replay an exploit against a Harbor task in a fresh Docker container.

Adversarial findings are only "confirmed" once the exploit works when replayed
from scratch, and the repaired counterpart of a task must resist the same
script. The replay follows the trial lifecycle: build the task's environment,
let a "solver" (the given script, the reference solution, or nothing) act with
no access to ``tests/``, then upload ``tests/`` and run the grader.

    python scripts/adversarial_replay.py replay TASK_DIR --exploit SCRIPT --output-dir EVIDENCE_DIR
    python scripts/adversarial_replay.py replay TASK_DIR --oracle --output-dir EVIDENCE_DIR
    python scripts/adversarial_replay.py replay TASK_DIR --nop --output-dir EVIDENCE_DIR
    python scripts/adversarial_replay.py stage PAIR_ID OUT_DIR

``stage`` copies a fixture pair under blinded names (``median-a``/``median-b``)
and prints the key, so an attacker and QA never see "vulnerable" in an upload.
Every command prints one JSON object per run.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "adversarial"
# Run a script the way production replay does (REPLAY_SOLVE_SH): directly when
# it is executable, else with its shebang's interpreter, else with sh.
RUN_SCRIPT = (
    'chmod +x "$1" 2>/dev/null; [ -x "$1" ] && exec "$1"; '
    'interp=$(sed -n \'1s/^#! *//p\' "$1"); exec ${interp:-sh} "$1"'
)


@dataclass
class ReplayResult:
    task: str
    mode: str
    solver_exit: int | None
    verifier_exit: int | None
    reward: float | None
    evidence_files: list[str]


def replay(
    task_dir: Path,
    *,
    output_dir: Path,
    exploit: Path | None = None,
    oracle: bool = False,
) -> ReplayResult:
    """Run in a fresh container and retain full evidence without overwriting it."""
    task_dir = Path(task_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    mode = "oracle" if oracle else "exploit" if exploit else "nop"
    evidence_files = [
        "solver.stdout.txt",
        "solver.stderr.txt",
        "verifier.stdout.txt",
        "verifier.stderr.txt",
        "reward.txt",
        "commands.json",
    ]
    result = ReplayResult(task_dir.name, mode, None, None, None, evidence_files)
    commands: list[list[str]] = []

    def docker(*args: str, check: bool = True):
        commands.append(["docker", *args])
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, check=check
        )

    for name in evidence_files:
        if name != "commands.json":
            (output_dir / name).write_text("")
    tag = f"adversarial-replay-{uuid.uuid4().hex[:10]}"
    container = None
    try:
        if exploit is not None:
            shutil.copyfile(exploit, output_dir / "exploit.sh")
            evidence_files.append("exploit.sh")
        docker("build", "-q", "-t", tag, str(task_dir / "environment"))
        container = docker(
            "run", "-d", "--network", "none", tag, "sleep", "600"
        ).stdout.strip()
        run_script = ("exec", container, "sh", "-c", RUN_SCRIPT, "run")
        if oracle:
            docker("cp", str(task_dir / "solution") + "/.", f"{container}:/solution")
            run = docker(*run_script, "/solution/solve.sh", check=False)
        elif exploit is not None:
            docker("cp", str(output_dir / "exploit.sh"), f"{container}:/exploit.sh")
            run = docker(*run_script, "/exploit.sh", check=False)
        else:
            run = None
        if run is not None:
            result.solver_exit = run.returncode
            (output_dir / "solver.stdout.txt").write_text(run.stdout)
            (output_dir / "solver.stderr.txt").write_text(run.stderr)
        docker("exec", container, "mkdir", "-p", "/tests", "/logs/verifier")
        docker("cp", str(task_dir / "tests") + "/.", f"{container}:/tests")
        verify = docker("exec", container, "sh", "/tests/test.sh", check=False)
        result.verifier_exit = verify.returncode
        (output_dir / "verifier.stdout.txt").write_text(verify.stdout)
        (output_dir / "verifier.stderr.txt").write_text(verify.stderr)
        reward_file = docker(
            "exec", container, "cat", "/logs/verifier/reward.txt", check=False
        )
        (output_dir / "reward.txt").write_text(reward_file.stdout)
        try:
            result.reward = float(reward_file.stdout.strip())
        except ValueError:
            result.reward = None
        return result
    except (OSError, subprocess.CalledProcessError) as exc:
        error = str(exc)
        if isinstance(exc, subprocess.CalledProcessError):
            error += f"\nstdout:\n{exc.stdout or ''}\nstderr:\n{exc.stderr or ''}"
        (output_dir / "error.txt").write_text(error)
        evidence_files.append("error.txt")
        raise
    finally:
        try:
            try:
                if container:
                    docker("rm", "-f", container, check=False)
            finally:
                docker("rmi", "-f", tag, check=False)
        finally:
            (output_dir / "commands.json").write_text(json.dumps(commands, indent=2))
            (output_dir / "result.json").write_text(
                json.dumps(asdict(result), indent=2)
            )


def stage(pair_id: str, out_dir: Path, *, seed: int | None = None) -> dict:
    """Copy one fixture pair as ``median-a``/``median-b`` in random order."""
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    pair = next(p for p in manifest["pairs"] if p["id"] == pair_id)
    versions = ["vulnerable", "repaired"]
    random.Random(seed).shuffle(versions)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    key = {}
    for letter, version in zip("ab", versions):
        # The pair id names the defect, so it stays out of the staged names.
        target = out_dir / f"median-{letter}"
        shutil.copytree(FIXTURES / pair[version], target)
        config = target / "task.toml"
        config.write_text(
            config.read_text().replace(
                'name = "abundant/median"', f'name = "abundant/{target.name}"'
            )
        )
        key[target.name] = version
    return {"pair": pair_id, "defect": pair["defect"], "key": key}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("replay")
    run.add_argument("task_dir", type=Path)
    run.add_argument("--output-dir", type=Path, required=True)
    group = run.add_mutually_exclusive_group(required=True)
    group.add_argument("--exploit", type=Path)
    group.add_argument("--oracle", action="store_true")
    group.add_argument("--nop", action="store_true")
    stg = sub.add_parser("stage")
    stg.add_argument("pair_id")
    stg.add_argument("out_dir", type=Path)
    stg.add_argument("--seed", type=int)
    args = parser.parse_args(argv)
    if args.command == "replay":
        result = replay(
            args.task_dir,
            exploit=args.exploit,
            oracle=args.oracle,
            output_dir=args.output_dir,
        )
        print(json.dumps(asdict(result)))
    else:
        print(json.dumps(stage(args.pair_id, args.out_dir, seed=args.seed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
