"""Live QA issue reports for standalone installations (org_id=None).

Self-hosted twin of backend/api/routers/qa_issue_reports.py.
"""

from typing import Literal

from fastapi import APIRouter, Query

from oddish.core.qa_issue_report import qa_issue_report_core
from oddish.db import get_read_session
from oddish.schemas import QAIssueReportResponse

router = APIRouter()
_KIND = {"experiments": "experiment", "deliveries": "delivery"}


@router.get("/{scope}/{scope_id}/qa-issues", response_model=QAIssueReportResponse)
async def get_qa_issue_report(
    scope: Literal["experiments", "deliveries"],
    scope_id: str,
    category: str | None = None,
    blocking: bool = False,
    q: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=500),
    version: int | None = Query(None, ge=1),
) -> QAIssueReportResponse:
    async with get_read_session() as session:
        return await qa_issue_report_core(
            session,
            scope_kind=_KIND[scope],
            scope_id=scope_id,
            org_id=None,
            category=category,
            blocking=blocking,
            q=q,
            page=page,
            page_size=page_size,
            version=version,
        )
