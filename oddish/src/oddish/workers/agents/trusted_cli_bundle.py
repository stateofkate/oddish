"""Stage public agent CLIs from the trusted worker into a restricted sandbox.

EC2 Kubernetes task charts may start their deny-by-default proxy before Harbor
calls ``agent.install``. Harbor's NVM/npm installers then cannot reach their
package hosts. The worker image builds this bundle outside the task network;
uploading it through Harbor's environment API does not grant the agent those
hosts during the trial.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from harbor.environments.base import BaseEnvironment

TRUSTED_CLI_BUNDLE_DIR = Path("/opt")


async def stage_trusted_cli_bundle(
    agent: Any,
    environment: BaseEnvironment,
    *,
    cli: Literal["codex", "gemini"],
) -> None:
    bundle = TRUSTED_CLI_BUNDLE_DIR / f"oddish-agent-cli-{cli}.tgz"
    if not bundle.is_file():
        raise RuntimeError(
            "Restricted Kubernetes agent setup requires the Oddish worker image "
            f"with {bundle}"
        )

    remote_path = f"/tmp/oddish-agent-cli-{uuid4().hex}.tgz"
    await environment.upload_file(bundle, remote_path)
    # The archive is assembled by the worker image. The task cannot choose its
    # contents or the upload path. Keep the transient archive inaccessible to
    # the agent after extraction.
    await agent.exec_as_root(
        environment,
        command=(
            "set -e; mkdir -p /opt; "
            f"tar -xzf {remote_path} -C /opt; "
            f"for bin in node npm npx rg {cli}; do "
            "ln -sfn /opt/oddish-agent-cli/bin/$bin /usr/local/bin/$bin; "
            "done; "
            f"rm -f {remote_path}; "
            "node --version && rg --version >/dev/null"
        ),
    )
