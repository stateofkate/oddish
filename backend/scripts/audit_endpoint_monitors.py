"""Read monitoring setup; optionally probe selected models without writing history.

Run with the deployed worker's environment and credentials. Never prints secret
values, completion text, endpoint URLs, or unsanitized provider exceptions.
"""

import argparse
import asyncio
import json

from sqlalchemy import text

import endpoint_health as health
from oddish.db import close_database_connections, get_read_session


async def audit(models: list[str]) -> dict:
    import os

    explicit = "ODDISH_ENDPOINT_MONITORS" in os.environ
    async with get_read_session() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        await session.execute(text("SET LOCAL statement_timeout = 5000"))
        targets = (
            health.configured_targets()
            if explicit
            else await health.platform_targets(session)
        )
        registered = (
            (
                await session.execute(
                    text(
                        "SELECT count(*) AS total, count(*) FILTER (WHERE enabled) AS enabled, max(last_checked_at) AS last_checked_at FROM endpoint_monitors"
                    )
                )
            )
            .mappings()
            .one()
        )
    unknown = set(models) - {t.name for t in targets}
    if unknown:
        raise ValueError(f"Unknown monitor names: {', '.join(sorted(unknown))}")
    connections = []
    for target in targets:
        connection = {
            "name": target.name,
            "model": target.model,
            "credential_ref": target.api_key_env
            or (
                "AWS runtime credentials"
                if target.model.startswith("bedrock/")
                else "Not configured"
            ),
            "configuration_error": target.configuration_error,
        }
        if target.name in models:
            result = await health.check_endpoint(target)
            connection["probe"] = {
                "outcome": result.outcome,
                "status_code": result.status_code,
                "error": result.error,
                "latency_ms": result.latency_ms,
            }
        connections.append(connection)
    return {
        "enabled": health.monitoring_enabled(),
        "source": "explicit" if explicit else "model_concurrency_settings",
        "registered": dict(registered),
        "target_count": len(targets),
        "connections": connections,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probe",
        action="append",
        default=[],
        metavar="MONITOR_NAME",
        help="Send one small provider request for this exact monitor name; repeat to select more. Provider usage is billable.",
    )
    args = parser.parse_args()
    try:
        print(json.dumps(await audit(args.probe), default=str, indent=2))
    finally:
        await close_database_connections()


if __name__ == "__main__":
    asyncio.run(main())
