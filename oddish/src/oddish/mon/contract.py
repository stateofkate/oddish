"""What the launcher, the control plane and the in-sandbox monitor agree on.

The attempt spec (built by the API from the trial and task rows the worker
reads today, fetched by the monitor with its attempt credential), the names of
the environment variables the launcher sets and the monitor reads, the result
the monitor posts, and the lifecycle-event summary it sends. Pure: no database,
no Harbor, no HTTP, no process environment. It carries no secrets: credentials
reach the monitor through its environment, under the names below.

The /monitor formats are additive only: the API must accept any monitor deployed
in the last 12 hours, because a monitor keeps its code for its whole trial.
backend/tests/test_monitor_contract.py checks this against a recorded snapshot.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# Environment the launcher gives the monitor sandbox. ``mon.env`` reads them
# once and removes the secret ones from the process environment.
API_URL_VAR = "ODDISH_MON_API_URL"
TOKEN_VAR = "ODDISH_MON_TOKEN"
AGENT_ENV_VAR = "ODDISH_MON_AGENT_ENV"  # JSON object: env for the agent only
REGISTRY_AUTH_VAR = "ODDISH_MON_REGISTRY_AUTH"  # JSON list: the run's registry login
# A snapshot boot's code bundle, for scripts/mon-boot.sh: in the environment,
# never the start command, because the URL is presigned.
BUNDLE_URL_VAR = "ODDISH_MON_BUNDLE_URL"
BUNDLE_SHA256_VAR = "ODDISH_MON_BUNDLE_SHA256"
WARM_VAR = "ODDISH_MON_WARM"  # a WarmRequest as JSON, for ``oddish.mon warm``
# ``oddish.mon warm`` exits NOTHING_TO_WARM when it built nothing and the task
# names no image to pull, which is not a failure; WARM_FAILED when it built
# nothing and every pull failed.
NOTHING_TO_WARM = 3
WARM_FAILED = 1
# ``oddish.mon warm`` ends its output with ``metric=<this> fetch_s=.. build_s=..``;
# the launcher reads it back for ``metric=monitor_warm_timing``.
WARM_PHASES_METRIC = "monitor_warm_phases"

# POST /monitor/events accepts at most this many events per request.
MAX_EVENTS_PER_POST = 1000

# ``task_files_version`` is a sha256 hex digest cut to this many digits: the
# launcher cuts it, and the monitor keeps a task copy only under that shape.
TASK_FILES_VERSION_LENGTH = 32


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
    # Names exactly the files above (``attempt.task_files_version``). A warm
    # snapshot keeps its task under this name; the monitor uses that copy
    # instead of fetching when the names match. None: always fetch.
    task_files_version: str | None = None


class WarmRequest(BaseModel):
    """What a warm-up sandbox needs: where the task is, and its prebuilt image
    if it has one. No token, no model keys, no registry login. ``task_id`` and
    ``task_files_version`` name the copy of the task the snapshot keeps."""

    task_archive_url: str | None = None
    task_files: dict[str, str] = {}
    docker_image: str | None = None
    task_id: str | None = None
    task_files_version: str | None = None


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


class MonitorStats(BaseModel):
    """What the monitor's heartbeat and control loops delivered."""

    heartbeats_sent: int = Field(default=0, ge=0)
    heartbeats_failed: int = Field(default=0, ge=0)
    control_checks: int = Field(default=0, ge=0)
    control_failed: int = Field(default=0, ge=0)


# The reply of POST /monitor/artifacts: where to PUT the job archive. A comment,
# not a docstring, which would change the route's OpenAPI schema.
class ArtifactUploadResponse(BaseModel):
    upload_url: str
    upload_headers: dict[str, str]
    archive_key: str


class ResultPayload(BaseModel):
    """The body of POST /monitor/result: how the attempt ended."""

    # The flattened HarborOutcome, minus its sandbox-local paths.
    outcome: dict | None = None
    archive_uploaded: bool = False
    # Set when the driver raised instead of returning an outcome.
    execution_error: str | None = None
    retryable: bool = True
    monitor_stats: MonitorStats | None = None
