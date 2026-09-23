"""The two loops that run beside the driver, at the worker's cadences."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass

from oddish.mon.client import AttemptGone, MonitorClient, MonitorRejected
from oddish.mon.seams import ControlState
from oddish.mon.status import write_status

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 30.0
CONTROL_SECONDS = 15.0


@dataclass
class LoopStats:
    """What the two loops delivered. One summary line in the monitor's log, and
    the same numbers ride along in the result the monitor already posts."""

    heartbeats_sent: int = 0
    heartbeats_failed: int = 0
    control_checks: int = 0
    control_failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)

    def summary(self) -> str:
        return " ".join(f"{key}={value}" for key, value in self.as_dict().items())


class StopTrial(Exception):
    """The control plane ended this attempt: stop driving and do not report."""


async def state_stopped(state: ControlState) -> None:
    """Wait until a call outside the loops (the live tail's persist tick) got
    our API's 410/401, then raise StopTrial, as the loops do for their own."""
    await state.ended.wait()
    raise StopTrial(str(state.verdict)) from state.verdict


async def heartbeat_loop(
    client: MonitorClient,
    *,
    interval: float = HEARTBEAT_SECONDS,
    stats: LoopStats | None = None,
) -> None:
    stats = stats or LoopStats()
    failures, last_error = 0, None
    while True:
        try:
            await client.heartbeat(
                pending_failure_count=failures, pending_last_error=last_error
            )
            stats.heartbeats_sent += 1
            if failures:
                logger.info("heartbeat recovered after %d missed", failures)
            failures, last_error = 0, None
            if stats.heartbeats_sent % 2 == 0:
                # Sizing evidence between phases: a sandbox that dies
                # mid-verifier leaves no phase event, but it leaves this.
                from oddish.mon import memstat

                logger.info(
                    "%s at beat %d",
                    memstat.snapshot(with_containers=False),
                    stats.heartbeats_sent,
                )
        except AttemptGone as exc:
            raise StopTrial(str(exc)) from exc
        except MonitorRejected as exc:
            raise StopTrial(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            # Unreachable, or anything unexpected, is not a verdict. Keep
            # driving; report the gap later. Only our API can stop the trial.
            failures, last_error = failures + 1, str(exc) or type(exc).__name__
            stats.heartbeats_failed += 1
            logger.warning("heartbeat not delivered (%d in a row): %s", failures, exc)
        await asyncio.sleep(interval)


async def control_loop(
    client: MonitorClient,
    state: ControlState,
    *,
    interval: float = CONTROL_SECONDS,
    stats: LoopStats | None = None,
    trial_id: str = "",
) -> None:
    stats = stats or LoopStats()
    while True:
        try:
            answer = await client.control()
            stats.control_checks += 1
            if not answer.get("authorized", True):
                # Recorded where it is learned, as the client records a
                # 410/401: the tail flush that follows can outlast a kill.
                write_status(trial_id=trial_id, phase="stopped")
                raise StopTrial(
                    answer.get("detail") or "execution is no longer authorized"
                )
            state.pause_requested = bool(answer.get("pause_requested"))
        except AttemptGone as exc:
            raise StopTrial(str(exc)) from exc
        except (StopTrial, MonitorRejected) as exc:
            raise StopTrial(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - same rule as the heartbeat
            stats.control_failed += 1
            logger.warning("control check not delivered: %s", exc)
        await asyncio.sleep(interval)
