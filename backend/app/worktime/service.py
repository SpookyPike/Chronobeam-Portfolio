"""Worktime domain service — interval commands and private reads.

One transaction per command (the route's session commits once at the end;
get_db rolls back on any error). Overlap is application-level validation on
CURRENT intervals only, executed under the shared per-employee fact lock
(``app.db.lock_employee_facts``) so two concurrent creates cannot both pass it;
the check is narrowed to the candidate's own instant window, which is exactly
equivalent to the previous full-history scan. The hard exclusion constraint is
deliberately still not a DB constraint (BE §5.3:303): it cannot express
"current only".

Correction semantics (BE-CMD-002): the corrected row is preserved, a new row
with version+1 supersedes it, and a ``correction`` lineage row records
old->new. DELETE removes the current row of an unsubmitted fact and records
lineage + audit; once a fact is covered by a submission or grant, deletion
must become non-destructive supersession (ACCESS-011).
"""

import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from app.audit.models import record_audit
from app.auth.principal import Principal

# Delete guard for disclosed months (ACCESS-011). No import
# cycle: disclosure.guards imports only disclosure.models.
from app.db import lock_calendar_coordination, lock_employee_facts
from app.disclosure.guards import assert_interval_not_disclosed, assert_not_disclosed
from app.enums import ACTIVE_ASSIGNMENT_STATUSES, IntervalSource, PeriodStatus, WorkType
from app.identity.models import Employee, EmployeePreference

# No import cycle: app.shift.models imports only app.db and app.enums.
from app.shift.models import ShiftAssignment, ShiftPlanRevision
from app.worktime.models import BreakInterval, Correction, WorkInterval, WorkPeriod
from app.worktime.queries import (
    current_breaks,
    current_intervals,
    local_dates,
    month_bounds,
    spanned_local_dates,
)
from app.worktime.schemas import (
    ApplyTemplateIn,
    BreakIntervalIn,
    BreakIntervalOut,
    BreakIntervalPatch,
    DailyCalculationOut,
    DayDetail,
    DaySummary,
    IntervalIn,
    IntervalOut,
    IntervalPatch,
    MonthSummary,
    SplitIntervalIn,
)
from app.worktime.walltime import (
    AMBIGUOUS,
    BERLIN,
    DstResolutionError,
    local_date,
    resolve_wall_time,
)

if TYPE_CHECKING:
    from app.policy.engine import PeriodResult

from fastapi import HTTPException, status
from sqlalchemy import and_, exists, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

ACTOR_ROLE_EMPLOYEE = "EMPLOYEE"


def elapsed_seconds(start_at: datetime, end_at: datetime) -> int:
    """Elapsed instants, never naive clock subtraction (docs/ARCHITECTURE.md).

    Both operands are normalized to UTC first: Python's datetime treats two
    aware datetimes with the SAME tzinfo object as wall-clock values (fold is
    ignored), which would silently return the wrong duration across DST
    (covered by tests). DB-loaded values are UTC, but resolved wall
    times are Berlin-aware — normalize before subtracting.
    """
    utc = UTC
    return int((end_at.astimezone(utc) - start_at.astimezone(utc)).total_seconds())


def interval_out(interval: WorkInterval) -> IntervalOut:
    start_local = interval.start_at.astimezone(BERLIN).replace(tzinfo=None)
    end_local = interval.end_at.astimezone(BERLIN).replace(tzinfo=None)
    return IntervalOut(
        id=interval.id,
        work_type=interval.work_type,
        start_at=interval.start_at,
        end_at=interval.end_at,
        start_local=start_local,
        end_local=end_local,
        elapsed_seconds=elapsed_seconds(interval.start_at, interval.end_at),
        comment=interval.comment,
        source=interval.source,
        version=interval.version,
        supersedes_id=interval.supersedes_id,
        client_request_id=interval.client_request_id,
    )


def _resolve(name: str, naive: datetime, fold: int | None) -> datetime:
    """Resolve a wall time to an instant at the product's MINUTE resolution.

    Factual work commands are minute-resolution (the UI is minute-only and the
    day/month allocation is expressed in whole minutes). A sub-minute input is
    REJECTED here rather than floored, rounded or truncated: silently changing
    a value the employee typed would invent a rounding policy, and flooring is
    exactly what made a midnight-crossing span lose elapsed time (EINV-17).
    This is the single wall-time chokepoint for worktime, OCD/overtime, shift
    and travel, so every command boundary inherits the same contract.
    """
    try:
        resolved = resolve_wall_time(naive, fold=fold)
    except DstResolutionError as exc:
        details: dict[str, object] = {"kind": exc.kind}
        if exc.kind == AMBIGUOUS:
            first = naive.replace(fold=0, tzinfo=BERLIN)
            second = naive.replace(fold=1, tzinfo=BERLIN)
            details["candidates"] = [first.isoformat(), second.isoformat()]
        # The message must not name the API field (`name`) nor the internal kind
        # token: the employee reads it verbatim (BACKEND_SPEC §11) and cannot act
        # on either. The machine-readable `kind` stays in `details`, which is what
        # the client actually branches on.
        message = (
            "That wall time happens twice on the clock-change day; say which occurrence you mean."
            if exc.kind == AMBIGUOUS
            else "That wall time does not exist on the clock-change day; choose a valid time."
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "VALIDATION_DST_AMBIGUOUS",
                "message": message,
                "details": details,
            },
        ) from None
    except ValueError:  # aware input or similar
        # `str(exc)` is whatever the raising library chose to say; the employee
        # reads this verbatim (BACKEND_SPEC §11).
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "VALIDATION_FAILED",
                "message": "That date and time could not be read; enter a local date and time.",
            },
        ) from None
    if resolved.second or resolved.microsecond:
        raise _validation_error(
            "VALIDATION_FAILED",
            "Times are recorded to the minute; seconds are not accepted.",
        )
    return resolved


def _validation_error(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"code": code, "message": message},
    )


