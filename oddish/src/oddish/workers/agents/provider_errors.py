"""Recover typed provider failures from CLI agent output.

Some provider-native CLIs report an HTTP or account failure in their output but
still exit through Harbor's generic ``NonZeroAgentExitCodeError`` boundary.
Keeping that generic type publishes the verifier's reward as if the solver had
received a fair run. This module refines only strongly provider-shaped output;
an unrelated non-zero agent command remains the fallback Harbor returned.
"""

from __future__ import annotations

import json
import re
from typing import Any, cast

from harbor.agents.installed.base import (
    AgentAuthenticationError,
    ApiClientError,
    ApiError,
    ApiInternalServerError,
    ApiOverloadedError,
    ApiRateLimitError,
    ApiRequestTimeoutError,
    ApiUsageLimitError,
    ModelNotFoundError,
    NonZeroAgentExitCodeError,
    UnknownApiError,
)

_HTTP_STATUS_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r'"(?:http[_-]?status|status[_-]?code|statusCode)"\s*:\s*"?([45]\d\d)\b',
        r"\bstatus(?:[_ ]code)?[\"']?\s*[=:()]?\s*([45]\d\d)\b",
        r"\berror code\s*[=:]?\s*([45]\d\d)\b",
        r"\bHTTP(?:/\d(?:\.\d)?)?\s+([45]\d\d)\b",
        (
            r"\b([45]\d\d)\s+(?:bad request|payment required|unauthorized|"
            r"forbidden|not found|request timeout|too many requests|internal "
            r"server error|bad gateway|service unavailable|gateway timeout)\b"
        ),
    )
)
_REQUEST_ID_PATTERN = re.compile(
    r'\brequest[_ -]?id\b\s*[=:]\s*["\']?([A-Za-z0-9._:/-]+)', re.IGNORECASE
)
_PROVIDER_CONTEXT_PATTERN = re.compile(
    r"\b(?:api|provider|model|request|response|authentication|authorization|"
    r"unauthorized|forbidden|billing|credit|quota|usage|invoice|payment)\b",
    re.IGNORECASE,
)
_USAGE_LIMIT_PATTERN = re.compile(
    r"\b(?:billing (?:error|issue|account|disabled|required|failure|credits?|"
    r"limit|quota)|"
    r"credit balance(?: is)? (?:empty|exhausted|depleted|insufficient)|"
    r"credits? (?:are )?(?:empty|exhausted|depleted|insufficient)|"
    r"(?:no|without|add|purchase|insufficient|exhausted|depleted|not enough|"
    r"has no|does not have (?:any|enough)|doesn't have (?:any|enough)) credits?|"
    r"spend(?:ing)? (?:limit|cap)|usage limit|quota (?:exceeded|exhausted)|"
    r"unpaid invoice|payment required|insufficient (?:funds|balance)|"
    r"account limit|resource exhausted)\b",
    re.IGNORECASE,
)
_AUTH_PATTERN = re.compile(
    r"\b(?:invalid|missing|rejected|expired|unauthorized|forbidden)\b.{0,48}"
    r"\b(?:api[ _-]?key|credential|token|authentication|authorization|access)\b|"
    r"\b(?:api[ _-]?key|credential|token|authentication|authorization|access)\b"
    r".{0,48}\b(?:invalid|missing|rejected|expired|unauthorized|forbidden|denied)\b",
    re.IGNORECASE,
)
_MODEL_NOT_FOUND_PATTERN = re.compile(
    r"\bmodel\b.{0,80}\b(?:does not exist|not found|unknown|unavailable|"
    r"no access|do not have access|not permitted)\b",
    re.IGNORECASE,
)


def _output(result: Any, *, provider: str) -> str:
    stdout = getattr(result, "stdout", None) or ""
    stderr = getattr(result, "stderr", None) or ""
    if provider.lower() == "xai":
        # Stock Grok returns the full streaming transcript on stdout. Do not
        # classify an assistant merely discussing a 403 or billing problem as
        # the provider's own failure; only its structured error events count.
        error_events: list[str] = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or event.get("kind") or "").lower()
            if event_type in {"error", "internal_error", "failure"}:
                error_events.append(line)
        stdout = "\n".join(error_events)
    return f"{stdout}\n{stderr}"


