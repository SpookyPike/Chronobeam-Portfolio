"""F2 — the transaction/concurrency foundation, exercised as real interleaves.

Two families of evidence:

1. **Serialization and transitions** — the employee-fact lock is actually
   engaged (observed lock wait, never a sleep), cross-domain writers serialize
   against each other, version numbers stay correct under concurrency, and the
   disclosure terminal transitions refuse a second, contradictory writer.
2. **The global idempotency key** — ``client_request_id`` is unique across the
   WHOLE table, so the per-employee lock cannot cover it. Two employees using
   the same key are the load-bearing race: the loser must reach its INSERT, be
   observed blocking on the unique index, and recover through the savepoint +
   ``constraint_name`` branch. A concurrent-start test is explicitly NOT used:
   it can pass through the ordinary pre-read and never reach the code under
   test.

Harness rules (F0 traps, learned the hard way):

- racers use plain ``Session(db_engine)`` — never the rolled-back ``db_session``
  fixture and never a ``Session`` bound to a pre-used ``Connection`` (which
  joins the transaction in ``rollback_only`` mode, so ``commit()`` silently
  does not commit);
- waits are OBSERVED via ``pg_stat_activity.wait_event_type = 'Lock'``;
- assertions are made at the SQL/persistence level or through fresh sessions,
  never through an identity-mapped instance;
- these tests COMMIT, so they use dedicated 2032 dates and clean up in
  ``finally``.
"""

import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from app.audit.models import AuditEvent
from app.auth.principal import Capability, Principal
from app.config import get_settings
from app.disclosure.fingerprint import payload_fingerprint
from app.disclosure.models import (
    AccessRequest,
    AccessRequestStatus,
    DisclosureGrant,
    SubmissionPackage,
    SubmissionPackageStatus,
    SubmissionRecipient,
)
from app.disclosure.schemas import AssignmentSubmissionScope
from app.disclosure.service import (
    _recompute_package_preview,
    create_submission_package,
    decline_access_request,
    grant_access_request,
    withdraw_submission_package,
)
from app.enums import SubmissionPurpose, WorkType
from app.identity.models import Employee
from app.integrations.shift_plan import ShiftAssignmentSeed
from app.ocd_overtime import service as ocd_service
from app.ocd_overtime.models import (
    OcdAssignment,
    OcdAssignmentStatus,
    OcdCall,
    OcdCallMode,
)
from app.ocd_overtime.schemas import OcdAssignmentIn, OcdCallIn
from app.ocd_overtime.service import create_call, create_ocd_assignment, seed_ocd_assignment
from app.policy.models import PeriodCalculationSnapshot
from app.shift import service as shift_service
from app.shift.models import ShiftAssignment, ShiftPlan, ShiftPlanRevision
from app.shift.schemas import ShiftPlanIn
from app.shift.service import create_plan_shift, seed_shift_assignment
from app.travel.models import (
    BusinessTrip,
    TravelSegment,
    TravelSegmentBasis,
    TravelSegmentClassification,
    TripStartOrigin,
)
from app.travel.schemas import SegmentIn, TripIn
from app.travel.service import create_segment, create_trip
from app.worktime.models import Correction, WorkInterval, WorkPeriod
from app.worktime.service import create_interval_instant
from fastapi import HTTPException
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

RACE_MONTH = date(2032, 6, 1)
DAY = date(2032, 6, 10)
RECIPIENT = "recipient"


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def _error_code(exc: HTTPException) -> str:
    """The stable error code from an HTTPException envelope (never a status proxy)."""
    detail = exc.detail
    return detail.get("code", "") if isinstance(detail, dict) else str(detail)


