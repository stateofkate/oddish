"""``memory_mb`` means what it meant on the worker path: not a container cap.

Harbor's Daytona environment wrote a task's ``memory_mb`` as a compose
reservation, which never capped the container: the sandbox was the cap. The
Docker environment the monitor runs resolves the default ``auto`` policy as a
hard ``mem_limit`` (auto -> LIMIT), and cannot express a reservation at all
(its capabilities are limits only). Same task file, same number, different
meaning: QA40 task 11's Erlang compile thrashed at 4096 of 4096 MiB for eleven
minutes before the monitor was killed.

The monitor pins the memory policy to ``ignore`` when the task or trial has not
chosen one, so the container is again uncapped and the sandbox, sized from the
same number, is the cap. CPU keeps Harbor's default (a limit); it was never
the failure. An explicit policy in the stored config is respected.
"""

from __future__ import annotations

from typing import Any

UNCAPPED = "ignore"
_POLICY = "memory_enforcement_policy"


def with_reservation_semantics(harbor_config: dict[str, Any] | None) -> dict[str, Any]:
    """The trial's Harbor config with an unset/auto memory policy pinned to
    ``ignore``. Returns a new dict; the input is not modified."""
    config = dict(harbor_config or {})
    environment = dict(config.get("environment") or {})
    value = environment.get(_POLICY)
    if value is None or str(value).lower() == "auto":
        environment[_POLICY] = UNCAPPED
    config["environment"] = environment
    return config
