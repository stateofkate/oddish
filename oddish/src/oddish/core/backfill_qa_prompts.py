"""Backfill ``qa_prompts`` from git history and from past replays.

From ``oddish/`` with the intended database configured::

    uv run python -m oddish.core.backfill_qa_prompts --repo ..          # preview
    uv run python -m oddish.core.backfill_qa_prompts --repo .. --apply

1. Every committed version of each stage's shipped prompt becomes a global
   row with its first commit, author and date (the earliest commit wins when
   a text returns after a revert).
2. Every ``qa_eval`` replay whose prompt hash has no row gets one, with the
   text cut from its stored brief. The cut is kept only when it hashes to the
   replay's recorded ``prompt_sha256``, so a wrong cut is never stored.

Idempotent: existing rows are kept.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.analyze.qa_stages import STAGES, QAStage, prompt_sha256
from oddish.core.qa_eval_scores import REPLAY_PAYLOAD, REPLAY_STAGE
from oddish.db import ExperimentModel, QAPromptModel, TrialModel, get_session

_PROMPT_ROOT = "oddish/src/oddish/analyze"
_CLASSIFY_HEADER = "== PER-TRIAL CLASSIFICATION ==\n"


@dataclass(frozen=True, slots=True)
class GitVersion:
    sha256: str
    text: str
    commit: str
    author: str
    committed_at: datetime
    subject: str


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def git_versions(repo: Path, stage: QAStage) -> list[GitVersion]:
    """Each distinct text of the stage's prompt, at its earliest commit."""
    path = f"{_PROMPT_ROOT}/{STAGES[stage].prompt_file}"
    log = _git(
        repo,
        "log",
        "--follow",
        "--reverse",
        "--format=%x1e%H%x1f%an%x1f%aI%x1f%s",
        "--name-only",
        "--",
        path,
    )
    versions: dict[str, GitVersion] = {}
    for record in filter(None, (r.strip() for r in log.split("\x1e"))):
        header, _, names = record.partition("\n")
        commit, author, date, subject = header.split("\x1f")
        # --follow: the name the file had at this commit.
        committed_path = names.strip().splitlines()[-1] if names.strip() else path
        try:
            text = _git(repo, "show", f"{commit}:{committed_path}")
        except subprocess.CalledProcessError:
            continue  # the commit deleted the file
        sha = prompt_sha256(text)
        versions.setdefault(
            sha,
            GitVersion(
                sha256=sha,
                text=text.strip(),
                commit=commit,
                author=author,
                committed_at=datetime.fromisoformat(date),
                subject=subject[:128],
            ),
        )
    return list(versions.values())


def prompt_from_brief(brief: str | None, expected_sha256: str) -> str | None:
    """The classify prompt embedded in a QA brief, if it hashes as recorded."""
    if not brief or _CLASSIFY_HEADER not in brief:
        return None
    rest = brief.split(_CLASSIFY_HEADER, 1)[1]
    text = rest.split("\n\n== ", 1)[0].strip()
    return text if prompt_sha256(text) == expected_sha256 else None


async def backfill_git(session: AsyncSession, repo: Path) -> dict[str, int]:
    counts = {"inserted": 0, "unchanged": 0}
    for stage in QAStage:
        recorded = set(
            await session.scalars(
                select(QAPromptModel.sha256).where(
                    QAPromptModel.org_id.is_(None), QAPromptModel.stage == stage.value
                )
            )
        )
        for version in git_versions(repo, stage):
            if version.sha256 in recorded:
                counts["unchanged"] += 1
                continue
            session.add(
                QAPromptModel(
                    stage=stage.value,
                    sha256=version.sha256,
                    text=version.text,
                    name=version.subject,
                    source="production",
                    git_commit=version.commit,
                    git_author=version.author,
                    git_committed_at=version.committed_at,
                    created_at=version.committed_at,
                )
            )
            counts["inserted"] += 1
    await session.flush()
    return counts


async def backfill_replays(session: AsyncSession) -> dict[str, int]:
    """Recover the text of replayed prompts recorded before ``qa_prompts``."""
    sha = REPLAY_PAYLOAD["prompt_sha256"].astext
    known = select(QAPromptModel.id).where(
        QAPromptModel.stage == REPLAY_STAGE,
        QAPromptModel.sha256 == sha,
        or_(QAPromptModel.org_id == TrialModel.org_id, QAPromptModel.org_id.is_(None)),
    )
    rows = await session.execute(
        select(
            TrialModel.org_id,
            REPLAY_STAGE,
            sha,
            func.coalesce(REPLAY_PAYLOAD["prompt_name"].astext, ""),
            TrialModel.harbor_config["extra_instructions"].astext,
            TrialModel.created_at,
            ExperimentModel.owner_user_id,
        )
        .join(ExperimentModel, ExperimentModel.id == TrialModel.experiment_id)
        .where(TrialModel.kind == "qa_eval", sha.is_not(None), ~known.exists())
        .order_by(TrialModel.created_at)
    )
    recovered: set[tuple] = set()
    unrecoverable: set[tuple] = set()
    for org_id, row_stage, row_sha, name, brief, created_at, owner in rows:
        key = (org_id, row_stage, row_sha)
        if key in recovered:
            continue
        text = prompt_from_brief(brief, row_sha)
        if text is None:
            unrecoverable.add(key)
            continue
        session.add(
            QAPromptModel(
                org_id=org_id,
                stage=row_stage,
                sha256=row_sha,
                text=text,
                name=name,
                source="replay",
                created_by_user_id=owner,
                created_at=created_at,
            )
        )
        recovered.add(key)
    await session.flush()
    return {
        "recovered": len(recovered),
        "unrecoverable": len(unrecoverable - recovered),
    }


async def main(repo: Path, apply: bool) -> None:
    async with get_session() as session:
        print(f"git prompt versions: {await backfill_git(session, repo)}")
        print(f"replayed prompts: {await backfill_replays(session)}")
        if apply:
            await session.commit()
            print("applied")
        else:
            await session.rollback()
            print("preview only; rerun with --apply to write")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--repo", type=Path, required=True, help="Oddish git checkout root"
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.repo.resolve(), args.apply))