def _find_current_owned(
    session: Session, employee_id: uuid.UUID, interval_id: uuid.UUID
) -> WorkInterval:
    """Ownership INSIDE the lookup predicate: a foreign id is
    indistinguishable from a nonexistent one (404 NOT_FOUND)."""
    interval = session.scalar(
        select(WorkInterval).where(
            WorkInterval.id == interval_id,
            WorkInterval.employee_id == employee_id,
        )
    )
    if interval is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "Work interval not found."},
        )
    return interval


def _chain_head(session: Session, row: WorkInterval) -> WorkInterval:
    """Newest version of a logical interval (follows supersedes_id forward)."""
    head = row
    while True:
        follower = session.scalar(select(WorkInterval).where(WorkInterval.supersedes_id == head.id))
        if follower is None:
            return head
        head = follower


def _chain_root(session: Session, row: WorkInterval) -> WorkInterval:
    """Oldest version of a logical interval. The root row id is immutable:
    a copy/template retry after a correction must still derive the SAME
    idempotency key (its row id is stable even though the head's changes)."""
    root = row
    while root.supersedes_id is not None:
        follower = session.get(WorkInterval, root.supersedes_id)
        if follower is None:
            break
        root = follower
    return root


def _assert_not_superseded(session: Session, current: WorkInterval, supplied_version: int) -> None:
    """A row is stale when a LATER row supersedes it (the row's own
    ``supersedes_id`` points at its predecessor — that is the CURRENT-version
    direction). Correct/delete must target the newest version with the
    matching version number (optimistic locking, BE-CMD-002)."""
    newer = session.scalar(select(WorkInterval.id).where(WorkInterval.supersedes_id == current.id))
    if newer is not None or current.version != supplied_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "CONFLICT_VERSION",
                "message": "Interval changed since it was loaded; refresh and retry.",
            },
        )


def _ensure_period(session: Session, employee_id: uuid.UUID, start_at: datetime) -> WorkPeriod:
    """Get-or-create the period of the interval's Berlin START month (M5).

    Stored once: the work_period_id follows the start month even when the
    interval crosses into the next month (DATA-008). The ON CONFLICT clause
    makes concurrent first-writes safe.

    This is the canonical ownership rule and the ONLY way a
    ``work_period_id`` may be chosen: it is RE-DERIVED from the row's own
    ``start_at`` on every write — create AND correct — and is never copied
    from a predecessor. A correction that moves an interval into another month
    must therefore relink it (EINV-14: a persisted derived linkage may not
    contradict the fact it derives from).
    """
    period_month = local_date(start_at).replace(day=1)
    session.execute(
        pg_insert(WorkPeriod)
        .values(employee_id=employee_id, period_month=period_month, status=PeriodStatus.OPEN)
        .on_conflict_do_nothing(index_elements=["employee_id", "period_month"])
    )
    period = session.scalar(
        select(WorkPeriod).where(
            WorkPeriod.employee_id == employee_id,
            WorkPeriod.period_month == period_month,
        )
    )
    if period is None:  # pragma: no cover - ON CONFLICT guarantees presence
        raise RuntimeError("work period vanished after insert")
    return period


def _ensure_month_period(session: Session, employee_id: uuid.UUID, month: date) -> WorkPeriod:
    """Create an employee-only period marker even when it has no intervals."""
    return _ensure_period(session, employee_id, datetime.combine(month, time.min, tzinfo=BERLIN))


def _reopen_reviewed(session: Session, employee_id: uuid.UUID, criteria: Any) -> None:
    """REVIEWED -> OPEN for every matching period of one employee.

    Deliberately an ORM attribute assignment rather than a bulk UPDATE:
    ``session.scalar(select(WorkPeriod))`` returns an already-loaded instance
    unchanged, so a bulk UPDATE (or ``synchronize_session=False``) would leave
    a stale ``REVIEWED`` visible to ``month_summary`` inside the same
    transaction. Only ``REVIEWED`` is reopened — ``CLOSED`` is an
    institutional state that a fact mutation must not silently unlock.

    Flushed immediately when something changed: the invalidation is part of
    the command's effect on the transaction, not a pending side effect that a
    later ``expire()``/``expire_all()`` in the same session could discard.
    """
    changed = False
    for period in session.scalars(
        select(WorkPeriod).where(WorkPeriod.employee_id == employee_id, criteria)
    ).all():
        if period.status is PeriodStatus.REVIEWED:
            period.status = PeriodStatus.OPEN
            changed = True
    if changed:
        session.flush()


def open_reviewed_months(
    session: Session,
    employee_id: uuid.UUID,
    spans: Iterable[tuple[datetime, datetime]],
) -> None:
    """Reopen the review of every month an old or new fact span touches (EINV-15).

    The reviewed-month contract is bounded: ``REVIEWED`` asserts that the
    employee reviewed the factual entries belonging to that month, so a change
    reopens the months of BOTH the superseded span and its replacement — a
    cross-midnight edit legitimately reopens two months. An earlier month's
    correction does not cascade into later months through carry-forward.
    """
    months = {
        day.replace(day=1) for start_at, end_at in spans for day in local_dates(start_at, end_at)
    }
    if months:
        _reopen_reviewed(session, employee_id, WorkPeriod.period_month.in_(sorted(months)))


def open_reviewed_month_range(
    session: Session,
    employee_id: uuid.UUID,
    start: date,
    end: date | None = None,
) -> None:
    """Reopen the review of every month intersecting an inclusive date range.

    For effective-dated inputs (the employee's working-time schedule, an
    overtime authorization window) the affected months are bounded by the
    row's own effective range rather than by an instant span. ``end=None``
    means the range is open-ended; only EXISTING period rows are reopened, so
    months that have no facts are untouched.
    """
    criteria = WorkPeriod.period_month >= start.replace(day=1)
    if end is not None:
        criteria = and_(criteria, WorkPeriod.period_month <= end.replace(day=1))
    _reopen_reviewed(session, employee_id, criteria)


