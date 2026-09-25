"""QA burndown: how a scope's blocked / clear / waiting split changes per version.

Each task version keeps its own recorded findings and its own QA verdict, so
the split can be replayed by version. Point ``k`` counts every task at its
k-th version, or at its last version when it has fewer: blocked when that
version has a ``must_fix`` finding or a verdict that is not ``is_good``, clear
when its verdict is ``is_good``, and waiting when it has no verdict. A last point with ``version`` None is
the live headline, so the chart always has a "now" and ends where the report
does.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.task_findings import must_fix_labels
from oddish.db import TaskVersionModel


def _state(version: TaskVersionModel, must_fix: list[dict]) -> str:
    # The delivery board's rule: any published verdict counts, and only
    # ``is_good: true`` accepts. Older payloads carry no ``verdict`` string.
    verdict = version.verdict
    if must_fix:
        return "blocked"
    if isinstance(verdict, dict) and verdict:
        return "clear" if verdict.get("is_good") is True else "blocked"
    return "waiting"


async def qa_burndown(
    session: AsyncSession, task_ids: list[str], summary: dict[str, int]
) -> list[dict[str, Any]]:
    if not task_ids:
        return []
    versions = {
        v.id: v
        for v in await session.scalars(
            select(TaskVersionModel).where(TaskVersionModel.task_id.in_(task_ids))
        )
    }
    defects = await must_fix_labels(session, versions)
    states: dict[str, list[str]] = {}
    for v in sorted(versions.values(), key=lambda v: v.version):
        states.setdefault(v.task_id, []).append(_state(v, defects.get(v.id, [])))
    rounds = max((len(s) for s in states.values()), default=0)
    out: list[dict[str, Any]] = []
    for k in range(1, rounds + 1):
        counts = {"blocked": 0, "clear": 0, "waiting": 0}
        for task_states in states.values():
            counts[task_states[min(k, len(task_states)) - 1]] += 1
        out.append({"version": k, **counts})
    live = {key: summary[key] for key in ("blocked", "clear", "waiting")}
    return out + [{"version": None, **live}] if out else []
