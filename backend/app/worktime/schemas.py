"""Worktime API schemas (WT-BACKEND §7 BE-CMD-001/002, §8.1).

Wall-time inputs are NAIVE datetimes interpreted in Europe/Berlin
aware input is rejected. ``fold`` disambiguates fall-back
DST times only (DATA-009/010); ``timezone`` is a Literal defaulting to
Europe/Berlin — no travel-timezone feature exists yet.
"""

import uuid
from datetime import date, datetime
from typing import Literal

from app.enums import BreakKind, IntervalSource, PeriodStatus, WorkType
from app.policy.engine import CalendarClass
from pydantic import BaseModel, Field, field_validator


class IntervalIn(BaseModel):
    """BE-CMD-001 — create work interval."""

    start_local: datetime
    end_local: datetime
    timezone: Literal["Europe/Berlin"] = "Europe/Berlin"
    work_type: WorkType
    comment: str | None = Field(default=None, max_length=500)
    client_request_id: uuid.UUID | None = None
    fold: Literal[0, 1] | None = None

    @field_validator("start_local", "end_local")
    @classmethod
    def _reject_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is not None:
            raise ValueError("wall times must be naive; the timezone field selects the zone")
        return value


class IntervalPatch(BaseModel):
    """BE-CMD-002 — correct interval (requires the current version)."""

    version: int = Field(ge=1)
    start_local: datetime | None = None
    end_local: datetime | None = None
    timezone: Literal["Europe/Berlin"] = "Europe/Berlin"
    work_type: WorkType | None = None
    comment: str | None = Field(default=None, max_length=500)
    fold: Literal[0, 1] | None = None
    # Per-boundary folds: an interval may start before and end inside (or
    # straddle) the ambiguous fall-back hour, so one shared ``fold`` cannot
    # describe both boundaries. ``fold`` stays as a legacy fallback.
    start_fold: Literal[0, 1] | None = None
    end_fold: Literal[0, 1] | None = None
    reason: str | None = Field(default=None, max_length=500)
    # Attach or clear an employee-recorded overtime authorization
    # reference (ALLOW-001/002; only OVERTIME_* intervals may carry a link).
    # Explicit null clears the link; absent leaves it unchanged.
    overtime_authorization_id: uuid.UUID | None = None

    @field_validator("start_local", "end_local")
    @classmethod
    def _reject_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            raise ValueError("wall times must be naive; the timezone field selects the zone")
        return value


class IntervalDeleteBody(BaseModel):
    """DELETE body (free text stays out of URLs/access logs)."""

    reason: str | None = Field(default=None, max_length=500)


class IntervalOut(BaseModel):
    id: uuid.UUID
    work_type: WorkType
    start_at: datetime
    end_at: datetime
    start_local: datetime
    end_local: datetime
    elapsed_seconds: int
    comment: str | None
    source: IntervalSource
    version: int
    supersedes_id: uuid.UUID | None
    client_request_id: uuid.UUID | None


class DaySummary(BaseModel):
    date: date
    interval_count: int


class DailyCalculationOut(BaseModel):
    """Server-derived day result. Never editable client state (BE-POL-003)."""

    date: date
    worked_minutes: int
    required_minutes: int
    delta_minutes: int
    # TRAVEL-003: passive-travel minutes credited to close this day's
    # deficit. Derived display info — NOT worked time (TRAVEL-001).
    travel_topup_minutes: int = 0
    # The resolved CALENDAR classification of the day (F6/E8). The employee sees
    # WHY a day's requirement is zero, without inferring it from the number: a
    # public holiday and an employer-free day are institutional classifications,
    # not employee statuses (INV-10/EINV-21). Deliberately NOT part of the frozen
    # disclosure payload.
    calendar_class: CalendarClass
    # The minutes this day CONTRIBUTES to the time account. ``delta_minutes`` is
    # derived from it (`account_credit - required`), so without the field a
    # corrected credit rule shows as an unexplained arithmetic difference.
    # Employee-self projection only; never in the frozen disclosure payload.
    account_credit_minutes: int


class MonthSummary(BaseModel):
    period_month: date
    status: PeriodStatus
    interval_count: int
    days: list[DaySummary]
    daily_results: list[DailyCalculationOut]
    closing_balance_minutes: int


class BreakIntervalIn(BaseModel):
    """Create an explicit factual break (WT-BACKEND §5.3 ``break_interval``)."""

    start_local: datetime
    end_local: datetime
    timezone: Literal["Europe/Berlin"] = "Europe/Berlin"
    kind: BreakKind
    linked_shift_assignment_id: uuid.UUID | None = None
    client_request_id: uuid.UUID | None = None
    fold: Literal[0, 1] | None = None

    @field_validator("start_local", "end_local")
    @classmethod
    def _reject_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is not None:
            raise ValueError("wall times must be naive; the timezone field selects the zone")
        return value


class BreakIntervalPatch(BaseModel):
    """Correct a break (superseding version + lineage; requires the current version)."""

    version: int = Field(ge=1)
    start_local: datetime | None = None
    end_local: datetime | None = None
    timezone: Literal["Europe/Berlin"] = "Europe/Berlin"
    kind: BreakKind | None = None
    fold: Literal[0, 1] | None = None
    start_fold: Literal[0, 1] | None = None
    end_fold: Literal[0, 1] | None = None
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("start_local", "end_local")
    @classmethod
    def _reject_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            raise ValueError("wall times must be naive; the timezone field selects the zone")
        return value


class BreakIntervalOut(BaseModel):
    id: uuid.UUID
    kind: BreakKind
    start_at: datetime
    end_at: datetime
    start_local: datetime
    end_local: datetime
    elapsed_seconds: int
    linked_shift_assignment_id: uuid.UUID | None
    version: int
    supersedes_id: uuid.UUID | None
    client_request_id: uuid.UUID | None


class DayDetail(BaseModel):
    date: date
    intervals: list[IntervalOut]
    # Explicit factual breaks (WT-BACKEND §5.3). Separate from intervals: a
    # break is not work, and PAID_SHIFT_BREAK is compensated presence rather
    # than a gap (SHIFT-003). Empty for the ordinary case, where breaks are
    # inferred from gaps between intervals.
    breaks: list[BreakIntervalOut] = []


class ApplyTemplateIn(BaseModel):
    """Explicit employee-selected dates for a saved normal-day template."""

    dates: list[date] = Field(min_length=1, max_length=7)
    client_request_id: uuid.UUID | None = None


class CopyPreviousIn(BaseModel):
    """Optional command idempotency key for copy-previous-day (retry-safe)."""

    client_request_id: uuid.UUID | None = None


class ReviewMonthIn(BaseModel):
    """Employee-side month review marker; never a disclosure event."""

    acknowledged: bool = False


class SplitIntervalIn(BaseModel):
    """Atomically split one current interval into two factual intervals."""

    version: int = Field(ge=1)
    split_local: datetime
    timezone: Literal["Europe/Berlin"] = "Europe/Berlin"
    fold: Literal[0, 1] | None = None

    @field_validator("split_local")
    @classmethod
    def _reject_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is not None:
            raise ValueError("wall times must be naive; the timezone field selects the zone")
        return value
