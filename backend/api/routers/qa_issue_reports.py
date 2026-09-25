"""Live QA issue report for one experiment or one delivery.

Delivery reports need TASKS scope, like the delivery board; experiment
reports need READ scope, like the experiment page.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from auth import APIKeyScope, AuthContext, require_auth
from oddish.core.qa_issue_report import qa_issue_report_core
from oddish.db import get_read_session
from oddish.schemas import QAIssueReportResponse

router = APIRouter()
_KIND = {"experiments": "experiment", "deliveries": "delivery"}


@router.get("/{scope}/{scope_id}/qa-issues", response_model=QAIssueReportResponse)
async def get_qa_issue_report(
    scope: Literal["experiments", "deliveries"],
    scope_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    category: str | None = None,
    blocking: bool = False,
    q: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=500),
) -> QAIssueReportResponse:
    auth.require_scope(APIKeyScope.TASKS if scope == "deliveries" else APIKeyScope.READ)
    async with get_read_session() as session:
        return await qa_issue_report_core(
            session,
            scope_kind=_KIND[scope],
            scope_id=scope_id,
            org_id=auth.org_id,
            category=category,
            blocking=blocking,
            q=q,
            page=page,
            page_size=page_size,
        )
