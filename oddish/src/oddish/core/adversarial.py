"""Adversarial QA: organization-funded attacks on a task's grader.

An adversarial QA trial is an ordinary worker trial (``kind = 'adversarial'``) on
the task's real environment. The attacker gets the normal solver instruction
behind an attack objective and the original grader scores it. Its exploit.sh is
then replayed alone on a fresh copy. A passing replay, or a replay that copies
out a protected file, records a must-fix finding on the task version. QA never
reads attacks.
"""

from __future__ import annotations


import hashlib
from datetime import timedelta
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import (
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    is_active_trial_status,
    utcnow,
)
from oddish.db.storage import is_missing_object

# Retain the stored/API kind; this executes the real task, not a QA overlay.
ADVERSARIAL_TRIAL_KIND = "adversarial"
# GLM-5.3 through OpenRouter; the Fireworks route was unreliable.
ADVERSARIAL_MODEL = "openrouter/z-ai/glm-5.3"
# Descriptive label; each trial stores and verifies the actual prompt bytes.
ATTACK_PROMPT_VERSION = "adversarial-v5"
# The attacker finds different things on different runs; one click runs several.
# Every second attack is white box: it also gets the grader's code.
ATTACKS_PER_LAUNCH = 4
# Code files only. A data file can hold the expected answers, and an exploit
# that pastes them in would pass without showing anything a solver could do.
GRADER_CODE_SUFFIXES = frozenset(
    {".sh", ".py", ".go", ".js", ".ts", ".rs", ".c", ".cc", ".cpp", ".h", ".rb", ".java"}
)
# ponytail: mini-swe-agent passes the whole prompt as one argument, and Modal
# rejects a command over 64 KB (ARG_MAX), quoting included. Pass the prompt as
# a file if a task ever needs more grader code shown.
GRADER_CODE_MAX_BYTES = 32_000


def grader_code_section(task_dir: Path) -> str:
    """The white-box addition to an attack's instruction: the code in tests/."""
    parts = [
        "\n--------------------------------------------------------------------\n"
        "The grader's code follows (files from tests/; data files are left out)."
        " It is not in the container; it is copied in after you stop.\n"
    ]
    tests = task_dir / "tests"
    code = [
        path
        for path in (tests.rglob("*") if tests.is_dir() else [])
        if not path.is_symlink()
        and path.is_file()
        and path.suffix in GRADER_CODE_SUFFIXES
    ]
    # test.sh is what the grader runs, so it goes first; then smallest first.
    code.sort(key=lambda path: (path.name != "test.sh", path.stat().st_size, path))
    budget = GRADER_CODE_MAX_BYTES
    for path in code:
        name = path.relative_to(task_dir).as_posix()
        size = path.stat().st_size
        if size > budget:
            parts.append(f"=== {name} ({size} bytes, left out for length)\n")
        else:
            budget -= size
            parts.append(f"=== {name}\n{path.read_text(errors='replace')}\n")
    return "\n".join(parts)


# Numbered ideas in adversarial_prompt.txt. Left alone, attacks in one mode all
# chase the same idea, so each one in a mode starts from a different one.
ATTACK_START_IDEAS = (2, 3, 4)
MODE_LINES = {
    False: "You cannot see the grader. Before attacking, list what in this"
    " container the grader will run or read and you can write: programs on PATH,"
    " the Python installation and its packages, files next to your answer.",
    True: "The grader's code is at the end of this text. Before writing"
    " exploit.sh, name the file and line where the grader trusts something you"
    " control and the check it skips, and put both in your report.",
}


