"""Employee-private day-status endpoints (EINV-22).

Every route is /me-scoped: the employee identity comes from the principal
mapping, never from the URL — there is deliberately no path or query parameter
that could name another employee (IDOR-proof by construction, INV-04). The
category is named here and nowhere else: a disclosed artefact shows the truthful
arithmetic, never a status label.
"""

import re
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.absence import service as absence
from app.absence.metrics import compensation_metrics_out
from app.absence.schemas import (
    DayStatusCreate,
    DayStatusMonthOut,
    DayStatusOut,
    DayStatusPatch,
)
from app.auth.deps import get_current_employee, get_current_employee_writer
from app.db import get_db
from app.errors import ErrorCode, api_error
from app.identity.models import Employee

router = APIRouter(prefix="/api/v1/me", tags=["day-status"])

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _parse_month(value: str) -> datetime:
    if not _MONTH_RE.fullmatch(value):
        raise api_error(
            422, ErrorCode.VALIDATION_INVALID_MONTH, "That month is not a calendar month."
        )
    return datetime.strptime(value, "%Y-%m")


@router.get(
    "/day-status",
    response_model=DayStatusMonthOut,
    summary="Current day-status facts of one private month",
)
def read_day_status(
    employee: Annotated[Employee, Depends(get_current_employee)],
    session: Annotated[Session, Depends(get_db)],
    period: Annotated[str, Query(description="month as YYYY-MM")],
) -> DayStatusMonthOut:
    month = _parse_month(period).date()
    return DayStatusMonthOut(
        period=f"{month:%Y-%m}",
        entries=absence.list_day_status(session, employee, month),
        compensation_metrics=compensation_metrics_out(session, employee, month),
    )


@router.post(
    "/day-status",
    response_model=DayStatusOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record one day-status fact (idempotent on client_request_id)",
)
def create_day_status(
    payload: DayStatusCreate,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> DayStatusOut:
    return absence.create_day_status(session, employee, payload)


@router.patch(
    "/day-status/{entry_id}",
    response_model=DayStatusOut,
    summary="Correct one day-status fact (versioned supersession)",
)
def patch_day_status(
    entry_id: uuid.UUID,
    payload: DayStatusPatch,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> DayStatusOut:
    return absence.correct_day_status(session, employee, entry_id, payload)


@router.delete(
    "/day-status/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove one day-status fact and its whole version chain",
)
def delete_day_status(
    entry_id: uuid.UUID,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
    version: Annotated[int, Query(ge=1, description="current version (optimistic lock)")],
    reason: Annotated[str | None, Query(max_length=500)] = None,
) -> Response:
    absence.delete_day_status(session, employee, entry_id, version, reason)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
