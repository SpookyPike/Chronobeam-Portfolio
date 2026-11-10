"""Compensation-day METRICS — read-only, and never a policy verdict (F6.4/D5).

F6 exposes *counts* of the facts the employee recorded. It deliberately emits
**no finding**: ``WT-FLEX-008`` stays recorded and not enforced while the
counting treatment is unresolved.

Two independent reasons, both recorded in the frozen F6 plan:

1. **The counting semantics are unresolved.** The supplied sources state the
   limits (the sample policy sets **20 days per year** and **no more than five
   consecutive days**) but not how a HALF
   ``FLEX_COMPENSATION`` day counts against them, nor the precise meaning of
   "per year". Inventing a counting rule would be a stricter rule presented as
   authority (``INV-15``) — so the figures are reported as *limits*, marked
   ``enforced: false``, with the unresolved detail named in ``counting_note``.
2. **"A status row exists" is not established to mean "a policy day consumed".**
   A ``FLEX_COMPENSATION`` row on a day whose SCHEDULED requirement is zero (a
   weekend, a public holiday, an employer-free day, a schedule-less day) is a
   record of intent. The discriminator is the existing one: ``WT-STATUS-005``
   fires exactly when a day-status fact sits on a ``scheduled == 0`` day, and
   ``scheduled`` comes from the employee's effective schedule. Those rows are
   therefore surfaced on their own and excluded from the two counts.

``EINV-25``: a metric is **not a calculation input**. These numbers are computed
on read from the facts they describe; they do not enter ``PeriodInputs``, the
input fingerprint or the ``EINV-15`` reopen set, and they never gate
``review_month``. A year-scoped count cannot be an engine input without making
one month's result depend on other months' facts.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from app.absence.models import DayStatusEvent
from app.absence.schemas import CompensationLimitOut, CompensationMetricsOut
from app.enums import DayStatusCategory, DayStatusPortion
from app.identity.models import Employee
from app.policy.service import _requirements

#: Source-backed figures, reported as LIMITS, never as an entitlement.
MAX_DAYS_PER_YEAR = 20
MAX_CONSECUTIVE_DAYS = 5
LIMIT_SOURCE = (
    "POL-01 sample policy: flexible time compensation is capped at 20 days per "
    "calendar year, and no more than five consecutive days may be taken. These "
    "are sample parameters: configuration values, not a legal requirement."
)

#: The honesty requirement: the employee must not read a count as a verdict.
#:
#: Only the detail that is STILL open is named. The year scope is settled, so the
#: guidance names only the half-day calculation, which is still an open question.
COUNTING_NOTE = (
    "This is guidance, not a limit we apply. How a half day counts towards the "
    "20-day limit is an open configuration question."
)


@dataclass(frozen=True)
class CompensationMetrics:
    """Year-scoped counts of recorded compensation facts."""

    year: int
    flex_compensation_full_days: int
    flex_compensation_half_days: int
    flex_compensation_zero_requirement_days: int
    flex_compensation_longest_run_days: int
    shift_compensation_days: int
    max_days_per_year: int
    max_consecutive_days: int
    limit_source: str
    counting_note: str


def _current_rows(session: Session, employee_id: uuid.UUID, year: int) -> list[DayStatusEvent]:
    """Current-version day-status rows of one calendar year (the read predicate
    every sibling reader shares: a corrected row must not be counted twice)."""
    child = aliased(DayStatusEvent)
    return list(
        session.scalars(
            select(DayStatusEvent).where(
                DayStatusEvent.employee_id == employee_id,
                DayStatusEvent.day >= date(year, 1, 1),
                DayStatusEvent.day <= date(year, 12, 31),
                DayStatusEvent.category.in_(
                    [DayStatusCategory.FLEX_COMPENSATION, DayStatusCategory.SHIFT_COMPENSATION]
                ),
                ~exists(select(child.id).where(child.supersedes_id == DayStatusEvent.id)),
            )
        ).all()
    )


def _longest_run(days: set[date]) -> int:
    """Longest run of consecutive local dates (an OBSERVED run, not a verdict)."""
    longest = 0
    for day in sorted(days):
        # Counting forward from each run's FIRST day is linear; walking backwards
        # from every day would be O(n^2) for the same answer.
        if day - timedelta(days=1) in days:
            continue
        length = 1
        cursor = day + timedelta(days=1)
        while cursor in days:
            length += 1
            cursor += timedelta(days=1)
        longest = max(longest, length)
    return longest


def compensation_metrics(session: Session, employee: Employee, period: date) -> CompensationMetrics:
    """Count the compensation facts recorded in ``period``'s calendar year.

    The window is the calendar year of the requested month and is *named in the
    response* rather than presented as the rule's own definition, because
    ``OPEN-13`` leaves "per year" unresolved.
    """
    rows = _current_rows(session, employee.id, period.year)
    flex = [row for row in rows if row.category is DayStatusCategory.FLEX_COMPENSATION]
    shift = [row for row in rows if row.category is DayStatusCategory.SHIFT_COMPENSATION]

    scheduled: dict[date, int] = {}
    flex_days = sorted({row.day for row in flex})
    if flex_days:
        # One pass over the observed span, using the SAME effective-schedule path
        # the engine uses, so the zero-requirement split reuses WT-STATUS-005's
        # existing discriminator instead of inventing one.
        scheduled = _requirements(
            session, employee, flex_days[0], flex_days[-1] + timedelta(days=1)
        )

    def _is_consumed(day: date) -> bool:
        return scheduled.get(day, 0) > 0

    full_days = {
        row.day for row in flex if row.portion is DayStatusPortion.FULL and _is_consumed(row.day)
    }
    half_days = {
        row.day for row in flex if row.portion is DayStatusPortion.HALF and _is_consumed(row.day)
    }
    zero_requirement = {row.day for row in flex if not _is_consumed(row.day)}

    return CompensationMetrics(
        year=period.year,
        # Reported SEPARATELY and never combined: combining them IS the
        # unresolved weighting decision.
        flex_compensation_full_days=len(full_days),
        flex_compensation_half_days=len(half_days),
        flex_compensation_zero_requirement_days=len(zero_requirement),
        flex_compensation_longest_run_days=_longest_run(full_days),
        # Never deducted from the flex figures (POL-01: shift compensation days are
        # not deducted from the 20 flex time compensation days).
        shift_compensation_days=len({row.day for row in shift}),
        max_days_per_year=MAX_DAYS_PER_YEAR,
        max_consecutive_days=MAX_CONSECUTIVE_DAYS,
        limit_source=LIMIT_SOURCE,
        counting_note=COUNTING_NOTE,
    )


def compensation_metrics_out(
    session: Session, employee: Employee, period: date
) -> CompensationMetricsOut:
    """The API projection of the counts — one mapping, in one place."""
    metrics = compensation_metrics(session, employee, period)
    return CompensationMetricsOut(
        year=metrics.year,
        flex_compensation_full_days=metrics.flex_compensation_full_days,
        flex_compensation_half_days=metrics.flex_compensation_half_days,
        flex_compensation_zero_requirement_days=metrics.flex_compensation_zero_requirement_days,
        flex_compensation_longest_run_days=metrics.flex_compensation_longest_run_days,
        shift_compensation_days=metrics.shift_compensation_days,
        limits=CompensationLimitOut(
            max_days_per_year=metrics.max_days_per_year,
            max_consecutive_days=metrics.max_consecutive_days,
            source=metrics.limit_source,
            enforced=False,
        ),
        counting_note=metrics.counting_note,
    )
