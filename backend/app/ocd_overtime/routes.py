"""Employee-self OCD/overtime routes: no recipient or HR visibility."""

import uuid
from datetime import date, datetime, time
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth.deps import get_current_employee, get_current_employee_writer, get_current_principal
from app.auth.principal import Principal
from app.db import get_db
from app.errors import ErrorCode, api_error
from app.identity.models import Employee
from app.ocd_overtime.models import CompensationContextType
from app.ocd_overtime.schemas import (
    ElectionIn,
    ElectionOut,
    OcdAssignmentCancelIn,
    OcdAssignmentIn,
    OcdAssignmentPatch,
    OcdCallIn,
    OcdCallOut,
    OcdCallPatch,
    OcdCompensationPreviewOut,
    OcdOut,
    OvertimeAuthorizationIn,
    OvertimeAuthorizationOut,
    OvertimeAuthorizationPatch,
    OvertimePreviewOut,
    VoidIn,
)
from app.ocd_overtime.service import (
    cancel_ocd_assignment,
    correct_ocd_assignment,
    create_authorization,
    create_call,
    create_election,
    create_ocd_assignment,
    get_election,
    list_authorizations,
    list_ocd,
    ocd_preview,
    overtime_preview,
    patch_authorization,
    patch_call,
    void_call,
)
from app.worktime.routes import parse_day
from app.worktime.walltime import BERLIN

router = APIRouter(prefix="/api/v1/me", tags=["ocd-overtime"])


def _range(from_date: str, to_date: str) -> tuple[date, date]:
    start, end = parse_day(from_date), parse_day(to_date)
    if end <= start:
        raise api_error(
            422, ErrorCode.VALIDATION_INVALID_DATE, "The end of the range must be after its start."
        )
    return start, end


@router.get("/ocd", response_model=list[OcdOut])
def read_ocd(
    from_date: Annotated[str, Query(alias="from")],
    to_date: Annotated[str, Query(alias="to")],
    employee: Annotated[Employee, Depends(get_current_employee)],
    session: Annotated[Session, Depends(get_db)],
) -> list[OcdOut]:
    start, end = _range(from_date, to_date)
    return list_ocd(
        session,
        employee,
        datetime.combine(start, time.min, tzinfo=BERLIN),
        datetime.combine(end, time.min, tzinfo=BERLIN),
    )