def reopen_reviewed_months_in_publication(session: Session, months: Iterable[date]) -> int:
    """Reopen ``REVIEWED`` months for EVERY employee (EINV-15).

    Caller contract: ``lock_calendar_coordination`` is already held. That is what
    makes a set-based statement sound here — every ``REVIEWED`` transition is
    excluded until this transaction commits, so the ``UPDATE`` cannot miss a
    period that a concurrent review is about to create or mark. Only an
    institutional publication that changes EVERY employee's calculation inputs
    may use this; per-employee fact mutations keep using
    ``open_reviewed_month_range``.

    Deliberately a bulk ``UPDATE`` with ``synchronize_session="fetch"``, unlike
    ``_reopen_reviewed``'s ORM attribute assignment: that helper's rationale
    exists because a *fact-mutation command* subsequently reads that employee's
    month summary in the same session. A CLI publication does not, and the fetch
    strategy still keeps any already-loaded ``WorkPeriod`` consistent.
    """
    normalised = sorted({month.replace(day=1) for month in months})
    if not normalised:
        return 0
    result = session.execute(
        update(WorkPeriod)
        .where(
            WorkPeriod.period_month.in_(normalised),
            WorkPeriod.status == PeriodStatus.REVIEWED,
        )
        .values(status=PeriodStatus.OPEN)
        .execution_options(synchronize_session="fetch")
    )
    return int(getattr(result, "rowcount", 0) or 0)


