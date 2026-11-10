"""Private worktime endpoints (WT-BACKEND §8.1 Employee/self subset).

Every route is /me-scoped: the employee identity comes from the principal
mapping, never from the URL — no client-supplied employee id exists anywhere
(IDOR-proof by construction). All get_current_employee denials
share one byte-identical 403 body.
"""

import re
import uuid
from datetime import date, datetime
from typing import Annotated

from app.auth.deps import (
    get_current_employee,
    get_current_employee_writer,
    get_current_principal,
)
from app.auth.principal import Principal
from app.db import get_db
from app.errors import ErrorCode, api_error
from app.identity.models import Employee
from app.worktime.schemas import (
    ApplyTemplateIn,
    BreakIntervalIn,
    BreakIntervalOut,
    BreakIntervalPatch,
    CopyPreviousIn,
    DayDetail,
    IntervalDeleteBody,
    IntervalIn,
    IntervalOut,
    IntervalPatch,
    MonthSummary,
    ReviewMonthIn,
    SplitIntervalIn,
)
from app.worktime.service import (
    apply_normal_template,
    copy_previous_day,
    correct_break_interval,
    correct_interval,
    create_break_interval,
    create_interval,
    day_detail,
    delete_break_interval,
    delete_interval,
    month_summary,
    review_month,
    split_interval,
)
from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

router = APIRouter(prefix="/api/v1/me", tags=["worktime"])

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_month(value: str) -> date:
    if not _MONTH_RE.fullmatch(value):
        raise api_error(
            422, ErrorCode.VALIDATION_INVALID_MONTH, "That month is not a calendar month."
        )
    return datetime.strptime(value, "%Y-%m").date()


def parse_day(value: str) -> date:
    if not _DATE_RE.fullmatch(value):
        raise api_error(422, ErrorCode.VALIDATION_INVALID_DATE, "That date is not a calendar date.")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise api_error(
            422, ErrorCode.VALIDATION_INVALID_DATE, "That date is not a real calendar date."
        ) from None


@router.get("/months/{year_month}", response_model=MonthSummary, summary="Private month summary")
def read_month(
    year_month: str,
    employee: Annotated[Employee, Depends(get_current_employee)],
    session: Annotated[Session, Depends(get_db)],
) -> MonthSummary:
    return month_summary(session, employee, parse_month(year_month))


@router.get(
    "/months/{year_month}/days/{day}",
    response_model=DayDetail,
    summary="Private day timeline",
)
def read_day(
    year_month: str,
    day: str,
    employee: Annotated[Employee, Depends(get_current_employee)],
    session: Annotated[Session, Depends(get_db)],
) -> DayDetail:
    month = parse_month(year_month)
    parsed_day = parse_day(day)
    if parsed_day.year != month.year or parsed_day.month != month.month:
        raise api_error(
            422,
            ErrorCode.VALIDATION_INVALID_DATE,
            "That day is not in the month you are working on.",
        )
    return day_detail(session, employee, parsed_day)


@router.post(
    "/months/{year_month}/apply-template",
    response_model=list[IntervalOut],
    summary="Apply saved normal-day template to selected days (BE-CMD-003)",
)
def apply_template(
    year_month: str,
    payload: ApplyTemplateIn,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> list[IntervalOut]:
    return apply_normal_template(session, employee, parse_month(year_month), payload)


@router.post(
    "/months/{year_month}/review",
    response_model=MonthSummary,
    summary="Mark employee month reviewed without disclosing it",
)
def mark_month_reviewed(
    year_month: str,
    payload: ReviewMonthIn,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> MonthSummary:
    return review_month(session, employee, parse_month(year_month), payload.acknowledged)


@router.post(
    "/months/{year_month}/days/{day}/copy-previous",
    response_model=list[IntervalOut],
    summary="Copy previous-day facts atomically into the selected day",
)
def copy_day(
    year_month: str,
    day: str,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
    payload: CopyPreviousIn | None = None,
) -> list[IntervalOut]:
    return copy_previous_day(
        session,
        employee,
        parse_month(year_month),
        parse_day(day),
        payload.client_request_id if payload else None,
    )


@router.post(
    "/work-intervals",
    response_model=IntervalOut,
    summary="Create a factual work interval (BE-CMD-001)",
)
def create_work_interval(
    payload: IntervalIn,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> Response:
    out, created = create_interval(session, employee, payload)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        content=out.model_dump(mode="json"),
    )


@router.patch(
    "/work-intervals/{interval_id}",
    response_model=IntervalOut,
    summary="Correct an interval (BE-CMD-002, superseding version + lineage)",
)
def patch_work_interval(
    interval_id: uuid.UUID,
    payload: IntervalPatch,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> IntervalOut:
    return correct_interval(session, employee, interval_id, payload)


@router.post(
    "/work-intervals/{interval_id}/split",
    response_model=tuple[IntervalOut, IntervalOut],
    summary="Split a current work interval atomically",
)
def split_work_interval(
    interval_id: uuid.UUID,
    payload: SplitIntervalIn,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> tuple[IntervalOut, IntervalOut]:
    return split_interval(session, employee, interval_id, payload)


@router.delete(
    "/work-intervals/{interval_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete/tombstone an unsubmitted interval (with lineage)",
)
def delete_work_interval(
    interval_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
    version: Annotated[int, Query(ge=1, description="current version (optimistic lock)")],
    payload: IntervalDeleteBody | None = None,
) -> Response:
    delete_interval(
        session, principal, employee, interval_id, version, payload.reason if payload else None
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/break-intervals",
    response_model=BreakIntervalOut,
    summary="Record an explicit factual break (WT-BACKEND §5.3)",
)
def create_break(
    payload: BreakIntervalIn,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> Response:
    out, created = create_break_interval(session, employee, payload)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        content=out.model_dump(mode="json"),
    )


@router.patch(
    "/break-intervals/{break_id}",
    response_model=BreakIntervalOut,
    summary="Correct a break (superseding version + lineage)",
)
def patch_break(
    break_id: uuid.UUID,
    payload: BreakIntervalPatch,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> BreakIntervalOut:
    return correct_break_interval(session, employee, break_id, payload)


@router.delete(
    "/break-intervals/{break_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an unsubmitted break (whole chain; refused once disclosed)",
)
def delete_break(
    break_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
    version: Annotated[int, Query(ge=1, description="current version (optimistic lock)")],
    payload: IntervalDeleteBody | None = None,
) -> Response:
    delete_break_interval(
        session, principal, employee, break_id, version, payload.reason if payload else None
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
