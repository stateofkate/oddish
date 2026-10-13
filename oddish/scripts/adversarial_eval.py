"""Summarize per-finding replay results and per-run costs from a JSONL file.

Every finding needs a QA finding ID, kind, replay_status and evidence_path.
Evidence paths are relative to the input file. The person recording a replay
must cite saved commands/outputs and describe the observed prohibited action;
script exit status or an attacker's claim is not proof of disclosure.

Usage: python scripts/adversarial_eval.py runs.jsonl
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path


def summarize(
    runs: list[dict], *, evidence_root: Path = Path(".")
) -> dict[str, dict[str, dict]]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    trial_ids: set[str] = set()
    for run in runs:
        for field in ("pair", "version", "trial_id"):
            if not isinstance(run.get(field), str) or not run[field].strip():
                raise ValueError(f"{field} must be a nonempty string")
        if run["version"] not in ("vulnerable", "repaired"):
            raise ValueError("version must be vulnerable or repaired")
        if run["trial_id"] in trial_ids:
            raise ValueError(f"duplicate trial_id: {run['trial_id']}")
        trial_ids.add(run["trial_id"])
        if type(run.get("incomplete")) is not bool:
            raise ValueError("incomplete must be an explicit boolean")
        for field in ("replay_reward", "cost_usd"):
            if field not in run:
                raise ValueError(f"{field} is required; use null when unknown")
            value = run[field]
            if value is not None and (
                type(value) not in (int, float) or not math.isfinite(value) or value < 0
            ):
                raise ValueError(f"{field} must be a finite nonnegative number or null")
        if not isinstance(run.get("findings"), list):
            raise ValueError(
                "findings must be an array with per-finding replay results"
            )
        finding_ids: set[str] = set()
        for finding in run["findings"]:
            if (
                not isinstance(finding, dict)
                or not isinstance(finding.get("id"), str)
                or not finding["id"].strip()
            ):
                raise ValueError("each finding requires a nonempty QA finding id")
            if finding["id"] in finding_ids:
                raise ValueError(f"duplicate finding id: {finding['id']}")
            finding_ids.add(finding["id"])
            if finding.get("kind") not in (
                "grading_bypass",
                "protected_file_disclosure",
            ):
                raise ValueError(
                    "finding kind must be grading_bypass or protected_file_disclosure"
                )
            status = finding.get("replay_status")
            if status not in ("reproduced", "not_reproduced", "not_run", "error"):
                raise ValueError("invalid replay_status")
            if "evidence_path" not in finding:
                raise ValueError(
                    "evidence_path is required; use null when no evidence exists"
                )
            path = finding["evidence_path"]
            if status in ("reproduced", "not_reproduced") and path is None:
                raise ValueError("completed replay requires evidence_path")
            if status == "not_run" and path is not None:
                raise ValueError("not_run requires null evidence_path")
            if path is not None:
                if (
                    not isinstance(path, str)
                    or not path.strip()
                    or Path(path).is_absolute()
                ):
                    raise ValueError("evidence_path must be a nonempty relative path")
                if not (evidence_root / path).exists():
                    raise ValueError(f"replay evidence does not exist: {path}")
            if (
                finding["kind"] == "grading_bypass"
                and status == "reproduced"
                and run["replay_reward"] != 1.0
            ):
                raise ValueError("reproduced grading bypass requires replay_reward 1")
        groups[(run["pair"], run["version"])].append(run)
    out: dict[str, dict[str, dict]] = defaultdict(dict)
    for (pair, version), rows in sorted(groups.items()):
        findings = [finding for run in rows for finding in run["findings"]]
        reproduced = [f for f in findings if f["replay_status"] == "reproduced"]
        costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
        out[pair][version] = {
            "runs": len(rows),
            "execution_failures": sum(r["incomplete"] for r in rows),
            "findings_total": len(findings),
            "reproduced_grading_bypasses": sum(
                f["kind"] == "grading_bypass" for f in reproduced
            ),
            "reproduced_disclosures": sum(
                f["kind"] == "protected_file_disclosure" for f in reproduced
            ),
            "not_reproduced_findings": sum(
                f["replay_status"] == "not_reproduced" for f in findings
            ),
            "unverified_findings": sum(
                f["replay_status"] in ("not_run", "error") for f in findings
            ),
            "reproduced_findings_on_repaired_tasks": len(reproduced)
            if version == "repaired"
            else 0,
            "cost_usd_total": round(sum(costs), 4),
            "priced_runs": len(costs),
            "unpriced_runs": len(rows) - len(costs),
            "cost_usd_per_priced_run": round(sum(costs) / len(costs), 4)
            if costs
            else None,
        }
    return dict(out)


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        source = Path(argv[1])
        runs = [
            json.loads(line) for line in source.read_text().splitlines() if line.strip()
        ]
        result = summarize(runs, evidence_root=source.parent)
    else:
        result = summarize([json.loads(line) for line in sys.stdin if line.strip()])
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
