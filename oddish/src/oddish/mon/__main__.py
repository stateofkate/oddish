"""``python -m oddish.mon run`` drives an attempt; ``status`` prints one JSON line;
``warm`` prepares a task's images in a warm-up sandbox; ``startup-failed <code>``
tells the control plane the monitor died before it could report."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

from oddish.mon import env
from oddish.mon.exit_codes import EXIT_FAILED
from oddish.mon.status import read_status

logger = logging.getLogger(__name__)

LOG_PATH_VAR = "ODDISH_MON_LOG_PATH"
DEFAULT_LOG_PATH = "/var/log/oddish-mon.log"


def configure_logging() -> None:
    """One line per event to stdout (the provider's sandbox log) and to a file
    that is shipped with the trial's artifacts, so it outlives the sandbox.

    Level comes from ``ODDISH_MON_LOG_LEVEL`` (default INFO). HTTP client
    loggers are held at WARNING: at INFO they print every request URL, and
    presigned URLs carry a signature.
    """
    level = os.environ.get("ODDISH_MON_LOG_LEVEL", "INFO").upper()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(
            logging.FileHandler(os.environ.get(LOG_PATH_VAR, DEFAULT_LOG_PATH))
        )
    except OSError:
        pass
    logging.basicConfig(
        level=level,
        format="%(asctime)s oddish-mon %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    for noisy in ("httpx", "httpcore", "urllib3", "botocore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:] or ["run"]
    command = args[0]
    if command == "status":
        status = read_status()
        print(json.dumps(status))
        return 0 if status.get("alive") else 1
    if command == "run":
        configure_logging()
        try:
            # Settings reads os.environ once, when the runner's imports first
            # load oddish.config: unpack the model credentials before that.
            os.environ.update(env.load(dict(os.environ)).agent_env)
            from oddish.mon.runner import run

            return asyncio.run(run())
        except Exception:  # noqa: BLE001 - the traceback must reach the log file
            # Python prints an unhandled traceback to stderr, which is not in
            # the monitor log the startup-failure report tails. Log it through
            # the handlers (file + stdout) so a failed start says why.
            logger.exception("monitor run failed")
            return EXIT_FAILED
    if command == "warm":
        configure_logging()
        from oddish.mon.warm import run as warm

        return asyncio.run(warm())
    if command == "startup-failed":
        configure_logging()
        from oddish.mon.startup_failure import report

        exit_code = int(args[1]) if len(args) > 1 else EXIT_FAILED
        return asyncio.run(report(exit_code=exit_code))
    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
