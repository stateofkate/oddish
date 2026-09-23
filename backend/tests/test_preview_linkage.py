"""Real Postgres equivalence checks and a same-run before/after benchmark."""

import math
import os
from pathlib import Path
import statistics
import time

import pytest
import pytest_asyncio
from sqlalchemy import Column, ForeignKey, MetaData, String, Table, event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from preview_seed import _apply_linkage

URL = os.environ.get("ODDISH_DATABASE_URL")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not URL, reason="Postgres required"),
]


@pytest_asyncio.fixture
async def database():
    engine = create_async_engine(URL)
    md = MetaData()
    versions = Table(
        "link_versions",
        md,
        Column("id", String, primary_key=True),
        prefixes=["TEMPORARY"],
    )
    tasks = Table(
        "link_tasks",
        md,
        Column("id", String, primary_key=True),
        Column("current_version_id", String, ForeignKey(versions.c.id)),
        prefixes=["TEMPORARY"],
    )
    trials = Table(
        "link_trials",
        md,
        Column("id", String, primary_key=True),
        Column("superseded_by_trial_id", String, ForeignKey("link_trials.id")),
        prefixes=["TEMPORARY"],
    )
    try:
        async with engine.connect() as conn:
            async with conn.begin():
                await conn.run_sync(md.create_all)
                for table, count in [(versions, 5000), (tasks, 5000), (trials, 1000)]:
                    await conn.execute(
                        table.insert(), [{"id": str(i)} for i in range(count)]
                    )
                yield conn, md
                # Only temporary tables in this connection; never reset a shared schema.
    finally:
        await engine.dispose()


async def _serial(conn, md, linkage):
    """The pre-change loop, retained only as the benchmark/equivalence baseline."""
    for table_name, row_id, column, value in linkage:
        table = md.tables[table_name]
        await conn.execute(
            table.update()
            .where(table.c.id == row_id)
            .where(table.c[column].is_distinct_from(value))
            .values(**{column: value})
        )


def _links(tasks=5000, trials=1000):
    return [
        ("link_tasks", str(i), "current_version_id", str(i)) for i in range(tasks)
    ] + [
        ("link_trials", str(i), "superseded_by_trial_id", str((i + 1) % 1000))
        for i in range(trials)
    ]


async def _snapshot(conn, md):
    return [
        list(
            (
                await conn.execute(
                    select(md.tables[name]).order_by(md.tables[name].c.id)
                )
            ).all()
        )
        for name in ("link_tasks", "link_trials")
    ]


async def test_linkage_matches_serial_and_skips_unchanged(database):
    conn, md = database
    await conn.execute(
        md.tables["link_tasks"]
        .update()
        .where(md.tables["link_tasks"].c.id.in_(["0", "1"]))
        .values(current_version_id="0")
    )
    before = await conn.scalar(text("SELECT ctid::text FROM link_tasks WHERE id='0'"))
    links = _links(2001, 1000) + [
        ("link_tasks", "1", "current_version_id", None),
        ("link_tasks", "missing", "current_version_id", "0"),
    ]
    async with conn.begin_nested() as transaction:
        await _serial(conn, md, links)
        expected = await _snapshot(conn, md)
        await transaction.rollback()
    await _apply_linkage(conn, md, links)
    assert await _snapshot(conn, md) == expected
    assert (
        await conn.scalar(text("SELECT ctid::text FROM link_tasks WHERE id='0'"))
        == before
    )
    await _apply_linkage(conn, md, links)
    assert await _snapshot(conn, md) == expected


async def test_linkage_failure_rolls_back_all_batches(database):
    conn, md = database
    before = await _snapshot(conn, md)
    with pytest.raises(IntegrityError):
        async with conn.begin_nested():
            await _apply_linkage(
                conn,
                md,
                _links(1001, 0)
                + [("link_tasks", "1001", "current_version_id", "invalid")],
            )
    assert await _snapshot(conn, md) == before


async def test_empty_linkage_issues_no_sql(database):
    conn, md = database
    statements = []

    def record(*args):
        statements.append(args[2])

    event.listen(conn.sync_connection, "before_cursor_execute", record)
    try:
        await _apply_linkage(conn, md, [])
    finally:
        event.remove(conn.sync_connection, "before_cursor_execute", record)
    assert statements == []


async def test_linkage_benchmark(database):
    conn, md = database
    links = _links()
    timings = {"serial": [], "batched": []}
    counts = {}
    expected = None
    # Warm both implementations, then alternate order across three measured
    # rounds. Every run starts from the same rows using a rolled-back savepoint.
    for round_number in range(4):
        methods = [("serial", _serial), ("batched", _apply_linkage)]
        if round_number % 2:
            methods.reverse()
        for name, method in methods:
            async with conn.begin_nested() as transaction:
                statements = []

                def record(*args):
                    statements.append(args[2])

                event.listen(conn.sync_connection, "before_cursor_execute", record)
                try:
                    started = time.perf_counter()
                    await method(conn, md, links if round_number else links[:10])
                    elapsed = time.perf_counter() - started
                finally:
                    event.remove(conn.sync_connection, "before_cursor_execute", record)
                if round_number:
                    timings[name].append(elapsed)
                    counts[name] = len(statements)
                    snapshot = await _snapshot(conn, md)
                    if expected is None:
                        expected = snapshot
                    assert snapshot == expected
                await transaction.rollback()
    assert counts == {
        "serial": len(links),
        "batched": math.ceil(5000 / 1000) + math.ceil(1000 / 1000),
    }
    old, new = (statistics.median(timings[name]) for name in ("serial", "batched"))
    report = (
        "### Preview relationship benchmark\n\n"
        "6,000 synthetic relationships (5,000 task/version + 1,000 trial links), "
        "real local Postgres, same connection and initial rows, median of three "
        "alternating runs after warmup. Measures updates only, without commit, "
        "sampling, or schema restoration; no artificial network latency.\n\n"
        f"| Method | Median seconds | SQL statements |\n|---|---:|---:|\n"
        f"| Previous serial loop | {old:.4f} | {counts['serial']} |\n"
        f"| Batched updates | {new:.4f} | {counts['batched']} |\n\n"
        f"**{old / new:.1f}x faster; {100 * (1 - new / old):.1f}% less time.** "
        "Identical final relationships verified on every measured run.\n"
    )
    print(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as output:
            output.write(report)
