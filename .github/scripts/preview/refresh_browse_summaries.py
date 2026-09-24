"""Manual repair: recalculate all preview counters from the preview's trials.

Normal deploys do not run this full scan. First setup refreshes only the task
versions with copied active trials that it stops, inside its transaction.
"""

import asyncio
import sys

from bootstrap_preview_db import (
    _assert_preview_branch,
    _branch_db_url,
    _engine,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker


async def refresh_browse_summaries(engine: AsyncEngine) -> int:
    """Rebuild counters from recorded trials, in batches of 200 versions."""
    from oddish.core.task_browse_summary import refresh_task_browse_summaries
    from oddish.db import TaskBrowseSummaryModel, TaskVersionModel

    sessions = async_sessionmaker(engine)
    last_id = ""
    refreshed = 0
    missing = 0
    while True:
        async with sessions.begin() as session:
            rows = (
                await session.execute(
                    select(TaskVersionModel.id, TaskBrowseSummaryModel.task_version_id)
                    .outerjoin(
                        TaskBrowseSummaryModel,
                        TaskBrowseSummaryModel.task_version_id == TaskVersionModel.id,
                    )
                    .where(TaskVersionModel.id > last_id)
                    .order_by(TaskVersionModel.id)
                    .limit(200)
                )
            ).all()
            if not rows:
                break
            missing += sum(summary_id is None for _, summary_id in rows)
            await refresh_task_browse_summaries(session, [row.id for row in rows])
            last_id = rows[-1].id
            refreshed += len(rows)
    print(
        f"Refreshed {refreshed} browse summaries ({missing} were missing)",
        file=sys.stderr,
    )
    return refreshed


async def main() -> None:
    url = _branch_db_url()
    _assert_preview_branch(url)
    engine = _engine(url)
    try:
        await refresh_browse_summaries(engine)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
