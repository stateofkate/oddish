"""Where a warm-up's time goes: one ``metric=monitor_warm_timing`` line per try.

``image_cache.resolve`` starts a timing for each try (``begin``) and prints it
with the try's outcome (``emit``). The provider's ``build_warm_image`` times
the phases it runs itself, in the launcher (``current().enter``):

- ``create_s``: the warm-up sandbox's create call. It includes the provider
  building or pulling the monitor image when the provider has not cached it,
  and on Modal the app lookup.
- ``exec_s``: ``mon-entrypoint.sh warm`` in the sandbox, start to exit: Docker's
  start and stop around ``python -m oddish.mon warm``, and that command.
- ``snapshot_s``: the disk snapshot.

``fetch_s`` and ``build_s`` are measured inside the sandbox, by the warm-up
itself (``oddish.mon.warm``), which prints them as its last log line; the
provider reads them from the output it gets back with the exit code
(``read_phases``). ``build_s`` is everything after the fetch: the task image or
Compose build, the pulls and the egress sidecar. Both are ``0.00`` when the
warm-up printed nothing (killed, cancelled, never started).

``total_s`` is the whole try as ``resolve`` sees it: signing the task links,
the phases above and removing the warm-up sandbox. A phase cut short by an
error or a cancel runs to the end of the try, the sandbox's removal included.
"""

from __future__ import annotations

import logging
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field

from oddish.mon.contract import WARM_PHASES_METRIC

logger = logging.getLogger(__name__)

_PHASES = re.compile(rf"metric={WARM_PHASES_METRIC} fetch_s=([\d.]+) build_s=([\d.]+)")


@dataclass
class WarmTiming:
    key: str | None = None
    provider: str | None = None
    attempt: int = 0
    create_s: float = 0.0
    fetch_s: float = 0.0
    build_s: float = 0.0
    exec_s: float = 0.0
    snapshot_s: float = 0.0
    started: float = field(default_factory=time.monotonic)
    _phase: str | None = None
    _phase_started: float = 0.0

    def enter(self, phase: str) -> None:
        """Start timing ``phase`` (``create_s``, ``exec_s``, ``snapshot_s``),
        ending the phase before it."""
        self.stop()
        self._phase, self._phase_started = phase, time.monotonic()

    def stop(self) -> None:
        """End the phase being timed, if any. A provider ends each phase
        itself, so the sandbox's removal counts only in ``total_s``."""
        if self._phase is not None:
            elapsed = time.monotonic() - self._phase_started
            setattr(self, self._phase, getattr(self, self._phase) + elapsed)
            self._phase = None

    def read_phases(self, output: str | None) -> None:
        """Take ``fetch_s`` and ``build_s`` from the warm-up's output: its
        last ``metric=monitor_warm_phases`` line. Nothing found: left at 0."""
        found = _PHASES.findall(output or "")
        if found:
            self.fetch_s, self.build_s = (float(v) for v in found[-1])

    def emit(self, outcome: str) -> None:
        """Print the try's line. ``outcome``: snapshot, nothing_to_warm,
        failed or cancelled."""
        self.stop()
        logger.info(
            "metric=monitor_warm_timing key=%s provider=%s try=%d create_s=%.2f "
            "fetch_s=%.2f build_s=%.2f exec_s=%.2f snapshot_s=%.2f total_s=%.2f "
            "outcome=%s",
            self.key,
            self.provider,
            self.attempt,
            self.create_s,
            self.fetch_s,
            self.build_s,
            self.exec_s,
            self.snapshot_s,
            time.monotonic() - self.started,
            outcome,
        )
        if _current.get() is self:
            _current.set(None)


_current: ContextVar[WarmTiming | None] = ContextVar("warm_timing", default=None)


def begin(key: str, provider: str, attempt: int) -> WarmTiming:
    """Time one warm-up try; the provider's ``build_warm_image`` finds it
    with ``current``."""
    timing = WarmTiming(key=key, provider=str(provider), attempt=attempt)
    _current.set(timing)
    return timing


def current() -> WarmTiming:
    """The try ``resolve`` is timing, or a timing nobody prints when the
    provider is called on its own."""
    return _current.get() or WarmTiming()
