"""A share link can change its QA setting without changing its token."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from api.routers import tasks
from api.schemas import ExperimentPublishRequest


@pytest.fixture
def share_session(monkeypatch):
    experiment = SimpleNamespace(
        id="experiment", name="Sales demo", org_id="org-1", is_public=True,
        public_token="existing-token", description="Demo", show_qa=False,
        shadow_of=None,
    )
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(
            scalar_one_or_none=lambda: experiment, first=lambda: (experiment, None)
        )),
        commit=AsyncMock(),
    )

    @asynccontextmanager
    async def get_session():
        yield session

    monkeypatch.setattr(tasks, "get_session", get_session)
    monkeypatch.setattr(tasks, "get_read_session", get_session)
    return experiment, session


@pytest.mark.asyncio
async def test_publish_toggles_qa_without_changing_existing_token(share_session):
    experiment, session = share_session
    auth = SimpleNamespace(org_id="org-1")
    for show_qa in (True, False):
        response = await tasks.publish_experiment(
            "experiment", auth, ExperimentPublishRequest(show_qa=show_qa)
        )
        assert experiment.show_qa is show_qa
        assert response.show_qa is show_qa
        assert response.public_token == "existing-token"
    assert session.commit.await_count == 2
    params = session.execute.call_args.args[0].compile().params
    assert "org-1" in params.values()
    assert "experiment" in params.values()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, ExperimentPublishRequest()])
async def test_older_publish_requests_keep_saved_qa_choice(share_session, payload):
    experiment, _ = share_session
    experiment.show_qa = True
    response = await tasks.publish_experiment(
        "experiment", SimpleNamespace(org_id="org-1"), payload
    )
    assert response.show_qa is True
    assert response.public_token == "existing-token"


@pytest.mark.asyncio
async def test_share_metadata_returns_saved_qa_choice(share_session):
    experiment, _ = share_session
    experiment.show_qa = True
    response = await tasks.get_experiment_share(
        "experiment", SimpleNamespace(org_id="org-1", require_scope=lambda _: None)
    )
    assert response.show_qa is True


@pytest.mark.asyncio
async def test_publish_missing_experiment_cannot_change_share(share_session):
    _, session = share_session
    session.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: None)
    with pytest.raises(HTTPException) as exc:
        await tasks.publish_experiment(
            "missing", SimpleNamespace(org_id="other-org"),
            ExperimentPublishRequest(show_qa=True),
        )
    assert exc.value.status_code == 404
    session.commit.assert_not_called()