def _http_status(output: str) -> int | None:
    """Return the last provider-shaped HTTP status in command output."""
    found: list[tuple[int, int]] = []
    for pattern in _HTTP_STATUS_PATTERNS:
        for match in pattern.finditer(output):
            # A bare HTTP-looking code in task output is not enough. Require
            # provider vocabulary on the same line unless the field name itself
            # is the unambiguous structured ``http_status`` key.
            line_start = output.rfind("\n", 0, match.start()) + 1
            line_end = output.find("\n", match.end())
            if line_end < 0:
                line_end = len(output)
            line = output[line_start:line_end]
            if "http_status" not in match.group(0).lower() and not (
                _PROVIDER_CONTEXT_PATTERN.search(line)
                or re.search(r"\bAPI error\b", line, re.IGNORECASE)
            ):
                continue
            found.append((match.end(), int(match.group(1))))
    return max(found, default=(0, 0))[1] or None


def _request_id(output: str) -> str | None:
    matches = list(_REQUEST_ID_PATTERN.finditer(output))
    return matches[-1].group(1) if matches else None


def _api_error_type(value: Any) -> type[ApiError]:
    """Give mypy a boundary for Harbor's untyped exception exports."""
    return cast(type[ApiError], value)


def _exception_class(output: str, http_status: int | None) -> type[ApiError] | None:
    # Account exhaustion sometimes arrives as 400 or 403 rather than 402, so
    # the provider's billing language is more specific than the status family.
    if _USAGE_LIMIT_PATTERN.search(output):
        return _api_error_type(ApiUsageLimitError)
    if _AUTH_PATTERN.search(output) or http_status in {401, 403}:
        return _api_error_type(AgentAuthenticationError)
    if _MODEL_NOT_FOUND_PATTERN.search(output) or http_status == 404:
        return _api_error_type(ModelNotFoundError)
    if http_status == 402:
        return _api_error_type(ApiUsageLimitError)
    if http_status == 408:
        return _api_error_type(ApiRequestTimeoutError)
    if http_status == 429:
        return _api_error_type(ApiRateLimitError)
    if http_status == 529:
        return _api_error_type(ApiOverloadedError)
    if http_status is not None and 500 <= http_status < 600:
        return _api_error_type(ApiInternalServerError)
    if http_status is not None and 400 <= http_status < 500:
        return _api_error_type(ApiClientError)
    if re.search(r"\b(?:API|provider) error\b", output, re.IGNORECASE):
        return _api_error_type(UnknownApiError)
    return None


def refine_provider_exec_error(
    fallback: NonZeroAgentExitCodeError,
    result: Any,
    *,
    provider: str,
) -> NonZeroAgentExitCodeError:
    """Refine a generic CLI exit into Harbor's typed provider errors.

    Existing specific classifications (rate limit, safety refusal, context
    exhaustion, and so on) remain authoritative. ``UnknownApiError`` is the
    one typed result refined further because the HTTP status can make it exact.
    """
    output = _output(result, provider=provider)
    status = _http_status(output)
    exception_class = _exception_class(output, status)

    refinable = {NonZeroAgentExitCodeError, UnknownApiError}
    if exception_class is ApiUsageLimitError:
        # Muse's structured status parser can only see the 400/403 family and
        # initially calls these ApiClientError/AgentAuthenticationError. The
        # accompanying billing language is the stronger, non-retryable signal.
        refinable.update({ApiClientError, AgentAuthenticationError})
    if type(fallback) not in refinable:
        return fallback
    if exception_class is None:
        return fallback

    return exception_class(
        f"{fallback}\n{provider} provider API failure",
        return_code=getattr(result, "return_code", None),
        http_status=status,
        request_id=_request_id(output),
    )
