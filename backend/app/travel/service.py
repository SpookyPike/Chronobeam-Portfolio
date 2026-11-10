"""Transactional employee-owned travel facts (TRAVEL-001..004)."""

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import NoReturn

from fastapi import HTTPException
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.audit.models import record_audit
from app.auth.principal import Principal
from app.db import lock_employee_facts
from app.disclosure.guards import assert_interval_not_disclosed, assert_not_disclosed
from app.enums import IntervalSource, WorkType
from app.errors import ErrorCode, api_error
from app.identity.models import Employee
from app.policy.service import _calendar_classes, _requirements
from app.travel.context import load_travel_calculation
from app.travel.engine import (
    TravelResult,
    TravelTripResult,
    add_one_calendar_month_clamped,
    following_month_start,
)
from app.travel.models import (
    BusinessTrip,
    TravelSegment,
    TravelSegmentBasis,
    TravelSegmentClassification,
)
from app.travel.schemas import (
    SegmentIn,
    SegmentOut,
    SegmentPatch,
    TravelCreditOut,
    TravelFindingOut,
    TravelSummaryOut,
    TravelTopupOut,
    TravelTripSummaryOut,
    TripIn,
    TripOut,
    TripPatch,
)
from app.worktime.models import Correction, WorkInterval
from app.worktime.service import (
    _check_overlap,
    _ensure_period,
    _resolve,
    open_reviewed_month_range,
    open_reviewed_months,
)
from app.worktime.walltime import BERLIN

ACTOR_ROLE_EMPLOYEE = "EMPLOYEE"


def _not_found() -> NoReturn:
    raise api_error(404, ErrorCode.NOT_FOUND, "Not found.")


def _error(code: str, message: str, status_code: int = 422) -> NoReturn:
    raise HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _current_trip(session: Session, employee_id: uuid.UUID, trip_id: uuid.UUID) -> BusinessTrip:
    child = aliased(BusinessTrip)
    row = session.scalar(
        select(BusinessTrip).where(
            BusinessTrip.id == trip_id,
            BusinessTrip.employee_id == employee_id,
            ~exists(select(child.id).where(child.supersedes_id == BusinessTrip.id)),
        )
    )
    if row is None:
        _not_found()
    return row


def _current_segment(
    session: Session, employee_id: uuid.UUID, segment_id: uuid.UUID
) -> TravelSegment:
    child = aliased(TravelSegment)
    row = session.scalar(
        select(TravelSegment)
        .join(BusinessTrip, BusinessTrip.id == TravelSegment.trip_id)
        .where(
            TravelSegment.id == segment_id,
            BusinessTrip.employee_id == employee_id,
            ~exists(select(child.id).where(child.supersedes_id == TravelSegment.id)),
        )
    )
    if row is None:
        _not_found()
    return row


def _trip_head(session: Session, trip: BusinessTrip) -> BusinessTrip:
    head = trip
    while True:
        child = session.scalar(select(BusinessTrip).where(BusinessTrip.supersedes_id == head.id))
        if child is None:
            return head
        head = child


def _segment_head(session: Session, segment: TravelSegment) -> TravelSegment:
    """Resolve a segment to its current supersede-chain head."""
    head = segment
    while True:
        child = session.scalar(select(TravelSegment).where(TravelSegment.supersedes_id == head.id))
        if child is None:
            return head
        head = child


def _trip_chain_ids(session: Session, trip: BusinessTrip) -> list[uuid.UUID]:
    ids = [trip.id]
    current = trip
    while current.supersedes_id is not None:
        parent = session.get(BusinessTrip, current.supersedes_id)
        if parent is None:  # pragma: no cover - FK makes this unreachable
            break
        ids.append(parent.id)
        current = parent
    return ids


def _segments_for_trip(session: Session, trip: BusinessTrip) -> list[TravelSegment]:
    child = aliased(TravelSegment)
    return list(
        session.scalars(
            select(TravelSegment)
            .where(
                TravelSegment.trip_id.in_(_trip_chain_ids(session, trip)),
                ~exists(select(child.id).where(child.supersedes_id == TravelSegment.id)),
            )
            .order_by(TravelSegment.start_at, TravelSegment.id)
        ).all()
    )


