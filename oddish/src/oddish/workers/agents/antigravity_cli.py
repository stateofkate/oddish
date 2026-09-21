"""Oddish Antigravity CLI (agy) wrapper for restricted-network trials."""

from __future__ import annotations

import re
from typing import Any

from harbor.agents.installed.antigravity_cli import AntigravityCli
from harbor.utils.env import parse_bool_env_value

from oddish.config import VERTEX_AI_MODE_SERVICE_ACCOUNT
from oddish.workers.harbor.model_hosts import (
    ANTIGRAVITY_INSTALL_HOSTS,
    ANTIGRAVITY_RUNTIME_HOSTS,
    outbound_hosts_for_model,
)
from oddish.workers.harbor.vertex_ai import MODE_ENV as _VERTEX_MODE_ENV

# agy refuses to run a Gemini 3 model without an effort level and rejects one
# on earlier generations, so the default applies to the Gemini 3 family only
# (``gemini-3-…``, ``gemini-3.5-…``, ``gemini-3.8-…``).
_DEFAULT_REASONING_EFFORT = "high"
_GEMINI_3_FAMILY = re.compile(r"^gemini-3(?:[.-]|$)")


def _wants_default_effort(model_name: str | None) -> bool:
    if not model_name:
        return False
    bare = model_name.rsplit("/", 1)[-1].strip().lower()
    return _GEMINI_3_FAMILY.match(bare) is not None


def _env_is_true(value: str | None) -> bool:
    if value is None or not value.strip():
        return False
    try:
        return parse_bool_env_value(value, name="GOOGLE_GENAI_USE_VERTEXAI")
    except ValueError:
        return False


class OddishAntigravityCli(AntigravityCli):
    """agy wrapper for closed-internet trials and Vertex AI.

    agy self-installs (curl | bash from antigravity.google) during agent
    SETUP, under the ENVIRONMENT baseline network policy — so the runner's
    antigravity arm merges ``ANTIGRAVITY_INSTALL_HOSTS`` plus the model
    transport host into the environment baseline (see
    ``_antigravity_environment_hosts``), exactly like the opencode arm.

    Unlike ``OddishGeminiCli`` there is no ``disable_web_tools`` switch: agy's
    settings.json has no tool-exclusion layer, so provider-side web tools cannot
    ride around the network boundary: a live closed-network probe showed
    agy's read_url_content and shell curl both fail closed under the egress
    allowlist (agy 1.1.19 reported NO WEB ACCESS).

    Vertex AI: stock agy authenticates with a Gemini API key, which the Vertex
    profile blanks on purpose, and its enterprise ADC mode is a separate
    opt-in. This wrapper reads the profile the way Gemini CLI does natively:
    on the service-account profile (the Vertex flag, the ``service_account``
    marker and a credential file) it turns ADC mode on, so the uploaded
    service-account file is the credential, unless the run sets
    ``AGY_ADC_AUTH`` itself; the express profile carries only an API key and
    stays on agy's key path. The name matches Harbor's ``AUTH`` redaction, so
    a literal opt-out is persisted as ``****`` and fails agy's boolean parse;
    ``AGY_ADC_AUTH=${AGY_ADC_AUTH:-false}`` survives and resolves to
    ``false`` on the worker. It also defaults ``reasoning_effort`` to ``high``
    for Gemini 3 models, which agy will not run without an effort level.
    Behaviour is otherwise identical to the stock harbor ``AntigravityCli``.
    """

    def __init__(self, *args, reasoning_effort: str | None = None, **kwargs):
        super().__init__(*args, reasoning_effort=reasoning_effort, **kwargs)
        # Harbor's positional slots after logs_dir are the prompt template and
        # version, so the model is only known once the base class resolved it.
        if reasoning_effort is None and _wants_default_effort(self.model_name):
            self._validate_reasoning_effort(_DEFAULT_REASONING_EFFORT, self.model_name)
            self._reasoning_effort = _DEFAULT_REASONING_EFFORT

    def _use_adc_auth(self) -> bool:
        if self._adc_disabled_via_extra_env:
            return False
        explicit = self._get_env("AGY_ADC_AUTH")
        if explicit is not None and explicit.strip():
            return super()._use_adc_auth()
        if self._vertex_service_account_profile():
            return True
        return super()._use_adc_auth()

    def _vertex_service_account_profile(self) -> bool:
        """True when Oddish's Vertex profile is present in service-account mode.

        Express mode publishes the same Vertex flag but only an API key, which
        ADC mode would drop, so it stays on agy's key path.
        """
        mode = (self._get_env(_VERTEX_MODE_ENV) or "").strip()
        credentials = (self._get_env("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
        return (
            _env_is_true(self._get_env("GOOGLE_GENAI_USE_VERTEXAI"))
            and mode == VERTEX_AI_MODE_SERVICE_ACCOUNT
            and bool(credentials)
        )

    @classmethod
    def required_outbound_domains(
        cls,
        model_name: str | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> list[str]:
        domains: set[str] = set(ANTIGRAVITY_INSTALL_HOSTS) | set(
            ANTIGRAVITY_RUNTIME_HOSTS
        )
        for host in outbound_hosts_for_model(
            model_name, agent_kwargs=kwargs, infer_bare_provider=True
        ):
            domains.add(host)
        return sorted(domains)
