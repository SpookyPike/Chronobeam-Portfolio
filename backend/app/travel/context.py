"""Shared read-time loader for private travel calculations (TRAVEL-001..004).

Only an AUTHORITATIVE approval suppresses ``TRAVEL-SUNDAY-001`` (``ACCESS-017``/
``EINV-23``): the window comes from an APPROVED institutional decision, never
from a legacy employee-self-declared ``APPROVED`` row.

This is the ONE place a travel calculation resolves its policy edition, so the
private month view and the travel summary cannot drift apart: they call the same
loader and therefore read the same edition for the same month (F8-9).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased

from app.identity.models import Employee
from app.ocd_overtime.models import (
    AUTHORITATIVE_SOURCES,
    OvertimeAuthorization,
    OvertimeAuthStatus,
)
from app.policy.engine import BERLIN, CalendarClass
from app.policy.models import PolicyVersion
from app.travel.engine import PassiveFact, TravelResult, TripFact, WorkFact, calculate_travel
from app.travel.models import BusinessTrip, TravelSegment, TravelSegmentClassification
from app.worktime.models import WorkInterval
from app.worktime.queries import current_intervals, month_bounds


@dataclass(frozen=True)
class TravelCalculationContext:
    trips: tuple[BusinessTrip, ...]
    segments_by_trip: dict[object, tuple[TravelSegment, ...]]
    result: TravelResult


@dataclass(frozen=True)
class MonthIndependentTravelFacts:
    """Employee-scoped facts reused only within one calculation.

    Segments remain keyed by the current trip even when recorded against an
    ancestor. ``intervals`` is the ORIGINAL full-history prefetch: ordinary carry
    inputs are narrowed per month, but travel must retain its existing history.
    A standalone travel read leaves it unset and fetches its own interval window.
    """

    trips: tuple[BusinessTrip, ...]
    authorizations: tuple[OvertimeAuthorization, ...]
    segments_by_trip: dict[object, tuple[TravelSegment, ...]]
    intervals: Sequence[WorkInterval] | None = None


def month_independent_travel_facts(
    session: Session, employee_id: object
) -> MonthIndependentTravelFacts:
    """Fetch current trips, their canonical segments and authoritative approvals.

    Read the employee's trip versions together so ancestry does not cost a round
    trip per corrected version. The same-owner FK ensures each chain stays in
    this employee's rows. Canonical segments are still selected by the database's
    successor anti-join, not by treating root rows as current.
    """
    versions = session.scalars(
        select(BusinessTrip)
        .where(BusinessTrip.employee_id == employee_id)
        .order_by(BusinessTrip.start_at)
    ).all()
    by_id = {row.id: row for row in versions}
    superseded = {row.supersedes_id for row in versions if row.supersedes_id is not None}
    trips = tuple(row for row in versions if row.id not in superseded)
    head_by_version: dict[object, object] = {}
    for head in trips:
        cursor = head
        while True:
            head_by_version[cursor.id] = head.id
            parent = by_id.get(cursor.supersedes_id) if cursor.supersedes_id is not None else None
            if parent is None:
                break
            cursor = parent
    grouped: dict[object, list[TravelSegment]] = {head.id: [] for head in trips}
    if trips:
        child = aliased(TravelSegment)
        segments = session.scalars(
            select(TravelSegment)
            .join(BusinessTrip, BusinessTrip.id == TravelSegment.trip_id)
            .where(
                BusinessTrip.employee_id == employee_id,
                ~exists(select(child.id).where(child.supersedes_id == TravelSegment.id)),
            )
            .order_by(TravelSegment.start_at, TravelSegment.id)
        ).all()
        for segment in segments:
            head_id = head_by_version.get(segment.trip_id)
            if head_id is not None:
                grouped[head_id].append(segment)
    authorization_child = aliased(OvertimeAuthorization)
    # ACCESS-017/EINV-23 — the SQL form of `is_authoritative_approval`: a window
    # suppresses TRAVEL-SUNDAY-001 only when the row is an APPROVED *institutional*
    # decision. `AUTHORITATIVE_SOURCES` is the one deny-by-default value set, so a
    # legacy employee-self-declared `APPROVED` row (and any unrecognised
    # provenance) fails closed here instead of buying compliance relief.
    authorizations = tuple(
        session.scalars(
            select(OvertimeAuthorization).where(
                OvertimeAuthorization.employee_id == employee_id,
                OvertimeAuthorization.status == OvertimeAuthStatus.APPROVED,
                OvertimeAuthorization.source.in_(sorted(AUTHORITATIVE_SOURCES)),
                # Employee-scoped anti-join: `supersedes_id` is a plain self-FK, so a
                # restored/imported row belonging to ANOTHER employee must not
                # retire this employee's approval and silently restore the
                # TRAVEL-SUNDAY-001 finding.
                ~exists(
                    select(authorization_child.id).where(
                        authorization_child.supersedes_id == OvertimeAuthorization.id,
                        authorization_child.employee_id == OvertimeAuthorization.employee_id,
                    )
                ),
            )
        ).all()
    )
    return MonthIndependentTravelFacts(
        trips=trips,
        authorizations=authorizations,
        segments_by_trip={key: tuple(rows) for key, rows in grouped.items()},
    )


def load_travel_calculation(
    session: Session,
    employee: Employee,
    month: date,
    daily_required: dict[date, int],
    calendar_classes: dict[date, CalendarClass],
    *,
    prefetch: MonthIndependentTravelFacts | None = None,
    policy_versions: Sequence[PolicyVersion] | None = None,
) -> TravelCalculationContext:
    """Load shared current facts so private views cannot drift from each other."""
    # Deferred on purpose: `app.policy.service` imports THIS module at module
    # level, so a module-level import back would be a cycle. The travel edition
    # resolver lives in the policy adapter because that is the module allowed to
    # read a `policy_version` row and validate its identity (editions.py doctrine).
    from app.policy.service import travel_edition_for

    policy_edition, parameters = travel_edition_for(session, month, versions=policy_versions)
    _month_start_at, month_end_at = month_bounds(month)
    # A caller that computes more than one month passes the prefetch in; a
    # single-month caller (the travel summary) fetches it here, as it always did.
    month_independent = prefetch or month_independent_travel_facts(session, employee.id)
    trips = month_independent.trips
    trip_facts: list[TripFact] = []
    passive_facts: list[PassiveFact] = []
    travel_class_by_interval: dict[object, str] = {}
    segments_by_trip: dict[object, tuple[TravelSegment, ...]] = {}
    for head in trips:
        segments = tuple(
            row
            for row in month_independent.segments_by_trip[head.id]
            if row.start_at < month_end_at
        )
        segments_by_trip[head.id] = segments
        # A declared trip is a FACT the employee owns, and the UI lists it with
        # "No segments yet — add the journey legs below." Dropping it here made a
        # freshly created trip invisible in every month: `travel_summary` selects
        # from `result.trips`, and the frontend decides list membership from that
        # summary. A segment-less trip contributes no credit and no finding
        # (eligible 0 => excess 0 => credit 0), so it is inert in the arithmetic
        # and only enters the input fingerprint — which is the correct input set.
        trip_facts.append(TripFact(str(head.id), head.start_at.astimezone(BERLIN).date()))
        for segment in segments:
            if segment.classification is TravelSegmentClassification.PASSIVE_TRAVEL:
                passive_facts.append(
                    PassiveFact(str(head.id), segment.start_at, segment.end_at, segment.basis.value)
                )
            elif segment.work_interval_id is not None:
                travel_class_by_interval[segment.work_interval_id] = segment.classification.value

    intervals = (
        current_intervals(session, employee_id=employee.id, intersects_to=month_end_at)
        if month_independent.intervals is None
        else [row for row in month_independent.intervals if row.start_at < month_end_at]
    )
    work_facts = tuple(
        WorkFact(
            row.start_at,
            row.end_at,
            travel_class_by_interval.get(row.id, row.work_type.value),
            is_travel=row.id in travel_class_by_interval,
        )
        for row in intervals
    )
    # The row set is month-independent (memoised above); the OPEN-ENDED sentinel is
    # not, so it is still resolved against THIS month's end.
    authorizations = tuple(
        (
            row.valid_from,
            row.valid_to if row.valid_to is not None else month_end_at + timedelta(days=3650),
        )
        for row in month_independent.authorizations
    )
    result = calculate_travel(
        trips=tuple(trip_facts),
        passive_facts=tuple(passive_facts),
        work_facts=work_facts,
        required_minutes={day: value for day, value in daily_required.items() if value > 0},
        authorization_windows=authorizations,
        calendar_classes={day: value.value for day, value in calendar_classes.items()},
        parameters=parameters,
        policy_edition=policy_edition,
    )
    return TravelCalculationContext(trips, segments_by_trip, result)
