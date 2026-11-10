"""Day-status domain service (EINV-22, ACCESS-011, EINV-15).

Every mutating entry point takes ``lock_employee_facts`` as its FIRST statement
— the shared employee-fact model, exactly like ``create_interval``,
``correct_interval`` and ``set_working_time``. A day-status fact changes no
calendar data, so it deliberately does NOT join the calendar coordination
protocol: doing so would put an employee command inside the global section with
no invariant's benefit.

Frozen-history contract (EINV-07/ACCESS-011) — the *existing* rule, reused
verbatim rather than re-implemented: on a month covered by a non-withdrawn
submission package or a live disclosure grant, **destructive delete is refused**
(``disclosure/guards.py::assert_not_disclosed``), while **create and correct
remain permitted** because a correction preserves lineage and the frozen
snapshot stays authoritative for what was disclosed. No frozen column is ever
rewritten by a day-status mutation.
"""

import uuid
from datetime import date

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.absence.models import DayStatusEvent
from app.absence.schemas import DayStatusCreate, DayStatusOut, DayStatusPatch
from app.audit.models import record_audit
from app.db import constraint_name, lock_employee_facts
from app.disclosure.guards import assert_not_disclosed
from app.enums import DayStatusCategory, DayStatusDayPart, DayStatusPortion
from app.errors import ErrorCode, api_error
from app.identity.models import Employee
from app.worktime.models import Correction
from app.worktime.service import ACTOR_ROLE_EMPLOYEE, open_reviewed_month_range

#: A day has two supported halves. Exceeding that is refused at the WRITE path
#: (``_assert_coverage_within_day``), which is what makes the engine's clamp a
#: backstop for restored or imported data rather than the contract.
DAY_HALVES = 2
PORTION_HALVES = {DayStatusPortion.FULL: 2, DayStatusPortion.HALF: 1}

#: Half-day sickness is BLOCKED_POLICY (ABSENCE-008 / OPEN-08): refused with an
#: explicit reason rather than guessed, and never frozen into the schema.
BLOCKED_HALF_DAY_SICK = "ABSENCE-008"

#: The one named constraint a losing concurrent insert may be excused by. Any
#: other violation (root-slot convergence) stays loud.
IDEMPOTENCY_CONSTRAINT = "uq_day_status_event_client_request_id"


def _current_heads(session: Session, employee_id: uuid.UUID, day: date) -> list[DayStatusEvent]:
    """Every CURRENT (unsuperseded) day-status row for one employee and date."""
    child = aliased(DayStatusEvent)
    return list(
        session.scalars(
            select(DayStatusEvent).where(
                DayStatusEvent.employee_id == employee_id,
                DayStatusEvent.day == day,
                ~exists(select(child.id).where(child.supersedes_id == DayStatusEvent.id)),
            )
        ).all()
    )


