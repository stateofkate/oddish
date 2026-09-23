"""Which path a claimed trial job takes: the in-sandbox monitor or the worker.

Pure: the decision is a function of the trial id, the attempt number and two
settings, so a trial always gets the same answer and a log line can say why.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

BUCKETS = 100


@dataclass(frozen=True, slots=True)
class Route:
    monitor: bool
    reason: str

    @property
    def name(self) -> str:
        return "monitor" if self.monitor else "worker"


def bucket_for(trial_id: str) -> int:
    """0..99, uniform over trial ids, stable across processes and time."""
    return zlib.crc32(trial_id.encode("utf-8")) % BUCKETS


def monitor_route(
    trial_id: str, attempt: int, *, percent: int, fallback_after: int
) -> Route:
    """``percent`` of trials (by bucket) go to the monitor for their first
    ``fallback_after`` attempts; everything else runs on the worker. At 100
    percent the fallback is off: every attempt runs on the monitor."""
    if percent >= BUCKETS:
        return Route(True, "percent=100")
    if percent <= 0:
        return Route(False, "percent=0")
    if attempt > fallback_after:
        return Route(False, f"fallback attempt={attempt}>{fallback_after}")
    bucket = bucket_for(trial_id)
    if bucket < percent:
        return Route(True, f"sampled bucket={bucket} percent={percent}")
    return Route(False, f"not-sampled bucket={bucket} percent={percent}")
