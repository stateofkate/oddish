"""The monitor's input contract: what one attempt has to run.

Shared by the API (which builds it from the trial and task rows the worker
reads today) and the monitor (which fetches it with its attempt credential).
It carries no secrets: credentials reach the monitor through its environment.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

# POST /monitor/events accepts at most this many events per request
# (EventsRequest.events max_length in backend/api/routers/monitor.py).
MAX_EVENTS_PER_POST = 1000


class AttemptSpec(BaseModel):
    trial_id: str
    worker_job_id: str
    worker_job_attempt: int
    trial_attempt: int
    trial_kind: str
    task_id: str
    task_name: str
    agent: str
    model: str | None
    harbor_config: dict[str, Any] | None
    org_id: str | None
    billed_user_id: str | None
    # How to fetch the task, in the same forms the worker can fetch it today:
    # one archive to extract, or loose files keyed by path relative to the task
    # directory. Values are presigned GET URLs.
    task_archive_url: str | None = None
    task_files: dict[str, str] = {}


class WarmRequest(BaseModel):
    """What a warm-up sandbox needs: where the task is, and its prebuilt image
    if it has one. No token, no model keys, no registry login."""

    task_archive_url: str | None = None
    task_files: dict[str, str] = {}
    docker_image: str | None = None


def summarize_hook_event(hook_event: Any) -> dict[str, Any]:
    """The JSON-safe fields of a hook event that the handler reads."""
    summary: dict[str, Any] = {"event": hook_event.event.value}
    timestamp = getattr(hook_event, "timestamp", None)
    if isinstance(timestamp, datetime):
        summary["timestamp"] = timestamp.isoformat()
    result = getattr(hook_event, "result", None)
    if result is not None:
        verifier = getattr(result, "verifier_result", None)
        exc = getattr(result, "exception_info", None)
        summary["result"] = {
            "verifier_result": (
                {"rewards": dict(getattr(verifier, "rewards", None) or {})}
                if verifier is not None
                else None
            ),
            "exception_info": (
                {
                    "exception_type": getattr(exc, "exception_type", None),
                    "exception_message": getattr(exc, "exception_message", None),
                    "provider_error_code": getattr(exc, "provider_error_code", None),
                }
                if exc is not None
                else None
            ),
        }
    return summary
