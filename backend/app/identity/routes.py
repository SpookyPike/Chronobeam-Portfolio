"""/me identity endpoints (WT-BACKEND §8.1 Employee/self subset).

GET /me is the identity card for ANY authenticated principal (zero employee
data). GET /me/profile and PATCH /me/preferences are employee-only.
"""

from datetime import date
from typing import Annotated

from app.auth.deps import get_current_employee, get_current_employee_writer, get_current_principal
from app.auth.principal import Principal
from app.db import get_db
from app.identity.models import Employee
from app.identity.schemas import (
    IdentityOut,
    PreferencesOut,
    PreferencesPatch,
    ProfileOut,
    ScheduleOut,
    WorkingTimePut,
)
from app.identity.service import profile, set_working_time, upsert_preferences
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

router = APIRouter(prefix="/api/v1/me", tags=["identity"])


@router.get("", response_model=IdentityOut, summary="Identity card (any authenticated principal)")
def me(principal: Annotated[Principal, Depends(get_current_principal)]) -> IdentityOut:
    return IdentityOut(
        subject=principal.subject,
        display_name=principal.display_name,
        capabilities=sorted(capability.value for capability in principal.capabilities),
    )


@router.get("/profile", response_model=ProfileOut, summary="Effective-dated profile/schedule")
def get_profile(
    employee: Annotated[Employee, Depends(get_current_employee)],
    session: Annotated[Session, Depends(get_db)],
    at: date | None = None,
) -> ProfileOut:
    return profile(session, employee, at)


@router.patch("/preferences", response_model=PreferencesOut, summary="Employee preferences only")
def patch_preferences(
    payload: PreferencesPatch,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> PreferencesOut:
    changes: dict[str, object] = {}
    if "usual_work_type" in payload.model_fields_set:
        changes["usual_work_type"] = payload.usual_work_type
    if "preferred_calendar_view" in payload.model_fields_set:
        changes["preferred_calendar_view"] = payload.preferred_calendar_view
    if "normal_day_template" in payload.model_fields_set:
        changes["normal_day_template_json"] = (
            [item.model_dump(mode="json") for item in payload.normal_day_template]
            if payload.normal_day_template is not None
            else None
        )
    return upsert_preferences(session, employee, changes)


@router.put(
    "/working-time",
    response_model=ScheduleOut,
    summary="Set employee-maintained weekly target and weekday pattern",
)
def put_working_time(
    payload: WorkingTimePut,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> ScheduleOut:
    """Employee-owned configuration: does not touch the HR contract."""
    return set_working_time(session, employee, payload)
