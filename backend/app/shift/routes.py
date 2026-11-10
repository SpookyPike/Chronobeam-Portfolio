"""Employee-self shift routes; there is deliberately no recipient API."""

import uuid
from datetime import datetime, time
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth.deps import get_current_employee, get_current_employee_writer, get_current_principal
from app.auth.principal import Principal
from app.db import get_db
from app.errors import ErrorCode, api_error
from app.identity.models import Employee
from app.shift.schemas import (
    ShiftConfirmIn,
    ShiftDeviationIn,
    ShiftOut,
    ShiftPlanIn,
    ShiftPlanPatch,
    ShiftReportPreviewOut,
    ShiftWithdrawIn,
)
from app.shift.service import (
    confirm_shift,
    create_plan_shift,
    edit_plan_shift,
    list_shifts,
    report_preview,
    save_deviation,
    withdraw_plan_shift,
)
from app.worktime.routes import parse_day
from app.worktime.walltime import BERLIN

router = APIRouter(prefix="/api/v1/me", tags=["shift"])


@router.get("/shifts", response_model=list[ShiftOut], summary="Private planned and actual shifts")
def read_shifts(
    from_date: Annotated[str, Query(alias="from", description="inclusive date YYYY-MM-DD")],
    to_date: Annotated[str, Query(alias="to", description="exclusive date YYYY-MM-DD")],
    employee: Annotated[Employee, Depends(get_current_employee)],
    session: Annotated[Session, Depends(get_db)],
) -> list[ShiftOut]:
    start_day = parse_day(from_date)
    end_day = parse_day(to_date)
    if end_day <= start_day:
        raise api_error(
            422, ErrorCode.VALIDATION_INVALID_DATE, "The end of the range must be after its start."
        )
    return list_shifts(
        session,
        employee,
        datetime.combine(start_day, time.min, tzinfo=BERLIN),
        datetime.combine(end_day, time.min, tzinfo=BERLIN),
    )


@router.post(
    "/shifts/{assignment_id}/confirm",
    response_model=ShiftOut,
    summary="Confirm a shift was worked exactly as approved (BE-CMD-004)",
)
def confirm(
    assignment_id: uuid.UUID,
    payload: ShiftConfirmIn,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> ShiftOut:
    return confirm_shift(session, employee, assignment_id, payload)


@router.post(
    "/shifts/{assignment_id}/deviation",
    response_model=ShiftOut,
    summary="Record separate actual shift facts and structured deviation",
)
def deviation(
    assignment_id: uuid.UUID,
    payload: ShiftDeviationIn,
    principal: Annotated[Principal, Depends(get_current_principal)],
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> ShiftOut:
    return save_deviation(session, principal, employee, assignment_id, payload)


@router.get(
    "/shifts/{assignment_id}/report-preview",
    response_model=ShiftReportPreviewOut,
    summary="Private minimized Shift Report preview; not submitted or disclosed",
)
def preview(
    assignment_id: uuid.UUID,
    employee: Annotated[Employee, Depends(get_current_employee)],
    session: Annotated[Session, Depends(get_db)],
) -> ShiftReportPreviewOut:
    return report_preview(session, employee, assignment_id)


# --- Employee plan entry (pilot slice) -------------------------------------
#
# Each of these three depends on the SHIFT domain's switch and on nothing else: a
# deployment whose shift planning has moved to an authoritative source turns
# ``enable_shift_plan_entry`` off, and OCD entry stays untouched (and vice versa).
# The switch is enforced INSIDE the service commands rather than here, because the
# create command must consult it only AFTER its replay pre-read — an
# already-committed same-owner retry is not a new mutation and must still succeed
# with the switch off. Nothing about creating a plan opens a new read path: the
# employee already reads their own shifts through ``GET /shifts``.


@router.post(
    "/shifts",
    response_model=ShiftOut,
    summary="Record a planned shift for yourself (BE-CMD-001)",
)
def create_plan(
    payload: ShiftPlanIn,
    principal: Annotated[Principal, Depends(get_current_principal)],
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> Response:
    out, created = create_plan_shift(session, principal, employee, payload)
    # 201 for a real create, 200 for an idempotent replay — the house pattern for a
    # create-producing command (``POST /me/work-intervals``).
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        content=out.model_dump(mode="json"),
    )


@router.patch(
    "/shifts/{assignment_id}",
    response_model=ShiftOut,
    summary="Correct a planned shift you recorded",
)
def edit_plan(
    assignment_id: uuid.UUID,
    payload: ShiftPlanPatch,
    principal: Annotated[Principal, Depends(get_current_principal)],
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> ShiftOut:
    return edit_plan_shift(session, principal, employee, assignment_id, payload)


@router.post(
    "/shifts/{assignment_id}/withdraw",
    response_model=ShiftOut,
    summary="Remove a planned shift that was entered by mistake",
)
def withdraw_plan(
    assignment_id: uuid.UUID,
    payload: ShiftWithdrawIn,
    principal: Annotated[Principal, Depends(get_current_principal)],
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> ShiftOut:
    return withdraw_plan_shift(session, principal, employee, assignment_id, payload)
