"""Cutover opening-state request/response schemas (BE spec §5.8)."""

import uuid
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.identity.schemas import ScheduleOut


class CutoverSeedIn(BaseModel):
    """One-time validated legacy closing state for one employee.

    Only the continuity values are accepted — never workbook contents,
    historical intervals, or day statuses (CUTOVER-T01..03).
    """

    model_config = ConfigDict(extra="forbid")

    employee_external_id: str = Field(min_length=1, max_length=64)
    cutover_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    minutes: int = Field(ge=-100_000, le=100_000)


class CutoverSeedOut(BaseModel):
    seed_id: uuid.UUID
    employee_id: uuid.UUID
    cutover_month: str
    occurred_on: date
    minutes: int
    previous_period: str
    confirmed_by: str


# The one month shape this surface accepts: the POST body pattern and the
# GET query validation are the same literal, so they cannot drift.
MONTH_PATTERN: str = r"^\d{4}-(0[1-9]|1[0-2])$"


class OnboardingState(StrEnum):
    """Describes the month whose readiness was REQUESTED, never a guess.

    ``BLOCKED`` means that month lacks a prerequisite; it is a permanent
    property of the month, not a transient fault. There is deliberately no
    "not yet chosen" state: the read always names a month.
    """

    READY = "READY"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"


class OnboardingBlockedReason(StrEnum):
    POLICY_EDITION_MISSING = "POLICY_EDITION_MISSING"
    CALENDAR_MISSING = "CALENDAR_MISSING"
    ROLLOUT_BOUNDARY = "ROLLOUT_BOUNDARY"


class OpeningInUseReason(StrEnum):
    """Why the opening state can no longer be self-corrected (D6)."""

    IN_USE_BY_FACTS = "IN_USE_BY_FACTS"
    IN_USE_BY_REVIEW = "IN_USE_BY_REVIEW"
    IN_USE_BY_DISCLOSURE = "IN_USE_BY_DISCLOSURE"


class OpeningStateOut(BaseModel):
    """The established one-time opening state, month-INDEPENDENT.

    ``cutover_month`` is the employee's ACTUAL start month, set at establishment
    and immutable afterwards. It is deliberately a different field from the
    response's ``month`` (the month whose readiness was requested): they coincide
    only by accident of the default before establishment, and after onboarding
    ``month`` follows whatever the employee browses while this stays fixed.
    """

    cutover_month: str
    minutes: int  # effective total = seed + every correction delta
    seed_minutes: int
    occurred_on: date
    previous_period: str
    confirmed_by: str
    # Optimistic-concurrency token, DERIVED from the append-only facts
    # (1 + correction count). No fake entity version is stored anywhere.
    revision: int
    # WHEN the last correction was made. Deliberately the correction row's own
    # creation instant, not its `occurred_on`: every opening-state row shares the
    # seed's date (M-1's last day), which would report a date that can be LATER
    # than the correction itself.
    corrected_at: datetime | None


class OnboardingStateOut(BaseModel):
    state: OnboardingState
    # The month whose readiness was requested (the current Berlin month when
    # `start_month` is omitted).
    month: str
    # Earliest month at/after the current Berlin month with BOTH policy and
    # calendar coverage; null when no covered month exists yet.
    default_start_month: str | None
    opening: OpeningStateOut | None
    blocked_reason: OnboardingBlockedReason | None
    blocked_month: str | None
    # Surface-only warning (never a block): the month already contains facts, so
    # establishing the opening state will recalculate it.
    has_facts_in_month: bool
    correctable: bool
    in_use_reason: OpeningInUseReason | None
    schedule: ScheduleOut | None


class OnboardingIn(BaseModel):
    """The employee's one-time starting state (D1: the employee owns this).

    Exactly the existing working-time shape plus the start month and the carried
    balance — no second schedule mechanism, and no contract/master-data
    prerequisite: for this MVP the employee's own entry is the only source.
    """

    model_config = ConfigDict(extra="forbid")

    start_month: str = Field(pattern=MONTH_PATTERN)
    weekly_required_minutes: int = Field(ge=0, strict=True)
    mon_required_minutes: int = Field(ge=0, le=1440, strict=True)
    tue_required_minutes: int = Field(ge=0, le=1440, strict=True)
    wed_required_minutes: int = Field(ge=0, le=1440, strict=True)
    thu_required_minutes: int = Field(ge=0, le=1440, strict=True)
    fri_required_minutes: int = Field(ge=0, le=1440, strict=True)
    sat_required_minutes: int = Field(ge=0, le=1440, strict=True)
    sun_required_minutes: int = Field(ge=0, le=1440, strict=True)
    # Signed, and mirrors the operator seed's bounds exactly.
    opening_balance_minutes: int = Field(ge=-100_000, le=100_000)


class OnboardingCorrectionIn(BaseModel):
    """A bounded pre-use correction of the carried balance (D5-D9).

    ``opening_balance_minutes`` is the CORRECTED TOTAL, not a delta — the delta is
    computed under the lock. ``expected_revision`` is the optimistic token the
    employee last read.
    """

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    opening_balance_minutes: int = Field(ge=-100_000, le=100_000)
    reason: str = Field(min_length=1, max_length=500)
