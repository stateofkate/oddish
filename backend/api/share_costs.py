"""Blank cost figures on share routes for orgs whose shares hide them.

``set_share_viewer`` decides per request (``share_hides_costs``); this zeroes
every numeric ``*cost*`` field in the JSON or NDJSON body, so no response
builder has to know about customer shares.
"""

from __future__ import annotations

import json
from typing import Any

from oddish.core.sharing.helpers import share_hides_costs


def strip_costs(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                0
                if "cost" in key
                and isinstance(item, (int, float))
                and not isinstance(item, bool)
                else strip_costs(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [strip_costs(item) for item in value]
    return value


def _strip_line(line: bytes) -> bytes:
    """Rewrite one JSON document; pass through anything that is not JSON (a
    trial's own .json file can be malformed)."""
    try:
        return json.dumps(strip_costs(json.loads(line))).encode()
    except ValueError:
        return line


class ShareCostsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # File downloads pass through untouched: rewriting would alter the
        # artifact, and the same files are reachable via presigned S3 URLs.
        path = scope.get("path", "")
        if scope["type"] != "http" or "/public/" not in path or "/files/" in path:
            await self.app(scope, receive, send)
            return
        state: dict[str, Any] = {"hide": False, "ndjson": False, "buffer": b""}

        async def wrapped_send(message):
            if message["type"] == "http.response.start":
                headers = dict(message.get("headers") or [])
                content_type = headers.get(b"content-type", b"")
                state["hide"] = share_hides_costs.get() and b"json" in content_type
                if state["hide"]:
                    state["ndjson"] = b"ndjson" in content_type
                    message = {
                        **message,
                        "headers": [
                            (k, v)
                            for k, v in message.get("headers") or []
                            if k.lower() != b"content-length"
                        ],
                    }
                await send(message)
                return
            if message["type"] != "http.response.body" or not state["hide"]:
                await send(message)
                return
            state["buffer"] += message.get("body", b"")
            more = message.get("more_body", False)
            if state["ndjson"]:
                *lines, state["buffer"] = state["buffer"].split(b"\n")
                body = b"".join(_strip_line(line) + b"\n" for line in lines)
                if not more:
                    body += _strip_line(state["buffer"])
            elif more:
                return
            else:
                body = _strip_line(state["buffer"])
            await send({"type": "http.response.body", "body": body, "more_body": more})

        await self.app(scope, receive, wrapped_send)