def _check_overlap(
    session: Session,
    employee_id: uuid.UUID,
    start_at: datetime,
    end_at: datetime,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """Reject overlap against OTHER current facts (M3).

    The corrected row itself is excluded (``exclude_id``), so editing an
    interval to still share time with its own predecessor succeeds. The
    predicate is period-agnostic (spans months) and uses instants.

    The query is narrowed to the candidate's own instant window: that is
    EXACTLY equivalent to the Python test below (``current_intervals`` applies
    ``end_at > from AND start_at < to``; the loop tests
    ``other.start_at < end_at and other.end_at > start_at`` — the same
    half-open intersection), and it keeps an O(history) load out of every
    interval write. ``exclude_id`` becomes a no-op for rows outside the
    window, which is correct: such a row cannot overlap.
    """
    for other in current_intervals(
        session,
        employee_id=employee_id,
        intersects_from=start_at,
        intersects_to=end_at,
    ):
        if exclude_id is not None and other.id == exclude_id:
            continue
        if other.start_at < end_at and other.end_at > start_at:
            raise _validation_error(
                "VALIDATION_INTERVAL_OVERLAP",
                "Interval overlaps an existing work interval; adjust the times.",
            )


def _validate_overtime_link(
    session: Session, employee_id: uuid.UUID, work_type: WorkType, authorization_id: uuid.UUID
) -> None:
    """An overtime authorization reference is valid only on OVERTIME_*
    facts owned by the same employee (ALLOW-001/002, ACCESS-006).
    The FK guarantees existence; this closes the cross-employee hole.

    Local import: ocd_overtime depends on worktime (ARCH module table), so the
    ownership check on this write path is imported here instead of creating a
    reverse module dependency.
    """
    from app.ocd_overtime.models import OvertimeAuthorization

    if work_type not in {WorkType.OVERTIME_PAID, WorkType.OVERTIME_TIME_COMP}:
        raise _validation_error(
            "VALIDATION_FAILED",
            "Overtime authorization links are only valid on OVERTIME_* intervals.",
        )
    newer = aliased(OvertimeAuthorization)
    authorization = session.scalar(
        select(OvertimeAuthorization).where(
            OvertimeAuthorization.id == authorization_id,
            OvertimeAuthorization.employee_id == employee_id,
            # Employee-scoped anti-join: `supersedes_id` is a plain self-FK, so a
            # restored/imported row belonging to ANOTHER employee must not make
            # this employee's own authorization unavailable for linking.
            ~exists(
                select(newer.id).where(
                    newer.supersedes_id == OvertimeAuthorization.id,
                    newer.employee_id == OvertimeAuthorization.employee_id,
                )
            ),
        )
    )
    if authorization is None:
        raise _validation_error(
            "VALIDATION_FAILED", "Overtime authorization reference is not available."
        )


def _assert_not_ocd_linked(current: WorkInterval) -> None:
    """OCD-linked intervals are corrected/exercised through the OCD call
    commands only (INV-10): direct interval mutation would break
    the call<->fact atomicity invariant."""
    if current.linked_ocd_call_id is not None:
        raise _validation_error(
            "VALIDATION_FAILED",
            "OCD call facts are corrected through the OCD call command.",
        )


def _assert_not_travel_linked(session: Session, current: WorkInterval) -> None:
    """A work interval linked to a CURRENT travel
    segment is corrected only through the travel segment command, so the
    segment<->interval pair can never drift."""
    from app.travel.models import TravelSegment

    child = aliased(TravelSegment)
    linked = session.scalar(
        select(TravelSegment.id).where(
            TravelSegment.work_interval_id == current.id,
            ~exists(select(child.id).where(child.supersedes_id == TravelSegment.id)),
        )
    )
    if linked is not None:
        raise _validation_error(
            "VALIDATION_FAILED",
            "Travel work intervals are corrected through the travel segment command.",
        )


def create_interval(
    session: Session,
    employee: Employee,
    payload: IntervalIn,
) -> tuple[IntervalOut, bool]:
    """BE-CMD-001 with client_request_id idempotency (M10).

    Returns (output, created). A retried create with the same
    client_request_id returns the existing interval (created=False, 200).
    """
    lock_employee_facts(session, employee.id)
    if payload.client_request_id is not None:
        existing = session.scalar(
            select(WorkInterval).where(
                WorkInterval.employee_id == employee.id,
                WorkInterval.client_request_id == payload.client_request_id,
            )
        )
        if existing is not None:
            # The retried create must never report a SUPERSEDED version:
            # follow the chain to its current head.
            return interval_out(_chain_head(session, existing)), False

    start_at = _resolve("start_local", payload.start_local, payload.fold)
    end_at = _resolve("end_local", payload.end_local, payload.fold)
    return create_interval_instant(
        session,
        employee,
        start_at=start_at,
        end_at=end_at,
        work_type=payload.work_type,
        comment=payload.comment,
        client_request_id=payload.client_request_id,
    )


def create_interval_instant(
    session: Session,
    employee: Employee,
    *,
    start_at: datetime,
    end_at: datetime,
    work_type: WorkType,
    comment: str | None,
    client_request_id: uuid.UUID | None = None,
    linked_shift_assignment_id: uuid.UUID | None = None,
    linked_ocd_call_id: uuid.UUID | None = None,
    overtime_authorization_id: uuid.UUID | None = None,
    source: IntervalSource = IntervalSource.EMPLOYEE,
) -> tuple[IntervalOut, bool]:
    """Insert a new interval from already-resolved aware instants.

    The single ``fold`` field cannot describe a split point and an end that
    fall in DIFFERENT DST occurrences, so split resolves both boundaries to
    instants and uses this helper instead of round-tripping through naive
    wall times."""
    lock_employee_facts(session, employee.id)
    if end_at <= start_at:
        raise _validation_error(
            "VALIDATION_INTERVAL_ORDER", "The end time must be after the start time."
        )
    # BUSINESS_TRIP_WORK exists only as the linked side
    # of a travel segment command (the travel service inserts it directly);
    # every generic creation path — API create, split, template, copy — is
    # closed here once, at the single insertion chokepoint.
    if work_type is WorkType.BUSINESS_TRIP_WORK:
        raise _validation_error(
            "VALIDATION_FAILED",
            "Business-trip work is recorded through the travel segment command.",
        )
    if overtime_authorization_id is not None:
        _validate_overtime_link(session, employee.id, work_type, overtime_authorization_id)
    _check_overlap(session, employee.id, start_at, end_at)
    period = _ensure_period(session, employee.id, start_at)
    interval = WorkInterval(
        employee_id=employee.id,
        work_period_id=period.id,
        start_at=start_at,
        end_at=end_at,
        work_type=work_type,
        comment=comment,
        linked_shift_assignment_id=linked_shift_assignment_id,
        linked_ocd_call_id=linked_ocd_call_id,
        overtime_authorization_id=overtime_authorization_id,
        source=source,
        version=1,
        client_request_id=client_request_id,
        created_by=employee.id,
    )
    session.add(interval)
    try:
        session.flush()
    except IntegrityError as exc:  # client_request_id collision with another employee
        raise _validation_error(
            "VALIDATION_FAILED", "This change was already recorded — reload the page to see it."
        ) from exc
    # EINV-15: a new fact reopens the review of every month it touches. This is
    # the single insertion chokepoint, so API create, template apply,
    # copy-previous-day, split's second half, shift confirmation and a new
    # shift deviation all pass through it.
    open_reviewed_months(session, employee.id, [(start_at, end_at)])
    return interval_out(interval), True


def correct_interval(
    session: Session,
    employee: Employee,
    interval_id: uuid.UUID,
    payload: IntervalPatch,
) -> IntervalOut:
    """BE-CMD-002 — create a superseding version + correction lineage."""
    lock_employee_facts(session, employee.id)
    current = _find_current_owned(session, employee.id, interval_id)
    _assert_not_superseded(session, current, payload.version)
    _assert_not_ocd_linked(current)
    # The segment<->interval pair is only ever mutated
    # as a unit through the travel segment command.
    _assert_not_travel_linked(session, current)

    work_type = payload.work_type if payload.work_type is not None else current.work_type
    # A plain fact may not be RE-TYPED into business-trip work either; only
    # the travel command's own linked creation produces that type.
    if work_type is WorkType.BUSINESS_TRIP_WORK and current.work_type is not work_type:
        raise _validation_error(
            "VALIDATION_FAILED",
            "Business-trip work is recorded through the travel segment command.",
        )
    if "overtime_authorization_id" in payload.model_fields_set:
        ot_link = payload.overtime_authorization_id
        if ot_link is not None:
            _validate_overtime_link(session, employee.id, work_type, ot_link)
    else:
        ot_link = current.overtime_authorization_id
        if ot_link is not None and work_type not in {
            WorkType.OVERTIME_PAID,
            WorkType.OVERTIME_TIME_COMP,
        }:
            raise _validation_error(
                "VALIDATION_FAILED",
                "Remove the overtime authorization link before changing the work type.",
            )

    start_at = (
        _resolve(
            "start_local",
            payload.start_local,
            payload.start_fold if payload.start_fold is not None else payload.fold,
        )
        if payload.start_local is not None
        else current.start_at
    )
    end_at = (
        _resolve(
            "end_local",
            payload.end_local,
            payload.end_fold if payload.end_fold is not None else payload.fold,
        )
        if payload.end_local is not None
        else current.end_at
    )
    if end_at <= start_at:
        raise _validation_error(
            "VALIDATION_INTERVAL_ORDER", "The end time must be after the start time."
        )
    _check_overlap(session, employee.id, start_at, end_at, exclude_id=current.id)

    work_type = payload.work_type if payload.work_type is not None else current.work_type
    # ``comment`` uses presence, not truthiness: absent = unchanged,
    # explicit null = cleared.
    comment = payload.comment if "comment" in payload.model_fields_set else current.comment

    superseding = WorkInterval(
        employee_id=employee.id,
        # EINV-14: re-derived from THIS row's start month, never copied from
        # the predecessor. A correction that moves the interval across a month
        # boundary must relink it; when the start month is unchanged this
        # resolves to the same period row.
        work_period_id=_ensure_period(session, employee.id, start_at).id,
        start_at=start_at,
        end_at=end_at,
        work_type=work_type,
        comment=comment,
        source=current.source,
        linked_shift_assignment_id=current.linked_shift_assignment_id,
        linked_ocd_call_id=current.linked_ocd_call_id,
        overtime_authorization_id=ot_link,
        version=current.version + 1,
        supersedes_id=current.id,
        client_request_id=None,
        created_by=employee.id,
    )
    session.add(superseding)
    session.add(
        Correction(
            entity_type="WORK_INTERVAL",
            entity_id=superseding.id,
            old_version=current.version,
            new_version=superseding.version,
            actor_employee_id=employee.id,
            actor_role=ACTOR_ROLE_EMPLOYEE,
            reason=payload.reason,
        )
    )
    session.flush()
    # EINV-15: the superseded span AND the replacement reopen their months, so
    # a cross-midnight/cross-month correction reopens both sides.
    open_reviewed_months(
        session, employee.id, [(current.start_at, current.end_at), (start_at, end_at)]
    )
    return interval_out(superseding)


def delete_interval(
    session: Session,
    principal: Principal,
    employee: Employee,
    interval_id: uuid.UUID,
    version: int,
    reason: str | None,
) -> None:
    """Correction/tombstone for an unsubmitted fact.

    Requires the current version (optimistic locking). The row is removed and
    correction lineage + audit event remain. Once facts are covered by a
    submission package or a granted snapshot the delete is REFUSED
    (CONFLICT_DISCLOSED_FACT, ACCESS-011): the recipient
    reviewed the old state, so the employee must use a versioned correction
    (preserves lineage); re-submitting or re-granting shares the superseding
    view.
    """
    lock_employee_facts(session, employee.id)
    current = _find_current_owned(session, employee.id, interval_id)
    outcome = "SUCCESS"
    _assert_not_superseded(session, current, version)
    _assert_not_ocd_linked(current)
    # Deleting the interval side of a travel pair
    # without its segment would leave the segment pointing at nothing.
    _assert_not_travel_linked(session, current)
    assert_interval_not_disclosed(session, employee.id, current)
    # EINV-15: deleting a fact reopens the review of every month it touched.
    open_reviewed_months(session, employee.id, [(current.start_at, current.end_at)])
    # Deleting a fact removes the WHOLE version chain: removing
    # only the head would resurrect the superseded predecessor as "current"
    # (the current-version predicate is NOT EXISTS supersedes_id).
    chain = [current]
    cursor = current
    while cursor.supersedes_id is not None:
        ancestor = session.get(WorkInterval, cursor.supersedes_id)
        if ancestor is None:  # pragma: no cover - FK guarantees presence
            break
        chain.append(ancestor)
        cursor = ancestor
    # Head-first with an explicit flush per row: a batched DELETE would
    # violate the self-referential fk_interval_supersedes (the ancestor is
    # still referenced until its child is gone).
    for row in chain:
        session.delete(row)
        session.flush()
    session.add(
        Correction(
            entity_type="WORK_INTERVAL",
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
        action="WORK_INTERVAL_DELETE",
        outcome=outcome,
        object_type="WORK_INTERVAL",
        object_id=current.id,
        employee_subject_id=employee.id,
        metadata={"interval_version": current.version},
    )
    session.flush()


# ---------------------------------------------------------------------------
# Explicit factual breaks (WT-BACKEND §5.3 ``break_interval``)
#
# Only for a break that a gap between intervals cannot express — primarily a
# break INSIDE one factual interval (a shift), which the span would otherwise
# count as worked time. Ordinary gaps stay inferred, so an employee is never
# asked to enter lunch twice. The command shape mirrors the interval commands
# exactly (idempotency, version chain, month reopen, disclosed-fact guard). The
# overlap check is break-vs-break, never break-vs-work: an explicit break exists
# precisely to sit inside a work span.
# ---------------------------------------------------------------------------


def break_interval_out(row: BreakInterval) -> BreakIntervalOut:
    return BreakIntervalOut(
        id=row.id,
        kind=row.kind,
        start_at=row.start_at,
        end_at=row.end_at,
        start_local=row.start_at.astimezone(BERLIN).replace(tzinfo=None),
        end_local=row.end_at.astimezone(BERLIN).replace(tzinfo=None),
        elapsed_seconds=elapsed_seconds(row.start_at, row.end_at),
        linked_shift_assignment_id=row.linked_shift_assignment_id,
        version=row.version,
        supersedes_id=row.supersedes_id,
        client_request_id=row.client_request_id,
    )


def _break_chain_head(session: Session, row: BreakInterval) -> BreakInterval:
    """Newest version of a logical break (follows supersedes_id forward)."""
    head = row
    while True:
        follower = session.scalar(
            select(BreakInterval).where(BreakInterval.supersedes_id == head.id)
        )
        if follower is None:
            return head
        head = follower


def _find_current_owned_break(
    session: Session, employee_id: uuid.UUID, break_id: uuid.UUID
) -> BreakInterval:
    """Ownership INSIDE the lookup predicate: a foreign id is
    indistinguishable from a nonexistent one (404 NOT_FOUND)."""
    row = session.scalar(
        select(BreakInterval).where(
            BreakInterval.id == break_id,
            BreakInterval.employee_id == employee_id,
        )
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "Break interval not found."},
        )
    return row


def _assert_break_not_superseded(
    session: Session, current: BreakInterval, supplied_version: int
) -> None:
    newer = session.scalar(
        select(BreakInterval.id).where(BreakInterval.supersedes_id == current.id)
    )
    if newer is not None or current.version != supplied_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "CONFLICT_VERSION",
                "message": "Break changed since it was loaded; refresh and retry.",
            },
        )


