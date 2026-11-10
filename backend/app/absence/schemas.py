"""Day-status command and view schemas (BE-ENUM-002, narrowed).

``DayStatusPatch`` is deliberately **presence-based** (``model_fields_set``
drives what changes): the ``EINV-12`` lesson is that a full-row replace built
from a create schema silently resets whatever the client omitted.
"""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field, model_validator

from app.enums import DayStatusCategory, DayStatusDayPart, DayStatusPortion

_HALF_PARTS = frozenset({DayStatusDayPart.MORNING, DayStatusDayPart.AFTERNOON})


def _check_portion(portion: DayStatusPortion, day_part: DayStatusDayPart) -> None:
    if portion is DayStatusPortion.FULL and day_part is not DayStatusDayPart.ALL:
        raise ValueError("a FULL day status covers the whole day; day_part must be ALL")
    if portion is DayStatusPortion.HALF and day_part not in _HALF_PARTS:
        raise ValueError("a HALF day status must name MORNING or AFTERNOON")


class DayStatusCreate(BaseModel):
    day: date
    category: DayStatusCategory
    portion: DayStatusPortion
    day_part: DayStatusDayPart = DayStatusDayPart.ALL
    client_request_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "DayStatusCreate":
        _check_portion(self.portion, self.day_part)
        return self


class DayStatusPatch(BaseModel):
    """Presence-based correction. ``version`` is the optimistic-lock token."""

    version: int = Field(ge=1)
    category: DayStatusCategory | None = None
    portion: DayStatusPortion | None = None
    day_part: DayStatusDayPart | None = None
    reason: str | None = Field(None, max_length=500)


class DayStatusOut(BaseModel):
    """Employee-facing view.

    Deliberately WITHOUT ``external_category``: that is HR-side provenance the
    employee never authored (ACCESS-016/ABSENCE-004), and it is not part of the
    private ledger view.
    """

    id: uuid.UUID
    day: date
    category: DayStatusCategory
    portion: DayStatusPortion
    day_part: DayStatusDayPart
    version: int
    supersedes_id: uuid.UUID | None
    created_at: datetime


class CompensationLimitOut(BaseModel):
    """A source-backed LIMIT, named as a limit and explicitly not enforced.

    ``enforced`` is ``False`` while the counting treatment is unresolved
    (``OPEN-13``): the figures are evidence, the application of them is not.
    """

    max_days_per_year: int
    max_consecutive_days: int
    source: str
    enforced: bool


class CompensationMetricsOut(BaseModel):
    """Read-only counts of recorded compensation facts (F6.4/EINV-25).

    Deliberately contains **no finding and no verdict**: no ``WT-FLEX-008`` entry
    is emitted at any count, full and half days are never combined, and no field
    is named "remaining", "entitled" or "exceeded". The window is *named*
    (``year``) rather than presented as the rule's own definition.
    """

    year: int
    flex_compensation_full_days: int
    flex_compensation_half_days: int
    flex_compensation_zero_requirement_days: int
    flex_compensation_longest_run_days: int
    shift_compensation_days: int
    limits: CompensationLimitOut
    counting_note: str


class DayStatusMonthOut(BaseModel):
    period: str
    entries: list[DayStatusOut]
    compensation_metrics: CompensationMetricsOut
