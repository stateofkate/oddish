"""Worker-side packages. The exports below load on first use, so importing a
subpackage (``oddish.workers.harbor`` inside the monitor sandbox) does not pull
in the queue and its database stack."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oddish.workers.harbor.runner import HarborOutcome
    from oddish.workers.queue import run_polling_worker

__all__ = [
    "HarborOutcome",
    "run_polling_worker",
]


def __getattr__(name: str):
    if name == "HarborOutcome":
        from oddish.workers.harbor.runner import HarborOutcome

        return HarborOutcome
    if name == "run_polling_worker":
        from oddish.workers.queue import run_polling_worker

        return run_polling_worker
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
