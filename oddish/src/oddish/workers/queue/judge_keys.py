"""Judge-model credentials a task's own env blocks ask for, and nothing else.

The old worker ran Harbor inside a process that carried the platform's whole
runtime secret bundle, so a verifier that interpolated ``${GEMINI_API_KEY}``
found it ambiently. The monitor sandbox carries no ambient secrets, and the
agent shares that sandbox, so attaching the bundle is not an option. Instead
the launcher resolves only the judge keys the task's env blocks reference,
from the platform's own settings, and forwards those and no others.

Two properties are deliberate:

- A task that references nothing gets nothing. The other trials keep running
  in a secret-free sandbox exactly as before.
- Only names in ``JUDGE_KEY_SOURCES`` are ever resolved. A task that names any
  other host variable fails to resolve exactly as it does today, so this cannot
  be used to pull an arbitrary platform secret into a sandbox.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from harbor.utils.env import get_required_host_vars

# Supported task credential/endpoint source names; arbitrary host secrets are excluded.
JUDGE_KEY_SOURCES: dict[str, str] = {
    "ANTHROPIC_API_KEY": "anthropic_api_key",
    "OPENAI_API_KEY": "openai_api_key",
    "AZURE_OPENAI_API_KEY": "azure_openai_api_key",
    "AZURE_OPENAI_ENDPOINT": "azure_openai_endpoint",
    "AZURE_OPENAI_API_VERSION": "azure_openai_api_version",
    "GEMINI_API_KEY": "gemini_api_key",
    "GOOGLE_GENERATIVE_AI_API_KEY": "gemini_api_key",
    "GOOGLE_API_KEY": "gemini_api_key",
}


def available_judge_keys(settings: Any) -> dict[str, str | None]:
    """The allowlist, with the platform's value for each name (None if unset)."""
    return {
        name: getattr(settings, attribute, None)
        for name, attribute in JUDGE_KEY_SOURCES.items()
    }


def referenced_judge_env(
    env_blocks: Iterable[Mapping[str, str] | None],
    available: Mapping[str, str | None],
    *,
    phase: str = "task",
) -> dict[str, str]:
    """Resolve declared sources only; reject required missing names before launch."""
    resolved = {}
    for block in env_blocks:
        for name, default in get_required_host_vars(dict(block or {})):
            value = available.get(name)
            if value:
                resolved[name] = value
            elif default is None:
                raise ValueError(
                    f"Task {phase} requires unavailable host variable '{name}'"
                )
    return resolved


def task_env_for_task(
    task_path: Path,
    available: Mapping[str, str | None],
    *,
    verifier_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Platform sources referenced by task phases, separate from agent credentials."""
    from harbor.models.task.config import TaskConfig

    task = TaskConfig.model_validate_toml((task_path / "task.toml").read_text())
    phases = [
        ("environment", task.environment.env),
        ("verifier", {**task.verifier.env, **(verifier_env or {})}),
        ("solution", task.solution.env),
        *(
            (
                f"step {step.name!r} verifier",
                {**step.verifier.env, **(verifier_env or {})},
            )
            for step in task.steps or []
        ),
    ]
    resolved = {}
    for phase, block in phases:
        resolved.update(referenced_judge_env([block], available, phase=phase))
    return resolved
