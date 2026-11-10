"""Identity/profile API schemas (WT-BACKEND §8.1 /me, /me/profile, /me/preferences)."""

import uuid
from datetime import date, time

from app.enums import MasterDataSource, PayGroupBucket, WorkType
from pydantic import BaseModel, Field


class IdentityOut(BaseModel):
    """Identity card for ANY authenticated principal.

    Deliberately contains zero employee data: it is the answer to "who am I",
    not a profile endpoint. Mapping to employee-owned data happens in
    GET /me/profile (employee-only).
    """

    subject: str
    display_name: str
    # This is the current principal's own authorization context, not employee
    # data. Client navigation uses it only for presentation; server routes
    # remain authoritative.
    capabilities: list[str]


class EmployeeOut(BaseModel):
    id: uuid.UUID
    external_employee_id: str | None
    legal_name: str
    email: str | None
    active: bool


class ContractOut(BaseModel):
    valid_from: date
    valid_to: date | None
    fte: float | None
    weekly_required_minutes: int
    pay_group_bucket: PayGroupBucket
    source: MasterDataSource
    version: int


class ScheduleOut(BaseModel):
    valid_from: date
    valid_to: date | None
    weekly_required_minutes: int
    mon_required_minutes: int
    tue_required_minutes: int
    wed_required_minutes: int
    thu_required_minutes: int
    fri_required_minutes: int
    sat_required_minutes: int
    sun_required_minutes: int
    mon_typical_start: time | None
    mon_typical_end: time | None
    tue_typical_start: time | None
    tue_typical_end: time | None
    wed_typical_start: time | None
    wed_typical_end: time | None
    thu_typical_start: time | None
    thu_typical_end: time | None
    fri_typical_start: time | None
    fri_typical_end: time | None
    sat_typical_start: time | None
    sat_typical_end: time | None
    sun_typical_start: time | None
    sun_typical_end: time | None
    source: MasterDataSource
    version: int


_WEEKDAY_FIELD = Field(
    ge=0,
    le=1440,
    strict=True,
    description="Required integer minutes for this weekday (0..1440).",
)


class WorkingTimePut(BaseModel):
    """Employee-maintained working time.

    Machine contract in minutes; the UI presents human-readable durations.
    The weekday allocation must sum exactly to ``weekly_required_minutes``
    (validated in the service so the error message can state both values).
    """

    valid_from: date
    weekly_required_minutes: int = Field(ge=0, strict=True)
    mon_required_minutes: int = _WEEKDAY_FIELD
    tue_required_minutes: int = _WEEKDAY_FIELD
    wed_required_minutes: int = _WEEKDAY_FIELD
    thu_required_minutes: int = _WEEKDAY_FIELD
    fri_required_minutes: int = _WEEKDAY_FIELD
    sat_required_minutes: int = _WEEKDAY_FIELD
    sun_required_minutes: int = _WEEKDAY_FIELD


class ProfileOut(BaseModel):
    """Effective-dated profile at a point in time (INV-07)."""

    effective_at: date
    employee: EmployeeOut
    contract: ContractOut | None
    schedule: ScheduleOut | None
    preferences: "PreferencesOut"


class NormalDayTemplateInterval(BaseModel):
    """A convenience pattern which an employee must explicitly apply.

    This remains a preference, never an assertion that the employee worked
    these times (FE-UX-001/002 and BE-CMD-003).
    """

    start: time
    end: time
    ends_next_day: bool = False
    work_type: WorkType
    comment: str | None = Field(default=None, max_length=500)


class PreferencesPatch(BaseModel):
    """Employee preferences only (BE §5.1:205: convenience, never HR facts).

    The normal-day template is structured and validated here, but it remains
    a convenience preference until the employee explicitly applies it.
    """

    usual_work_type: WorkType | None = Field(default=None)
    preferred_calendar_view: str | None = Field(default=None, max_length=20)
    normal_day_template: list[NormalDayTemplateInterval] | None = Field(default=None, max_length=8)


class PreferencesOut(BaseModel):
    usual_work_type: WorkType | None
    preferred_calendar_view: str | None
    normal_day_template: list[NormalDayTemplateInterval] | None