def _assert_slot_free(
    session: Session,
    employee_id: uuid.UUID,
    day: date,
    category: DayStatusCategory,
    day_part: DayStatusDayPart,
    *,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """EINV-04 convergence guard: no OTHER current head occupies this slot.

    The partial root index alone cannot express this. Two chains with different
    root keys can converge: correcting ``(day, VACATION, MORNING)`` to
    ``AFTERNOON`` yields a head with ``supersedes_id IS NOT NULL`` (outside the
    partial index) while a root ``(day, VACATION, AFTERNOON)`` may still exist —
    leaving two current rows for one effective key, which would silently
    double-count a half-day fraction. Read under the employee lock, so it is
    race-free against the sibling create/correct commands.
    """
    occupant = next(
        (
            row
            for row in _current_heads(session, employee_id, day)
            if row.id != exclude_id and row.category is category and row.day_part is day_part
        ),
        None,
    )
    if occupant is not None:
        raise api_error(
            409,
            ErrorCode.CONFLICT_VERSION,
            "A current day-status fact already covers this day, category and day part; "
            "correct that entry instead of adding a second one.",
            details={
                "day": day.isoformat(),
                "category": category.value,
                "day_part": day_part.value,
            },
        )


def _assert_coverage_within_day(
    session: Session,
    employee_id: uuid.UUID,
    day: date,
    *,
    category: DayStatusCategory,
    portion: DayStatusPortion,
    replacing_id: uuid.UUID | None = None,
) -> None:
    """Refuse a day whose total status coverage would exceed one day.

    Applied to the change in memory: the day's halves across ALL categories,
    after the change, must not exceed ``DAY_HALVES``. This is a semantic
    constraint the database cannot express — the same class of guard as
    ``_assert_slot_free``.
    """
    total = PORTION_HALVES[portion]
    for row in _current_heads(session, employee_id, day):
        if row.id == replacing_id:
            continue
        total += PORTION_HALVES[row.portion]
    if total > DAY_HALVES:
        raise api_error(
            422,
            ErrorCode.VALIDATION_FAILED,
            "Day-status coverage for this date would exceed one day; adjust or remove "
            "the other entries first.",
            details={"day": day.isoformat(), "requested_category": category.value},
        )


def _reject_blocked_half_day_sick(category: DayStatusCategory, portion: DayStatusPortion) -> None:
    """Half-day sickness is a BLOCKED_POLICY rule, refused explicitly.

    The storage schema can represent a fraction and that is not a reason to
    execute one: ``ABSENCE-008``/``OPEN-08`` may resolve to a different
    treatment, so the policy decision must not be frozen into code or schema.
    """
    if category is DayStatusCategory.SICK and portion is DayStatusPortion.HALF:
        raise api_error(
            422,
            ErrorCode.POLICY_BLOCKED,
            "Half-day sickness cannot be recorded yet: how a half sick day should "
            "be counted is still being confirmed.",
            details={"rule_id": BLOCKED_HALF_DAY_SICK, "category": category.value},
        )


def _out(row: DayStatusEvent) -> DayStatusOut:
    return DayStatusOut(
        id=row.id,
        day=row.day,
        category=row.category,
        portion=row.portion,
        day_part=row.day_part,
        version=row.version,
        supersedes_id=row.supersedes_id,
        created_at=row.created_at,
    )


def list_day_status(session: Session, employee: Employee, month: date) -> list[DayStatusOut]:
    """Current day-status facts of one month, scoped to the session employee."""
    end = date(month.year + 1, 1, 1) if month.month == 12 else date(month.year, month.month + 1, 1)
    child = aliased(DayStatusEvent)
    rows = session.scalars(
        select(DayStatusEvent)
        .where(
            DayStatusEvent.employee_id == employee.id,
            DayStatusEvent.day >= month,
            DayStatusEvent.day < end,
            ~exists(select(child.id).where(child.supersedes_id == DayStatusEvent.id)),
        )
        .order_by(DayStatusEvent.day, DayStatusEvent.category, DayStatusEvent.day_part)
    ).all()
    return [_out(row) for row in rows]


def _replay_or_reject(
    session: Session, employee: Employee, client_request_id: uuid.UUID
) -> DayStatusEvent | None:
    """The day-status idempotency contract — the F1/F2 rule, reused.

    ``client_request_id`` is unique GLOBALLY while the employee-fact lock is PER
    EMPLOYEE, so this is a race class of its own:

    - same owner -> replay the existing row (a retry is a no-op, ``BE-CMD-001``);
    - other owner -> the privacy-preserving 422. Returning the winner's row would
      hand one employee another employee's date, category and portion — a
      disclosure (``INV-04``);
    - absent -> ``None`` (the caller proceeds to create).
    """
    existing = session.scalar(
        select(DayStatusEvent).where(DayStatusEvent.client_request_id == client_request_id)
    )
    if existing is None:
        return None
    if existing.employee_id == employee.id:
        return existing
    raise api_error(
        422,
        ErrorCode.VALIDATION_FAILED,
        "This change was already recorded — reload the page to see it.",
    )


def create_day_status(
    session: Session, employee: Employee, payload: DayStatusCreate
) -> DayStatusOut:
    """Record one day-status fact; idempotent on ``client_request_id``."""
    lock_employee_facts(session, employee.id)
    request_id = payload.client_request_id
    if request_id is not None:
        replay = _replay_or_reject(session, employee, request_id)
        if replay is not None:
            # BE-CMD-001: a retry replays. No new version, no reopen.
            return _out(replay)
    _reject_blocked_half_day_sick(payload.category, payload.portion)
    _assert_slot_free(session, employee.id, payload.day, payload.category, payload.day_part)
    _assert_coverage_within_day(
        session,
        employee.id,
        payload.day,
        category=payload.category,
        portion=payload.portion,
    )
    row = DayStatusEvent(
        employee_id=employee.id,
        day=payload.day,
        category=payload.category,
        portion=payload.portion,
        day_part=payload.day_part,
        version=1,
        client_request_id=request_id,
    )
    try:
        # SAVEPOINT: the global unique index on ``client_request_id`` can only be
        # violated by a DIFFERENT employee (two employees hold different locks),
        # so a violation must neither poison the request transaction nor discard
        # the advisory lock.
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as exc:
        if request_id is None or constraint_name(exc) != IDEMPOTENCY_CONSTRAINT:
            raise  # root-slot convergence, or a NULL id that cannot collide -> loud
        # In READ COMMITTED the losing INSERT blocked on the winner's uncommitted
        # unique-index entry until it committed, so the re-read is guaranteed to
        # see it; if the winner had rolled back there would be no exception at
        # all. No sleep, no retry loop.
        replay = _replay_or_reject(session, employee, request_id)
        if replay is None:
            raise  # winner vanished: do not guess
        return _out(replay)
    # EINV-15: the day's month reopens; a fact mutation never cascades into
    # later months through carry-forward.
    open_reviewed_month_range(session, employee.id, payload.day)
    record_audit(
        session,
        actor_principal=employee.idp_subject or str(employee.id),
        action="DAY_STATUS_CREATE",
        outcome="SUCCESS",
        object_type="DAY_STATUS_EVENT",
        object_id=row.id,
        employee_subject_id=employee.id,
    )
    return _out(row)


def _find_current_owned(
    session: Session, employee_id: uuid.UUID, row_id: uuid.UUID
) -> DayStatusEvent:
    """Ownership INSIDE the lookup predicate: a foreign id is
    indistinguishable from a nonexistent one (404 NOT_FOUND, INV-04)."""
    row = session.scalar(
        select(DayStatusEvent).where(
            DayStatusEvent.id == row_id,
            DayStatusEvent.employee_id == employee_id,
        )
    )
    if row is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Day-status entry not found.")
    child = aliased(DayStatusEvent)
    newer = session.scalar(select(child.id).where(child.supersedes_id == row.id))
    if newer is not None:
        raise api_error(
            409,
            ErrorCode.CONFLICT_VERSION,
            "This day-status entry was superseded; refresh and retry.",
        )
    return row


def correct_day_status(
    session: Session,
    employee: Employee,
    row_id: uuid.UUID,
    payload: DayStatusPatch,
) -> DayStatusOut:
    """Supersede one day-status fact, preserving lineage (BE-CMD-002).

    Permitted on a disclosed month, exactly like ``correct_interval``: the
    correction preserves lineage and the frozen snapshot remains authoritative
    for what was disclosed.
    """
    lock_employee_facts(session, employee.id)
    current = _find_current_owned(session, employee.id, row_id)
    if current.version != payload.version:
        raise api_error(
            409,
            ErrorCode.CONFLICT_VERSION,
            "Day-status entry changed since it was loaded; refresh and retry.",
        )
    # Presence-based: only fields the client actually sent change (EINV-12).
    sent = payload.model_fields_set
    category = payload.category if "category" in sent and payload.category else current.category
    portion = payload.portion if "portion" in sent and payload.portion else current.portion
    day_part = payload.day_part if "day_part" in sent and payload.day_part else current.day_part
    if portion is DayStatusPortion.FULL:
        # A FULL status always covers the whole day; an omitted day_part must not
        # be inherited from the superseded HALF row.
        day_part = DayStatusDayPart.ALL
    _reject_blocked_half_day_sick(category, portion)
    if portion is DayStatusPortion.HALF and day_part is DayStatusDayPart.ALL:
        raise api_error(
            422,
            ErrorCode.VALIDATION_FAILED,
            "A HALF day status must name MORNING or AFTERNOON.",
        )
    _assert_slot_free(session, employee.id, current.day, category, day_part, exclude_id=current.id)
    _assert_coverage_within_day(
        session,
        employee.id,
        current.day,
        category=category,
        portion=portion,
        replacing_id=current.id,
    )
    successor = DayStatusEvent(
        employee_id=employee.id,
        day=current.day,
        category=category,
        portion=portion,
        day_part=day_part,
        version=current.version + 1,
        supersedes_id=current.id,
    )
    session.add(successor)
    session.flush()
    # A correction reopens the OLD and the NEW day's months (two calls, so a
    # cross-month move reopens exactly those two and not the months between).
    open_reviewed_month_range(session, employee.id, current.day)
    open_reviewed_month_range(session, employee.id, successor.day)
    session.add(
        Correction(
            entity_type="DAY_STATUS_EVENT",
            entity_id=current.id,
            old_version=current.version,
            new_version=successor.version,
            actor_employee_id=employee.id,
            actor_role=ACTOR_ROLE_EMPLOYEE,
            reason=payload.reason,
        )
    )
    record_audit(
        session,
        actor_principal=employee.idp_subject or str(employee.id),
        action="DAY_STATUS_CORRECT",
        outcome="SUCCESS",
        object_type="DAY_STATUS_EVENT",
        object_id=successor.id,
        employee_subject_id=employee.id,
    )
    return _out(successor)


def delete_day_status(
    session: Session,
    employee: Employee,
    row_id: uuid.UUID,
    version: int,
    reason: str | None,
) -> None:
    """Remove the whole version chain of one day-status fact.

    REFUSED once the day's month is disclosed (ACCESS-011) — the existing
    ``assert_not_disclosed`` call, not an independent approximation.
    """
    lock_employee_facts(session, employee.id)
    current = _find_current_owned(session, employee.id, row_id)
    if current.version != version:
        raise api_error(
            409,
            ErrorCode.CONFLICT_VERSION,
            "Day-status entry changed since it was loaded; refresh and retry.",
        )
    assert_not_disclosed(session, employee.id, current.day.replace(day=1))
    open_reviewed_month_range(session, employee.id, current.day)
    # The WHOLE chain, head-first with an explicit flush per row: deleting only
    # the head would resurrect the predecessor as "current".
    chain = [current]
    cursor = current
    while cursor.supersedes_id is not None:
        ancestor = session.get(DayStatusEvent, cursor.supersedes_id)
        if ancestor is None:  # pragma: no cover - FK guarantees presence
            break
        chain.append(ancestor)
        cursor = ancestor
    for row in chain:
        session.delete(row)
        session.flush()
    session.add(
        Correction(
            entity_type="DAY_STATUS_EVENT",
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
        actor_principal=employee.idp_subject or str(employee.id),
        action="DAY_STATUS_DELETE",
        outcome="SUCCESS",
        object_type="DAY_STATUS_EVENT",
        object_id=current.id,
        employee_subject_id=employee.id,
    )
    session.flush()