def _check_break_overlap(
    session: Session,
    employee_id: uuid.UUID,
    start_at: datetime,
    end_at: datetime,
    *,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """Two current BREAKS of one employee must not overlap.

    Deliberately NOT checked against work intervals: an explicit break exists to
    sit inside a factual interval, which is the case a gap cannot express.
    """
    for existing in current_breaks(
        session, employee_id=employee_id, intersects_from=start_at, intersects_to=end_at
    ):
        if exclude_id is not None and existing.id == exclude_id:
            continue
        if existing.start_at < end_at and existing.end_at > start_at:
            raise _validation_error(
                "VALIDATION_INTERVAL_OVERLAP", "Break intervals must not overlap."
            )


def _assert_shift_assignment_owned(
    session: Session,
    employee_id: uuid.UUID,
    assignment_id: uuid.UUID | None,
    start_at: datetime,
    end_at: datetime,
) -> None:
    """A break's shift link must be the employee's OWN assignment, and the break
    must fall inside that shift.

    Ownership alone is not enough. The link is what SHIFT-003's
    ``paid_break_status`` is resolved from, so a break recorded on some other
    date could otherwise be linked to a shift and make that shift — and the
    report submitted from it — claim a paid-break fact that never occurred in
    it. The assignment's approved PLANNED span is the shift's own extent, and it
    is the cross-midnight-correct one. Ownership sits inside the lookup
    predicate, so a foreign id is indistinguishable from a nonexistent one.
    """
    if assignment_id is None:
        return
    span = session.execute(
        select(ShiftPlanRevision.planned_start_at, ShiftPlanRevision.planned_end_at)
        .join(ShiftAssignment, ShiftAssignment.approved_revision_id == ShiftPlanRevision.id)
        .where(
            ShiftAssignment.id == assignment_id,
            ShiftAssignment.employee_id == employee_id,
            # Only an ACTIVE plan can be the link a paid-break fact is resolved
            # from. A withdrawn plan is retained lineage, so linking a break to it
            # would let a retracted shift claim a SHIFT-003 paid-break status.
            ShiftAssignment.status.in_(ACTIVE_ASSIGNMENT_STATUSES),
        )
    ).first()
    if span is None:
        raise _validation_error(
            "VALIDATION_FAILED",
            "That shift is not one of your approved shift assignments.",
        )
    planned_start, planned_end = span
    # FULL containment, not mere overlap. A break that only grazes the shift
    # (18:00-19:01 against a 19:00-07:00 shift) would otherwise mark that shift's
    # paid-break requirement met while almost none of the asserted break happened
    # in it. A break that genuinely runs past the shift end is a different fact
    # and must be recorded as one.
    if start_at < planned_start or end_at > planned_end:
        raise _validation_error(
            "VALIDATION_FAILED", "The break must fall entirely inside the linked shift."
        )


def create_break_interval(
    session: Session, employee: Employee, payload: BreakIntervalIn
) -> tuple[BreakIntervalOut, bool]:
    """Record an explicit factual break. Idempotent on client_request_id."""
    lock_employee_facts(session, employee.id)
    if payload.client_request_id is not None:
        existing = session.scalar(
            select(BreakInterval).where(
                BreakInterval.employee_id == employee.id,
                BreakInterval.client_request_id == payload.client_request_id,
            )
        )
        if existing is not None:
            return break_interval_out(_break_chain_head(session, existing)), False
    start_at = _resolve("start_local", payload.start_local, payload.fold)
    end_at = _resolve("end_local", payload.end_local, payload.fold)
    if end_at <= start_at:
        raise _validation_error(
            "VALIDATION_INTERVAL_ORDER", "The break end must be after the break start."
        )
    _check_break_overlap(session, employee.id, start_at, end_at)
    _assert_shift_assignment_owned(
        session, employee.id, payload.linked_shift_assignment_id, start_at, end_at
    )
    period = _ensure_period(session, employee.id, start_at)
    row = BreakInterval(
        employee_id=employee.id,
        work_period_id=period.id,
        start_at=start_at,
        end_at=end_at,
        kind=payload.kind,
        linked_shift_assignment_id=payload.linked_shift_assignment_id,
        version=1,
        client_request_id=payload.client_request_id,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:  # client_request_id collision with another employee
        raise _validation_error(
            "VALIDATION_FAILED", "This change was already recorded — reload the page to see it."
        ) from exc
    # EINV-15: a break is a calculation input (it changes worked minutes), so a
    # new one reopens the review of every month it touches.
    open_reviewed_months(session, employee.id, [(start_at, end_at)])
    return break_interval_out(row), True


def correct_break_interval(
    session: Session,
    employee: Employee,
    break_id: uuid.UUID,
    payload: BreakIntervalPatch,
) -> BreakIntervalOut:
    """Supersede a break (version+1) with correction lineage; relinks the period."""
    lock_employee_facts(session, employee.id)
    current = _find_current_owned_break(session, employee.id, break_id)
    _assert_break_not_superseded(session, current, payload.version)
    start_at = (
        _resolve(
            "start_local",
            payload.start_local,
            payload.start_fold if payload.start_fold is not None else payload.fold,
        )
        if payload.start_local is not None
        else current.start_at
    )
    end_at = (
        _resolve(
            "end_local",
            payload.end_local,
            payload.end_fold if payload.end_fold is not None else payload.fold,
        )
        if payload.end_local is not None
        else current.end_at
    )
    if end_at <= start_at:
        raise _validation_error(
            "VALIDATION_INTERVAL_ORDER", "The break end must be after the break start."
        )
    _check_break_overlap(session, employee.id, start_at, end_at, exclude_id=current.id)
    # The link is PRESERVED by a correction, so it must be re-validated against
    # the NEW times: a correction could otherwise move a linked break out of the
    # shift it still claims to belong to.
    _assert_shift_assignment_owned(
        session, employee.id, current.linked_shift_assignment_id, start_at, end_at
    )
    # EINV-14: the owning period is re-derived from the NEW start month, never
    # copied from the predecessor.
    period = _ensure_period(session, employee.id, start_at)
    superseding = BreakInterval(
        employee_id=employee.id,
        work_period_id=period.id,
        start_at=start_at,
        end_at=end_at,
        kind=payload.kind if payload.kind is not None else current.kind,
        linked_shift_assignment_id=current.linked_shift_assignment_id,
        version=current.version + 1,
        supersedes_id=current.id,
    )
    session.add(superseding)
    session.flush()
    session.add(
        Correction(
            entity_type="BREAK_INTERVAL",
            entity_id=current.id,
            old_version=current.version,
            new_version=superseding.version,
            actor_employee_id=employee.id,
            actor_role=ACTOR_ROLE_EMPLOYEE,
            reason=payload.reason,
        )
    )
    open_reviewed_months(
        session, employee.id, [(current.start_at, current.end_at), (start_at, end_at)]
    )
    return break_interval_out(superseding)


def delete_break_interval(
    session: Session,
    principal: Principal,
    employee: Employee,
    break_id: uuid.UUID,
    version: int,
    reason: str | None,
) -> None:
    """Remove a break (whole version chain) unless its month is disclosed."""
    lock_employee_facts(session, employee.id)
    current = _find_current_owned_break(session, employee.id, break_id)
    _assert_break_not_superseded(session, current, version)
    # ACCESS-011: the same month-granular guard as an interval. A break is a
    # calculation input, so deleting one after the month was disclosed is
    # refused and the versioned correction is the employee's path.
    for covered_day in local_dates(current.start_at, current.end_at):
        assert_not_disclosed(session, employee.id, covered_day.replace(day=1))
    open_reviewed_months(session, employee.id, [(current.start_at, current.end_at)])
    chain = [current]
    cursor = current
    while cursor.supersedes_id is not None:
        ancestor = session.get(BreakInterval, cursor.supersedes_id)
        if ancestor is None:  # pragma: no cover - FK guarantees presence
            break
        chain.append(ancestor)
        cursor = ancestor
    for row in chain:
        session.delete(row)
        session.flush()
    session.add(
        Correction(
            entity_type="BREAK_INTERVAL",
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
        action="BREAK_INTERVAL_DELETE",
        outcome="SUCCESS",
        object_type="BREAK_INTERVAL",
        object_id=current.id,
        employee_subject_id=employee.id,
        # No metadata: the audit allowlist is a fixed, deliberately narrow
        # vocabulary, and the version is already recorded on the correction row.
    )
    session.flush()


def month_summary(
    session: Session,
    employee: Employee,
    month: date,
    *,
    calculation: "PeriodResult | None" = None,
) -> MonthSummary:
    """Private month calendar with server-derived daily calculation results."""
    start, end = month_bounds(month)
    intervals = current_intervals(
        session, employee_id=employee.id, intersects_from=start, intersects_to=end
    )
    buckets: dict[date, int] = {}
    for interval in intervals:
        for day in spanned_local_dates(interval):
            if start.date() <= day < end.date():
                buckets[day] = buckets.get(day, 0) + 1
    period = session.scalar(
        select(WorkPeriod).where(
            WorkPeriod.employee_id == employee.id,
            WorkPeriod.period_month == month,
        )
    )
    # Keep calculations in the existing pure engine/persistence adapter.  The
    # frontend renders these values but never re-derives authoritative totals.
    from app.policy.service import calculate_employee_period

    if calculation is None:
        calculation = calculate_employee_period(session, employee, month)
    return MonthSummary(
        period_month=month,
        status=period.status if period is not None else PeriodStatus.OPEN,
        interval_count=len(intervals),
        days=[DaySummary(date=day, interval_count=count) for day, count in sorted(buckets.items())],
        daily_results=[
            DailyCalculationOut(
                date=result.day,
                worked_minutes=result.worked_minutes,
                required_minutes=result.required_minutes,
                delta_minutes=result.flex_delta_minutes,
                travel_topup_minutes=result.travel_topup_minutes,
                calendar_class=result.calendar_class,
                account_credit_minutes=result.account_credit_minutes,
            )
            for result in calculation.daily_results
        ],
        closing_balance_minutes=calculation.closing_balance_minutes,
    )


def day_detail(session: Session, employee: Employee, day: date) -> DayDetail:
    """Private day timeline: current intervals intersecting the local day (M5)."""
    from app.worktime.queries import day_bounds

    start, end = day_bounds(day)
    intervals = current_intervals(
        session, employee_id=employee.id, intersects_from=start, intersects_to=end
    )
    breaks = current_breaks(
        session, employee_id=employee.id, intersects_from=start, intersects_to=end
    )
    return DayDetail(
        date=day,
        intervals=[interval_out(i) for i in intervals],
        breaks=[break_interval_out(b) for b in breaks],
    )


def apply_normal_template(
    session: Session, employee: Employee, month: date, payload: ApplyTemplateIn
) -> list[IntervalOut]:
    """BE-CMD-003: explicitly turn a saved preference into editable facts."""
    lock_employee_facts(session, employee.id)
    if any(day.year != month.year or day.month != month.month for day in payload.dates):
        raise _validation_error(
            "VALIDATION_INVALID_DATE", "Template dates must belong to this month."
        )
    preference = session.scalar(
        select(EmployeePreference).where(EmployeePreference.employee_id == employee.id)
    )
    template = preference.normal_day_template_json if preference is not None else None
    if not isinstance(template, list) or not template:
        raise _validation_error(
            "VALIDATION_FAILED", "Save a normal-day template before applying it."
        )

    created: list[IntervalOut] = []
    for day in sorted(set(payload.dates)):
        for index, entry in enumerate(template):
            if not isinstance(entry, dict):  # Defensive validation for old/corrupt preference data.
                raise _validation_error("VALIDATION_FAILED", "Normal-day template is invalid.")
            try:
                start_clock = time.fromisoformat(str(entry["start"]))
                end_clock = time.fromisoformat(str(entry["end"]))
                work_type = entry["work_type"]
            except (KeyError, ValueError) as exc:
                raise _validation_error(
                    "VALIDATION_FAILED", "Normal-day template is invalid."
                ) from exc
            start_local = datetime.combine(day, start_clock)
            end_day = day + timedelta(days=1) if entry.get("ends_next_day") else day
            end_local = datetime.combine(end_day, end_clock)
            out, _ = create_interval(
                session,
                employee,
                IntervalIn(
                    start_local=start_local,
                    end_local=end_local,
                    work_type=work_type,
                    comment=entry.get("comment"),
                    client_request_id=_command_key(
                        payload.client_request_id,
                        employee.id,
                        # Index disambiguates two template entries with the
                        # same start clock: without it the second would be
                        # misread as an idempotent replay of the first.
                        f"{day.isoformat()}|{index}|{start_local.isoformat()}",
                    ),
                ),
            )
            created.append(out)
    return created


def _command_key(
    command_id: uuid.UUID | None, employee_id: uuid.UUID, tag: str
) -> uuid.UUID | None:
    """Derive a stable per-interval idempotency key for a create-producing
    command, namespaced by employee (the client_request_id uniqueness on
    ``work_interval`` is global). Retrying the same command replays; a new
    click is a new command."""
    if command_id is None:
        return None
    return uuid.uuid5(uuid.NAMESPACE_URL, f"chronobeam:{command_id}|{employee_id}|{tag}")


def review_month(
    session: Session,
    employee: Employee,
    month: date,
    acknowledged: bool = False,
) -> MonthSummary:
    """Employee-side lifecycle marker only; it has no disclosure effect.

    ``REVIEWED`` means the employee has seen and acknowledged the month's
    findings; refuse the marker when findings are unresolved and the
    acknowledgement flag is absent, so a direct API call cannot claim a
    review the UI would not allow (privacy semantics stay employee-owned)."""
    from app.policy.service import calculate_employee_period

    # EINV-15: REVIEWED asserts a review of the calculation against the calendar
    # state that applied. The calendar is a global input, so the coordination
    # lock comes FIRST — before lock_employee_facts, never the reverse — and
    # excludes a concurrent publication until this transaction commits. The
    # review that arrives during a publication therefore blocks before reading
    # anything, and then computes against the calendar state that publication
    # left behind (READ COMMITTED gives it a fresh per-statement snapshot).
    lock_calendar_coordination(session)
    lock_employee_facts(session, employee.id)
    result = calculate_employee_period(session, employee, month)
    if result.findings and not acknowledged:
        raise _validation_error(
            "VALIDATION_FAILED",
            "Acknowledge the unresolved findings before marking this month reviewed.",
        )
    period = _ensure_month_period(session, employee.id, month)
    period.status = PeriodStatus.REVIEWED
    session.flush()
    return month_summary(session, employee, month, calculation=result)


def split_interval(
    session: Session,
    employee: Employee,
    interval_id: uuid.UUID,
    payload: SplitIntervalIn,
) -> tuple[IntervalOut, IntervalOut]:
    """Split in one request/transaction so a failed second fact cannot leave
    a shortened factual interval behind (FE-SCREEN-002)."""
    lock_employee_facts(session, employee.id)
    current = _find_current_owned(session, employee.id, interval_id)
    _assert_not_superseded(session, current, payload.version)
    _assert_not_ocd_linked(current)
    # A travel pair is corrected only as a unit.
    _assert_not_travel_linked(session, current)
    split_at = _resolve("split_local", payload.split_local, payload.fold)
    if not current.start_at < split_at < current.end_at:
        raise _validation_error(
            "VALIDATION_INTERVAL_ORDER", "The split point must be inside the current interval."
        )
    first = correct_interval(
        session,
        employee,
        interval_id,
        IntervalPatch(version=payload.version, end_local=payload.split_local, fold=payload.fold),
    )
    second, _ = create_interval_instant(
        session,
        employee,
        start_at=split_at,
        end_at=current.end_at,
        work_type=current.work_type,
        comment=current.comment,
        linked_shift_assignment_id=current.linked_shift_assignment_id,
        linked_ocd_call_id=current.linked_ocd_call_id,
        overtime_authorization_id=current.overtime_authorization_id,
        source=current.source,
    )
    return first, second


def copy_previous_day(
    session: Session,
    employee: Employee,
    month: date,
    day: date,
    command_id: uuid.UUID | None = None,
) -> list[IntervalOut]:
    """Copy prior-start-day facts atomically into the selected local day."""
    lock_employee_facts(session, employee.id)
    if day.year != month.year or day.month != month.month:
        raise _validation_error(
            "VALIDATION_INVALID_DATE", "That day is not in the month you are working on."
        )
    source_day = day - timedelta(days=1)
    # Day bounds give an instant intersection, then selecting start date avoids
    # duplicating an interval which merely ended on the previous day.
    from app.worktime.queries import day_bounds

    source_start, source_end = day_bounds(source_day)
    source_intervals = [
        interval
        for interval in current_intervals(
            session,
            employee_id=employee.id,
            intersects_from=source_start,
            intersects_to=source_end,
        )
        if local_date(interval.start_at) == source_day
        # Business-trip work belongs to its travel
        # segment; copying it without the travel fact would create an
        # unlinked/orphan BUSINESS_TRIP_WORK row. Copy skips such intervals
        # instead of failing the whole day-copy.
        if interval.work_type is not WorkType.BUSINESS_TRIP_WORK
    ]
    created: list[IntervalOut] = []
    for interval in source_intervals:
        # Key off the immutable chain ROOT id: a retry after the source was
        # corrected must derive the same key (the head id would change).
        root_id = _chain_root(session, interval).id
        created_interval, _ = create_interval(
            session,
            employee,
            IntervalIn(
                start_local=interval.start_at.astimezone(BERLIN).replace(tzinfo=None)
                + timedelta(days=1),
                end_local=interval.end_at.astimezone(BERLIN).replace(tzinfo=None)
                + timedelta(days=1),
                work_type=interval.work_type,
                comment=interval.comment,
                client_request_id=_command_key(command_id, employee.id, str(root_id)),
            ),
        )
        created.append(created_interval)
    return created
