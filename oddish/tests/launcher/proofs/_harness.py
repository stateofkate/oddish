"""Shared boot for the proof scripts, which run as subprocesses of
``test_monitor_heartbeat_real_db.py`` against a throwaway Postgres.

Importing this module puts ``src`` on the path and selects the null pool, as
the test conftest does; ``boot()`` binds a fresh engine to the loop that
calls it and creates the schema (every table but ``api_keys``).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from oddish.config import Settings  # noqa: E402

Settings.db_use_null_pool = True

import oddish.db.connection as _conn  # noqa: E402
from oddish.db.models import Base  # noqa: E402


async def boot() -> None:
    _conn.engine = _conn._create_engine()
    _conn.async_session_maker = _conn._create_session_maker(_conn.engine)
    tables = [t for name, t in Base.metadata.tables.items() if name != "api_keys"]
    async with _conn.engine.begin() as connection:
        await connection.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables)
        )


async def dispose() -> None:
    await _conn.engine.dispose()
