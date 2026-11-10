"""Demo data seed (dev/test only, explicit action).

Provisions the whole deterministic demo state in one action::

    just demo-reset && just demo-seed

What it publishes:

- **The institutional calendar.** The Schleswig-Holstein statutory day set
  (``SFTG`` §2(1)) for the entire MVP horizon, from ``SH_HORIZON_FROM``
  (2026-01-01) to ``SH_HORIZON_TO`` (2035-12-31), through the ONE calendar writer.
  ``just demo-reset`` gives this database a fresh schema, so nothing competes for
  2026-01-01 and no month of the horizon is left as ordinary working days — the
  compromise the *dev* seed makes (see ``app.dev_seed``) is deliberately not
  inherited here.
- **Fourteen fictional personas** covering the product's paths. Each spec carries
  a ``covers`` note naming the path it exists to demonstrate. Role assignments are
  DB rows, so the demo shows the real role model rather than fixture privileges.
- **Each persona's facts**, written through the same service seams the adapters and
  commands use (``create_interval_instant``, ``create_day_status``, ``create_trip``,
  ``create_call``, ``create_authorization``/``decide_authorization``,
  ``seed_shift_assignment``, ``seed_ocd_assignment``, ``seed_opening_balance``,
  ``create_submission_package``), so every guard, ledger invariant and derived
  finding applies exactly as in production. Nothing is written by raw model
  construction.

Determinism: every seeded fact carries a fixed ``client_request_id`` derived from a
stable namespace (``_demo_uuid``), so re-running the seed replays existing facts
instead of duplicating them, and two runs from an empty schema produce the same
domain state. Server-set ``created_at``/``updated_at`` are the only values that
differ between runs.

All identities are fictional and synthetic; no real PII. Refuses unless
CHRONOBEAM_ENV is exactly ``dev``.
"""

import sys
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.absence.schemas import DayStatusCreate
from app.absence.service import create_day_status
from app.auth.dev_principals import DEV_PRINCIPALS
from app.auth.models import RoleAssignment
from app.auth.principal import Capability, Principal
from app.calendar.holidays import (
    SH_HORIZON_FROM,
    SH_HORIZON_TO,
    statutory_days,
    statutory_source,
    statutory_version,
)
from app.calendar.models import CalendarVersion
from app.calendar.service import publish_calendar_edition
from app.config import get_settings
from app.cutover.service import CutoverSeedRejected, seed_opening_balance
from app.db import get_session_factory
from app.disclosure.schemas import AssignmentSubmissionScope
from app.disclosure.service import create_submission_package
from app.enums import (
    BreakKind,
    DayStatusCategory,
    DayStatusDayPart,
    DayStatusPortion,
    MasterDataSource,
    PayGroupBucket,
    ShiftDeviationType,
    SubmissionPurpose,
    WorkType,
)
from app.identity.models import Employee, EmploymentContract, WorkScheduleVersion
from app.integrations.shift_plan import ShiftAssignmentSeed
from app.ocd_overtime.models import OcdCallMode, OvertimeAuthorization, OvertimeAuthStatus
from app.ocd_overtime.schemas import (
    OcdCallIn,
    OvertimeAuthorizationDecisionIn,
    OvertimeAuthorizationIn,
)
from app.ocd_overtime.service import (
    create_authorization,
    create_call,
    decide_authorization,
    seed_ocd_assignment,
)
from app.shift.models import ShiftAssignment, ShiftDeviation
from app.shift.schemas import ShiftDeviationIn
from app.shift.service import report_preview, save_deviation, seed_shift_assignment
from app.travel.models import TravelSegmentClassification, TripStartOrigin
from app.travel.schemas import SegmentIn, TripIn
from app.travel.service import create_segment, create_trip
from app.worktime.models import WorkInterval
from app.worktime.schemas import BreakIntervalIn
from app.worktime.service import create_break_interval, create_interval_instant

BERLIN = ZoneInfo("Europe/Berlin")

#: The demo lands on SEPTEMBER 2026 — the month every persona was onboarded in —
#: and the seeded facts run through 8 October 2026, so the month in progress is
#: full rather than half-empty. Deterministic on purpose: the demo state is
#: authored, not derived from the wall clock, so a rehearsal and the recorded run
#: match.
DEMO_MONTH = date(2026, 9, 1)
DEMO_FROM = date(2026, 9, 1)
DEMO_TO = date(2026, 10, 8)

#: The month every persona's start state is established in. "Onboarded in
#: September" IS the demo's story: the service went live on 1 September, so each
#: employee's opening balance is seeded at the August boundary (M-1, the ordinary
#: ``CUTOVER_SEED`` convention) and no fact may predate it — the clean-cutover
#: guard refuses a start state for an employee who already holds pre-cutover
#: facts, which is exactly why the onboarding persona stays fact-free.
DEMO_CUTOVER_MONTH = "2026-09-01"

#: The opening balance carried in at that boundary. Zero for most personas (the
#: honest default: they start clean), with a real carry on the two the demo uses
#: to show a balance that did not start at zero.
DEMO_OPENING_MINUTES: dict[str, int] = {
    "demo-overtime": 240,  # the carried-forward credit its story turns on
    "demo-findings": -180,  # a small debt, so the findings view has a balance too
}

#: The three standard shift windows, in the order the blocks rotate. Each is 8:30;
#: the third crosses midnight, which the end DATE expresses directly.
SHIFT_WINDOWS: list[tuple[str, str]] = [
    ("07:00", "15:30"),
    ("15:00", "23:30"),
    ("23:00", "07:30"),
]

#: How many consecutive days one window is worked before the block rotates.
SHIFT_BLOCK_DAYS = 3

#: The recipient every seeded package is addressed to. Registered in the dev/test
#: recipient registry (``app.disclosure.service.valid_recipient_subjects``).
DEMO_RECIPIENT_SUBJECT = "demo-recipient"

#: Provenance recorded on seeded facts and audit rows.
DEMO_SOURCE = "DEMO_SEED"


class DemoSeedRefused(RuntimeError):
    """The demo seed refuses to build on state it cannot own."""


def _demo_uuid(*parts: str) -> uuid.UUID:
    """A stable ``client_request_id`` for a seeded fact.

    Every fact that takes an idempotency key uses this, so a re-run replays the
    existing row (the services treat a matching key as a replay) instead of
    inserting a duplicate.
    """
    return uuid.uuid5(uuid.NAMESPACE_URL, ":".join(("chronobeam-demo", *parts)))


def _berlin(wall: str) -> datetime:
    """A Berlin wall time as an aware instant (demo wall times are unambiguous)."""
    return datetime.fromisoformat(wall).replace(tzinfo=BERLIN)


def _naive(wall: str) -> datetime:
    """A naive wall time, which is what the employee-facing schemas accept."""
    return datetime.fromisoformat(wall)


def _opening(subject: str) -> dict[str, Any]:
    """The start state every persona but the onboarding one carries.

    Established in SEPTEMBER (``DEMO_CUTOVER_MONTH``), with the carry the demo's
    story needs and zero otherwise — an employee who starts clean is the honest
    default, and a balance that only exists because the seed invented it would
    misreport the carried-forward concept the whole feature rests on.
    """
    return {
        "cutover_month": DEMO_CUTOVER_MONTH,
        "minutes": DEMO_OPENING_MINUTES.get(subject, 0),
    }


