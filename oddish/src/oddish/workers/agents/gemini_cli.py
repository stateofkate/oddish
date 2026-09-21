"""Oddish Gemini CLI wrapper for restricted-network trials."""

from __future__ import annotations

import json
import shlex
from typing import Any

from harbor.agents.installed.gemini_cli import GeminiCli
from harbor.environments.base import BaseEnvironment

from .trusted_cli_bundle import stage_trusted_cli_bundle

_WEB_TOOLS = ("google_web_search", "web_fetch")
_SYSTEM_SETTINGS_PATH = "/etc/gemini-cli/settings.json"


class OddishGeminiCli(GeminiCli):
    """Gemini CLI with a non-bypassable server-side web-tool switch.

    Harbor still owns shell egress. This wrapper only removes tools whose work
    happens at the model service and therefore would not cross the sandbox's
    network namespace.
    """

    def __init__(
        self,
        *args: Any,
        disable_web_tools: bool = False,
        trusted_cli_bundle: bool = False,
        **kwargs: Any,
    ):
        self._oddish_disable_web_tools = disable_web_tools
        self._oddish_trusted_cli_bundle = trusted_cli_bundle
        super().__init__(*args, **kwargs)

    async def install(self, environment: BaseEnvironment) -> None:
        if self._oddish_trusted_cli_bundle:
            if self._version is not None:
                raise ValueError("Trusted Gemini CLI bundle requires an unpinned CLI version")
            await stage_trusted_cli_bundle(self, environment, cli="gemini")
            await self.exec_as_agent(
                environment,
                command=(
                    "mkdir -p ~/.gemini && "
                    "printf '%s\\n' '{\"experimental\":{\"skills\":true}}' "
                    "> ~/.gemini/settings.json && gemini --version"
                ),
            )
        else:
            await super().install(environment)
        if not self._oddish_disable_web_tools:
            return

        # Gemini's documented Linux system-settings layer overrides user and
        # project settings. Writing the exclusion only to ~/.gemini would let a
        # task's /app/.gemini/settings.json turn the remote tools back on.
        payload = json.dumps({"tools": {"exclude": list(_WEB_TOOLS)}}, indent=2)
        await self.exec_as_root(
            environment,
            command=(
                "install -d -m 0755 /etc/gemini-cli && "
                f"printf %s {shlex.quote(payload)} > {_SYSTEM_SETTINGS_PATH} && "
                f"chmod 0444 {_SYSTEM_SETTINGS_PATH}"
            ),
        )

    def _build_settings_config(
        self,
        model: str | None = None,
        auth_type: str | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        config, model_alias = super()._build_settings_config(model, auth_type)
        if not self._oddish_disable_web_tools:
            return config, model_alias

        updated = dict(config or {})
        tools = dict(updated.get("tools") or {})
        excluded = list(tools.get("exclude") or [])
        tools["exclude"] = list(dict.fromkeys([*excluded, *_WEB_TOOLS]))
        updated["tools"] = tools
        return updated, model_alias
