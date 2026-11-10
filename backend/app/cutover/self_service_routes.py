"""Employee self-onboarding routes (``/api/v1/me/onboarding``).

Routes on the EXISTING employee baseline capability (``OWN_LEDGER_WRITE``): no
new capability is introduced and ``CUTOVER_SEED_WRITE`` is deliberately NOT
required or granted, so the operator route stays the only operator path.

The target is always the authenticated employee — no employee id is accepted
anywhere, so there is no IDOR surface and no way to reach another employee's
opening state.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth.deps import get_current_employee, get_current_employee_writer
from app.cutover.schemas import (
    OnboardingCorrectionIn,
    OnboardingIn,
    OnboardingStateOut,
)
from app.cutover.self_service import (
    OnboardingRejected,
    correct_opening_state,
    establish_opening_state,
    onboarding_state,
)
from app.db import get_db
from app.identity.models import Employee

router = APIRouter(prefix="/api/v1/me/onboarding", tags=["onboarding"])


def _refusal(result: OnboardingRejected) -> JSONResponse:
    """The refusal envelope, byte-identical in shape to the operator route's."""
    return JSONResponse(
        status_code=result.status_code,
        content={
            "detail": {
                "code": result.code.value,
                "message": result.message,
                "details": {"reason": result.reason},
            }
        },
    )


@router.get("", response_model=OnboardingStateOut, summary="Month-aware onboarding state")
def get_onboarding_state(
    employee: Annotated[Employee, Depends(get_current_employee)],
    session: Annotated[Session, Depends(get_db)],
    start_month: str | None = None,
) -> OnboardingStateOut:
    """Describes the readiness of the REQUESTED month (the current Berlin month
    when ``start_month`` is omitted). Advisory only — POST is authoritative."""
    return onboarding_state(session, employee, start_month=start_month)


@router.post(
    "",
    response_model=OnboardingStateOut,
    status_code=status.HTTP_201_CREATED,
    summary="Establish the one-time opening state (schedule + carried balance)",
)
def post_onboarding(
    payload: OnboardingIn,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> OnboardingStateOut | JSONResponse:
    """One atomic, one-time, audited cutover action. The employee owns the
    working-time setup (no contract/master-data prerequisite)."""
    result = establish_opening_state(session, employee, payload)
    return _refusal(result) if isinstance(result, OnboardingRejected) else result


@router.patch(
    "",
    response_model=OnboardingStateOut,
    summary="Correct the carried balance while the opening state is unused",
)
def patch_onboarding(
    payload: OnboardingCorrectionIn,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> OnboardingStateOut | JSONResponse:
    """Append-only correction of the opening balance, allowed only while the
    opening state is genuinely unused. After that the window closes permanently
    and the employee is told to contact the administrator — no administrative
    workflow is claimed to exist."""
    result = correct_opening_state(session, employee, payload)
    return _refusal(result) if isinstance(result, OnboardingRejected) else result