@router.post("/ocd-calls", response_model=OcdCallOut)
def add_call(
    payload: OcdCallIn,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> OcdCallOut:
    return create_call(session, employee, payload)[0]


@router.patch("/ocd-calls/{call_id}", response_model=OcdCallOut)
def edit_call(
    call_id: uuid.UUID,
    payload: OcdCallPatch,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> OcdCallOut:
    return patch_call(session, employee, call_id, payload)


@router.post("/ocd-calls/{call_id}/void", response_model=OcdCallOut)
def void(
    call_id: uuid.UUID,
    payload: VoidIn,
    principal: Annotated[Principal, Depends(get_current_principal)],
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> OcdCallOut:
    return void_call(session, principal, employee, call_id, payload)


@router.get(
    "/ocd-assignments/{assignment_id}/compensation-preview",
    response_model=OcdCompensationPreviewOut,
)
def ocd_compensation(
    assignment_id: uuid.UUID,
    employee: Annotated[Employee, Depends(get_current_employee)],
    session: Annotated[Session, Depends(get_db)],
) -> OcdCompensationPreviewOut:
    return ocd_preview(session, employee, assignment_id)


@router.post("/overtime-authorizations", response_model=OvertimeAuthorizationOut)
def add_auth(
    payload: OvertimeAuthorizationIn,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> OvertimeAuthorizationOut:
    result = create_authorization(session, employee, payload)
    # EINV-11: `get_db` commits in FastAPI's dependency teardown, which runs
    # AFTER the response is sent. The response acknowledges a policy-relevant
    # state transition (a request now exists), so the row must be durable before
    # the caller regains control. Deliberately local to these two routes — the
    # teardown itself is F7's.
    session.commit()
    return result


@router.patch("/overtime-authorizations/{auth_id}", response_model=OvertimeAuthorizationOut)
def edit_auth(
    auth_id: uuid.UUID,
    payload: OvertimeAuthorizationPatch,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> OvertimeAuthorizationOut:
    result = patch_authorization(session, employee, auth_id, payload)
    session.commit()
    return result


@router.get("/overtime-authorizations", response_model=list[OvertimeAuthorizationOut])
def read_auth(
    from_date: Annotated[str, Query(alias="from")],
    to_date: Annotated[str, Query(alias="to")],
    employee: Annotated[Employee, Depends(get_current_employee)],
    session: Annotated[Session, Depends(get_db)],
) -> list[OvertimeAuthorizationOut]:
    start, end = _range(from_date, to_date)
    return list_authorizations(
        session,
        employee,
        datetime.combine(start, time.min, tzinfo=BERLIN),
        datetime.combine(end, time.min, tzinfo=BERLIN),
    )


@router.get("/overtime/preview", response_model=OvertimePreviewOut)
def overtime(
    from_date: Annotated[str, Query(alias="from")],
    to_date: Annotated[str, Query(alias="to")],
    employee: Annotated[Employee, Depends(get_current_employee)],
    session: Annotated[Session, Depends(get_db)],
) -> OvertimePreviewOut:
    start, end = _range(from_date, to_date)
    return overtime_preview(session, employee, start, end)


@router.get("/compensation-elections", response_model=ElectionOut | None)
def read_election(
    context_type: CompensationContextType,
    context_id: uuid.UUID,
    employee: Annotated[Employee, Depends(get_current_employee)],
    session: Annotated[Session, Depends(get_db)],
) -> ElectionOut | None:
    return get_election(session, employee, context_type, context_id)


@router.post("/compensation-elections", response_model=ElectionOut)
def add_election(
    payload: ElectionIn,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> ElectionOut:
    return create_election(session, employee, payload)


# --- Employee plan entry (pilot slice) -------------------------------------
#
# Each of these three depends on the OCD domain's switch and on nothing else: a
# deployment whose availability planning has moved to an authoritative source turns
# ``enable_ocd_plan_entry`` off, and shift entry stays untouched (and vice versa).
# The switch is enforced INSIDE the service commands rather than here, because the
# create command must consult it only AFTER its replay pre-read — an already-committed
# same-owner retry is not a new mutation and must still succeed with the switch off.
# Nothing here opens a new read path: the employee already reads their own
# availabilities through ``GET /ocd``, and no recipient or HR route is added.


@router.post(
    "/ocd-assignments",
    response_model=OcdOut,
    summary="Record an on-call availability for yourself (BE-CMD-001)",
)
def create_assignment(
    payload: OcdAssignmentIn,
    principal: Annotated[Principal, Depends(get_current_principal)],
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> Response:
    out, created = create_ocd_assignment(session, principal, employee, payload)
    # 201 for a real create, 200 for an idempotent replay — the house pattern for a
    # create-producing command (``POST /me/work-intervals``).
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        content=out.model_dump(mode="json"),
    )


@router.patch(
    "/ocd-assignments/{assignment_id}",
    response_model=OcdOut,
    summary="Correct an on-call availability you recorded",
)
def edit_assignment(
    assignment_id: uuid.UUID,
    payload: OcdAssignmentPatch,
    principal: Annotated[Principal, Depends(get_current_principal)],
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> OcdOut:
    return correct_ocd_assignment(session, principal, employee, assignment_id, payload)


@router.post(
    "/ocd-assignments/{assignment_id}/cancel",
    response_model=OcdOut,
    summary="Cancel an on-call availability you recorded",
)
def cancel_assignment(
    assignment_id: uuid.UUID,
    payload: OcdAssignmentCancelIn,
    principal: Annotated[Principal, Depends(get_current_principal)],
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> OcdOut:
    return cancel_ocd_assignment(session, principal, employee, assignment_id, payload)
