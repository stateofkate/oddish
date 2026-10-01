#!/usr/bin/env python3
"""Collect independent trial evidence, render reviews, and export approved goldens.

Standard library only; remote operations are GETs. Human decisions are edited in
review.json. This helper validates recorded approval, not a reviewer's identity.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

SCHEMA = "oddish_golden_review_v1"
LABELS = {"GOOD_SUCCESS", "GOOD_FAILURE", "BAD_SUCCESS", "BAD_FAILURE", "HARNESS_ERROR"}
FACTS = (
    "id task_id task_version_id task_version kind agent model status reward "
    "started_at finished_at attempts has_trajectory error_message harbor_sha environment"
).split()
TASK_ROOTS = {"instruction.md", "task.toml", "environment", "tests", "solution"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def local_file(root, relative):
    path = Path(relative)
    require(not path.is_absolute() and path.parts and ".." not in path.parts, f"Unsafe evidence path: {relative}")
    target = root / path
    require(target.resolve().is_relative_to(root.resolve()), f"Path leaves packet: {relative}")
    return target


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(text)
        temporary = Path(handle.name)
    temporary.replace(path)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Source:
    def __init__(self, api, cli):
        parsed = urlsplit(api)
        require(
            parsed.scheme in {"http", "https"}
            and parsed.netloc
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment,
            "Use an API base URL without credentials or query",
        )
        self.api, self.cli = api.rstrip("/"), cli

    def status(self, subject, *options):
        # Capture output: status can contain QA answers or authentication errors.
        result = subprocess.run(
            [self.cli, "status", subject, *options, "--api", self.api, "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        require(result.returncode == 0, f"CLI status failed (exit {result.returncode}); check access and CLI version")
        return json.loads(result.stdout)

    def get(self, path, **params):
        url = self.api + path + ("?" + urlencode(params) if params else "")
        key = os.environ.get("ODDISH_API_KEY")
        require(bool(key), "Set ODDISH_API_KEY before collection or remote verification")
        return json.loads(self.download(url, {"Authorization": f"Bearer {key}"}))

    @staticmethod
    def download(url, headers=None):
        require(urlsplit(url).scheme in {"https", "http"}, "Unsupported download URL scheme")
        try:
            # Do not forward authorization to redirects or presigned storage URLs.
            with build_opener(NoRedirect()).open(Request(url, headers=headers or {}), timeout=120) as response:
                return response.read()
        except HTTPError as exc:
            raise ValueError(f"Evidence request returned HTTP {exc.code}") from None
        except (URLError, TimeoutError):
            raise ValueError("Evidence request failed or timed out") from None


def require_direct_trial_identity(trial):
    # The importer may store a copy's label under its same-org original. The
    # status response cannot establish that original's organization or absence.
    imported = (trial.get("harbor_config") or {}).get("imported_source")
    original = imported.get("trial_id") if isinstance(imported, dict) else None
    require(
        not original or original == trial["id"],
        f"{trial['id']}: copied from {original}; the importer may label that original instead. "
        f"Collect and review {original} directly; copied-trial approval cannot be transferred",
    )


def version_snapshot(source, facts):
    data = source.status(facts["task_id"], "--versions", "--version", str(facts["task_version"]))
    require(
        data.get("id") == facts["task_version_id"]
        and data.get("task_id") == facts["task_id"]
        and data.get("version") == facts["task_version"],
        "Task version identity mismatch",
    )
    require(bool(data.get("content_hash")), "Task version has no content fingerprint")
    return data["content_hash"]


def collect_task(source, facts, root, expected_hash):
    endpoint = f"/tasks/{quote(facts['task_id'], safe='')}/files"
    cursor, seen, omitted = "", set(), []
    while True:
        page = source.get(
            endpoint,
            version=facts["task_version"],
            recursive="true",
            inline="false",
            presign="true",
            limit=1000,
            cursor=cursor,
        )
        require(page.get("source_hash") == expected_hash, "Task contents changed or fingerprint unavailable")
        archive = None
        if page.get("archive_url"):
            archive = tarfile.open(fileobj=io.BytesIO(source.download(page["archive_url"])), mode="r:*")
        members = {str(PurePosixPath(member.name)): member for member in archive.getmembers()} if archive else {}
        try:
            for entry in page["files"]:
                relative = entry["path"]
                dest = local_file(root, relative)
                if Path(relative).parts[0] not in TASK_ROOTS:
                    omitted.append(relative)
                    continue
                if archive is not None:
                    member = members[relative]
                    require(member.isfile(), f"Task member is not a regular file: {relative}")
                    with archive.extractfile(member) as handle:
                        content = handle.read()
                else:
                    require(bool(entry.get("url")), f"No byte download available for {relative}")
                    content = source.download(entry["url"])
                require(len(content) == entry["size"], f"File size mismatch: {relative}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(content)
        finally:
            if archive is not None:
                archive.close()
        if not page.get("truncated"):
            break
        cursor = page.get("cursor")
        require(cursor and cursor not in seen, "Task file listing has an invalid continuation cursor")
        seen.add(cursor)
    require(
        (root / "instruction.md").is_file() and (root / "task.toml").is_file(), "Missing instruction.md or task.toml"
    )
    require((root / "tests").is_dir() and any((root / "tests").rglob("*")), "Missing grading source under tests/")
    return omitted


def collect_case(source, trial_id, root):
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", trial_id), "Use an exact trial ID, not a path or URL")
    case = dict(
        source_trial_id=trial_id,
        task_version_id=None,
        task_content_hash=None,
        proposed_classification=None,
        expected_classification=None,
        decision="pending",
        evidence=[],
        reasoning="",
        alternatives="",
        open_questions=[],
        blockers=[],
        reproductions=[],
        reviewed_by=None,
        approval_note=None,
        artifacts={},
        omitted_task_files=[],
    )
    folder = root / "cases" / trial_id
    require(not folder.exists(), f"Case directory already exists: {trial_id}; use a fresh output directory")
    folder.mkdir(parents=True)
    try:
        raw = source.status(trial_id)
        agent = (raw.get("agent") or "").strip().lower()
        require(
            raw.get("id") == trial_id
            and raw.get("kind") == "agent"
            and bool(agent)
            and agent not in {"nop", "oracle"}
            and not agent.startswith(("nop-", "oracle-", "agent-nop", "agent-oracle"))
            and raw.get("status") in {"success", "failed"},
            "Expected a finished agent trial",
        )
        require(
            raw.get("superseded_by_trial_id") is None,
            f"{trial_id}: replaced by {raw.get('superseded_by_trial_id')}; review the replacement trial",
        )
        require_direct_trial_identity(raw)
        facts = {key: raw.get(key) for key in FACTS}
        require(facts["task_version_id"] and facts["task_version"], "Trial has no pinned task version")
        save_json(folder / "facts.json", facts)
        case["task_version_id"] = facts["task_version_id"]
        case["task_content_hash"] = version_snapshot(source, facts)
        case["omitted_task_files"] = collect_task(source, facts, folder / "task", case["task_content_hash"])
        endpoint = f"/trials/{quote(trial_id, safe='')}"
        # Read only source resources, never QA/audit/summarize trial artifacts.
        for name in ("result", "trajectory"):
            if name == "trajectory" and not facts["has_trajectory"]:
                continue
            if name == "result" and not facts["started_at"]:
                continue
            value = source.get(f"{endpoint}/{name}")
            require(isinstance(value, dict) and bool(value), f"Missing {name} JSON object")
            save_json(folder / f"{name}.json", value)
        logs = source.get(f"{endpoint}/logs/structured", verifier_only="true")
        verifier = logs.get("verifier") or {}
        selected = {key: verifier.get(key) for key in ("stdout", "stderr")}
        selected["exception"] = logs.get("exception")
        if facts["started_at"]:
            require(any(value is not None for value in selected.values()), "Missing verifier output and exception")
        save_json(folder / "verifier.json", selected)
        require(version_snapshot(source, facts) == case["task_content_hash"], "Task changed during collection")
    except (ValueError, OSError, KeyError, TypeError, tarfile.TarError, subprocess.TimeoutExpired) as exc:
        # Avoid raw subprocess/API payloads that may contain answers or secrets.
        case["blockers"].append(str(exc) if isinstance(exc, ValueError) else f"Collection failed: {type(exc).__name__}")
    case["artifacts"] = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }
    return case


def read_review(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(data, dict) and data.get("schema_version") == SCHEMA, "Unsupported review schema")
    require(isinstance(data.get("cases"), list), "cases must be a list")
    for case in data["cases"]:
        require(isinstance(case, dict), "Each case must be an object")
        require(case.get("decision") in {"pending", "approved", "unresolved"}, "Invalid review decision")
        require(
            case.get("proposed_classification") is None or case["proposed_classification"] in LABELS,
            "Invalid proposed classification",
        )
    return data


def render(path):
    data = read_review(path)
    lines = [
        "# Golden review",
        "",
        f"Deployment: {data['api_url']}",
        "",
        "Review source evidence before approving. Unresolved cases cannot be exported.",
        "",
    ]
    for case in data["cases"]:
        lines.extend([f"## {case['source_trial_id']}", ""])
        for key in (
            "task_version_id",
            "task_content_hash",
            "decision",
            "proposed_classification",
            "expected_classification",
            "reviewed_by",
            "approval_note",
            "reasoning",
            "alternatives",
        ):
            lines.extend([f"{key}: {case.get(key) or '(not recorded)'}", ""])
        for key in ("evidence", "open_questions", "blockers", "reproductions", "omitted_task_files"):
            lines.extend([f"### {key}", "", "```json", json.dumps(case.get(key, []), indent=2), "```", ""])
        lines.extend(["### Collected artifacts", ""])
        lines.extend(f"- {name}" for name in case["artifacts"])
        lines.append("")
    path.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")


def approved_rows(path, data):
    rows, seen = [], set()
    for case in data["cases"]:
        trial_id = case["source_trial_id"]
        require(trial_id not in seen, f"Duplicate trial ID: {trial_id}; keep one review record")
        seen.add(trial_id)
        if case["decision"] != "approved":
            continue
        require(case.get("expected_classification") in LABELS, f"{trial_id}: invalid expected classification")
        for key in (
            "reasoning",
            "alternatives",
            "reviewed_by",
            "approval_note",
            "task_version_id",
            "task_content_hash",
        ):
            require(isinstance(case.get(key), str) and case[key].strip(), f"{trial_id}: missing {key}")
        for key in ("open_questions", "blockers"):
            require(case.get(key) == [], f"{trial_id}: resolve {key} before approval")
        require(isinstance(case.get("artifacts"), dict) and case["artifacts"], f"{trial_id}: no collected artifacts")
        facts_path = f"cases/{trial_id}/facts.json"
        require(facts_path in case["artifacts"], f"{trial_id}: missing trial facts")
        for name, digest in case["artifacts"].items():
            artifact = local_file(path.parent, name)
            require(
                artifact.is_file() and hashlib.sha256(artifact.read_bytes()).hexdigest() == digest,
                f"{trial_id}: evidence missing or changed: {name}",
            )
        facts = json.loads(local_file(path.parent, facts_path).read_text(encoding="utf-8"))
        require(
            facts["id"] == trial_id and facts["task_version_id"] == case["task_version_id"],
            f"{trial_id}: review identity differs from collected facts",
        )
        require(isinstance(case.get("evidence"), list) and case["evidence"], f"{trial_id}: missing evidence")
        roles, citations = set(), []
        for item in case["evidence"]:
            require(isinstance(item, dict), "Each evidence citation must be an object")
            name = item["artifact"]
            require(name in case["artifacts"], f"{trial_id}: citation is not a collected artifact: {name}")
            require(
                isinstance(item.get("observation"), str) and item["observation"].strip(), "Missing evidence observation"
            )
            match = re.fullmatch(r"([1-9][0-9]*)(?:-([1-9][0-9]*))?", str(item.get("lines", "")))
            require(match, f"{trial_id}: cite one-based lines, e.g. 12-14")
            start, end = int(match[1]), int(match[2] or match[1])
            count = len(local_file(path.parent, name).read_text(encoding="utf-8").splitlines())
            require(start <= end <= count, f"{trial_id}: citation outside file: {name}")
            require(item.get("role") in {"requirement", "behavior", "grading"}, "Invalid evidence role")
            roles.add(item["role"])
            citations.append(f"{name}:{item['lines']} ({item['observation']})")
        require(
            roles == {"requirement", "behavior", "grading"},
            f"{trial_id}: cite requirement, behavior and grading evidence",
        )
        require(isinstance(case.get("reproductions"), list), "reproductions must be a list")
        for reproduction in case["reproductions"]:
            require(isinstance(reproduction, dict), "Each reproduction must be an object")
            require(
                reproduction.get("command")
                and reproduction.get("output")
                and isinstance(reproduction.get("exit_code"), int),
                "Reproductions need command, output, exit_code",
            )
        note = (
            f"Reviewed by {case['reviewed_by']}. Approval: {case['approval_note']}. "
            f"Version: {case['task_version_id']} ({case['task_content_hash']}). "
            f"Evidence: {'; '.join(citations)}. Reason: {case['reasoning']}"
        )
        require(
            len(note) <= 20_000,
            f"{trial_id}: note is {len(note)} characters; the import limit is 20000. "
            "Shorten the approval, observations, or reasoning before exporting.",
        )
        rows.append(dict(source_trial_id=trial_id, expected_classification=case["expected_classification"], note=note))
    require(rows, "No approved cases to export")
    return rows


def verify_remote(source, case, root):
    facts = json.loads(local_file(root, f"cases/{case['source_trial_id']}/facts.json").read_text(encoding="utf-8"))
    current = source.status(case["source_trial_id"])
    require_direct_trial_identity(current)
    require(
        current.get("superseded_by_trial_id") is None,
        f"{case['source_trial_id']}: replaced by {current.get('superseded_by_trial_id')}; review the replacement trial",
    )
    require(all(current.get(key) == facts[key] for key in FACTS), "Trial facts changed; collect and review again")
    require(
        version_snapshot(source, facts) == case["task_content_hash"], "Task contents changed; collect and review again"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect", help="Collect source evidence; skip already saved cases on resume")
    collect.add_argument("trials", nargs="+")
    collect.add_argument("--api", required=True)
    collect.add_argument("--out", type=Path, required=True)
    collect.add_argument("--cli", default="oddish")
    report = commands.add_parser("render", help="Regenerate review.md from edited review.json")
    report.add_argument("review", type=Path)
    export = commands.add_parser("export", help="Validate and export approved cases; no remote writes")
    export.add_argument("review", type=Path)
    export.add_argument("--out", type=Path, required=True)
    export.add_argument(
        "--verify-remote", action="store_true", help="Also check trial facts and task fingerprint before import"
    )
    export.add_argument("--cli", default="oddish")
    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            source = Source(args.api, args.cli)
            path = args.out / "review.json"
            data = read_review(path) if path.exists() else dict(schema_version=SCHEMA, api_url=source.api, cases=[])
            require(data["api_url"] == source.api, "Resume requires the packet's original deployment")
            if not path.exists():
                save_json(path, data)
            saved = {case["source_trial_id"] for case in data["cases"]}
            for trial in dict.fromkeys(args.trials):
                if trial not in saved:
                    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", trial), "Use an exact trial ID, not a path or URL")
                    folder = local_file(args.out, f"cases/{trial}")
                    if folder.exists():
                        # No saved record owns these bytes; preserve them and recollect.
                        backup = Path(tempfile.mkdtemp(prefix=f".interrupted-{trial}-", dir=args.out))
                        folder.rename(backup / "evidence")
                        print(f"Preserved unfinished evidence for {trial} in {backup}; collecting again")
                    data["cases"].append(collect_case(source, trial, args.out))
                    save_json(path, data)
            render(path)
            blocked = sum(bool(case["blockers"]) for case in data["cases"])
            print(f"Saved {len(data['cases'])} cases ({blocked} blocked) to {path}")
            return 1 if blocked else 0
        if args.command == "render":
            render(args.review)
            print(args.review.with_suffix(".md"))
            return 0
        data = read_review(args.review)
        rows = approved_rows(args.review, data)
        if args.verify_remote:
            source = Source(data["api_url"], args.cli)
            for case in data["cases"]:
                if case["decision"] == "approved":
                    verify_remote(source, case, args.review.parent)
        # Exclusive creation avoids leaving an old CSV masquerading as a new export.
        with args.out.open("x", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["source_trial_id", "expected_classification", "note"])
            writer.writeheader()
            writer.writerows(rows)
        print(
            f"Exported {len(rows)} approved cases; excluded {len(data['cases']) - len(rows)}. "
            f"Remote check: {'passed' if args.verify_remote else 'not run; required before import'}"
        )
        return 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
