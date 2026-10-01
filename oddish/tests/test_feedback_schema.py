"""QA feedback validation at the API schema boundary."""

import pytest
from pydantic import ValidationError

from oddish.schemas import FeedbackCreate


@pytest.mark.parametrize(
    "data",
    [
        {},
        {
            "target": "experiment",
            "target_key": "x",
            "vote": "agree",
            "trial_id": "trial-1",
        },
        {
            "target": "qa_verdict",
            "target_key": " ",
            "vote": "agree",
            "trial_id": "trial-1",
        },
        {
            "target": "qa_verdict",
            "target_key": "BAD_FAILURE",
            "vote": "maybe",
            "trial_id": "trial-1",
        },
        {
            "target": "qa_verdict",
            "target_key": "BAD_FAILURE",
            "vote": "agree",
            "trial_id": " ",
        },
    ],
)
def test_feedback_create_rejects_invalid_votes(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        FeedbackCreate.model_validate(data)


def test_feedback_create_normalizes_valid_vote() -> None:
    request = FeedbackCreate(
        body="  needs context  ",
        target="qa_action_item",
        target_key="  action-1  ",
        vote="disagree",
        trial_id="trial-1",
    )

    assert request.body == "needs context"
    assert request.target_key == "action-1"


@pytest.mark.parametrize("target", ["qa_trajectory", "qa_task_verdict", "qa_pre_trial"])
def test_report_feedback_requires_producing_run(target: str) -> None:
    data = dict(target=target, target_key="report", vote="agree", trial_id="trial-1")
    with pytest.raises(ValidationError, match="report_trial_id is required"):
        FeedbackCreate.model_validate(data)
    request = FeedbackCreate.model_validate({**data, "report_trial_id": " qa-1 "})
    assert request.report_trial_id == "qa-1"