def _trip_out(session: Session, trip: BusinessTrip) -> TripOut:
    return TripOut(
        id=trip.id,
        start_at=trip.start_at,
        end_at=trip.end_at,
        start_local=trip.start_at.astimezone(BERLIN).replace(tzinfo=None),
        end_local=trip.end_at.astimezone(BERLIN).replace(tzinfo=None),
        start_origin=trip.start_origin,
        source=trip.source,
        reference_label=trip.reference_label,
        version=trip.version,
        supersedes_id=trip.supersedes_id,
        client_request_id=trip.client_request_id,
        segments=[_segment_out(segment) for segment in _segments_for_trip(session, trip)],
    )


def _segment_out(segment: TravelSegment) -> SegmentOut:
    return SegmentOut(
        id=segment.id,
        trip_id=segment.trip_id,
        start_at=segment.start_at,
        end_at=segment.end_at,
        start_local=segment.start_at.astimezone(BERLIN).replace(tzinfo=None),
        end_local=segment.end_at.astimezone(BERLIN).replace(tzinfo=None),
        classification=segment.classification,
        basis=segment.basis,
        work_interval_id=segment.work_interval_id,
        version=segment.version,
        supersedes_id=segment.supersedes_id,
        client_request_id=segment.client_request_id,
    )


def _bounds(
    start_local: datetime | None,
    end_local: datetime | None,
    *,
    start_fold: int | None,
    end_fold: int | None,
    fold: int | None,
    previous_start: datetime | None = None,
    previous_end: datetime | None = None,
) -> tuple[datetime, datetime]:
    start = (
        _resolve("start_local", start_local, start_fold if start_fold is not None else fold)
        if start_local
        else previous_start
    )
    end = (
        _resolve("end_local", end_local, end_fold if end_fold is not None else fold)
        if end_local
        else previous_end
    )
    if start is None or end is None:
        _error("VALIDATION_INTERVAL_ORDER", "The end must be after the start.")
    if end.astimezone(UTC) <= start.astimezone(UTC):
        _error("VALIDATION_INTERVAL_ORDER", "The end must be after the start.")
    return start, end


def _assert_inside_trip(trip: BusinessTrip, start: datetime, end: datetime) -> None:
    if start < trip.start_at or end > trip.end_at:
        _error("VALIDATION_TRIP_ENVELOPE", "Travel segments must lie inside the trip envelope.")


def _assert_passive_no_overlap(
    session: Session,
    employee_id: uuid.UUID,
    start: datetime,
    end: datetime,
    exclude_id: uuid.UUID | None = None,
) -> None:
    child = aliased(TravelSegment)
    rows = session.scalars(
        select(TravelSegment)
        .join(BusinessTrip, BusinessTrip.id == TravelSegment.trip_id)
        .where(
            BusinessTrip.employee_id == employee_id,
            TravelSegment.classification == TravelSegmentClassification.PASSIVE_TRAVEL,
            ~exists(select(child.id).where(child.supersedes_id == TravelSegment.id)),
        )
    ).all()
    if any(row.id != exclude_id and row.start_at < end and row.end_at > start for row in rows):
        _error("VALIDATION_TRAVEL_OVERLAP", "Passive travel overlaps an existing passive segment.")


def _check_segment_shape(
    classification: TravelSegmentClassification, basis: TravelSegmentBasis
) -> None:
    if classification == TravelSegmentClassification.PASSIVE_TRAVEL:
        return
    if basis != TravelSegmentBasis.ACTUAL:
        _error(
            "VALIDATION_FAILED",
            "Fictitious business-route duration is only allowed for passive travel.",
        )


