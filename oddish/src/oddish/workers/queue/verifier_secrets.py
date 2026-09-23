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

import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from harbor.utils.env import get_required_host_vars

logger = logging.getLogger(__name__)

# Env var name a task may interpolate -> the settings attribute that holds the
# platform's value for it. Seeded with the Google names because those are the
# ones the runner already treats as the platform's Gemini credential (see
# ``_GEMINI_RUNTIME_SECRET_KEYS``); add a judge provider here only when a task
# verifier is found to reference it.
JUDGE_KEY_SOURCES: dict[str, str] = {
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
) -> dict[str, str]:
    """Return ``{name: value}`` for the allowlisted keys the task references.

    ``available`` is the allowlist: its keys are the only names resolved, and a
    ``None`` value (platform not configured with that key) is skipped, so the
    task fails to resolve it the same way it would with no forward at all.
    """
    referenced: set[str] = set()
    for block in env_blocks:
        for name, _default in get_required_host_vars(dict(block or {})):
            referenced.add(name)
    return {
        name: value
        for name, value in available.items()
        if name in referenced and value
    }


def task_env_blocks(task: Any) -> list[Mapping[str, str]]:
    """Every env block Harbor resolves against the host for this task.

    These are the ``env`` dicts on ``[environment]``, ``[verifier]``,
    ``[solution]`` and each step's ``[steps.verifier]``; ``[agent]`` carries no
    host-resolved env block.
    """
    blocks: list[Mapping[str, str]] = [
        task.environment.env,
        task.verifier.env,
        task.solution.env,
    ]
    for step in task.steps or []:
        blocks.append(step.verifier.env)
    return blocks


def judge_env_for_task(task_path: Path, settings: Any) -> dict[str, str]:
    """The judge keys to forward into the sandbox for the task at ``task_path``.

    An unreadable or unparsable task config forwards nothing: the trial then
    fails on its own terms inside the sandbox, as it would today.
    """
    from harbor.models.task.config import TaskConfig

    try:
        task = TaskConfig.model_validate_toml((task_path / "task.toml").read_text())
    except Exception:  # noqa: BLE001 - a bad task file is the trial's problem
        logger.warning("could not read task config for judge keys at %s", task_path)
        return {}
    return referenced_judge_env(task_env_blocks(task), available_judge_keys(settings))
