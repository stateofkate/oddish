"""Tell the control plane the monitor died before it could report.

The entrypoint runs this when ``oddish.mon run`` exits before the control
plane knows how the attempt ended (status file not "done" or "stopped"; without
one, an exit other than EXIT_ACCEPTED or EXIT_STOPPED from
``oddish.mon.exit_codes``), or when Docker never came up. The message is a failed, retryable result under the
attempt's own credential: the launcher's wait ends now instead of at its
deadline, and the attempt retries through the same settlement as any other
failure. Best effort: a control plane that cannot be reached, or an attempt
that is already over, is logged and dropped; the launcher's deadline and the
reconciler remain the backstop.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path

import httpx

from oddish.mon import env as monitor_env
from oddish.mon.client import (
    AttemptGone,
    ControlPlaneUnavailable,
    MonitorClient,
    MonitorRejected,
)

logger = logging.getLogger(__name__)

# Enough of each log to say why, not enough to flood ``trials.error_message``.
LOG_TAIL_CHARS = 1500
DEFAULT_LOG_PATHS: tuple[str, ...] = (
    "/var/log/oddish-mon.log",
    "/var/log/dockerd.log",
)
ATTEMPTS = 5
DELAY_SECONDS = 5.0


def _tail(path: Path) -> str:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    return text[-LOG_TAIL_CHARS:].strip()


def describe(exit_code: int, log_paths: Sequence[str] = DEFAULT_LOG_PATHS) -> str:
    """The execution error the attempt settles with: why, then the logs' tails."""
    parts = [
        f"Monitor startup failed: oddish-mon exited {exit_code} "
        "before reporting a result"
    ]
    for path in log_paths:
        tail = _tail(Path(path))
        if tail:
            parts.append(f"--- {path} ---\n{tail}")
    return "\n".join(parts)


async def report(
    *,
    exit_code: int,
    environ: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    attempts: int = ATTEMPTS,
    delay: float = DELAY_SECONDS,
    log_paths: Sequence[str] = DEFAULT_LOG_PATHS,
) -> int:
    """Post the failed result. Always returns 0: the entrypoint exits with the
    monitor's own status, and nothing here may mask it."""
    try:
        loaded = monitor_env.load(environ)
    except monitor_env.MonitorEnvError as exc:
        logger.warning("no credential to report a failed start with: %s", exc)
        return 0
    error = describe(exit_code, log_paths)
    client = MonitorClient(loaded, transport=transport)
    try:
        for attempt in range(1, attempts + 1):
            try:
                reply = await client.result(
                    outcome=None,
                    archive_uploaded=False,
                    execution_error=error,
                    retryable=True,
                )
            except ControlPlaneUnavailable as exc:
                logger.warning(
                    "failed start not delivered (%s/%s): %s", attempt, attempts, exc
                )
                if attempt < attempts:
                    await asyncio.sleep(delay)
                continue
            except (AttemptGone, MonitorRejected) as exc:
                logger.info("failed start not reported; the attempt is over: %s", exc)
                return 0
            logger.info(
                "failed start reported (duplicate=%s)", bool(reply.get("duplicate"))
            )
            return 0
        logger.warning("failed start never delivered; the deadline will end it")
    finally:
        await client.aclose()
    return 0
