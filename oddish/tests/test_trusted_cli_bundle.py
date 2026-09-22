from __future__ import annotations

import asyncio

import pytest
from oddish.workers.agents import trusted_cli_bundle


def test_trusted_cli_bundle_uploads_before_install(tmp_path, monkeypatch):
    bundle = tmp_path / "oddish-agent-cli-codex.tgz"
    bundle.write_bytes(b"trusted archive")
    monkeypatch.setattr(trusted_cli_bundle, "TRUSTED_CLI_BUNDLE_DIR", tmp_path)
    calls = []

    class Environment:
        async def upload_file(self, source_path, target_path):
            calls.append(("upload", source_path, target_path))

    class Agent:
        async def exec_as_root(self, environment, command):
            calls.append(("extract", command))

    asyncio.run(
        trusted_cli_bundle.stage_trusted_cli_bundle(
            Agent(), Environment(), cli="codex"
        )
    )

    assert calls[0][0:2] == ("upload", bundle)
    assert calls[1][0] == "extract"
    assert calls[0][2] in calls[1][1]
    assert "raw.githubusercontent.com" not in calls[1][1]


def test_trusted_cli_bundle_fails_before_sandbox_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(trusted_cli_bundle, "TRUSTED_CLI_BUNDLE_DIR", tmp_path)

    class Environment:
        async def upload_file(self, source_path, target_path):
            pytest.fail("missing bundle must not upload")

    with pytest.raises(RuntimeError, match="worker image"):
        asyncio.run(
            trusted_cli_bundle.stage_trusted_cli_bundle(
                object(), Environment(), cli="gemini"
            )
        )
