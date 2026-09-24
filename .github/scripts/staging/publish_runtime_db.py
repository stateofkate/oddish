"""Publish Neon's pooled staging URL after direct-connection migrations pass."""

import os
import subprocess
from urllib.parse import urlsplit


def runtime_url(direct_url: str, pooled_url: str) -> str:
    """Validate the two URLs returned by Neon before publishing the runtime one."""
    direct, pooled = urlsplit(direct_url), urlsplit(pooled_url)
    direct_host, pooled_host = direct.hostname or "", pooled.hostname or ""
    endpoint, _, suffix = pooled_host.partition(".")
    if (
        direct.scheme != "postgresql+asyncpg"
        or pooled.scheme != "postgresql+asyncpg"
        or not direct_host.endswith(".neon.tech")
        or not pooled_host.endswith(".neon.tech")
        or "-pooler." in direct_host
        or not endpoint.endswith("-pooler")
        or endpoint.removesuffix("-pooler") + "." + suffix != direct_host
        or direct.port not in (None, 5432)
        or pooled.port not in (None, 5432)
        or not direct.username
        or not direct.password
        or (direct.username, direct.password, direct.path)
        != (pooled.username, pooled.password, pooled.path)
        or direct.path in ("", "/")
    ):
        raise ValueError("Expected matching direct and pooled Neon staging URLs")
    return pooled_url


def main() -> None:
    url = runtime_url(
        os.environ["ODDISH_DATABASE_URL"], os.environ["ODDISH_RUNTIME_DATABASE_URL"]
    )
    print(f"::add-mask::{url}", flush=True)
    subprocess.run(
        [
            "uv",
            "run",
            "modal",
            "secret",
            "create",
            "--env",
            "staging",
            "--force",
            "oddish-staging-db",
            f"ODDISH_DATABASE_URL={url}",
            "ODDISH_DASHBOARD_URL=https://staging.oddish.app",
            "CORS_ALLOWED_ORIGINS=https://staging.oddish.app",
            "LOGFIRE_ENVIRONMENT=staging",
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