def attack_prompt(template: str, offset: int) -> tuple[str, bool]:
    """The prompt for the launch's ``offset``-th attack, and whether it is white box."""
    white_box = offset % 2 == 1
    start = ATTACK_START_IDEAS[offset // 2 % len(ATTACK_START_IDEAS)]
    return template.format(mode=MODE_LINES[white_box], start=start), white_box


# A replay runs the attacker's /logs/agent/exploit.sh alone in a fresh copy of
# the task, then the grader. It is stored as an adversarial trial under its own
# agent name so no nop/oracle baseline check counts it; the worker executes it
# with Harbor's oracle agent, which runs solution/solve.sh.
REPLAY_AGENT = "exploit-replay"
EXPLOIT_ARTIFACT = "agent/exploit.sh"


def execution_agent(agent: str) -> str:
    """The Harbor agent a stored trial agent runs as."""
    return "oracle" if agent == REPLAY_AGENT else agent


def adversarial_conditions(
    task_id: str, task_version_id: str | None, *, replays: bool | None = False
) -> list:
    """``replays``: False for attacks only, True for replays only, None for both."""
    conditions = [
        TrialModel.task_id == task_id,
        TrialModel.kind == ADVERSARIAL_TRIAL_KIND,
        TrialModel.superseded_by_trial_id.is_(None),
        TrialModel.deleted_at.is_(None),
    ]
    if replays is not None:
        conditions.append(
            (TrialModel.agent == REPLAY_AGENT)
            if replays
            else (TrialModel.agent != REPLAY_AGENT)
        )
    if task_version_id is not None:
        conditions.append(TrialModel.task_version_id == task_version_id)
    return conditions


def replay_target(harbor_config: dict | None) -> tuple[str, int] | None:
    """The (attack trial id, attack attempt) a replay re-ran, or None."""
    pins = (harbor_config or {}).get(ADVERSARIAL_TRIAL_KIND) or {}
    attack_id, attempt = pins.get("replay_of"), pins.get("attack_attempt")
    if isinstance(attack_id, str) and isinstance(attempt, int):
        return attack_id, attempt
    return None


# The replay's solution/ holds the exploit, the protected files' hashes, and a
# solve.sh that runs the exploit and then, before the grader copies tests/ in,
# moves LEAK_DIR aside. Snapshotting after grading would count a background
# process that copies the arriving tests/. The worker hashes the snapshot
# (write_leak_check), so the task image needs only sh and mv.
# ponytail: the exploit can forge the snapshot or leak-check.txt; that only
# fakes a leak it was asked to prove, so no nonce or out-of-container check.
LEAK_DIR = "/logs/agent/leaked"
LEAK_SNAPSHOT = "leaked-before-grading"
LEAK_CHECK_ARTIFACT = "agent/leak-check.txt"
# Tiny files (an empty __init__.py, "1\n") match by accident.
PROTECTED_MIN_BYTES = 64
REPLAY_SOLVE_SH = f"""#!/bin/sh
here=$(cd "$(dirname "$0")" && pwd)
chmod +x "$here/exploit.sh" 2>/dev/null
if [ -x "$here/exploit.sh" ]; then
  "$here/exploit.sh"
else
  interp=$(sed -n '1s/^#! *//p' "$here/exploit.sh")
  ${{interp:-sh}} "$here/exploit.sh"
fi
rc=$?
[ -d {LEAK_DIR} ] && mv {LEAK_DIR} /logs/agent/{LEAK_SNAPSHOT}
exit $rc
"""


def write_leak_check(job_dir: Path, protected: str) -> None:
    """Write agent/leak-check.txt next to each downloaded leak snapshot.

    ``protected`` is the replay's protected.sha256; the check lists the
    protected files whose bytes the exploit copied before grading.
    """
    names = dict(line.split("  ", 1) for line in protected.splitlines() if line)
    for snapshot in job_dir.rglob(LEAK_SNAPSHOT):
        if snapshot.parent.name != "agent" or not snapshot.is_dir():
            continue
        leaked = {
            names[h]
            for p in snapshot.rglob("*")
            if p.is_file()
            and not p.is_symlink()
            and (h := hashlib.sha256(p.read_bytes()).hexdigest()) in names
        }
        (snapshot.parent / "leak-check.txt").write_text(
            "".join(f"{name}\n" for name in sorted(leaked))
        )


def protected_hashes(task_dir: Path) -> str:
    """``sha256sum`` lines for tests/ and solution/ files the solver is not given.

    A file whose bytes are also under environment/ is openly given, so it is
    left out, as are files under PROTECTED_MIN_BYTES.
    """

    def files(folder: str) -> list[Path]:
        root = task_dir / folder
        return [
            p
            for p in (root.rglob("*") if root.is_dir() else [])
            if p.is_file() and not p.is_symlink()
        ]

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    given = {digest(p) for p in files("environment")}
    lines = {}
    for path in files("tests") + files("solution"):
        if path.stat().st_size < PROTECTED_MIN_BYTES:
            continue
        h = digest(path)
        if h not in given:
            lines.setdefault(h, path.relative_to(task_dir).as_posix())
    return "".join(f"{h}  {name}\n" for h, name in sorted(lines.items()))


async def read_leak_check(storage: Any, replay: Any) -> list[str]:
    """Protected task files the replay's exploit copied out, as task paths."""
    from oddish.core.trial_artifacts import resolve_trial_artifact_layout

    layout = await resolve_trial_artifact_layout(replay, storage)
    if layout.artifact_prefix is None:
        return []
    try:
        text = await storage.download_text(
            f"{layout.artifact_prefix}{LEAK_CHECK_ARTIFACT}"
        )
    except ClientError as exc:
        # No file means nothing was copied out. Any other storage error is
        # raised so the healer retries the hook instead of recording no leak.
        if is_missing_object(exc):
            return []
        raise
    return sorted({line.strip() for line in text.splitlines() if line.strip()})


def replay_findings(replay: TrialModel, leaked: list[str]) -> list[dict]:
    """Must-fix findings a settled replay demonstrates, in the ActionItem shape."""
    attack_id = (replay_target(replay.harbor_config) or (None, 0))[0]
    base = {
        "source": ADVERSARIAL_TRIAL_KIND,
        "problem_type": "incompleteness",
        "line_start": 1,
        "line_end": 1,
        "tier": "must_fix",
        "replay_trial_id": replay.id,
        "attack_trial_id": attack_id,
    }
    found = []
    if replay.status == TrialStatus.SUCCESS and replay.reward == 1:
        found.append(
            {
                **base,
                "id": "adversarial-verifier",
                "dimension": "verifier",
                "file": "tests/test.sh",
                "title": "The grader passes an exploit that does not solve the task.",
                "detail": f"Exploit replay {replay.id} ran attack {attack_id}'s"
                " exploit.sh alone on a fresh copy of the task and the grader"
                " gave reward 1.",
                "recommendation": "Close the hole the attack's"
                " adversarial_report.md describes, then upload a new version.",
            }
        )
    for name in leaked:
        found.append(
            {
                **base,
                "id": "adversarial-leak:"
                + hashlib.sha256(name.encode()).hexdigest()[:16],
                "dimension": "info_leakage",
                "file": name,
                "title": f"The solver can read {name}.",
                "detail": f"Exploit replay {replay.id} copied {name} byte for"
                " byte out of a fresh copy of the task before grading.",
                "recommendation": f"Keep {name} out of the solver's image and"
                " filesystem.",
            }
        )
    return found


async def record_replay_findings(
    session: AsyncSession, replay: TrialModel, leaked: list[str]
) -> list[dict]:
    """Add a replay's findings to its task version's retained findings."""
    found = replay_findings(replay, leaked)
    if not found or replay.task_version_id is None:
        return []
    version = await session.get(
        TaskVersionModel, replay.task_version_id, with_for_update=True
    )
    if version is None:
        return []
    reports = list(version.reported_findings or [])
    seen = {(r.get("finding") or {}).get("id") for r in reports}
    added = [
        {"finding": f, "source": ADVERSARIAL_TRIAL_KIND, "reporting_trial_id": replay.id}
        for f in found
        if f["id"] not in seen
    ]
    if added:
        version.reported_findings = reports + added
    return added


# One retry for sandbox start failures; the exploit itself is deterministic.
REPLAY_MAX_ATTEMPTS = 2


def has_exploit(script: str | None) -> bool:
    """Whether exploit.sh does anything beyond the prompt's "exit 0" placeholder."""
    commands = [
        line.strip()
        for line in (script or "").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return bool(commands) and commands != ["exit 0"]


async def read_exploit_script(storage: Any, attack: Any) -> str | None:
    """The attack attempt's saved /logs/agent/exploit.sh, or None."""
    from oddish.core.trial_artifacts import resolve_trial_artifact_layout

    layout = await resolve_trial_artifact_layout(attack, storage)
    if layout.artifact_prefix is None:
        return None
    try:
        script = await storage.download_text(
            f"{layout.artifact_prefix}{EXPLOIT_ARTIFACT}"
        )
    except ClientError as exc:
        # A missing script means nothing to replay. Any other storage error is
        # raised so the healer retries the hook instead of skipping the replay.
        if is_missing_object(exc):
            return None
        raise
    return script if has_exploit(script) else None


async def queue_exploit_replay(
    session: AsyncSession, *, task: TaskModel, attack: TrialModel, script: str
) -> TrialModel | None:
    """Queue one replay of ``attack``'s current attempt; the caller holds the task lock."""
    from oddish.config import NOP_ORACLE_QUEUE_KEY, settings
    from oddish.queue import enqueue_trial_worker_job, reserve_next_trial_index

    target = (attack.id, int(attack.attempts or 0))
    replays = await session.scalars(
        select(TrialModel.harbor_config).where(
            *adversarial_conditions(task.id, attack.task_version_id, replays=True)
        )
    )
    if any(replay_target(config) == target for config in replays):
        return None
    next_index = await reserve_next_trial_index(session, task_id=task.id)
    config = {
        key: value
        for key, value in (attack.harbor_config or {}).items()
        if key not in ("agent_config", ADVERSARIAL_TRIAL_KIND)
    }
    config[ADVERSARIAL_TRIAL_KIND] = {
        "replay_of": target[0],
        "attack_attempt": target[1],
        "exploit_text": script,
        "exploit_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
    }
    replay = TrialModel(
        id=f"{task.id}-{next_index}",
        name=f"{task.name}-exploit-replay-{next_index}",
        task_id=task.id,
        task_version_id=attack.task_version_id,
        experiment_id=attack.experiment_id,
        org_id=attack.org_id,
        billed_user_id=None,
        agent=REPLAY_AGENT,
        model=NOP_ORACLE_QUEUE_KEY,
        provider=settings.get_provider_for_trial("oracle", None),
        queue_key=NOP_ORACLE_QUEUE_KEY,
        environment=attack.environment,
        kind=ADVERSARIAL_TRIAL_KIND,
        status=TrialStatus.QUEUED,
        max_attempts=REPLAY_MAX_ATTEMPTS,
        harbor_config=config,
    )
    session.add(replay)
    await session.flush()
    await enqueue_trial_worker_job(
        session,
        trial_id=replay.id,
        queue_key=replay.queue_key,
        org_id=replay.org_id,
        max_attempts=REPLAY_MAX_ATTEMPTS,
    )
    return replay


# Markers the settle hooks leave on a trial's adversarial pins, so QA admission
# and the healer can tell a handled attack or replay from one still pending.
REPLAY_CHECKED = "replay_checked_attempt"
FINDINGS_RECORDED = "findings_recorded"
# A hook that keeps failing stops holding QA back this long after its trial ended.
HOOK_DEADLINE = timedelta(hours=1)
# The sweep gives up on a claimed launch that keeps failing after this long.
LAUNCH_DEADLINE = timedelta(hours=1)


def mark_adversarial(trial: TrialModel, **values: Any) -> None:
    """Merge ``values`` into the trial's adversarial pins (reassigned for JSONB)."""
    config = dict(trial.harbor_config or {})
    config[ADVERSARIAL_TRIAL_KIND] = {
        **(config.get(ADVERSARIAL_TRIAL_KIND) or {}),
        **values,
    }
    trial.harbor_config = config


def auto_selected(version_id: str) -> bool:
    """Whether this version is in ``adversarial_auto_percent``; stable per id."""
    from oddish.config import settings

    bucket = int.from_bytes(hashlib.sha256(version_id.encode()).digest()[:2], "big")
    return bucket % 100 < settings.adversarial_auto_percent


async def maybe_claim_adversarial(
    session: AsyncSession, *, task: TaskModel, task_version_id: str | None
) -> bool:
    """Once per task version, CAS adversarial_status None -> QUEUED.

    The cleanup sweep launches the attacks: the launch transfers the task
    archive with no transaction open, so it cannot run inside a submission.
    """
    version_id = task_version_id or task.current_version_id
    if version_id is None or not task.run_analysis or not auto_selected(version_id):
        return False
    # Same lock reasoning as the audit claim: an UPDATE, not FOR UPDATE, so it
    # stays compatible with the KEY SHARE locks of concurrent trial inserts.
    claimed = await session.scalar(
        update(TaskVersionModel)
        .where(
            TaskVersionModel.id == version_id,
            TaskVersionModel.adversarial_status.is_(None),
        )
        .values(
            adversarial_status=VerdictStatus.QUEUED,
            adversarial_started_at=utcnow(),
            adversarial_error=None,
        )
        .returning(TaskVersionModel.id)
    )
    return claimed is not None


def _handled(status: Any, harbor_stage: str | None) -> bool:
    """A terminal trial whose settle hook has nothing to do."""
    return status not in (TrialStatus.SUCCESS, TrialStatus.FAILED) or (
        harbor_stage == "cancelled"
    )


async def adversarial_unsettled(
    session: AsyncSession, task_id: str, version_id: str
) -> bool:
    """True while an attack, its replay, or either's settle hook is outstanding."""
    rows = (
        await session.execute(
            select(
                TrialModel.id,
                TrialModel.agent,
                TrialModel.status,
                TrialModel.harbor_stage,
                TrialModel.attempts,
                TrialModel.finished_at,
                TrialModel.harbor_config,
            ).where(*adversarial_conditions(task_id, version_id, replays=None))
        )
    ).all()
    replays = {
        replay_target(row.harbor_config): row for row in rows if row.agent == REPLAY_AGENT
    }
    expired = utcnow() - HOOK_DEADLINE

    def pending(row: Any, marker: str, value: Any) -> bool:
        if is_active_trial_status(row.status):
            return True
        if _handled(row.status, row.harbor_stage):
            return False
        pins = (row.harbor_config or {}).get(ADVERSARIAL_TRIAL_KIND) or {}
        return pins.get(marker) != value and not (
            row.finished_at is not None and row.finished_at < expired
        )

    for attack in rows:
        if attack.agent == REPLAY_AGENT:
            continue
        attempt = int(attack.attempts or 0)
        if pending(attack, REPLAY_CHECKED, attempt):
            return True
        replay = replays.get((attack.id, attempt))
        if replay is not None and pending(replay, FINDINGS_RECORDED, True):
            return True
    return False


async def task_adversarial_pending(session: AsyncSession, task: TaskModel) -> bool:
    """QA admission waits for a claimed or running adversarial QA of the version."""
    if task.current_version_id is None:
        return False
    status = await session.scalar(
        select(TaskVersionModel.adversarial_status).where(
            TaskVersionModel.id == task.current_version_id
        )
    )
    if status == VerdictStatus.QUEUED:
        return True
    if status != VerdictStatus.RUNNING:
        return False
    return await adversarial_unsettled(session, task.id, task.current_version_id)


async def settle_adversarial_version(
    session: AsyncSession, *, task_id: str, version_id: str | None
) -> None:
    """Close the version's adversarial QA once settled; re-enter QA admission.

    The caller holds the task lock, so concurrent hooks of one task serialize.
    """
    from oddish.queue import maybe_start_task_qa_stage

    if version_id is None:
        return
    await session.flush()
    if await adversarial_unsettled(session, task_id, version_id):
        return
    await session.execute(
        update(TaskVersionModel)
        .where(
            TaskVersionModel.id == version_id,
            TaskVersionModel.adversarial_status == VerdictStatus.RUNNING,
        )
        .values(adversarial_status=VerdictStatus.SUCCESS)
    )
    await maybe_start_task_qa_stage(session, task_id)