def _month_end(month: date) -> date:
    return (month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def _weekdays_between(start: date, end: date) -> list[date]:
    """Every weekday in the CLOSED range ``start``..``end``."""
    days: list[date] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _working_range(
    start: date,
    end: date,
    start_time: str,
    end_time: str,
    work_type: WorkType = WorkType.ON_SITE,
    *,
    skip: tuple[date, ...] = (),
) -> list[dict[str, Any]]:
    """One interval per weekday of the CLOSED range, ``start_time``/``end_time``
    being ``HH:MM`` on the same day."""
    return [
        {
            "start": f"{day.isoformat()}T{start_time}",
            "end": f"{day.isoformat()}T{end_time}",
            "type": work_type,
        }
        for day in _weekdays_between(start, end)
        if day not in skip
    ]


def _shift_blocks(
    start: date, end: date, *, phase: int = 0, skip: tuple[date, ...] = ()
) -> list[tuple[date, str, str]]:
    """``(day, window_start, window_end)`` in blocks of ``SHIFT_BLOCK_DAYS``.

    Three identical shifts in a row, then the next window: three earlies, three
    lates, three nights, repeating. ``phase`` rotates the starting window so two
    personas do not sit on the same one. ``skip`` removes a day a persona has
    scripted facts on, so its ordinary work pattern cannot collide with them.

    A NIGHT block is followed by one day out of the rotation. The night window
    ends at 07:30 on the following morning, so an early shift starting at 07:00
    that morning would overlap the shift still running — and a roster that works
    three nights and then straight onto a morning is not a roster. The day in
    between is an ordinary working day (the ordinary pattern starts at 08:00,
    after the night has ended), so nothing is lost from the month.
    """
    days = [day for day in _weekdays_between(start, end) if day not in skip]
    blocks: list[tuple[date, str, str]] = []
    position = 0
    index = 0
    while position < len(days):
        window = SHIFT_WINDOWS[(index + phase) % len(SHIFT_WINDOWS)]
        for _ in range(SHIFT_BLOCK_DAYS):
            if position >= len(days):
                break
            blocks.append((days[position], *window))
            position += 1
        if window[1] <= window[0]:  # the night window reaches into the next morning
            position += 1
        index += 1
    return blocks


def _shift_wall(day: date, start_time: str, end_time: str) -> tuple[str, str]:
    """The window as wall strings, rolling the end to the NEXT day after midnight.

    A 23:00->07:30 shift ends on the following date; that is what keeps a block of
    three nights three separate factual days rather than one 32-hour interval.
    """
    end_day = day + timedelta(days=1) if end_time <= start_time else day
    return f"{day.isoformat()}T{start_time}", f"{end_day.isoformat()}T{end_time}"


def _shift_break(planned_start: str, planned_end: str) -> dict[str, str]:
    """The compensated break a standard 8:30 shift carries: 30 minutes, four hours in.

    Recorded as an explicit fact rather than left as a gap, because a gap between
    intervals cannot express a break INSIDE one shift — and without it the shift is
    8:30 of uninterrupted work, which is exactly what the break rule refuses and
    what SHIFT-003 asks the employee to confirm.
    """
    start = _berlin(planned_start) + timedelta(hours=4)
    return {
        "start": start.strftime("%Y-%m-%dT%H:%M"),
        "end": (start + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M"),
    }


def _shift_facts(
    start: date,
    end: date,
    *,
    phase: int = 0,
    cancelled: tuple[int, ...] = (),
    skip: tuple[date, ...] = (),
    kind: str = "SHIFT",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], set[date]]:
    """Planned shift blocks, the work matching them, their breaks, and their days.

    A cancelled block is planned and then deviated as ``CANCELLED`` — the plan is
    KEPT, because the scheduled shift is the basis for any allowance, and the
    deviation is the record that nothing was worked — so neither work nor a break
    is written for those days.

    Shift work is recorded as ``WorkType.SHIFT``, not as ordinary work: the
    engine's 06:00-21:00 corridor rule exists for ordinary trusted-flex work and
    deliberately skips shift and on-call work, which is what a night shift is.

    The covered set is returned because a shift owns its whole footprint: the
    ordinary working pattern must not also fill those days (the windows overlap
    it), and a night shift reaches into the FOLLOWING morning.
    """
    shifts: list[dict[str, Any]] = []
    intervals: list[dict[str, Any]] = []
    breaks: list[dict[str, Any]] = []
    covered: set[date] = set()
    for index, (day, window_start, window_end) in enumerate(
        _shift_blocks(start, end, phase=phase, skip=skip)
    ):
        planned_start, planned_end = _shift_wall(day, window_start, window_end)
        is_cancelled = index in cancelled
        shifts.append(
            {
                "start": planned_start,
                "end": planned_end,
                "kind": kind,
                "cancelled": is_cancelled,
            }
        )
        covered.add(day)
        if window_end <= window_start:
            covered.add(day + timedelta(days=1))
        if not is_cancelled:
            intervals.append({"start": planned_start, "end": planned_end, "type": WorkType.SHIFT})
            breaks.append(
                {
                    **_shift_break(planned_start, planned_end),
                    "kind": BreakKind.PAID_SHIFT_BREAK,
                    "shift": index,
                }
            )
    return shifts, intervals, breaks, covered


def _shift_worker_facts(
    subject: str,
    *,
    phase: int,
    cancelled: tuple[int, ...] = (),
    skip: tuple[date, ...] = (),
    extra_shifts: list[dict[str, Any]] | None = None,
    ocd: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The fact set of a shift-working persona.

    Rotating blocks of three, the recorded work and compensated break that match
    them, and the days they cover left out of the ordinary 08:00-13:48 pattern.
    ``extra_shifts`` appends scripted shifts a persona's own story needs (a frozen
    report package, a deviation recorded live), and ``ocd`` adds on-call duties.
    """
    shifts, shift_intervals, shift_breaks, covered = _shift_facts(
        DEMO_FROM, DEMO_TO, phase=phase, cancelled=cancelled, skip=skip
    )
    facts: dict[str, Any] = {
        "opening_balance": _opening(subject),
        "intervals": [
            *_working_range(DEMO_FROM, DEMO_TO, "08:00", "13:48", skip=tuple(sorted(covered))),
            *shift_intervals,
        ],
        "breaks": shift_breaks,
        "shifts": [*shifts, *(extra_shifts or [])],
    }
    if ocd:
        facts["ocd_assignments"] = ocd
    return facts


# ---------------------------------------------------------------------------
# Personas
# ---------------------------------------------------------------------------
#
# Mara Klein keeps her own hand-written facts (``INTERVALS``/``SHIFTS``/
# ``OCD_ASSIGNMENT`` below): they are the recorded product demo's scripted state,
# including the "September 2026, 7: 1 interval" day the demo clicks and the short-rest
# finding it deliberately surfaces, so they are not generated.
#
# Every other persona is data-driven: the keys below are expanded by the generic
# seeders, and each fact takes a stable idempotency key.
#
# EXACTLY ONE persona (``demo-onboarding``) declares NO facts, and that is load
# bearing rather than an omission. Onboarding — establishing a start month, the
# weekly working time and the carried balance — is refused by the cutover service
# (``cutover/self_service.py``, INV-13) for any employee who already holds facts
# BEFORE the start month: an additive opening balance would double-count them.
# Every other persona is onboarded in September and carries a start state already,
# so the live onboarding demonstration needs an employee that has neither — which
# is what ``demo-onboarding`` is. The dev principal is ``demo-onboarding`` and the
# demo opens it on its own tab. ``tests/test_demo_seed.py`` pins both halves of
# that: the persona is fact-free, and it resolves to a READY onboarding state.

DEMO_PERSONAS: list[dict[str, Any]] = [
    {
        "idp_subject": "demo-employee",
        "external_employee_id": "emp-0101",
        "legal_name": "Mara Klein",
        "email": "mara.klein@demo.example.invalid",
        "weekly_required_minutes": 1740,  # 29:00
        "schedule_minutes": 348,  # Mon-Fri 5:48
        "pay_group_bucket": PayGroupBucket.LE_9B,
        "typical_start": time(8, 0),
        "typical_end": time(16, 48),
        "role": None,
        "covers": "ordinary full-time work, mixed on-site/remote, a cross-midnight "
        "day, shifts and an OCD duty; the recorded demo's scripted actor",
        # Only the START STATE lives here: her facts are hand-written above
        # (INTERVALS/SHIFTS/OCD_ASSIGNMENT), because the recorded demo clicks
        # specific days rather than a generated month.
        "facts": {"opening_balance": _opening("demo-employee")},
    },
    {
        "idp_subject": "demo-parttime",
        "external_employee_id": "emp-0102",
        "legal_name": "Svenja Ohlsen",
        "email": "svenja.ohlsen@demo.example.invalid",
        "weekly_required_minutes": 1200,  # 20:00
        "schedule_minutes": 240,  # Mon-Fri 4:00
        "pay_group_bucket": PayGroupBucket.LE_9B,
        "fte": 0.5,
        "typical_start": time(8, 0),
        "typical_end": time(12, 0),
        "role": None,
        "covers": "part-time: a lower daily requirement from the employee's own schedule",
        "facts": {
            "opening_balance": _opening("demo-parttime"),
            "intervals": _working_range(DEMO_FROM, DEMO_TO, "08:00", "12:00"),
        },
    },
    {
        "idp_subject": "demo-overtime",
        "external_employee_id": "emp-0103",
        "legal_name": "Jonas Peters",
        "email": "jonas.peters@demo.example.invalid",
        "weekly_required_minutes": 1740,
        "schedule_minutes": 348,
        "pay_group_bucket": PayGroupBucket.LE_9B,
        "typical_start": time(8, 0),
        "typical_end": time(16, 48),
        "role": None,
        "covers": "overtime-heavy: the request -> institutional approval -> overtime work "
        "chain, plus a carried-forward opening balance",
        "facts": {
            "opening_balance": _opening("demo-overtime"),
            "intervals": _working_range(DEMO_FROM, DEMO_TO, "08:00", "13:48"),
            "overtime": [
                {
                    "valid_from": "2026-09-16T18:00",
                    "valid_to": "2026-09-23T20:00",
                    "purpose": "Deployment support",
                    "decision": OvertimeAuthStatus.APPROVED,
                }
            ],
            "overtime_intervals": [
                {
                    "start": "2026-09-16T18:00",
                    "end": "2026-09-16T20:30",
                    "type": WorkType.OVERTIME_PAID,
                    "comment": "Deployment support",
                    "overtime": 0,
                },
                {
                    "start": "2026-09-23T18:00",
                    "end": "2026-09-23T19:30",
                    "type": WorkType.OVERTIME_TIME_COMP,
                    "comment": None,
                    "overtime": 0,
                },
            ],
        },
    },
    {
        "idp_subject": "demo-ocd",
        "external_employee_id": "emp-0104",
        "legal_name": "Aylin Demir",
        "email": "aylin.demir@demo.example.invalid",
        "weekly_required_minutes": 1740,
        "schedule_minutes": 348,
        "pay_group_bucket": PayGroupBucket.LE_9B,
        "typical_start": time(8, 0),
        "typical_end": time(16, 48),
        "role": None,
        "covers": "on-call duty: a scheduled OCD assignment and a call recorded against it",
        "facts": {
            "opening_balance": _opening("demo-ocd"),
            "intervals": _working_range(DEMO_FROM, DEMO_TO, "08:00", "13:48"),
            "ocd_assignments": [
                {
                    "start": "2026-09-20T19:00",
                    "end": "2026-09-21T07:00",
                    "scheduled_at": "2026-09-11T08:00",
                }
            ],
            "ocd_calls": [
                {
                    "assignment": 0,
                    "call_start": "2026-09-20T23:42",
                    "call_end": "2026-09-21T00:18",
                    "mode": OcdCallMode.REMOTE,
                }
            ],
        },
    },
    {
        "idp_subject": "demo-travel",
        "external_employee_id": "emp-0105",
        "legal_name": "Marek Nowak",
        "email": "marek.nowak@demo.example.invalid",
        "weekly_required_minutes": 1740,
        "schedule_minutes": 348,
        "pay_group_bucket": PayGroupBucket.LE_9B,
        "typical_start": time(8, 0),
        "typical_end": time(16, 48),
        "role": None,
        "covers": "business travel: a declared trip with passive-travel segments, producing "
        "a visible day top-up and a travel credit",
        "facts": {
            "opening_balance": _opening("demo-travel"),
            # The two travel days carry no ordinary intervals, so the passive-travel
            # top-up is what closes them — otherwise the travel would be invisible
            # (a day worked to requirement closes at zero with or without a trip).
            # The 1200 passive minutes also exceed the 900-minute threshold, so the
            # trip produces a real credit rather than a zero one.
            "intervals": _working_range(
                DEMO_FROM,
                DEMO_TO,
                "08:00",
                "13:48",
                skip=(date(2026, 9, 7), date(2026, 9, 9)),
            ),
            "trips": [
                {
                    "start": "2026-09-07T05:00",
                    "end": "2026-09-09T22:00",
                    "origin": TripStartOrigin.WORKPLACE,
                    "label": "Customer site support",
                    "segments": [
                        {
                            "start": "2026-09-07T05:00",
                            "end": "2026-09-07T17:00",
                            "classification": TravelSegmentClassification.PASSIVE_TRAVEL,
                        },
                        {
                            "start": "2026-09-09T14:00",
                            "end": "2026-09-09T22:00",
                            "classification": TravelSegmentClassification.PASSIVE_TRAVEL,
                        },
                    ],
                }
            ],
        },
    },
    {
        "idp_subject": "demo-flex",
        "external_employee_id": "emp-0106",
        "legal_name": "Ines Wagner",
        "email": "ines.wagner@demo.example.invalid",
        "weekly_required_minutes": 1740,
        "schedule_minutes": 348,
        "pay_group_bucket": PayGroupBucket.LE_9B,
        "typical_start": time(8, 0),
        "typical_end": time(16, 48),
        "role": None,
        "covers": "day status and flex: vacation, sickness and compensation days, and the "
        "requirement reduction they produce",
        "facts": {
            "opening_balance": _opening("demo-flex"),
            # No work on a day that carries a FULL status: that combination is a
            # finding, and demo-findings owns that case deliberately.
            "intervals": _working_range(
                DEMO_FROM,
                DEMO_TO,
                "08:00",
                "13:48",
                skip=(date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 21), date(2026, 9, 22)),
            ),
            "day_statuses": [
                {"day": "2026-09-14", "category": DayStatusCategory.VACATION},
                {"day": "2026-09-15", "category": DayStatusCategory.VACATION},
                {"day": "2026-09-21", "category": DayStatusCategory.SICK},
                {"day": "2026-09-22", "category": DayStatusCategory.FLEX_COMPENSATION},
                {
                    "day": "2026-09-23",
                    "category": DayStatusCategory.SHIFT_COMPENSATION,
                    "portion": DayStatusPortion.HALF,
                    "day_part": DayStatusDayPart.MORNING,
                },
            ],
        },
    },
    {
        "idp_subject": "demo-findings",
        "external_employee_id": "emp-0107",
        "legal_name": "Tomas Brandt",
        "email": "tomas.brandt@demo.example.invalid",
        "weekly_required_minutes": 1740,
        "schedule_minutes": 348,
        "pay_group_bucket": PayGroupBucket.LE_9B,
        "typical_start": time(8, 0),
        "typical_end": time(16, 48),
        "role": None,
        "covers": "employee-private compliance findings: a missing 6-hour break, a >10 hour "
        "day, and work recorded on a full flex-compensation day",
        "facts": {
            "opening_balance": _opening("demo-findings"),
            # A full month like every other persona's, with the three finding days
            # kept OUT of the ordinary pattern: work to requirement on those days
            # would close at zero and erase the findings this persona exists to
            # show. 08:00-13:48 is 5:48, under the 6-hour break threshold, so the
            # fill itself produces nothing.
            "intervals": [
                *_working_range(
                    DEMO_FROM,
                    DEMO_TO,
                    "08:00",
                    "13:48",
                    skip=(date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4)),
                ),
                # 7h without a break -> WT-CORE-003 and WT-CORE-005.
                {"start": "2026-09-02T08:00", "end": "2026-09-02T15:00", "type": WorkType.ON_SITE},
                # 10.5h -> WT-CORE-004, WT-CORE-005 and WT-CORE-006.
                {"start": "2026-09-03T08:00", "end": "2026-09-03T18:30", "type": WorkType.ON_SITE},
                # Work on a FULL flex day -> WT-STATUS-003.
                {"start": "2026-09-04T08:00", "end": "2026-09-04T12:00", "type": WorkType.ON_SITE},
            ],
            "day_statuses": [
                {"day": "2026-09-04", "category": DayStatusCategory.FLEX_COMPENSATION},
            ],
        },
    },
    {
        "idp_subject": "demo-clean",
        "external_employee_id": "emp-0108",
        "legal_name": "Nora Sandberg",
        "email": "nora.sandberg@demo.example.invalid",
        "weekly_required_minutes": 1740,
        "schedule_minutes": 348,
        "pay_group_bucket": PayGroupBucket.LE_9B,
        "typical_start": time(8, 0),
        "typical_end": time(13, 48),
        "role": None,
        "covers": "the clean happy path: every scheduled day worked exactly to requirement, "
        "no findings, a zero closing balance",
        "facts": {
            "opening_balance": _opening("demo-clean"),
            # 5:48 a day, worked without a break: under the 6-hour threshold, so no
            # break finding, and exactly the required minutes, so no delta.
            "intervals": _working_range(DEMO_FROM, DEMO_TO, "08:00", "13:48"),
        },
    },
    {
        "idp_subject": "demo-shift",
        "external_employee_id": "emp-0109",
        "legal_name": "Pia Lindqvist",
        "email": "pia.lindqvist@demo.example.invalid",
        "weekly_required_minutes": 1740,
        "schedule_minutes": 348,
        "pay_group_bucket": PayGroupBucket.LE_9B,
        "typical_start": time(8, 0),
        "typical_end": time(16, 48),
        "role": None,
        "covers": "shift work and the recipient side: rotating blocks of three shifts, "
        "a cancelled one, and a frozen Shift Report package the recipient can open",
        "facts": _shift_worker_facts(
            "demo-shift",
            # The scripted 22 Sep shift carries the frozen package, so the rotating
            # blocks step around it rather than stacking a second shift on the day.
            phase=0,
            skip=(date(2026, 9, 22),),
            cancelled=(4, 13),
            extra_shifts=[
                {
                    "start": "2026-09-22T06:00",
                    "end": "2026-09-22T14:00",
                    "kind": "SHIFT",
                    "package": SubmissionPurpose.SHIFT_REPORT,
                }
            ],
            ocd=[
                {
                    "start": "2026-10-03T19:00",
                    "end": "2026-10-04T07:00",
                    "scheduled_at": "2026-09-25T08:00",
                }
            ],
        ),
    },
    {
        "idp_subject": "demo-supervisor",
        "external_employee_id": "emp-0110",
        "legal_name": "Katrin Voss",
        "email": "katrin.voss@demo.example.invalid",
        "weekly_required_minutes": 1740,
        "schedule_minutes": 348,
        "pay_group_bucket": PayGroupBucket.LE_9B,
        "typical_start": time(8, 0),
        "typical_end": time(16, 48),
        "role": None,
        "covers": "the deny boundary: a supervisory position is NOT a role, so it grants nothing "
        "over other employees' data — while the person still records and sees their own "
        "time like every mapped+active employee (ACCESS-012/013); also works rotating "
        "shift blocks with an on-call duty",
        "facts": _shift_worker_facts(
            "demo-supervisor",
            phase=1,
            cancelled=(7, 16),
            ocd=[
                {
                    "start": "2026-09-12T19:00",
                    "end": "2026-09-13T07:00",
                    "scheduled_at": "2026-09-04T08:00",
                }
            ],
        ),
    },
    {
        "idp_subject": "demo-onboarding",
        "external_employee_id": "emp-0111",
        "legal_name": "Julia Rehm",
        "email": "julia.rehm@demo.example.invalid",
        "weekly_required_minutes": 1740,
        "schedule_minutes": 348,
        "pay_group_bucket": PayGroupBucket.LE_9B,
        "typical_start": time(8, 0),
        "typical_end": time(16, 48),
        "role": None,
        # Deliberately the ONLY persona with no facts at all — see the note above.
        "covers": "onboarding: a new hire who establishes their start state live",
        "facts": {},
    },
    {
        "idp_subject": "demo-admin",
        "external_employee_id": "emp-1002",
        "legal_name": "Nils Brandt",
        "email": "nils.brandt@demo.example.invalid",
        "weekly_required_minutes": 1740,
        "schedule_minutes": 348,
        "pay_group_bucket": PayGroupBucket.LE_9B,
        "typical_start": time(8, 0),
        "typical_end": time(16, 48),
        "role": "APPLICATION_ADMIN",
        "covers": "application administration (users/access), which grants no business-data "
        "read; records their own time like any employee",
        "facts": {
            "opening_balance": _opening("demo-admin"),
            "intervals": _working_range(DEMO_FROM, DEMO_TO, "08:00", "13:48"),
        },
    },
    {
        "idp_subject": "demo-hr",
        "external_employee_id": "emp-1003",
        "legal_name": "Lena Hoffmann",
        "email": "lena.hoffmann@demo.example.invalid",
        "weekly_required_minutes": 1740,
        "schedule_minutes": 348,
        "pay_group_bucket": PayGroupBucket.LE_9B,
        "typical_start": time(8, 0),
        "typical_end": time(16, 48),
        "role": "HR_REQUESTER",
        "covers": "the HR random-check workflow, which exposes no time data before a grant; "
        "records their own time, which stays private from the employees they may request; "
        "also works rotating shift blocks",
        "facts": _shift_worker_facts("demo-hr", phase=2, cancelled=(10, 19)),
    },
    {
        "idp_subject": "demo-recipient",
        "external_employee_id": "emp-1004",
        "legal_name": "Tobias Fischer",
        "email": "tobias.fischer@demo.example.invalid",
        "weekly_required_minutes": 1740,
        "schedule_minutes": 348,
        "pay_group_bucket": PayGroupBucket.LE_9B,
        "typical_start": time(8, 0),
        "typical_end": time(16, 48),
        "role": "SUBMISSION_RECIPIENT",
        "covers": "the recipient side: reading only the packages explicitly addressed to "
        "them; records their own time, which stays private from the submitters",
        "facts": {
            "opening_balance": _opening("demo-recipient"),
            "intervals": _working_range(DEMO_FROM, DEMO_TO, "08:00", "13:48"),
        },
    },
]


# ---------------------------------------------------------------------------
# The calendar
# ---------------------------------------------------------------------------


def publish_demo_calendar(session: Session) -> CalendarVersion:
    """Publish the institutional statutory calendar for the whole MVP horizon.

    ``just demo-reset`` builds this database from an empty schema, so nothing
    competes for ``SH_HORIZON_FROM`` and the statutory set starts on 1 January:
    every month of every horizon year is classified, including the
    2026-01-01..2026-09-30 window the *dev* seed deliberately leaves ordinary.

    Refuses rather than guessing if another edition already governs that date: the
    only way to reach that state is a database built by ``just seed-dev``, whose
    calendar the demo must not silently adopt.
    """
    expected = statutory_version(SH_HORIZON_FROM, SH_HORIZON_TO)
    occupied = session.scalar(
        select(CalendarVersion).where(CalendarVersion.valid_from == SH_HORIZON_FROM)
    )
    if occupied is not None and occupied.version != expected:
        raise DemoSeedRefused(
            f"cannot publish the demo calendar: edition {occupied.version!r} already "
            f"governs {SH_HORIZON_FROM.isoformat()}, which the demo owns. This database "
            "was seeded by `just seed-dev`; run `just demo-reset` first."
        )
    return publish_calendar_edition(
        session,
        version=expected,
        valid_from=SH_HORIZON_FROM,
        valid_to=SH_HORIZON_TO,
        source=statutory_source(SH_HORIZON_FROM, SH_HORIZON_TO),
        days=statutory_days(SH_HORIZON_FROM, SH_HORIZON_TO),
        actor_principal=DEMO_SOURCE,
    )


# ---------------------------------------------------------------------------
# Personas
# ---------------------------------------------------------------------------


def _ensure_persona(session: Session, spec: dict[str, Any]) -> Employee:
    """Upsert one persona's employee row, contract, schedule and role assignment."""
    employee = session.scalar(select(Employee).where(Employee.idp_subject == spec["idp_subject"]))
    if employee is None:
        employee = Employee(
            idp_subject=str(spec["idp_subject"]),
            external_employee_id=str(spec["external_employee_id"]),
            legal_name=str(spec["legal_name"]),
            email=str(spec["email"]),
            active=True,
        )
        session.add(employee)
        session.flush()
    if not session.scalars(
        select(EmploymentContract).where(EmploymentContract.employee_id == employee.id)
    ).first():
        session.add(
            EmploymentContract(
                employee_id=employee.id,
                valid_from=date(2026, 1, 1),
                valid_to=None,
                fte=float(spec.get("fte", 1.0)),
                weekly_required_minutes=int(spec["weekly_required_minutes"]),
                pay_group_bucket=spec["pay_group_bucket"],
                employment_start=date(2020, 1, 1),
                employment_end=None,
                source=MasterDataSource.ADMIN_SEED,
                version=1,
            )
        )
    if not session.scalars(
        select(WorkScheduleVersion).where(WorkScheduleVersion.employee_id == employee.id)
    ).first():
        minutes = int(spec["schedule_minutes"])
        session.add(
            WorkScheduleVersion(
                employee_id=employee.id,
                valid_from=date(2026, 1, 1),
                valid_to=None,
                weekly_required_minutes=minutes * 5,
                mon_required_minutes=minutes,
                tue_required_minutes=minutes,
                wed_required_minutes=minutes,
                thu_required_minutes=minutes,
                fri_required_minutes=minutes,
                sat_required_minutes=0,
                sun_required_minutes=0,
                mon_typical_start=spec["typical_start"],
                mon_typical_end=spec["typical_end"],
                tue_typical_start=spec["typical_start"],
                tue_typical_end=spec["typical_end"],
                wed_typical_start=spec["typical_start"],
                wed_typical_end=spec["typical_end"],
                thu_typical_start=spec["typical_start"],
                thu_typical_end=spec["typical_end"],
                fri_typical_start=spec["typical_start"],
                fri_typical_end=spec["typical_end"],
                sat_typical_start=None,
                sat_typical_end=None,
                sun_typical_start=None,
                sun_typical_end=None,
                source=MasterDataSource.ADMIN_SEED,
                version=1,
            )
        )
    role = spec.get("role")
    if role is not None:
        active = session.scalar(
            select(RoleAssignment).where(
                RoleAssignment.employee_id == employee.id,
                RoleAssignment.role == role,
                RoleAssignment.revoked_at.is_(None),
            )
        )
        if active is None:
            session.add(
                RoleAssignment(
                    employee_id=employee.id,
                    role=role,
                    granted_by_principal=DEMO_SOURCE,
                )
            )
    return employee


# ---------------------------------------------------------------------------
# Facts, one generic seeder per fact kind
# ---------------------------------------------------------------------------


def _demo_principal(spec: dict[str, Any]) -> Principal:
    """The dev-seam principal for a persona, which the header seam must know.

    A subject missing from ``DEV_PRINCIPALS`` answers 401 on the dev seam, so a
    persona the seed provisions but does not register is unusable in the demo.
    """
    principal = DEV_PRINCIPALS.get(str(spec["idp_subject"]))
    if principal is None:
        raise DemoSeedRefused(
            f"persona {spec['idp_subject']!r} is not registered in "
            "app.auth.dev_principals.DEV_PRINCIPALS, so the dev header seam cannot "
            "resolve it (401 AUTH_UNKNOWN_PRINCIPAL)"
        )
    return principal


def _seed_overtime(session: Session, employee: Employee, spec: dict[str, Any]) -> list[uuid.UUID]:
    """Seed each overtime authorization and return its current head id.

    ``OvertimeAuthorizationIn`` carries no idempotency key, so this is a
    check-then-skip: an existing demo-authored authorization is reused, and the
    institutional decision is only written while the chain head does not already
    carry the requested status. That keeps a re-run a no-op instead of adding a
    second request or a second approval version.
    """
    heads: list[uuid.UUID] = []
    for index, row in enumerate(spec["facts"].get("overtime", [])):
        valid_from = _berlin(row["valid_from"])
        chain = session.scalars(
            select(OvertimeAuthorization).where(
                OvertimeAuthorization.employee_id == employee.id,
                OvertimeAuthorization.source_ref == DEMO_SOURCE,
                OvertimeAuthorization.valid_from == valid_from,
            )
        ).all()
        superseded = {item.supersedes_id for item in chain if item.supersedes_id is not None}
        head = next((item for item in chain if item.id not in superseded), None)
        if head is None:
            created = create_authorization(
                session,
                employee,
                OvertimeAuthorizationIn(
                    valid_from=_naive(row["valid_from"]),
                    valid_to=_naive(row["valid_to"]) if row.get("valid_to") else None,
                    purpose=row.get("purpose"),
                    source_ref=DEMO_SOURCE,
                ),
            )
            head_id = created.id
            head_status = created.status
        else:
            head_id = head.id
            head_status = head.status
        decision = row.get("decision")
        if decision is not None and head_status != decision:
            # The decision writes a NEW version, so the current head — the one an
            # overtime interval must link to (``_validate_overtime_link`` accepts
            # only an authoritative approval) — is the decided row, not the request.
            head_id = decide_authorization(
                session,
                employee.id,
                head_id,
                OvertimeAuthorizationDecisionIn(
                    status=decision,
                    authorized_by="WoCo (demo)",
                    purpose=row.get("purpose"),
                ),
                actor_principal=DEMO_SOURCE,
            ).id
        if head_id is None:  # pragma: no cover - the writers always return a row
            raise DemoSeedRefused(f"overtime authorization {index} could not be created")
        heads.append(head_id)
    return heads


def _seed_intervals(
    session: Session,
    employee: Employee,
    spec: dict[str, Any],
    authorizations: list[uuid.UUID],
) -> None:
    subject = str(spec["idp_subject"])
    facts = spec["facts"]
    rows = [*facts.get("intervals", []), *facts.get("overtime_intervals", [])]
    # ``create_interval_instant`` has no replay path — its ``client_request_id`` is
    # only a uniqueness guard — so an interval is skipped when its key is already
    # present. Checking the KEY rather than the (start, end, type) tuple keeps two
    # legitimately identical intervals distinguishable.
    existing = set(
        session.scalars(
            select(WorkInterval.client_request_id).where(WorkInterval.employee_id == employee.id)
        )
    )
    for index, row in enumerate(rows):
        key = _demo_uuid("interval", subject, str(index))
        if key in existing:
            continue
        linked = row.get("overtime")
        create_interval_instant(
            session,
            employee,
            start_at=_berlin(row["start"]),
            end_at=_berlin(row["end"]),
            work_type=row["type"],
            comment=row.get("comment"),
            client_request_id=key,
            overtime_authorization_id=None if linked is None else authorizations[linked],
        )


def _seed_day_statuses(session: Session, employee: Employee, spec: dict[str, Any]) -> None:
    subject = str(spec["idp_subject"])
    for index, row in enumerate(spec["facts"].get("day_statuses", [])):
        create_day_status(
            session,
            employee,
            DayStatusCreate(
                day=date.fromisoformat(row["day"]),
                category=row["category"],
                portion=row.get("portion", DayStatusPortion.FULL),
                day_part=row.get("day_part", DayStatusDayPart.ALL),
                client_request_id=_demo_uuid("day-status", subject, str(index)),
            ),
        )


def _seed_ocd(session: Session, employee: Employee, spec: dict[str, Any]) -> None:
    subject = str(spec["idp_subject"])
    facts = spec["facts"]
    assignments = [
        seed_ocd_assignment(
            session,
            employee,
            start_at=_berlin(row["start"]),
            end_at=_berlin(row["end"]),
            scheduled_at=_berlin(row["scheduled_at"]) if row.get("scheduled_at") else None,
            source=DEMO_SOURCE,
            external_id=f"demo-ocd-{subject}-{index}",
        )
        for index, row in enumerate(facts.get("ocd_assignments", []))
    ]
    for row in facts.get("ocd_calls", []):
        create_call(
            session,
            employee,
            OcdCallIn(
                ocd_assignment_id=assignments[row["assignment"]].id,
                call_start_local=_naive(row["call_start"]),
                call_end_local=_naive(row["call_end"]),
                mode=row["mode"],
                travel_out_minutes=row.get("travel_out_minutes"),
                travel_back_minutes=row.get("travel_back_minutes"),
                comment=row.get("comment"),
                client_request_id=_demo_uuid("ocd-call", subject, row["call_start"]),
            ),
        )


def _seed_trips(session: Session, employee: Employee, spec: dict[str, Any]) -> None:
    subject = str(spec["idp_subject"])
    for index, row in enumerate(spec["facts"].get("trips", [])):
        trip, _created = create_trip(
            session,
            employee,
            TripIn(
                start_local=_naive(row["start"]),
                end_local=_naive(row["end"]),
                start_origin=row["origin"],
                reference_label=row.get("label"),
                client_request_id=_demo_uuid("trip", subject, str(index)),
            ),
        )
        for segment_index, segment in enumerate(row.get("segments", [])):
            create_segment(
                session,
                employee,
                trip.id,
                SegmentIn(
                    start_local=_naive(segment["start"]),
                    end_local=_naive(segment["end"]),
                    classification=segment["classification"],
                    client_request_id=_demo_uuid(
                        "segment", subject, str(index), str(segment_index)
                    ),
                ),
            )


def _seed_opening_balance(session: Session, employee: Employee, spec: dict[str, Any]) -> None:
    """Seed the one authorized opening balance, through the cutover service.

    A ``DUPLICATE_SEED`` rejection is the expected result of a re-run and is not an
    error: the service is deliberately not an upsert (``EINV-08`` allows exactly one
    seed per employee). Any other rejection means the database carries facts the demo
    seed does not own, which it must not paper over.
    """
    row = spec["facts"].get("opening_balance")
    if row is None:
        return
    result = seed_opening_balance(
        session,
        Principal(
            subject=DEMO_SOURCE,
            display_name="Demo seed",
            capabilities=frozenset({Capability.CUTOVER_SEED_WRITE}),
        ),
        employee_external_id=str(spec["external_employee_id"]),
        cutover_month=date.fromisoformat(row["cutover_month"]),
        minutes=int(row["minutes"]),
    )
    if isinstance(result, CutoverSeedRejected) and result.reason != "DUPLICATE_SEED":
        raise DemoSeedRefused(
            f"opening balance for {spec['idp_subject']!r} was rejected "
            f"({result.reason}: {result.message}); the demo seed needs a database with "
            "no earlier facts for that persona"
        )


def _seed_shift_packages(
    session: Session,
    employee: Employee,
    spec: dict[str, Any],
    shifts: list[ShiftAssignment],
) -> None:
    """Freeze the Shift Report package each shift spec asks for.

    The preview is recomputed and fingerprinted here exactly as the UI does, so the
    seeded package is the same frozen projection a live submission would produce,
    and the recipient reads it through the ordinary object-scoped path.
    """
    subject = str(spec["idp_subject"])
    for index, (row, assignment) in enumerate(
        zip(spec["facts"].get("shifts", []), shifts, strict=True)
    ):
        purpose = row.get("package")
        if purpose is None:
            continue
        preview = report_preview(session, employee, assignment.id)
        fingerprint = preview.preview_fingerprint
        if not fingerprint:  # pragma: no cover - the preview always attaches one
            raise DemoSeedRefused(f"shift report preview for {subject!r} carried no fingerprint")
        create_submission_package(
            session,
            _demo_principal(spec),
            employee,
            purpose=purpose,
            scope=AssignmentSubmissionScope(assignment_id=assignment.id),
            preview_fingerprint=fingerprint,
            client_request_id=_demo_uuid("package", subject, str(index)),
            recipients=[DEMO_RECIPIENT_SUBJECT],
        )


def _seed_shifts(
    session: Session, employee: Employee, spec: dict[str, Any]
) -> list[ShiftAssignment]:
    """Create each planned shift, and record the CANCELLED deviation for the ones
    the spec marks cancelled.

    A cancellation is NOT a status on the plan: the plan stays ``PLANNED`` because
    the scheduled shift is the basis for any allowance, and the deviation is the
    separate record that the Company cancelled it and nothing was worked
    (docs/INVARIANTS.md, INV-09). The deviation is written through the ordinary command,
    so the seeded state is one the product could have produced itself.
    """
    subject = str(spec["idp_subject"])
    principal = _demo_principal(spec)
    assignments: list[ShiftAssignment] = []
    for row in spec["facts"].get("shifts", []):
        assignment = seed_shift_assignment(
            session,
            employee,
            ShiftAssignmentSeed(
                plan_key=f"demo-plan-{subject}",
                plan_name="Shift plan September 2026",
                planned_start_at=_berlin(row["start"]),
                planned_end_at=_berlin(row["end"]),
                shift_kind=row["kind"],
                source=DEMO_SOURCE,
                source_ref=f"demo-shift-plan-{subject}",
            ),
        )
        if row.get("cancelled") and not session.scalar(
            select(ShiftDeviation).where(ShiftDeviation.shift_assignment_id == assignment.id)
        ):
            # ``save_deviation`` is a command, not an upsert: replaying it against
            # an assignment that already carries a deviation is a version conflict
            # by design, so a re-run skips instead of failing.
            save_deviation(
                session,
                principal,
                employee,
                assignment.id,
                ShiftDeviationIn(
                    deviation_type=ShiftDeviationType.CANCELLED,
                    decision_maker_type="Department Head",
                    reason="Shift cancelled by the Company.",
                ),
            )
        assignments.append(assignment)
    return assignments


def _seed_breaks(
    session: Session, employee: Employee, spec: dict[str, Any], shifts: list[ShiftAssignment]
) -> None:
    """Record each explicit factual break, linked to the shift it sits inside.

    Idempotent on its own key, so a re-run replays rather than duplicating.
    """
    subject = str(spec["idp_subject"])
    for index, row in enumerate(spec["facts"].get("breaks", [])):
        create_break_interval(
            session,
            employee,
            BreakIntervalIn(
                start_local=_naive(row["start"]),
                end_local=_naive(row["end"]),
                kind=row["kind"],
                linked_shift_assignment_id=shifts[row["shift"]].id,
                client_request_id=_demo_uuid("break", subject, str(index)),
            ),
        )


def _seed_persona_facts(session: Session, employee: Employee, spec: dict[str, Any]) -> None:
    """Seed every fact kind a persona declares, in dependency order."""
    authorizations = _seed_overtime(session, employee, spec)
    _seed_intervals(session, employee, spec, authorizations)
    _seed_day_statuses(session, employee, spec)
    _seed_ocd(session, employee, spec)
    _seed_trips(session, employee, spec)
    shifts = _seed_shifts(session, employee, spec)
    # Before the packages: a frozen Shift Report carries the break's own state
    # (SHIFT-003), so the break has to exist when the preview is fingerprinted.
    _seed_breaks(session, employee, spec, shifts)
    _seed_shift_packages(session, employee, spec, shifts)


# ---------------------------------------------------------------------------
# Mara Klein — the recorded demo's hand-written September 2026
# ---------------------------------------------------------------------------
#
# These facts are deliberately NOT generated: the recorded product demo clicks
# specific days ("September 2026, 7: 1 interval") and relies on the short-rest finding
# the 2026-09-09 morning produces, so the layout is scripted state rather than
# representative filler.

# Mara's deterministic SEPTEMBER 2026 facts: (start_local, end_local, work_type,
# comment). Wall times in Europe/Berlin (CEST, +02:00 in September). A realistic
# month: nearly every weekday worked, modest deltas, one long cross-midnight
# day, a short day after the night shift, and one paid-overtime evening.
INTERVALS: list[tuple[str, str, WorkType, str | None]] = [
    # Wed 2 Sep.
    ("2026-09-02T08:00", "2026-09-02T12:00", WorkType.ON_SITE, None),
    ("2026-09-02T12:30", "2026-09-02T14:30", WorkType.ON_SITE, None),
    # Thu 3 Sep.
    ("2026-09-03T08:00", "2026-09-03T12:00", WorkType.ON_SITE, None),
    ("2026-09-03T12:30", "2026-09-03T14:18", WorkType.ON_SITE, None),
    # Fri 4 Sep.
    ("2026-09-04T08:00", "2026-09-04T12:00", WorkType.REMOTE, None),
    ("2026-09-04T12:30", "2026-09-04T14:18", WorkType.REMOTE, None),
    # Mon 7 Sep — mixed on-site/remote day with a cross-midnight evening
    # (release support). The demo's "long day".
    ("2026-09-07T08:00", "2026-09-07T10:00", WorkType.ON_SITE, None),
    ("2026-09-07T10:15", "2026-09-07T12:00", WorkType.ON_SITE, None),
    ("2026-09-07T12:30", "2026-09-07T14:00", WorkType.REMOTE, "Deep work"),
    ("2026-09-07T20:30", "2026-09-08T01:00", WorkType.REMOTE, None),
    # Wed 9 Sep — short morning after the night shift (short rest -> the
    # engine surfaces its employee-private rest-period finding here).
    ("2026-09-09T10:00", "2026-09-09T13:00", WorkType.REMOTE, None),
    ("2026-09-09T13:15", "2026-09-09T15:00", WorkType.REMOTE, None),
    # Thu 10 Sep.
    ("2026-09-10T08:00", "2026-09-10T11:55", WorkType.ON_SITE, None),
    ("2026-09-10T12:25", "2026-09-10T14:24", WorkType.ON_SITE, None),
    # Fri 11 Sep.
    ("2026-09-11T08:00", "2026-09-11T12:00", WorkType.ON_SITE, None),
    ("2026-09-11T12:30", "2026-09-11T14:18", WorkType.ON_SITE, None),
    # Mon 14 Sep — long start.
    ("2026-09-14T07:45", "2026-09-14T12:00", WorkType.ON_SITE, None),
    ("2026-09-14T12:30", "2026-09-14T14:24", WorkType.ON_SITE, None),
    # Tue 15 Sep — remote day.
    ("2026-09-15T08:00", "2026-09-15T12:00", WorkType.REMOTE, None),
    ("2026-09-15T12:30", "2026-09-15T14:18", WorkType.REMOTE, None),
    # Wed 16 Sep — ordinary day + paid overtime evening.
    ("2026-09-16T08:00", "2026-09-16T12:00", WorkType.ON_SITE, None),
    ("2026-09-16T12:30", "2026-09-16T14:18", WorkType.ON_SITE, None),
    ("2026-09-16T18:00", "2026-09-16T20:30", WorkType.OVERTIME_PAID, "Deployment support"),
    # Thu 17 Sep.
    ("2026-09-17T08:00", "2026-09-17T12:00", WorkType.ON_SITE, None),
    ("2026-09-17T12:30", "2026-09-17T14:18", WorkType.ON_SITE, None),
    # Sat 19 Sep — one working Saturday morning.
    ("2026-09-19T08:00", "2026-09-19T12:00", WorkType.ON_SITE, None),
    # Fri 18 Sep — short day.
    ("2026-09-18T08:00", "2026-09-18T11:30", WorkType.ON_SITE, None),
    ("2026-09-18T11:45", "2026-09-18T13:45", WorkType.ON_SITE, None),
    # Mon 21 Sep.
    ("2026-09-21T08:00", "2026-09-21T12:00", WorkType.ON_SITE, None),
    ("2026-09-21T12:30", "2026-09-21T14:24", WorkType.ON_SITE, None),
    # Tue 22 Sep — the shift day; the deviation is recorded live in the demo,
    # the actual work intervals (with a qualifying break) are pre-seeded.
    ("2026-09-22T06:30", "2026-09-22T10:00", WorkType.ON_SITE, None),
    ("2026-09-22T10:30", "2026-09-22T13:30", WorkType.ON_SITE, None),
    # Thu 24 Sep.
    ("2026-09-24T08:00", "2026-09-24T12:00", WorkType.ON_SITE, None),
    ("2026-09-24T12:30", "2026-09-24T14:18", WorkType.ON_SITE, None),
    # Fri 25 Sep — short day (appointment).
    ("2026-09-25T08:00", "2026-09-25T11:00", WorkType.ON_SITE, None),
    ("2026-09-25T11:15", "2026-09-25T13:00", WorkType.ON_SITE, None),
    # Mon 28 Sep.
    ("2026-09-28T08:00", "2026-09-28T12:00", WorkType.ON_SITE, None),
    ("2026-09-28T12:30", "2026-09-28T14:18", WorkType.ON_SITE, None),
    # Tue 29 Sep.
    ("2026-09-29T08:00", "2026-09-29T12:00", WorkType.ON_SITE, None),
    ("2026-09-29T12:30", "2026-09-29T14:30", WorkType.ON_SITE, None),
    # Wed 30 Sep.
    ("2026-09-30T08:00", "2026-09-30T12:00", WorkType.ON_SITE, None),
    ("2026-09-30T12:30", "2026-09-30T14:18", WorkType.ON_SITE, None),
    # Thu 1 Oct.
    ("2026-10-01T08:00", "2026-10-01T12:00", WorkType.ON_SITE, None),
    ("2026-10-01T12:30", "2026-10-01T14:18", WorkType.ON_SITE, None),
    # Fri 2 Oct.
    ("2026-10-02T08:00", "2026-10-02T11:45", WorkType.ON_SITE, None),
    ("2026-10-02T12:15", "2026-10-02T14:18", WorkType.ON_SITE, None),
]

# Shift assignments: two live ones plus one the Company cancelled.
SHIFTS: list[dict[str, Any]] = [
    {"start": "2026-09-08T19:00", "end": "2026-09-09T03:00", "kind": "SHIFT"},
    {"start": "2026-09-22T06:00", "end": "2026-09-22T14:00", "kind": "SHIFT"},
    # Cancelled by the Company. The plan stays PLANNED — the scheduled shift is the
    # basis for any allowance — and the deviation is the separate record that
    # nothing was worked, so no actual interval is written for this day.
    {
        "start": "2026-10-06T07:00",
        "end": "2026-10-06T15:30",
        "kind": "SHIFT",
        "cancelled": True,
    },
]

#: The days Mara's scripted facts already own — her hand-written intervals and the
#: whole footprint of each shift (a night shift reaches into the next morning). The
#: generated fill must leave them alone, or an interval would overlap a scripted one.
_MARA_SCRIPTED_DAYS: set[date] = {date.fromisoformat(row[0][:10]) for row in INTERVALS} | {
    day
    for row in SHIFTS
    for day in (
        date.fromisoformat(row["start"][:10]),
        date.fromisoformat(row["end"][:10]),
    )
}

#: The rest of the shared September..October range, so the month in progress is
#: full for the recorded demo's actor too rather than ending where the script does.
MARA_FILL: list[tuple[str, str, WorkType, str | None]] = [
    (row["start"], row["end"], row["type"], None)
    for row in _working_range(
        DEMO_FROM, DEMO_TO, "08:00", "13:48", skip=tuple(sorted(_MARA_SCRIPTED_DAYS))
    )
]

# One OCD duty (assignment only; the call is recorded live in the demo).
OCD_ASSIGNMENT = ("2026-09-20T19:00", "2026-09-21T07:00", "2026-09-11T08:00")


def _seed_mara_facts(session: Session, mara: Employee) -> None:
    principal = DEV_PRINCIPALS["demo-employee"]
    for row in SHIFTS:
        assignment = seed_shift_assignment(
            session,
            mara,
            ShiftAssignmentSeed(
                plan_key="demo-september-2026",
                plan_name="Shift plan September 2026",
                planned_start_at=_berlin(row["start"]),
                planned_end_at=_berlin(row["end"]),
                shift_kind=row["kind"],
                source=DEMO_SOURCE,
                source_ref="demo-shift-plan-v1",
            ),
        )
        if row.get("cancelled") and not session.scalar(
            select(ShiftDeviation).where(ShiftDeviation.shift_assignment_id == assignment.id)
        ):
            save_deviation(
                session,
                principal,
                mara,
                assignment.id,
                ShiftDeviationIn(
                    deviation_type=ShiftDeviationType.CANCELLED,
                    decision_maker_type="Department Head",
                    reason="Shift cancelled by the Company.",
                ),
            )

    existing = {
        (row.start_at, row.end_at, row.work_type)
        for row in session.scalars(select(WorkInterval).where(WorkInterval.employee_id == mara.id))
    }
    for index, (start, end, work_type, comment) in enumerate([*MARA_FILL, *INTERVALS]):
        start_at = _berlin(start)
        end_at = _berlin(end)
        if (start_at.astimezone(UTC), end_at.astimezone(UTC), work_type) in existing:
            continue
        create_interval_instant(
            session,
            mara,
            start_at=start_at,
            end_at=end_at,
            work_type=work_type,
            comment=comment,
            client_request_id=_demo_uuid("interval", "demo-employee", str(index)),
        )
    scheduled_start, scheduled_end, scheduled_at = OCD_ASSIGNMENT
    seed_ocd_assignment(
        session,
        mara,
        start_at=_berlin(scheduled_start),
        end_at=_berlin(scheduled_end),
        scheduled_at=_berlin(scheduled_at),
        source=DEMO_SOURCE,
        external_id="demo-ocd-1",
    )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def seed_demo(session: Session) -> None:
    """Idempotent demo seed: the calendar, every persona and every persona's facts."""
    publish_demo_calendar(session)
    for spec in DEMO_PERSONAS:
        employee = _ensure_persona(session, spec)
        _demo_principal(spec)  # fail fast if the dev header seam cannot resolve it
        if spec["idp_subject"] == "demo-employee":
            _seed_mara_facts(session, employee)
        else:
            _seed_persona_facts(session, employee, spec)
        # The start state goes last for EVERY persona, the hand-written one
        # included: the cutover service refuses an opening balance for an employee
        # holding facts BEFORE the cutover month, so running it after the facts
        # makes that guard a real check on the seeded state rather than a check on
        # an empty ledger. Every persona's facts start IN September, so none of
        # them trips it; `demo-onboarding` carries none at all, which is precisely
        # why it is the one whose start-state form can be demonstrated live.
        _seed_opening_balance(session, employee, spec)
    session.commit()


def main() -> None:
    settings = get_settings()
    if settings.env != "dev":
        sys.exit(f"refusing to seed: CHRONOBEAM_ENV must be 'dev' (got {settings.env!r})")
    with get_session_factory()() as session:
        seed_demo(session)
    print(
        f"demo seed complete ({len(DEMO_PERSONAS)} personas; onboarded in "
        f"{DEMO_CUTOVER_MONTH[:7]}; facts {DEMO_FROM}..{DEMO_TO}; "
        f"SH statutory calendar {SH_HORIZON_FROM}..{SH_HORIZON_TO})"
    )


if __name__ == "__main__":
    main()
