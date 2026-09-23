"""Judge keys are forwarded only when a task's env blocks ask for them.

Regression: QA40 task 12's verifier is a Gemini judge that interpolates
``${GEMINI_API_KEY}``. The old worker carried the platform's whole secret
bundle ambiently, so it resolved; the secret-free monitor sandbox did not.
The forward must be apples to apples: that one key, for that task, and nothing
for the other trials.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from oddish.workers.queue import judge_keys as vs

AVAILABLE = {
    "GEMINI_API_KEY": "gem-1",
    "GOOGLE_GENERATIVE_AI_API_KEY": "gem-1",
    "GOOGLE_API_KEY": "gem-1",
}


def test_referenced_judge_key_is_forwarded():
    blocks = [{"GEMINI_API_KEY": "${GEMINI_API_KEY}", "MODEL_NAME": "gemini-3"}]
    assert vs.referenced_judge_env(blocks, AVAILABLE) == {"GEMINI_API_KEY": "gem-1"}


def test_task_that_references_nothing_gets_nothing():
    blocks = [{"MODEL_NAME": "gemini-3"}, {}, None]
    assert vs.referenced_judge_env(blocks, AVAILABLE) == {}


def test_only_referenced_names_are_forwarded_not_the_whole_allowlist():
    blocks = [{"GOOGLE_API_KEY": "${GOOGLE_API_KEY}"}]
    assert vs.referenced_judge_env(blocks, AVAILABLE) == {"GOOGLE_API_KEY": "gem-1"}


def test_non_allowlisted_host_var_is_never_resolved():
    # A task cannot pull an arbitrary platform secret by naming it.
    blocks = [{"AWS_SECRET_ACCESS_KEY": "${AWS_SECRET_ACCESS_KEY}"}]
    assert vs.referenced_judge_env(blocks, AVAILABLE) == {}


def test_unconfigured_platform_key_is_skipped():
    blocks = [{"GEMINI_API_KEY": "${GEMINI_API_KEY}"}]
    assert vs.referenced_judge_env(blocks, {"GEMINI_API_KEY": None}) == {}


def test_available_judge_keys_read_settings_by_attribute():
    settings = SimpleNamespace(gemini_api_key="gem-2")
    assert vs.available_judge_keys(settings) == {
        "GEMINI_API_KEY": "gem-2",
        "GOOGLE_GENERATIVE_AI_API_KEY": "gem-2",
        "GOOGLE_API_KEY": "gem-2",
    }


@pytest.mark.parametrize(
    "toml, expected",
    [
        # QA40 task 12's verifier block, verbatim shape.
        (
            '[verifier.env]\nGEMINI_API_KEY = "${GEMINI_API_KEY}"\n'
            'MODEL_NAME = "gemini-3-flash-preview"\n',
            {"GEMINI_API_KEY": "gem-1"},
        ),
        # A task with no interpolation anywhere: the other 39 of the 40.
        ('[environment]\ncpus = 2\nmemory_mb = 2048\n', {}),
        # A per-step verifier is read too.
        (
            '[[steps]]\nname = "s1"\n[steps.verifier.env]\n'
            'GEMINI_API_KEY = "${GEMINI_API_KEY}"\n',
            {"GEMINI_API_KEY": "gem-1"},
        ),
    ],
    ids=["task-12-verifier", "no-interpolation", "step-verifier"],
)
def test_judge_env_for_task_reads_real_task_toml(tmp_path: Path, toml, expected):
    (tmp_path / "task.toml").write_text(toml)
    settings = SimpleNamespace(gemini_api_key="gem-1")
    assert vs.judge_env_for_task(tmp_path, settings) == expected


def test_unreadable_task_config_forwards_nothing(tmp_path: Path):
    settings = SimpleNamespace(gemini_api_key="gem-1")
    assert vs.judge_env_for_task(tmp_path, settings) == {}