def create_trip(session: Session, employee: Employee, payload: TripIn) -> tuple[TripOut, bool]:
    lock_employee_facts(session, employee.id)
    if payload.client_request_id is not None:
        existing = session.scalar(
            select(BusinessTrip).where(
                BusinessTrip.employee_id == employee.id,
                BusinessTrip.client_request_id == payload.client_request_id,
            )
        )
        if existing is not None:
            return _trip_out(session, _trip_head(session, existing)), False
    start, end = _bounds(
        payload.start_local,
        payload.end_local,
        start_fold=payload.start_fold,
        end_fold=payload.end_fold,
        fold=payload.fold,
    )
    row = BusinessTrip(
        employee_id=employee.id,
        start_at=start,
        end_at=end,
        start_origin=payload.start_origin,
        source="EMPLOYEE_DECLARED",
        reference_label=payload.reference_label,
        version=1,
        client_request_id=payload.client_request_id,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        _error("VALIDATION_FAILED", "This change was already recorded — reload the page to see it.")
        raise AssertionError from exc  # pragma: no cover
    return _trip_out(session, row), True


def list_trips(session: Session, employee: Employee) -> list[TripOut]:
    child = aliased(BusinessTrip)
    trips = session.scalars(
        select(BusinessTrip)
        .where(
            BusinessTrip.employee_id == employee.id,
            ~exists(select(child.id).where(child.supersedes_id == BusinessTrip.id)),
        )
        .order_by(BusinessTrip.start_at, BusinessTrip.id)
    ).all()
    return [_trip_out(session, trip) for trip in trips]


def patch_trip(
    session: Session, employee: Employee, trip_id: uuid.UUID, payload: TripPatch
) -> TripOut:
    lock_employee_facts(session, employee.id)
    current = _current_trip(session, employee.id, trip_id)
    if current.version != payload.version:
        _error("CONFLICT_VERSION", "Trip changed; refresh and retry.", 409)
    start, end = _bounds(
        payload.start_local,
        payload.end_local,
        start_fold=payload.start_fold,
        end_fold=payload.end_fold,
        fold=payload.fold,
        previous_start=current.start_at,
        previous_end=current.end_at,
    )
    if any(
        segment.start_at < start or segment.end_at > end
        for segment in _segments_for_trip(session, current)
    ):
        _error("VALIDATION_TRIP_ENVELOPE", "Trip envelope cannot exclude current travel segments.")
    replacement = BusinessTrip(
        employee_id=employee.id,
        start_at=start,
        end_at=end,
        start_origin=payload.start_origin or current.start_origin,
        source="EMPLOYEE_DECLARED",
        reference_label=(
            payload.reference_label
            if "reference_label" in payload.model_fields_set
            else current.reference_label
        ),
        version=current.version + 1,
        supersedes_id=current.id,
    )
    session.add(replacement)
    session.flush()
    session.add(
        Correction(
            entity_type="BUSINESS_TRIP",
            entity_id=replacement.id,
            old_version=current.version,
            new_version=replacement.version,
            actor_employee_id=employee.id,
            actor_role=ACTOR_ROLE_EMPLOYEE,
            reason=payload.reason,
        )
    )
    session.flush()
    if replacement.start_at != current.start_at:
        # EINV-15: moving the trip start moves the TRAVEL-004 window (eligible
        # minutes) and the month the derived credit lands in. Widening or
        # narrowing the envelope without moving the start changes no
        # calculation, so it deliberately reopens nothing.
        old_start_day = current.start_at.astimezone(BERLIN).date()
        new_start_day = replacement.start_at.astimezone(BERLIN).date()
        open_reviewed_month_range(
            session,
            employee.id,
            min(old_start_day, new_start_day),
            max(_trip_window_last_month(old_start_day), _trip_window_last_month(new_start_day)),
        )
    return _trip_out(session, replacement)


def delete_trip(
    session: Session,
    principal: Principal,
    employee: Employee,
    trip_id: uuid.UUID,
    version: int,
    reason: str | None,
) -> None:
    lock_employee_facts(session, employee.id)
    current = _current_trip(session, employee.id, trip_id)
    if current.version != version:
        _error("CONFLICT_VERSION", "Trip changed; refresh and retry.", 409)
    if _segments_for_trip(session, current):
        _error("VALIDATION_FAILED", "Delete current travel segments before deleting the trip.")
    chain = [current]
    while chain[-1].supersedes_id is not None:
        parent = session.get(BusinessTrip, chain[-1].supersedes_id)
        if parent is None:  # pragma: no cover
            break
        chain.append(parent)
    for row in chain:
        session.delete(row)
        session.flush()
    session.add(
        Correction(
            entity_type="BUSINESS_TRIP",
            entity_id=current.id,
            old_version=current.version,
            new_version=None,
            actor_employee_id=employee.id,
            actor_role=ACTOR_ROLE_EMPLOYEE,
            reason=reason,
        )
    )
    record_audit(
        session,
        actor_principal=principal.subject,
        action="BUSINESS_TRIP_DELETE",
        outcome="SUCCESS",
        object_type="BUSINESS_TRIP",
        object_id=current.id,
        employee_subject_id=employee.id,
        metadata={"interval_version": current.version},
    )
    session.flush()


def _trip_window_last_month(start_day: date) -> date:
    """Last month a trip's TRAVEL-004 window can affect.

    The credit window is ``[start_day, start_day + 1 calendar month]`` and the
    derived credit is dated in the month after that window, so a change to the
    trip's start date can move both the eligible minutes and the month the
    credit lands in.
    """
    return following_month_start(add_one_calendar_month_clamped(start_day))


def _reopen_trip_credit_months(
    session: Session, employee_id: uuid.UUID, trip: BusinessTrip
) -> None:
    """Reopen the trip's TRAVEL-004 credit-effective month (EINV-15).

    A segment change is NOT confined to the segment's own months: the credit is
    derived from the passive minutes the trip's window contains and is dated in
    the month AFTER the window ends. Without this, a reviewed credit month could
    stay ``REVIEWED`` across a segment edit that changed its balance.

    ONLY the credit month — not the whole ``[start, window_end]`` range. A
    segment change cannot move the window itself (only a trip-start correction
    can, which ``patch_trip`` covers with the range), and the top-ups of the
    days the segment covers are already reopened by the caller's span
    invalidation. Reopening the intermediate window months would discard review
    markers for months whose result did not change.
    """
    credit_month = _trip_window_last_month(trip.start_at.astimezone(BERLIN).date())
    open_reviewed_month_range(session, employee_id, credit_month, credit_month)


def _new_work_interval(
    session: Session, employee: Employee, start: datetime, end: datetime
) -> WorkInterval:
    _check_overlap(session, employee.id, start, end)
    period = _ensure_period(session, employee.id, start)
    row = WorkInterval(
        employee_id=employee.id,
        work_period_id=period.id,
        start_at=start,
        end_at=end,
        work_type=WorkType.BUSINESS_TRIP_WORK,
        comment=None,
        source=IntervalSource.EMPLOYEE,
        version=1,
        created_by=employee.id,
    )
    session.add(row)
    session.flush()
    return row


def create_segment(
    session: Session, employee: Employee, trip_id: uuid.UUID, payload: SegmentIn
) -> tuple[SegmentOut, bool]:
    lock_employee_facts(session, employee.id)
    trip = _current_trip(session, employee.id, trip_id)
    if payload.client_request_id is not None:
        existing = session.scalar(
            select(TravelSegment)
            .join(BusinessTrip)
            .where(
                BusinessTrip.employee_id == employee.id,
                TravelSegment.client_request_id == payload.client_request_id,
            )
        )
        if existing is not None:
            # Replay: return the CURRENT head of the segment's supersede
            # chain, not the superseded version the original create returned.
            # Corrections never mutate the original row.
            return _segment_out(_segment_head(session, existing)), False
    start, end = _bounds(
        payload.start_local,
        payload.end_local,
        start_fold=payload.start_fold,
        end_fold=payload.end_fold,
        fold=payload.fold,
    )
    _assert_inside_trip(trip, start, end)
    _check_segment_shape(payload.classification, payload.basis)
    if payload.classification == TravelSegmentClassification.PASSIVE_TRAVEL:
        _assert_passive_no_overlap(session, employee.id, start, end)
        interval_id = None
    else:
        interval_id = _new_work_interval(session, employee, start, end).id
    row = TravelSegment(
        trip_id=trip.id,
        start_at=start,
        end_at=end,
        classification=payload.classification,
        basis=payload.basis,
        work_interval_id=interval_id,
        version=1,
        client_request_id=payload.client_request_id,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        _error("VALIDATION_FAILED", "This change was already recorded — reload the page to see it.")
        raise AssertionError from exc  # pragma: no cover
    # EINV-15: a segment is the fact that moves the month's travel top-up,
    # credit and findings — for BOTH classifications, so this sits at the
    # segment command rather than in _new_work_interval (which only the active
    # branch reaches). The trip's credit window is reopened as well: the derived
    # credit lands in a LATER month than the segment's own days.
    open_reviewed_months(session, employee.id, [(start, end)])
    _reopen_trip_credit_months(session, employee.id, trip)
    return _segment_out(row), True


def _linked_interval(session: Session, employee: Employee, segment: TravelSegment) -> WorkInterval:
    if segment.work_interval_id is None:
        raise RuntimeError("active travel segment has no linked work interval")
    interval = session.get(WorkInterval, segment.work_interval_id)
    if (
        interval is None
        or interval.employee_id != employee.id
        or interval.start_at != segment.start_at
        or interval.end_at != segment.end_at
    ):
        raise RuntimeError("travel work segment and linked interval drifted")
    return interval


def _delete_interval_chain(session: Session, interval: WorkInterval) -> None:
    """Delete a linked interval's current head and every predecessor safely."""
    chain = [interval]
    while chain[-1].supersedes_id is not None:
        parent = session.get(WorkInterval, chain[-1].supersedes_id)
        if parent is None:  # pragma: no cover - self FK makes this unreachable
            break
        chain.append(parent)
    for row in chain:
        session.delete(row)
        session.flush()


def _assert_segment_disclosable_delete(
    session: Session, employee_id: uuid.UUID, segment: TravelSegment
) -> None:
    for day in _segment_days(segment.start_at, segment.end_at):
        assert_not_disclosed(session, employee_id, day.replace(day=1))


def _segment_days(start: datetime, end: datetime) -> list[date]:
    first = start.astimezone(BERLIN).date()
    last = (end - timedelta(microseconds=1)).astimezone(BERLIN).date()
    result = []
    while first <= last:
        result.append(first)
        first += timedelta(days=1)
    return result


def patch_segment(
    session: Session, employee: Employee, segment_id: uuid.UUID, payload: SegmentPatch
) -> SegmentOut:
    lock_employee_facts(session, employee.id)
    current = _current_segment(session, employee.id, segment_id)
    if current.version != payload.version:
        _error("CONFLICT_VERSION", "Travel segment changed; refresh and retry.", 409)
    source_trip = session.get(BusinessTrip, current.trip_id)
    if source_trip is None:  # pragma: no cover - FK makes this unreachable
        raise RuntimeError("travel segment has no trip")
    trip = _trip_head(session, source_trip)
    start, end = _bounds(
        payload.start_local,
        payload.end_local,
        start_fold=payload.start_fold,
        end_fold=payload.end_fold,
        fold=payload.fold,
        previous_start=current.start_at,
        previous_end=current.end_at,
    )
    _assert_inside_trip(trip, start, end)
    classification = payload.classification or current.classification
    basis = payload.basis or current.basis
    _check_segment_shape(classification, basis)
    if classification != current.classification:
        _error(
            "VALIDATION_FAILED", "Travel segment classification cannot be changed by correction."
        )
    if classification == TravelSegmentClassification.PASSIVE_TRAVEL:
        _assert_passive_no_overlap(session, employee.id, start, end, current.id)
        interval_id = None
    else:
        old = _linked_interval(session, employee, current)
        _check_overlap(session, employee.id, start, end, exclude_id=old.id)
        replacement_interval = WorkInterval(
            employee_id=employee.id,
            work_period_id=_ensure_period(session, employee.id, start).id,
            start_at=start,
            end_at=end,
            work_type=WorkType.BUSINESS_TRIP_WORK,
            comment=None,
            source=IntervalSource.EMPLOYEE,
            version=old.version + 1,
            supersedes_id=old.id,
            created_by=employee.id,
        )
        session.add(replacement_interval)
        session.flush()
        interval_id = replacement_interval.id
        session.add(
            Correction(
                entity_type="WORK_INTERVAL",
                entity_id=replacement_interval.id,
                old_version=old.version,
                new_version=replacement_interval.version,
                actor_employee_id=employee.id,
                actor_role=ACTOR_ROLE_EMPLOYEE,
                reason=payload.reason,
            )
        )
    replacement = TravelSegment(
        trip_id=current.trip_id,
        start_at=start,
        end_at=end,
        classification=classification,
        basis=basis,
        work_interval_id=interval_id,
        version=current.version + 1,
        supersedes_id=current.id,
    )
    session.add(replacement)
    session.flush()
    session.add(
        Correction(
            entity_type="TRAVEL_SEGMENT",
            entity_id=replacement.id,
            old_version=current.version,
            new_version=replacement.version,
            actor_employee_id=employee.id,
            actor_role=ACTOR_ROLE_EMPLOYEE,
            reason=payload.reason,
        )
    )
    session.flush()
    # EINV-15: the superseded span AND the replacement reopen their months.
    open_reviewed_months(session, employee.id, [(current.start_at, current.end_at), (start, end)])
    # ...and the segment's membership in the trip pool changes the TRAVEL-004
    # credit, which lands in a LATER month than the segment itself.
    _reopen_trip_credit_months(session, employee.id, trip)
    return _segment_out(replacement)


def delete_segment(
    session: Session,
    principal: Principal,
    employee: Employee,
    segment_id: uuid.UUID,
    version: int,
    reason: str | None,
) -> None:
    lock_employee_facts(session, employee.id)
    current = _current_segment(session, employee.id, segment_id)
    if current.version != version:
        _error("CONFLICT_VERSION", "Travel segment changed; refresh and retry.", 409)
    _assert_segment_disclosable_delete(session, employee.id, current)
    # EINV-15: removing the segment removes its top-up/credit contribution —
    # including from the trip's credit window, which lands in a later month.
    open_reviewed_months(session, employee.id, [(current.start_at, current.end_at)])
    source_trip = session.get(BusinessTrip, current.trip_id)
    if source_trip is None:  # pragma: no cover - FK makes this unreachable
        raise RuntimeError("travel segment has no trip")
    _reopen_trip_credit_months(session, employee.id, _trip_head(session, source_trip))
    interval: WorkInterval | None = None
    if current.classification != TravelSegmentClassification.PASSIVE_TRAVEL:
        interval = _linked_interval(session, employee, current)
        assert_interval_not_disclosed(session, employee.id, interval)
    chain = [current]
    while chain[-1].supersedes_id is not None:
        parent = session.get(TravelSegment, chain[-1].supersedes_id)
        if parent is None:  # pragma: no cover
            break
        chain.append(parent)
    for row in chain:
        session.delete(row)
        session.flush()
    if interval is not None:
        _delete_interval_chain(session, interval)
        session.add(
            Correction(
                entity_type="WORK_INTERVAL",
                entity_id=interval.id,
                old_version=interval.version,
                new_version=None,
                actor_employee_id=employee.id,
                actor_role=ACTOR_ROLE_EMPLOYEE,
                reason=reason,
            )
        )
    session.add(
        Correction(
            entity_type="TRAVEL_SEGMENT",
            entity_id=current.id,
            old_version=current.version,
            new_version=None,
            actor_employee_id=employee.id,
            actor_role=ACTOR_ROLE_EMPLOYEE,
            reason=reason,
        )
    )
    record_audit(
        session,
        actor_principal=principal.subject,
        action="TRAVEL_SEGMENT_DELETE",
        outcome="SUCCESS",
        object_type="TRAVEL_SEGMENT",
        object_id=current.id,
        employee_subject_id=employee.id,
        metadata={"interval_version": current.version},
    )
    session.flush()


def _month_end(month: date) -> date:
    return date(month.year + 1, 1, 1) if month.month == 12 else date(month.year, month.month + 1, 1)


def travel_summary(session: Session, employee: Employee, month: date) -> TravelSummaryOut:
    required = _requirements(session, employee, month, _month_end(month))
    classes, _ = _calendar_classes(session, month, _month_end(month))
    context = load_travel_calculation(session, employee, month, required, classes)
    segments_by_trip = context.segments_by_trip
    result = context.result
    next_month = _month_end(month)
    selected = [
        item
        for item in result.trips
        if (
            (item.window_start < next_month and item.window_end >= month)
            or (item.credit_effective_on >= month and item.credit_effective_on < next_month)
            or any(
                segment.start_at < datetime.combine(next_month, datetime.min.time(), tzinfo=BERLIN)
                and segment.end_at > datetime.combine(month, datetime.min.time(), tzinfo=BERLIN)
                for segment in segments_by_trip[uuid.UUID(item.trip_id)]
            )
        )
    ]
    return _summary_out(month, result, selected)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _summary_out(
    month: date, result: TravelResult, selected: Sequence[TravelTripResult]
) -> TravelSummaryOut:
    return TravelSummaryOut(
        period_month=month,
        topups=[
            TravelTopupOut(day=item.day, minutes=item.minutes)
            for item in result.topups
            if item.day.year == month.year and item.day.month == month.month
        ],
        credits=[
            TravelCreditOut(
                occurred_on=item.occurred_on,
                minutes=item.minutes,
                rule_id=item.rule_id,
                source_entity_type=item.source_entity_type,
                source_entity_id=item.source_entity_id,
                window_start=item.window_start,
                window_end=item.window_end,
                eligible_minutes=item.eligible_minutes,
                threshold_minutes=item.threshold_minutes,
                excess_minutes=item.excess_minutes,
            )
            for item in result.credits
            if item.occurred_on.year == month.year and item.occurred_on.month == month.month
        ],
        trips=[
            TravelTripSummaryOut(
                trip_id=item.trip_id,
                window_start=item.window_start,
                window_end=item.window_end,
                eligible_minutes=item.eligible_minutes,
                threshold_minutes=item.threshold_minutes,
                excess_minutes=item.excess_minutes,
                credit_minutes=item.credit_minutes,
                credit_effective_on=item.credit_effective_on,
            )
            for item in selected
        ],
        findings=[
            TravelFindingOut(
                rule_id=item.rule_id, severity=item.severity, date=item.date, message=item.message
            )
            for item in result.findings
            if item.date is None
            or (item.date.year == month.year and item.date.month == month.month)
        ],
        input_fingerprint=result.input_fingerprint,
    )
