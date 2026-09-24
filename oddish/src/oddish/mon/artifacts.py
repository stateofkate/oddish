"""Archive the Harbor job directory, upload it, and hand over the result."""

from __future__ import annotations

import dataclasses
import io
import logging
import tarfile
from pathlib import Path
from typing import Any

import httpx

from oddish.mon.client import (
    ControlPlaneRefused,
    ControlPlaneUnavailable,
    MonitorClient,
)
from oddish.mon.contract import MonitorStats

logger = logging.getLogger(__name__)

# Sandbox-local paths mean nothing to the control plane.
LOCAL_ONLY_FIELDS = ("job_dir", "job_result_path")


def outcome_payload(outcome: Any) -> dict[str, Any]:
    data = dataclasses.asdict(outcome)
    for name in LOCAL_ONLY_FIELDS:
        data.pop(name, None)
    return data


def archive_directory(directory: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(directory, arcname=".")
    return buffer.getvalue()


async def upload(
    client: MonitorClient,
    outcome: Any,
    *,
    fallback_dir: Path | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """Upload the job directory if there is one: True once storage has it.

    ``fallback_dir`` is uploaded when Harbor never made a job directory (a
    failure before the trial started), so such an attempt still leaves its
    monitor log behind.
    """
    uploaded = False
    target = None
    job_dir = getattr(outcome, "job_dir", None)
    if job_dir is None or not Path(job_dir).is_dir():
        job_dir = fallback_dir
    if job_dir is not None and Path(job_dir).is_dir():
        try:
            target = await client.artifact_upload()
        except ControlPlaneRefused as exc:
            # A refused grant is the same fact as a refused upload: the files
            # cannot be stored, the verdict still can. Never a failed start.
            logger.error(
                "artifact grant refused (%s); reporting without artifacts", exc
            )
            target = None
    if target is not None:
        async with httpx.AsyncClient(timeout=600.0, transport=transport) as http:
            # A storage outage is retried like a control plane outage; a 4xx
            # will not get better by retrying.
            try:
                response = await http.put(
                    target.upload_url,
                    content=archive_directory(Path(job_dir)),
                    headers=target.upload_headers,
                )
            except httpx.TransportError as exc:
                raise ControlPlaneUnavailable(f"upload: {type(exc).__name__}") from None
            if response.status_code >= 500:
                raise ControlPlaneUnavailable(f"upload: HTTP {response.status_code}")
            if response.status_code >= 400:
                # Refused for good: the result still carries the verdict, and
                # settlement reads archive_uploaded=False as "no artifacts".
                logger.warning(
                    "artifact upload refused with HTTP %s; reporting the result "
                    "without artifacts",
                    response.status_code,
                )
            else:
                uploaded = True
    return uploaded


async def post_result(
    client: MonitorClient,
    outcome: Any,
    *,
    archive_uploaded: bool,
    execution_error: str | None = None,
    retryable: bool = True,
    monitor_stats: MonitorStats | None = None,
) -> dict[str, Any]:
    """Post the result. ``outcome`` is ``None`` when the driver raised;
    ``execution_error`` then says why, mirroring the worker's
    ``TrialExecutionResult``."""
    return await client.result(
        outcome=outcome_payload(outcome) if outcome is not None else None,
        archive_uploaded=archive_uploaded,
        execution_error=execution_error,
        retryable=retryable,
        monitor_stats=monitor_stats,
    )
