"""Employee-private derived findings endpoint (ACCESS-003)."""

import re
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.deps import get_current_employee
from app.db import get_db
from app.errors import ErrorCode, api_error
from app.identity.models import Employee
from app.policy.schemas import FindingOut, FindingsPeriodOut
from app.policy.service import calculate_employee_period

router = APIRouter(prefix="/api/v1/me", tags=["policy"])
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _parse_month(value: str):
    if not _MONTH_RE.fullmatch(value):
        raise api_error(
            422, ErrorCode.VALIDATION_INVALID_MONTH, "That period is not a calendar month."
        )
    return datetime.strptime(value, "%Y-%m").date()


@router.get("/findings", response_model=FindingsPeriodOut, summary="Private calculation findings")
def read_findings(
    period: Annotated[str, Query(description="calendar month in YYYY-MM format")],
    employee: Annotated[Employee, Depends(get_current_employee)],
    session: Annotated[Session, Depends(get_db)],
) -> FindingsPeriodOut:
    month = _parse_month(period)
    result = calculate_employee_period(session, employee, month)
    return FindingsPeriodOut(
        period_month=month,
        opening_balance_minutes=result.opening_balance_minutes,
        closing_balance_minutes=result.closing_balance_minutes,
        findings=[FindingOut(**finding.__dict__) for finding in result.findings],
    )
