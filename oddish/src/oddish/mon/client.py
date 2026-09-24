"""HTTP calls to the control plane's ``/monitor`` routes. Nothing else.

The token travels only in the Authorization header, never in a URL or a log
line. 410 means this attempt no longer owns the job and must stop; 401 means
the credential is bad. Any other 4xx from our API is a refusal, not a verdict;
only the result is retried through one, as a rollback or hotfix can lift it.
Everything else that fails (5xx, transport, an edge page) is an outage the
caller retries.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from oddish.mon.contract import (
    ArtifactUploadResponse,
    AttemptSpec,
    MonitorStats,
    ResultPayload,
)
from oddish.mon.env import MonitorEnv
from oddish.mon.status import MonitorPhase, write_status

logger = logging.getLogger(__name__)


class AttemptGone(Exception):
    """The control plane says this attempt is over (HTTP 410). Stop the trial."""


class MonitorRejected(Exception):
    """The control plane refused the credential (HTTP 401)."""


class ControlPlaneUnavailable(Exception):
    """Transport error or 5xx. Safe to retry: every route is idempotent."""


class ControlPlaneRefused(Exception):
    """Our API refused the request with a 4xx other than 401/410. Not a
    verdict: the control plane does not know how the attempt ended. Only the
    result is retried through one, in the outage's window."""


def _is_from_our_api(response: httpx.Response) -> bool:
    """FastAPI errors are JSON objects with a ``detail``; an edge or proxy page is not."""
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and "detail" in body


class MonitorClient:
    def __init__(
        self, env: MonitorEnv, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        self._http = httpx.AsyncClient(
            base_url=f"{env.api_url}/monitor",
            headers={"Authorization": f"Bearer {env.token}"},
            timeout=httpx.Timeout(30.0, connect=10.0),
            transport=transport,
        )
        self._trial_id = ""
        # Consecutive outages per route, for the log line of each one.
        self._failures: dict[str, int] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    def _unavailable(
        self, route: str, reason: str, body: str = ""
    ) -> ControlPlaneUnavailable:
        count = self._failures[route] = self._failures.get(route, 0) + 1
        logger.warning(
            "control plane unavailable: %s -> %s (failure %d in a row): %s",
            route,
            reason,
            count,
            body[:200],
        )
        return ControlPlaneUnavailable(reason)

    async def _call(self, method: str, path: str, json: Any = None) -> dict[str, Any]:
        route = f"{method} {path}"
        try:
            response = await self._http.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise self._unavailable(route, type(exc).__name__) from None
        # Only our own API can end an attempt. During a deploy the platform's
        # edge answers for it (404, 502, an HTML page): that is an outage, not a
        # verdict, and the trial must outlive it as the worker does today.
        if response.status_code in (401, 410) and _is_from_our_api(response):
            # Recorded before anything else can fail: from here on the control
            # plane knows how the attempt ended, and the entrypoint must not
            # report it as a failed start even if this process is killed.
            write_status(trial_id=self._trial_id, phase=MonitorPhase.STOPPED)
            if response.status_code == 410:
                raise AttemptGone(response.text[:200])
            raise MonitorRejected("monitor credential refused")
        if 400 <= response.status_code < 500 and _is_from_our_api(response):
            logger.error(
                "control plane refused %s: HTTP %s: %s",
                route,
                response.status_code,
                response.text[:200],
            )
            raise ControlPlaneRefused(f"{route}: HTTP {response.status_code}")
        if response.status_code >= 400:
            reason = f"HTTP {response.status_code}"
            raise self._unavailable(route, reason, response.text)
        try:
            reply = response.json()
        except ValueError:
            raise self._unavailable(route, "non-JSON reply", response.text) from None
        self._failures.pop(route, None)
        return reply

    async def attempt(self) -> AttemptSpec:
        spec = AttemptSpec.model_validate(await self._call("GET", "/attempt"))
        self._trial_id = spec.trial_id
        return spec

    async def heartbeat(
        self, *, pending_failure_count: int = 0, pending_last_error: str | None = None
    ) -> None:
        await self._call(
            "POST",
            "/heartbeat",
            {
                "pending_failure_count": pending_failure_count,
                "pending_last_error": pending_last_error,
            },
        )

    async def control(self) -> dict[str, Any]:
        return await self._call("GET", "/control")

    async def lifecycle(self, summary: dict[str, Any]) -> None:
        await self._call("POST", "/lifecycle", {"summary": summary})

    async def events(
        self,
        *,
        trial_attempt: int,
        events: list[dict[str, Any]],
        checkpoint: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return await self._call(
            "POST",
            "/events",
            {
                "trial_attempt": trial_attempt,
                "events": events,
                "checkpoint": checkpoint,
            },
        )

    async def artifact_upload(self) -> ArtifactUploadResponse:
        reply = await self._call("POST", "/artifacts")
        return ArtifactUploadResponse.model_validate(reply)

    async def result(
        self,
        *,
        outcome: dict[str, Any] | None,
        archive_uploaded: bool,
        execution_error: str | None = None,
        retryable: bool = True,
        monitor_stats: MonitorStats | None = None,
    ) -> dict[str, Any]:
        payload = ResultPayload(
            outcome=outcome,
            archive_uploaded=archive_uploaded,
            execution_error=execution_error,
            retryable=retryable,
            monitor_stats=monitor_stats,
        )
        return await self._call("POST", "/result", payload.model_dump())
