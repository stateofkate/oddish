"""Claude Code with a trial-local LiteLLM bridge for non-Anthropic APIs."""

from __future__ import annotations

import os
import secrets
from contextlib import suppress
from pathlib import Path

from harbor.agents.installed.claude_code import ClaudeCode

from oddish.config import settings
from oddish.workers.queue.job_tokens import scoped_model_env


class ProxiedClaudeCode(ClaudeCode):
    CLI_FLAGS = [
        flag for flag in ClaudeCode.CLI_FLAGS if flag.kwarg != "reasoning_effort"
    ]

    def __init__(self, *args, proxy_model: str, **kwargs):
        self.proxy_model = proxy_model
        self.proxy_effort = kwargs.pop("reasoning_effort", None)
        # The provider receives effort directly; Claude's CLI validates against
        # its own model catalog and cannot validate another vendor's values.
        super().__init__(*args, **kwargs)

    @staticmethod
    def _is_bedrock_mode() -> bool:
        return False

    async def install(self, environment):
        await super().install(environment)
        result = await self.exec_as_root(
            environment,
            command=(
                "set -eu; command -v python3 >/dev/null || "
                "(apt-get update && apt-get install -y python3 python3-venv); "
                "python3 -m venv /installed-agent/claude-proxy; "
                "/installed-agent/claude-proxy/bin/pip install --quiet litellm==1.83.14"
            ),
        )
        if result.return_code:
            raise RuntimeError(
                f"Claude protocol adapter installation failed: {result.stderr}"
            )
        await environment.upload_file(
            Path(__file__).with_name("claude_proxy.py"),
            "/installed-agent/claude_proxy.py",
        )

    async def run(self, instruction, environment, context):
        token = secrets.token_urlsafe(32)
        model = self.proxy_model
        env = (
            scoped_model_env(
                agent="claude-code", model=model, settings=settings, ambient=os.environ
            )
            | self.extra_env
        )
        if model.startswith("openai/") and settings.get_openai_provider() == "azure":
            model = f"azure/{settings.resolve_azure_openai_deployment(model)}"
        env |= {"ODDISH_PROXY_MODEL": model, "ODDISH_PROXY_TOKEN": token}
        if self.proxy_effort:
            env["ODDISH_PROXY_EFFORT"] = self.proxy_effort
        previous = self.extra_env
        self._extra_env = previous | {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787",
            "ANTHROPIC_API_KEY": token,
            "ANTHROPIC_AUTH_TOKEN": "",
            "CLAUDE_CODE_USE_BEDROCK": "",
            "AWS_BEARER_TOKEN_BEDROCK": "",
            "CLAUDE_CODE_USE_VERTEX": "",
            "CLAUDE_CODE_OAUTH_TOKEN": "",
            "CLAUDE_FORCE_OAUTH": "0",
            "ENABLE_TOOL_SEARCH": "false",
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
        try:
            result = await environment.exec(
                command=(
                    "nohup /installed-agent/claude-proxy/bin/python /installed-agent/claude_proxy.py "
                    ">/logs/agent/claude-proxy.log 2>&1 < /dev/null & "
                    "echo $! > /tmp/oddish-claude-proxy.pid; "
                    "for i in $(seq 1 60); do "
                    "curl -fsS http://127.0.0.1:8787/health >/dev/null 2>&1 && exit 0; "
                    "kill -0 $(cat /tmp/oddish-claude-proxy.pid) 2>/dev/null || break; sleep 1; done; "
                    "echo 'Claude protocol adapter did not start' >&2; exit 1"
                ),
                env=env,
            )
            if result.return_code:
                raise RuntimeError(
                    result.stderr or "Claude protocol adapter did not start"
                )
            with environment.scoped_exec_env(self.extra_env):
                await super().run(instruction, environment, context)
        finally:
            self._extra_env = previous
            # Teardown may have already removed the sandbox. Cleanup must not
            # replace the provider/agent exception that caused the teardown.
            with suppress(Exception):
                await environment.exec(
                    command="test ! -f /tmp/oddish-claude-proxy.pid || kill $(cat /tmp/oddish-claude-proxy.pid) 2>/dev/null || true"
                )
