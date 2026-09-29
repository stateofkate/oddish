import asyncpg
import pytest

from oddish.db import connection


@pytest.fixture
def driver(monkeypatch):
    """Replace asyncpg.connect with a scripted fake and record every call."""
    calls: list[tuple[tuple, dict]] = []
    outcomes: list[BaseException | object] = []

    async def fake_connect(*args, **kwargs):
        calls.append((args, kwargs))
        outcome = outcomes.pop(0) if outcomes else TimeoutError()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    monkeypatch.setattr(connection, "_CONNECT_RETRY_DELAY_SECONDS", 0)
    return calls, outcomes


@pytest.mark.asyncio
async def test_transient_connect_failures_are_retried(driver):
    calls, outcomes = driver
    conn = object()
    outcomes += [TimeoutError(), asyncpg.ConnectionFailureError("stall"), conn]

    assert await connection.connect_with_retry("postgresql://db", timeout=10) is conn
    assert calls == [(("postgresql://db",), {"timeout": 10})] * 3


@pytest.mark.asyncio
async def test_connect_gives_up_after_last_attempt(driver):
    calls, _ = driver

    with pytest.raises(TimeoutError):
        await connection.connect_with_retry("postgresql://db")
    assert len(calls) == connection._CONNECT_ATTEMPTS


@pytest.mark.asyncio
async def test_rejected_credentials_are_not_retried(driver):
    calls, outcomes = driver
    outcomes.append(asyncpg.InvalidPasswordError("bad password"))

    with pytest.raises(asyncpg.InvalidPasswordError):
        await connection.connect_with_retry("postgresql://db")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_engine_connects_retry_with_url_and_connect_args(driver, monkeypatch):
    calls, _ = driver
    monkeypatch.setattr(
        connection, "db_url", "postgresql+asyncpg://u:p@db.invalid:6543/oddish"
    )
    engine = connection._create_engine()
    try:
        with pytest.raises(TimeoutError):
            async with engine.connect():
                pass
    finally:
        await engine.dispose()

    assert len(calls) == connection._CONNECT_ATTEMPTS
    _, kwargs = calls[0]
    assert kwargs["host"] == "db.invalid"
    assert kwargs["port"] == 6543
    assert kwargs["user"] == "u"
    assert kwargs["database"] == "oddish"
    assert kwargs["timeout"] == 10
    assert kwargs["command_timeout"] == 30


@pytest.mark.asyncio
async def test_pool_connects_retry(driver, monkeypatch):
    calls, _ = driver
    monkeypatch.setattr(connection, "_pool", None)

    with pytest.raises(TimeoutError):
        await connection.get_pool()
    assert len(calls) == connection._CONNECT_ATTEMPTS
