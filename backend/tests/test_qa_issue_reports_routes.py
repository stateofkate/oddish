import pytest
from fastapi import HTTPException

from api.routers import qa_issue_reports
from auth import require_auth
from auth.types import AuthContext, AuthMethod
from models import APIKeyScope


def test_report_route_requires_auth() -> None:
    [route] = qa_issue_reports.router.routes
    assert require_auth in [d.call for d in route.dependant.dependencies]


@pytest.mark.asyncio
async def test_read_keys_cannot_read_delivery_reports() -> None:
    auth = AuthContext(method=AuthMethod.API_KEY, org_id="o", scope=APIKeyScope.READ)
    with pytest.raises(HTTPException) as err:
        await qa_issue_reports.get_qa_issue_report("deliveries", "d-1", auth)
    assert err.value.status_code == 403
