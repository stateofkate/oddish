"""Parsing rules of the qa_prompts backfill."""

from __future__ import annotations

from pathlib import Path

import pytest

from oddish.analyze.qa_stages import QAStage, prompt_sha256, shipped_prompt_sha256
from oddish.core.backfill_qa_prompts import git_versions, prompt_from_brief
from oddish.workers.analysis_trials import build_qa_brief

REPO = Path(__file__).resolve().parents[2]


def test_a_replayed_prompt_is_recovered_from_its_brief_only_when_it_hashes():
    candidate = "Classify with the causal test first.\n\nThen the rest."
    brief = build_qa_brief(
        task_name="demo",
        trial_ids=["source-1"],
        pre_trial_items=None,
        with_verdict=False,
        classification_prompt=candidate,
    )
    assert prompt_from_brief(brief, prompt_sha256(candidate)) == candidate
    assert prompt_from_brief(brief, prompt_sha256("something else")) is None
    assert prompt_from_brief(None, prompt_sha256(candidate)) is None


def test_git_history_includes_the_shipped_prompt_of_each_stage():
    if not (REPO / ".git").exists():
        pytest.skip("needs a git checkout")
    for stage in QAStage:
        versions = git_versions(REPO, stage)
        assert len({v.sha256 for v in versions}) == len(versions)
        assert shipped_prompt_sha256(stage) in {v.sha256 for v in versions}
        assert all(v.commit and v.author for v in versions)