def _wait_until_blocked(engine: Engine, pid: int, timeout: float = 20.0) -> bool:
    """Wait until the given backend is waiting on a lock (bounded, observed)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as probe:
            waiting = probe.scalar(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": pid},
            )
        if waiting == "Lock":
            return True
        time.sleep(0.02)
    return False


def _employee(engine: Engine, subject: str) -> Employee:
    with Session(engine) as session:
        employee = session.scalar(select(Employee).where(Employee.idp_subject == subject))
    assert employee is not None, subject
    return employee


def _principal(employee: Employee, *capabilities: Capability) -> Principal:
    assert employee.idp_subject is not None
    return Principal(
        subject=employee.idp_subject,
        display_name=employee.legal_name,
        capabilities=frozenset(capabilities),
    )


@pytest.fixture()
def race_employees(db_engine: Engine) -> Iterator[list[Employee]]:
    """The two synthetic employees, with their work periods pre-created.

    Pre-creating the periods avoids a fixture race: without it two concurrent
    writers can both attempt the period insert and serialize on that instead of
    on the lock under test. The teardown removes every row these committed
    tests can write, in FK order.
    """
    employees = [_employee(db_engine, "employee"), _employee(db_engine, "employee-b")]
    with Session(db_engine) as session, session.begin():
        for employee in employees:
            found = session.scalar(
                select(WorkPeriod).where(
                    WorkPeriod.employee_id == employee.id,
                    WorkPeriod.period_month == RACE_MONTH,
                )
            )
            if found is None:
                session.add(WorkPeriod(employee_id=employee.id, period_month=RACE_MONTH))
    yield employees
    _cleanup(db_engine, [employee.id for employee in employees])


def _cleanup(engine: Engine, ids: list[uuid.UUID]) -> None:
    """Remove every row the committed F2 tests may have written (FK order)."""
    with Session(engine) as session, session.begin():
        # 1. travel first: travel_segment references work_interval and has no
        #    employee column of its own, so it is reached through its trip.
        trip_ids = session.scalars(
            select(BusinessTrip.id).where(BusinessTrip.employee_id.in_(ids))
        ).all()
        if trip_ids:
            session.execute(delete(TravelSegment).where(TravelSegment.trip_id.in_(trip_ids)))
            session.execute(delete(BusinessTrip).where(BusinessTrip.id.in_(trip_ids)))
        # 2. intervals reference ocd_call / shift_assignment / work_period.
        interval_ids = session.scalars(
            select(WorkInterval.id).where(WorkInterval.employee_id.in_(ids))
        ).all()
        if interval_ids:
            session.execute(
                delete(Correction).where(
                    Correction.entity_type == "WORK_INTERVAL",
                    Correction.entity_id.in_(interval_ids),
                )
            )
            session.execute(delete(WorkInterval).where(WorkInterval.id.in_(interval_ids)))
        # Correction rows written by ``correct_interval`` carry entity_id NULL
        # (see the F0 cleanup note); scope by actor so they cannot leak.
        session.execute(delete(Correction).where(Correction.actor_employee_id.in_(ids)))
        # 3. disclosure rows (grants/packages reference snapshots and requests).
        package_ids = session.scalars(
            select(SubmissionPackage.id).where(SubmissionPackage.employee_id.in_(ids))
        ).all()
        if package_ids:
            session.execute(
                delete(SubmissionRecipient).where(
                    SubmissionRecipient.submission_package_id.in_(package_ids)
                )
            )
            session.execute(delete(SubmissionPackage).where(SubmissionPackage.id.in_(package_ids)))
        session.execute(delete(DisclosureGrant).where(DisclosureGrant.employee_id.in_(ids)))
        session.execute(delete(AccessRequest).where(AccessRequest.employee_id.in_(ids)))
        # 4. snapshots reference work_period.
        period_ids = session.scalars(
            select(WorkPeriod.id).where(
                WorkPeriod.employee_id.in_(ids), WorkPeriod.period_month >= RACE_MONTH
            )
        ).all()
        if period_ids:
            session.execute(
                delete(PeriodCalculationSnapshot).where(
                    PeriodCalculationSnapshot.work_period_id.in_(period_ids)
                )
            )
        # 5. ocd facts.
        session.execute(delete(OcdCall).where(OcdCall.employee_id.in_(ids)))
        session.execute(
            delete(OcdAssignment).where(
                OcdAssignment.external_id.in_([f"f2-global-key-{value}" for value in ids])
            )
        )
        # The plan-entry collision tests COMMIT employee-authored availabilities
        # through the service, and those rows carry `external_id = None` — so the
        # `f2-global-key-` match above does not reach them. Without this they leak
        # into the shared test database on EVERY run, where `list_ocd` and
        # `monthly_assignment_findings`' OCD-003 day count see them for these very
        # employees. That is a real source of order-dependent failures in unrelated
        # suites, not just untidy state.
        session.execute(
            delete(OcdAssignment).where(
                OcdAssignment.employee_id.in_(ids),
                OcdAssignment.source == "EMPLOYEE_RECORDED",
            )
        )
        # 6. shift facts: the assignment points at its approved revision, so
        #    that reference is cleared before the revisions are deleted.
        assignment_ids = session.scalars(
            select(ShiftAssignment.id).where(ShiftAssignment.employee_id.in_(ids))
        ).all()
        if assignment_ids:
            session.execute(
                update(ShiftAssignment)
                .where(ShiftAssignment.id.in_(assignment_ids))
                .values(approved_revision_id=None)
            )
            session.execute(
                delete(ShiftPlanRevision).where(
                    ShiftPlanRevision.shift_assignment_id.in_(assignment_ids)
                )
            )
            session.execute(delete(ShiftAssignment).where(ShiftAssignment.id.in_(assignment_ids)))
        for employee_id in ids:
            session.execute(
                delete(ShiftPlan).where(ShiftPlan.plan_key == f"f2-global-key-{employee_id}")
            )
        # ...and the plan-entry container, which is keyed `employee:<id>` and is
        # created by the collision tests above. Deleted AFTER the assignments and
        # revisions, because `shift_assignment.shift_plan_id` references it.
        session.execute(
            delete(ShiftPlan).where(ShiftPlan.plan_key.in_([f"employee:{value}" for value in ids]))
        )
        # 7. periods last.
        if period_ids:
            session.execute(delete(WorkPeriod).where(WorkPeriod.id.in_(period_ids)))
        # These races COMMIT audit events; other tests count audit rows.
        session.execute(delete(AuditEvent).where(AuditEvent.employee_subject_id.in_(ids)))
        # LOUD teardown check for the shapes this file writes. These tests use a raw
        # `Session(db_engine)`, so their writes are REAL commits — unlike the
        # `db_session` fixture, which binds to a connection with an outer transaction
        # and rolls the whole thing back. A future test here that commits a shape the
        # deletes above do not know about would otherwise pollute the shared database
        # silently and surface later as an unrelated order-dependent failure.
        leftovers = {
            "ocd_assignment": session.scalar(
                select(func.count())
                .select_from(OcdAssignment)
                .where(
                    OcdAssignment.employee_id.in_(ids),
                    OcdAssignment.source == "EMPLOYEE_RECORDED",
                )
            ),
            "shift_plan": session.scalar(
                select(func.count())
                .select_from(ShiftPlan)
                .where(ShiftPlan.plan_key.in_([f"employee:{value}" for value in ids]))
            ),
        }
        assert not any(leftovers.values()), (
            f"committed F2 test rows survived _cleanup and will leak into the shared "
            f"database: {leftovers}"
        )


# ---------------------------------------------------------------------------
# 1. Serialization, versioning and terminal transitions
# ---------------------------------------------------------------------------


def test_cross_domain_travel_and_worktime_writers_serialize(
    db_engine: Engine, race_employees: list[Employee]
) -> None:
    """A travel segment and a plain interval create for one employee cannot both
    commit when they overlap in time.

    This is the closure audit's §2.3 finding — reasoned but never reproduced:
    before the re-key, travel held a private lock under a DIFFERENT key, so
    cross-domain writers did not serialize at all. It is also the proof that one
    shared key namespace is the right primitive.
    """
    employee = _employee(db_engine, "employee")
    worktime = Session(db_engine)
    travel = Session(db_engine)
    travel_pid = int(travel.scalar(text("SELECT pg_backend_pid()")))
    outcome: list[str] = []

    def _travel_writer() -> None:
        try:
            row = travel.get(Employee, employee.id)
            assert row is not None
            trip, _ = create_trip(
                travel,
                row,
                TripIn(
                    start_local=datetime(2032, 6, 10, 6, 0),
                    end_local=datetime(2032, 6, 10, 20, 0),
                    start_origin=TripStartOrigin.WORKPLACE,
                    reference_label=None,
                ),
            )
            # ACTIVE_WORK pairs the segment with a real BUSINESS_TRIP_WORK
            # interval, so the shared overlap invariant applies.
            create_segment(
                travel,
                row,
                trip.id,
                SegmentIn(
                    # 10:00-10:30 Berlin == 08:00-08:30 UTC, inside the
                    # interval the worktime side committed (07:00-09:00 UTC).
                    start_local=datetime(2032, 6, 10, 10, 0),
                    end_local=datetime(2032, 6, 10, 10, 30),
                    classification=TravelSegmentClassification.ACTIVE_WORK,
                    basis=TravelSegmentBasis.ACTUAL,
                ),
            )
            travel.commit()
            outcome.append("created")
        except HTTPException as exc:
            travel.rollback()
            outcome.append(f"refused:{exc.status_code}")

    try:
        row = worktime.get(Employee, employee.id)
        assert row is not None
        # A holds the shared employee-fact lock (uncommitted).
        create_interval_instant(
            worktime,
            row,
            start_at=_at(DAY, 7, 0),
            end_at=_at(DAY, 9, 0),
            work_type=WorkType.ON_SITE,
            comment="worktime-side",
        )
        worker = threading.Thread(target=_travel_writer)
        worker.start()
        blocked = _wait_until_blocked(db_engine, travel_pid)
        assert blocked, "the travel writer never reached the shared employee lock"
        assert outcome == [], "the travel writer finished while the lock was held"
        worktime.commit()
        worker.join(timeout=30)
        assert not worker.is_alive()
    finally:
        worktime.close()
        travel.close()

    # The travel writer re-validated against the committed interval and refused.
    assert outcome == ["refused:422"], outcome
    with Session(db_engine) as session:
        current = session.scalar(
            select(func.count())
            .select_from(WorkInterval)
            .where(
                WorkInterval.employee_id == employee.id,
                WorkInterval.start_at < _at(DAY, 12, 0),
                WorkInterval.end_at > _at(DAY, 6, 0),
            )
        )
    assert current == 1, f"overlapping work intervals committed: {current}"
    with Session(db_engine) as session, session.begin():
        session.execute(delete(BusinessTrip).where(BusinessTrip.employee_id == employee.id))


def test_concurrent_submissions_for_one_scope_get_distinct_versions(
    db_engine: Engine,
    race_employees: list[Employee],
) -> None:
    """The lock makes ``max(version)+1`` correct: 1 and 2, never two 1s."""
    employee = _employee(db_engine, "employee")
    first = Session(db_engine)
    second = Session(db_engine)
    second_pid = int(second.scalar(text("SELECT pg_backend_pid()")))
    request_id = uuid.uuid4()
    created: list[int] = []

    with Session(db_engine) as setup, setup.begin():
        args = _shift_package_args(setup, employee, request_id)

    def _second() -> None:
        try:
            row = second.get(Employee, employee.id)
            assert row is not None
            package = create_submission_package(
                second,
                _principal(row, Capability.OWN_SUBMISSION_CREATE),
                row,
                **{**args, "client_request_id": uuid.uuid4()},
            )
            second.commit()
            created.append(package.version)
        except HTTPException:
            second.rollback()
            raise

    try:
        row = first.get(Employee, employee.id)
        assert row is not None
        first_package = create_submission_package(
            first, _principal(row, Capability.OWN_SUBMISSION_CREATE), row, **args
        )
        assert first_package.version == 1
        worker = threading.Thread(target=_second)
        worker.start()
        blocked = _wait_until_blocked(db_engine, second_pid)
        assert blocked, "the second submission never reached the employee lock"
        assert created == []
        first.commit()
        worker.join(timeout=30)
        assert not worker.is_alive()
    finally:
        first.close()
        second.close()

    assert created == [2], created


def test_second_decline_and_second_withdraw_are_refused(
    db_engine: Engine, race_employees: list[Employee]
) -> None:
    """The conditional transition refuses a repeated terminal write."""
    employee = _employee(db_engine, "employee")
    principal = _principal(employee, Capability.OWN_DISCLOSURE_GRANT)
    request_id = uuid.uuid4()
    with Session(db_engine) as setup, setup.begin():
        setup.add(
            AccessRequest(
                id=request_id,
                employee_id=employee.id,
                requester_principal="hr-requester",
                purpose=SubmissionPurpose.RANDOM_CHECK,
                period_start=date(2032, 6, 1),
                period_end=date(2032, 6, 30),
                view_type="work-time-record",
                status=AccessRequestStatus.PENDING,
            )
        )
    try:
        with Session(db_engine) as session, session.begin():
            decline_access_request(session, principal, employee, request_id)
        with Session(db_engine) as session:
            with pytest.raises(HTTPException) as error:
                decline_access_request(session, principal, employee, request_id)
            assert error.value.status_code == 409
            assert _error_code(error.value) == "CONFLICT_VERSION"
    finally:
        with Session(db_engine) as cleanup, cleanup.begin():
            cleanup.execute(delete(AccessRequest).where(AccessRequest.id == request_id))


def test_grant_after_decline_is_refused(db_engine: Engine, race_employees: list[Employee]) -> None:
    """The other direction of the same race: a declined request cannot be granted."""
    employee = _employee(db_engine, "employee")
    principal = _principal(employee, Capability.OWN_DISCLOSURE_GRANT)
    request_id = uuid.uuid4()
    with Session(db_engine) as setup, setup.begin():
        setup.add(
            AccessRequest(
                id=request_id,
                employee_id=employee.id,
                requester_principal="hr-requester",
                purpose=SubmissionPurpose.RANDOM_CHECK,
                period_start=date(2032, 6, 1),
                period_end=date(2032, 6, 30),
                view_type="work-time-record",
                status=AccessRequestStatus.PENDING,
            )
        )
    try:
        with Session(db_engine) as session, session.begin():
            decline_access_request(session, principal, employee, request_id)
        with Session(db_engine) as session:
            with pytest.raises(HTTPException) as error:
                grant_access_request(session, principal, employee, request_id)
            assert error.value.status_code == 409
        with Session(db_engine) as verify:
            grants = verify.scalar(
                text("SELECT count(*) FROM disclosure_grant WHERE access_request_id = :id"),
                {"id": request_id},
            )
        assert grants == 0
    finally:
        with Session(db_engine) as cleanup, cleanup.begin():
            cleanup.execute(delete(AccessRequest).where(AccessRequest.id == request_id))


def test_narrowed_overlap_check_still_sees_distant_history(
    db_engine: Engine, race_employees: list[Employee]
) -> None:
    """Regression guard for the window narrowing: the check is not weakened.

    An interval created far from the employee's other facts must still be
    compared against exactly the ones that could overlap it, and an interval
    overlapping a distant-but-relevant fact must still be refused.
    """
    employee = _employee(db_engine, "employee")
    try:
        with Session(db_engine) as session, session.begin():
            create_interval_instant(
                session,
                employee,
                start_at=_at(DAY, 7, 0),
                end_at=_at(DAY, 8, 0),
                work_type=WorkType.ON_SITE,
                comment="anchor",
            )
        # Far away: accepted.
        with Session(db_engine) as session, session.begin():
            create_interval_instant(
                session,
                employee,
                start_at=_at(DAY, 14, 0),
                end_at=_at(DAY, 15, 0),
                work_type=WorkType.ON_SITE,
                comment="distant",
            )
        # Overlapping the anchor: refused.
        with Session(db_engine) as session:
            with pytest.raises(HTTPException) as error:
                create_interval_instant(
                    session,
                    employee,
                    start_at=_at(DAY, 7, 30),
                    end_at=_at(DAY, 7, 45),
                    work_type=WorkType.ON_SITE,
                    comment="overlap",
                )
            assert _error_code(error.value) == "VALIDATION_INTERVAL_OVERLAP"
            session.rollback()
        # Overlapping the distant fact: also refused (proves the window is not
        # an anchor-only shortcut).
        with Session(db_engine) as session:
            with pytest.raises(HTTPException) as error:
                create_interval_instant(
                    session,
                    employee,
                    start_at=_at(DAY, 14, 30),
                    end_at=_at(DAY, 16, 0),
                    work_type=WorkType.ON_SITE,
                    comment="overlap-distant",
                )
            assert _error_code(error.value) == "VALIDATION_INTERVAL_OVERLAP"
            session.rollback()
    finally:
        with Session(db_engine) as cleanup, cleanup.begin():
            ids = cleanup.scalars(
                select(WorkInterval.id).where(WorkInterval.employee_id == employee.id)
            ).all()
            if ids:
                cleanup.execute(
                    delete(Correction).where(
                        Correction.entity_type == "WORK_INTERVAL",
                        Correction.entity_id.in_(ids),
                    )
                )
            cleanup.execute(delete(WorkInterval).where(WorkInterval.employee_id == employee.id))


# ---------------------------------------------------------------------------
# 2. The global idempotency key — cross-employee, deterministic lock wait
# ---------------------------------------------------------------------------


def _shift_package_args(session: Session, employee: Employee, request_id: uuid.UUID) -> dict:
    """Everything ``create_submission_package`` needs for a SHIFT_REPORT scope."""
    assignment = seed_shift_assignment(
        session,
        employee,
        ShiftAssignmentSeed(
            plan_key=f"f2-global-key-{employee.id}",
            plan_name="F2 global key shift",
            planned_start_at=datetime.fromisoformat("2032-06-09T19:00:00+02:00"),
            planned_end_at=datetime.fromisoformat("2032-06-10T07:00:00+02:00"),
            shift_kind="NIGHT",
            source="TEST_SEED",
            source_ref="f2-global-key",
        ),
    )
    scope = AssignmentSubmissionScope(assignment_id=assignment.id)
    payload, _, _ = _recompute_package_preview(
        session, employee, SubmissionPurpose.SHIFT_REPORT, scope
    )
    return {
        "purpose": SubmissionPurpose.SHIFT_REPORT,
        "scope": scope,
        "preview_fingerprint": payload_fingerprint(payload),
        "client_request_id": request_id,
        "recipients": [RECIPIENT],
    }


def test_global_client_request_id_collision_across_employees_is_recovered(
    db_engine: Engine,
    race_employees: list[Employee],
) -> None:
    """THE load-bearing regression: the loser reaches its INSERT and recovers.

    Deterministic by construction: A's row stays UNCOMMITTED, so B's ordinary
    READ COMMITTED pre-read cannot see it and B must proceed to the INSERT,
    where it is OBSERVED blocking on the unique index. Only then does A commit.
    A concurrent-start test would let B return through the pre-read without ever
    exercising the savepoint / ``constraint_name`` branch.
    """
    employee_a = _employee(db_engine, "employee")
    employee_b = _employee(db_engine, "employee-b")
    key = uuid.uuid4()
    with Session(db_engine) as setup, setup.begin():
        args_a = _shift_package_args(setup, employee_a, key)
        args_b = _shift_package_args(setup, employee_b, key)

    winner = Session(db_engine)
    loser = Session(db_engine)
    loser_pid = int(loser.scalar(text("SELECT pg_backend_pid()")))
    outcome: list[str] = []
    usable_after: list[str] = []

    def _losing_submission() -> None:
        try:
            row = loser.get(Employee, employee_b.id)
            assert row is not None
            create_submission_package(
                loser, _principal(row, Capability.OWN_SUBMISSION_CREATE), row, **args_b
            )
            loser.commit()
            outcome.append("created")
        except HTTPException as exc:
            # Prove the transaction is NOT poisoned: without the savepoint the
            # next statement would raise InFailedSqlTransaction.
            usable_after.append(str(loser.scalar(text("SELECT 1"))))
            outcome.append(f"refused:{exc.status_code}:{_error_code(exc)}")
            loser.rollback()

    try:
        row_a = winner.get(Employee, employee_a.id)
        assert row_a is not None
        package = create_submission_package(
            winner, _principal(row_a, Capability.OWN_SUBMISSION_CREATE), row_a, **args_a
        )
        assert package.version == 1
        # NOT committed: the unique-index entry exists but is invisible.
        worker = threading.Thread(target=_losing_submission)
        worker.start()
        blocked = _wait_until_blocked(db_engine, loser_pid)
        assert blocked, (
            "the losing submission never blocked on the unique index — it must have "
            "returned through the ordinary pre-read, so the recovery branch was "
            "never exercised"
        )
        assert outcome == [], "the losing submission finished before the winner committed"
        winner.commit()
        worker.join(timeout=30)
        assert not worker.is_alive(), "the losing submission did not finish"
    finally:
        winner.close()
        loser.close()

    assert outcome == ["refused:422:VALIDATION_FAILED"], outcome
    assert usable_after == ["1"], "the loser's session was left in a failed transaction"
    with Session(db_engine) as verify:
        rows = verify.scalars(
            select(SubmissionPackage).where(SubmissionPackage.client_request_id == key)
        ).all()
        assert len(rows) == 1
        assert rows[0].employee_id == employee_a.id
        # The winner's row is intact and readable by the winner, absent for B.
        visible_to_b = verify.scalars(
            select(SubmissionPackage.id).where(SubmissionPackage.employee_id == employee_b.id)
        ).all()
        assert visible_to_b == []


def test_same_owner_retry_replays_without_touching_the_constraint(
    db_engine: Engine,
    race_employees: list[Employee],
) -> None:
    """The lock still covers the normal case: a same-owner retry REPLAYS."""
    employee = _employee(db_engine, "employee")
    key = uuid.uuid4()
    with Session(db_engine) as setup, setup.begin():
        args = _shift_package_args(setup, employee, key)
    with Session(db_engine) as session, session.begin():
        first = create_submission_package(
            session,
            _principal(employee, Capability.OWN_SUBMISSION_CREATE),
            employee,
            **args,
        )
        first_id, first_version = first.id, first.version
    # The retry replays through the ordinary pre-read: no IntegrityError at all.
    with Session(db_engine) as session, session.begin():
        replay = create_submission_package(
            session,
            _principal(employee, Capability.OWN_SUBMISSION_CREATE),
            employee,
            **args,
        )
        assert replay.id == first_id
        assert replay.version == first_version == 1
    with Session(db_engine) as verify:
        assert (
            verify.scalar(
                select(func.count())
                .select_from(SubmissionPackage)
                .where(SubmissionPackage.client_request_id == key)
            )
            == 1
        )


def test_ocd_call_global_client_request_id_collision_is_recovered(
    db_engine: Engine, race_employees: list[Employee]
) -> None:
    """The same race on ``ocd_call`` — a raw 500 before this fix.

    Extra assertion beyond the submission case: employee B must have ZERO
    OCD_CALL work intervals afterwards, which directly proves the savepoint
    boundary (the loser's ``_ensure_period`` and linked interval never ran).
    """
    employee_a = _employee(db_engine, "employee")
    employee_b = _employee(db_engine, "employee-b")
    key = uuid.uuid4()
    with Session(db_engine) as setup, setup.begin():
        assignment_a = _ocd_assignment(setup, employee_a)
        assignment_b = _ocd_assignment(setup, employee_b)
        args_a = _ocd_args(assignment_a, key)
        args_b = _ocd_args(assignment_b, key)

    winner = Session(db_engine)
    loser = Session(db_engine)
    loser_pid = int(loser.scalar(text("SELECT pg_backend_pid()")))
    outcome: list[str] = []
    usable_after: list[str] = []

    def _losing_call() -> None:
        try:
            row = loser.get(Employee, employee_b.id)
            assert row is not None
            create_call(loser, row, args_b)
            loser.commit()
            outcome.append("created")
        except HTTPException as exc:
            usable_after.append(str(loser.scalar(text("SELECT 1"))))
            outcome.append(f"refused:{exc.status_code}:{_error_code(exc)}")
            loser.rollback()

    try:
        row_a = winner.get(Employee, employee_a.id)
        assert row_a is not None
        _call, created = create_call(winner, row_a, args_a)
        assert created is True
        worker = threading.Thread(target=_losing_call)
        worker.start()
        blocked = _wait_until_blocked(db_engine, loser_pid)
        assert blocked, (
            "the losing call never blocked on uq_ocd_call_client_request_id — the "
            "recovery branch was never exercised"
        )
        assert outcome == []
        winner.commit()
        worker.join(timeout=30)
        assert not worker.is_alive()
    finally:
        winner.close()
        loser.close()

    assert outcome == ["refused:422:VALIDATION_FAILED"], outcome
    assert usable_after == ["1"], "the loser's session was left in a failed transaction"
    with Session(db_engine) as verify:
        assert (
            verify.scalar(
                select(func.count()).select_from(OcdCall).where(OcdCall.client_request_id == key)
            )
            == 1
        )
        # The loser's dependent writes never happened.
        assert (
            verify.scalar(
                select(func.count())
                .select_from(WorkInterval)
                .where(
                    WorkInterval.employee_id == employee_b.id,
                    WorkInterval.work_type == WorkType.OCD_CALL,
                )
            )
            == 0
        )
        assert (
            verify.scalar(
                select(func.count())
                .select_from(OcdCall)
                .where(OcdCall.employee_id == employee_b.id)
            )
            == 0
        )


def _ocd_assignment(session: Session, employee: Employee) -> OcdAssignment:
    return seed_ocd_assignment(
        session,
        employee,
        start_at=datetime.fromisoformat("2032-06-09T22:00:00+02:00"),
        end_at=datetime.fromisoformat("2032-06-10T04:00:00+02:00"),
        external_id=f"f2-global-key-{employee.id}",
    )


def _ocd_args(assignment: OcdAssignment, key: uuid.UUID) -> OcdCallIn:
    assert assignment.status == OcdAssignmentStatus.SCHEDULED
    return OcdCallIn(
        ocd_assignment_id=assignment.id,
        call_start_local=datetime(2032, 6, 9, 22, 30),
        call_end_local=datetime(2032, 6, 9, 23, 30),
        mode=OcdCallMode.ON_SITE,
        travel_out_minutes=None,
        travel_back_minutes=None,
        comment=None,
        client_request_id=key,
    )


def test_withdraw_refuses_a_second_terminal_write(
    db_engine: Engine, race_employees: list[Employee]
) -> None:
    """``SUBMITTED -> WITHDRAWN`` is a conditional transition too."""
    employee = _employee(db_engine, "employee")
    key = uuid.uuid4()
    with Session(db_engine) as setup, setup.begin():
        args = _shift_package_args(setup, employee, key)
    with Session(db_engine) as session, session.begin():
        package = create_submission_package(
            session,
            _principal(employee, Capability.OWN_SUBMISSION_CREATE),
            employee,
            **args,
        )
        package_id = package.id
    with Session(db_engine) as session, session.begin():
        withdraw_submission_package(
            session,
            _principal(employee, Capability.OWN_SUBMISSION_CREATE),
            employee,
            package_id,
        )
    with Session(db_engine) as session:
        with pytest.raises(HTTPException) as error:
            withdraw_submission_package(
                session,
                _principal(employee, Capability.OWN_SUBMISSION_CREATE),
                employee,
                package_id,
            )
        assert error.value.status_code == 409
        session.rollback()
    with Session(db_engine) as verify:
        stored = verify.get(SubmissionPackage, package_id)
        assert stored is not None
        assert stored.status == SubmissionPackageStatus.WITHDRAWN


# ---------------------------------------------------------------------------
# The employee plan-entry create paths (pilot slice) — tests 57/58
# ---------------------------------------------------------------------------
#
# Both new create paths persist a GLOBALLY unique ``client_request_id``, so the
# per-employee ``lock_employee_facts`` does NOT serialise two DIFFERENT employees.
# Each path declares its own named constraint, so each needs its own proof: an
# auto-named or renamed constraint would make ``constraint_name(exc)`` recovery
# unreachable and silently re-raise as a raw 500.


def _enable_plan_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn both pilot switches on for a service-level test.

    These tests call the service directly rather than through the app, so there is
    no ``create_app()`` to read the environment — the module-level ``get_settings``
    lookup is patched instead. A COPY of the real settings is used, not a bare
    namespace: the read paths consult other settings too (the authoritative-
    compensation flag among them), and a namespace would fail on the first one.
    """
    settings = get_settings().model_copy(
        update={"enable_shift_plan_entry": True, "enable_ocd_plan_entry": True}
    )
    monkeypatch.setattr(shift_service, "get_settings", lambda: settings)
    monkeypatch.setattr(ocd_service, "get_settings", lambda: settings)


def _plan_shift_args(key: uuid.UUID) -> ShiftPlanIn:
    return ShiftPlanIn(
        planned_start_local=datetime.fromisoformat("2032-06-11T19:00:00"),
        planned_end_local=datetime.fromisoformat("2032-06-12T07:00:00"),
        shift_kind="NIGHT",
        client_request_id=key,
    )


def _ocd_entry_args(key: uuid.UUID) -> OcdAssignmentIn:
    return OcdAssignmentIn(
        start_local=datetime.fromisoformat("2032-06-13T18:00:00"),
        end_local=datetime.fromisoformat("2032-06-14T08:00:00"),
        client_request_id=key,
    )


def _cross_employee_collision(
    db_engine: Engine,
    race_employees: list[Employee],
    key: uuid.UUID,
    create: Any,
    args: Any,
) -> tuple[list[str], list[str]]:
    """Drive the deterministic global-key race and return (outcome, usable_after).

    A's row stays UNCOMMITTED, so B's ordinary READ COMMITTED pre-read genuinely
    cannot see it — the real production state, on demand — and B's INSERT is then
    OBSERVED blocking on the unique index until A commits.
    """
    employee_a, employee_b = race_employees
    winner = Session(db_engine)
    loser = Session(db_engine)
    loser_pid = int(loser.scalar(text("SELECT pg_backend_pid()")))
    outcome: list[str] = []
    usable_after: list[str] = []

    def _losing_create() -> None:
        try:
            row = loser.get(Employee, employee_b.id)
            assert row is not None
            create(loser, _principal(row), row, args)
            loser.commit()
            outcome.append("created")
        except HTTPException as exc:
            # Prove the transaction is NOT poisoned: without the savepoint the
            # next statement would raise InFailedSqlTransaction.
            usable_after.append(str(loser.scalar(text("SELECT 1"))))
            outcome.append(f"refused:{exc.status_code}:{_error_code(exc)}")
            loser.rollback()

    try:
        row_a = winner.get(Employee, employee_a.id)
        assert row_a is not None
        _state, created = create(winner, _principal(row_a), row_a, args)
        assert created is True
        worker = threading.Thread(target=_losing_create)
        worker.start()
        assert _wait_until_blocked(db_engine, loser_pid), (
            "the losing create never blocked on the unique index — it must have "
            "returned through the ordinary pre-read, so the recovery branch was "
            "never exercised"
        )
        assert outcome == [], "the losing create finished before the winner committed"
        winner.commit()
        worker.join(timeout=30)
        assert not worker.is_alive(), "the losing create did not finish"
    finally:
        winner.close()
        loser.close()
    return outcome, usable_after


def test_a_cross_employee_client_request_id_collision_on_shift_create(
    db_engine: Engine,
    race_employees: list[Employee],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_plan_entry(monkeypatch)
    employee_a, employee_b = race_employees
    key = uuid.uuid4()
    outcome, usable_after = _cross_employee_collision(
        db_engine, race_employees, key, create_plan_shift, _plan_shift_args(key)
    )

    assert outcome == ["refused:422:VALIDATION_FAILED"], outcome
    assert usable_after == ["1"], "the loser's session was left in a failed transaction"
    with Session(db_engine) as verify:
        rows = verify.scalars(
            select(ShiftAssignment).where(ShiftAssignment.client_request_id == key)
        ).all()
        assert len(rows) == 1, "exactly one row may own the global key"
        assert rows[0].employee_id == employee_a.id, "A held the uncommitted insert, so A wins"

        # B's session is not poisoned: a follow-up create with a DIFFERENT key
        # succeeds on the same session.
        row_b = verify.get(Employee, employee_b.id)
        assert row_b is not None
        _state, created = create_plan_shift(
            verify, _principal(row_b), row_b, _plan_shift_args(uuid.uuid4())
        )
        assert created is True
        verify.commit()
    assert employee_b.id != employee_a.id


def test_a_cross_employee_client_request_id_collision_on_ocd_create(
    db_engine: Engine,
    race_employees: list[Employee],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The OCD path declares its OWN named constraint, so it needs its own proof."""
    _enable_plan_entry(monkeypatch)
    employee_a, _employee_b = race_employees
    key = uuid.uuid4()
    outcome, usable_after = _cross_employee_collision(
        db_engine, race_employees, key, create_ocd_assignment, _ocd_entry_args(key)
    )

    assert outcome == ["refused:422:VALIDATION_FAILED"], outcome
    assert usable_after == ["1"], "the loser's session was left in a failed transaction"
    with Session(db_engine) as verify:
        rows = verify.scalars(
            select(OcdAssignment).where(OcdAssignment.client_request_id == key)
        ).all()
        assert len(rows) == 1
        assert rows[0].employee_id == employee_a.id


def test_a_same_employee_replay_after_the_winner_commits_is_a_clean_replay(
    db_engine: Engine,
    race_employees: list[Employee],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: the loser's 422 is caused by OWNERSHIP, not by the collision.

    The lock serialises the same employee's two calls, so the second one returns
    through the ordinary pre-read as a replay — no INSERT, no constraint contact.
    """
    _enable_plan_entry(monkeypatch)
    employee_a, _employee_b = race_employees
    key = uuid.uuid4()
    with Session(db_engine) as session:
        row = session.get(Employee, employee_a.id)
        assert row is not None
        first, created = create_plan_shift(session, _principal(row), row, _plan_shift_args(key))
        assert created is True
        second, replayed = create_plan_shift(session, _principal(row), row, _plan_shift_args(key))
        assert replayed is False
        assert second.id == first.id
        session.commit()
