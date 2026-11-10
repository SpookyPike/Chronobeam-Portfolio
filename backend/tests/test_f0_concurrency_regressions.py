"""F0 regression reproductions — concurrency and version-chain foundation.

Baseline freeze for the confirmed integrity defects recorded by the targeted
closure audit (2026-09-17, §2.2/§5.1): the ledger's integrity invariants were
enforced by application code only, so two concurrent requests from one employee
could commit states the domain model says cannot exist.

The reproductions are now **lock-aware**. Before the shared employee-fact lock
existed, "both transactions read before either commits" was the natural
interleave; with a blocking serialization primitive that interleave is
unconstructible — the second transaction blocks at the lock and the first's
``commit()`` is never reached. The shape used here is therefore:

    A holds the lock and mutates (uncommitted)
      -> B runs the real service path in a worker thread
      -> the test waits for the OBSERVED lock wait on B's backend
      -> A commits
      -> B proceeds, re-reads the now-committed state and must refuse

That asserts the invariant without depending on thread timing, and it proves
the lock is actually engaged rather than the test passing by accident.

``EINV-18`` needs no restructuring: it is a persistence-level test and the
constraint it exercises is exactly the fix.
"""

import threading
import time
import uuid
from datetime import UTC, date, datetime

import pytest
from app.audit.models import AuditEvent
from app.auth.principal import Capability, Principal
from app.disclosure.models import (
    AccessRequest,
    AccessRequestStatus,
    DisclosureGrant,
    SubmissionPackage,
)
from app.disclosure.service import decline_access_request
from app.enums import SubmissionPurpose, WorkType
from app.identity.models import Employee
from app.worktime.models import Correction, WorkInterval, WorkPeriod
from app.worktime.schemas import IntervalPatch
from app.worktime.service import correct_interval, create_interval_instant
from fastapi import HTTPException
from sqlalchemy import delete, exists, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

# Dedicated dates: these tests COMMIT, so they must not collide with the
# seeded dev fixtures or with the other committed-race tests.
RACE_MONTH = date(2032, 1, 1)
DAY = date(2032, 1, 5)
NEXT_MONTH = date(2032, 2, 1)


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def _wall(day: date, hour: int, minute: int = 0) -> datetime:
    """Naive Berlin wall time (what the PATCH schemas take)."""
    return datetime(day.year, day.month, day.day, hour, minute)


def _current_interval_filter():
    """The ledger's own current-version predicate (no successor exists).

    The F0 baseline version of this helper compared a row with ITSELF, so the
    subquery was empty and the filter matched every row — the same defect class
    the closure audit found in ``disclosure/service.py``. This is the real
    aliased anti-join.
    """
    child = aliased(WorkInterval)
    return ~exists(select(child.id).where(child.supersedes_id == WorkInterval.id))


@pytest.fixture()
def committed_employee(db_engine: Engine) -> Employee:
    """The seeded synthetic employee, resolved on a committed connection."""
    with Session(db_engine) as session:
        employee = session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    return employee


@pytest.fixture()
def race_period(db_engine: Engine, committed_employee: Employee):
    """Pre-create (and commit) the work periods the racers will target.

    Without this, two concurrent writers can both attempt the period insert and
    serialize on it — an artefact of the fixture rather than the defect under
    test.  The closure harness pre-created the same rows for the same reason.
    """
    with Session(db_engine) as session, session.begin():
        for month in (RACE_MONTH, NEXT_MONTH):
            exists_row = session.scalar(
                select(WorkPeriod).where(
                    WorkPeriod.employee_id == committed_employee.id,
                    WorkPeriod.period_month == month,
                )
            )
            if exists_row is None:
                session.add(WorkPeriod(employee_id=committed_employee.id, period_month=month))
    yield
    _cleanup_ledger(db_engine, committed_employee)


def _cleanup_ledger(engine: Engine, employee: Employee) -> None:
    """Remove every row the committed races may have written."""
    with Session(engine) as session, session.begin():
        interval_ids = session.scalars(
            select(WorkInterval.id).where(WorkInterval.employee_id == employee.id)
        ).all()
        if interval_ids:
            session.execute(
                delete(Correction).where(
                    Correction.entity_type == "WORK_INTERVAL",
                    Correction.entity_id.in_(interval_ids),
                )
            )
        # ``correct_interval`` builds its Correction from ``superseding.id``
        # BEFORE the flush, so the row's id is still None and ``entity_id`` is
        # stored NULL (a pre-existing lineage defect, out of F1/F2 scope). Scope
        # the cleanup by actor so these rows cannot leak into other tests.
        session.execute(delete(Correction).where(Correction.actor_employee_id == employee.id))
        session.execute(delete(WorkInterval).where(WorkInterval.employee_id == employee.id))
        # These races COMMIT audit events; other tests count audit rows, so the
        # committed ones must not leak into them.
        session.execute(delete(AuditEvent).where(AuditEvent.employee_subject_id == employee.id))
        session.execute(
            delete(WorkPeriod).where(
                WorkPeriod.employee_id == employee.id,
                WorkPeriod.period_month >= RACE_MONTH,
            )
        )


def _wait_until_blocked(engine: Engine, pid: int, timeout: float = 15.0) -> bool:
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


def test_EINV_05_concurrent_interval_creates_cannot_overlap(
    db_engine: Engine, committed_employee: Employee, race_period: None
) -> None:
    """Two interval creates for one employee must not both commit.

    The invariant is not "the second request fails" but "one employee's current
    intervals never overlap": whichever way the interleave resolves, at most one
    of the two rows may survive as current.
    """
    first, second = Session(db_engine), Session(db_engine)
    second_pid = int(second.scalar(text("SELECT pg_backend_pid()")))
    outcome: list[str] = []

    def _second() -> None:
        try:
            employee = second.get(Employee, committed_employee.id)
            assert employee is not None
            create_interval_instant(
                second,
                employee,
                start_at=_at(DAY, 7, 30),
                end_at=_at(DAY, 8, 30),
                work_type=WorkType.ON_SITE,
                comment="race-b",
            )
            second.commit()
            outcome.append("created")
        except HTTPException as exc:
            second.rollback()
            outcome.append(f"refused:{exc.status_code}")

    try:
        first_employee = first.get(Employee, committed_employee.id)
        assert first_employee is not None
        # A takes the employee-fact lock and inserts, WITHOUT committing.
        create_interval_instant(
            first,
            first_employee,
            start_at=_at(DAY, 7, 0),
            end_at=_at(DAY, 8, 0),
            work_type=WorkType.ON_SITE,
            comment="race-a",
        )
        worker = threading.Thread(target=_second)
        worker.start()
        blocked = _wait_until_blocked(db_engine, second_pid)
        assert blocked, "the second create never reached the employee-fact lock"
        assert outcome == [], "the second create finished while the lock was held"
        first.commit()
        worker.join(timeout=30)
        assert not worker.is_alive(), "the second create did not finish"
    finally:
        first.close()
        second.close()

    # B re-read after acquiring the lock, so it saw A's committed row.
    assert outcome == ["refused:422"], outcome
    with Session(db_engine) as session:
        overlapping = session.scalar(
            select(func.count())
            .select_from(WorkInterval)
            .where(
                WorkInterval.employee_id == committed_employee.id,
                WorkInterval.start_at < _at(DAY, 8, 30),
                WorkInterval.end_at > _at(DAY, 7, 0),
                _current_interval_filter(),
            )
        )
    assert overlapping == 1, f"two current intervals overlap: {overlapping}"


def test_EINV_03_concurrent_corrections_cannot_fork_the_chain(
    db_engine: Engine, committed_employee: Employee, race_period: None
) -> None:
    """Two corrections of one version must not both become current."""
    with Session(db_engine) as session, session.begin():
        base, _ = create_interval_instant(
            session,
            committed_employee,
            start_at=_at(DAY, 7, 0),
            end_at=_at(DAY, 9, 0),
            work_type=WorkType.ON_SITE,
            comment="original",
        )
        base_id = base.id

    first, second = Session(db_engine), Session(db_engine)
    second_pid = int(second.scalar(text("SELECT pg_backend_pid()")))
    outcome: list[str] = []

    def _second() -> None:
        try:
            employee = second.get(Employee, committed_employee.id)
            assert employee is not None
            correct_interval(
                second,
                employee,
                base_id,
                IntervalPatch(version=1, end_local=_wall(DAY, 10, 0)),
            )
            second.commit()
            outcome.append("corrected")
        except HTTPException as exc:
            second.rollback()
            outcome.append(f"refused:{exc.status_code}")

    try:
        first_employee = first.get(Employee, committed_employee.id)
        assert first_employee is not None
        correct_interval(
            first,
            first_employee,
            base_id,
            IntervalPatch(version=1, end_local=_wall(DAY, 9, 30)),
        )
        worker = threading.Thread(target=_second)
        worker.start()
        blocked = _wait_until_blocked(db_engine, second_pid)
        assert blocked, "the second correction never reached the employee-fact lock"
        assert outcome == [], "the second correction finished while the lock was held"
        first.commit()
        worker.join(timeout=30)
        assert not worker.is_alive(), "the second correction did not finish"
    finally:
        first.close()
        second.close()

    # B's version check now sees the successor A committed.
    assert outcome == ["refused:409"], outcome
    with Session(db_engine) as session:
        successors = session.scalars(
            select(WorkInterval).where(WorkInterval.supersedes_id == base_id)
        ).all()
    assert len(successors) == 1, f"one successor expected, found {len(successors)}"


def test_EINV_18_duplicate_submission_version_for_one_scope_is_rejected(
    db_session: Session, committed_employee: Employee
) -> None:
    """A submission scope may not hold two rows of the same version.

    Narrowest useful level: the persistence invariant itself.  No service call
    is involved because the defect is the absent database/scope constraint —
    only the request-level idempotency key was unique before.
    """

    def _package(client_request_id: uuid.UUID) -> SubmissionPackage:
        return SubmissionPackage(
            employee_id=committed_employee.id,
            purpose=SubmissionPurpose.OVERTIME_PAID,
            period_start=date(2032, 3, 1),
            period_end=date(2032, 3, 31),
            version=1,
            payload_json={},
            employee_confirmed_at=datetime.now(UTC),
            client_request_id=client_request_id,
        )

    accepted = 0
    try:
        for _ in range(2):
            db_session.add(_package(uuid.uuid4()))
            db_session.flush()
            accepted += 1
    except IntegrityError:
        db_session.rollback()
    assert accepted == 1, (
        "two submission packages with the same (employee, purpose, period, "
        f"version) were accepted: {accepted}"
    )


def test_EINV_06_grant_and_decline_cannot_produce_contradictory_state(
    db_engine: Engine, committed_employee: Employee
) -> None:
    """A request may not end DECLINED while an active grant for it exists.

    The confirmed interleave: the declining transaction resolves the request as
    PENDING, the granting transaction commits, and the decline decision — taken
    against the now-stale state — is still applied.  The two transactions are
    interleaved deterministically by waiting for the observed lock wait, so the
    reproduction does not depend on thread timing.

    The granter here writes the request row DIRECTLY (it does not take the
    advisory lock), which is exactly why the fix is a conditional UPDATE rather
    than the lock alone: the predicate must hold even for a lock-bypassing
    writer. The decline may therefore legitimately REFUSE instead of winning —
    the invariant is the absence of the contradictory state, not the outcome.
    """
    subject = committed_employee.idp_subject
    assert subject is not None
    principal = Principal(
        subject=subject,
        display_name="Employee A",
        capabilities=frozenset({Capability.OWN_DISCLOSURE_GRANT}),
    )
    request_id = uuid.uuid4()
    with Session(db_engine) as setup, setup.begin():
        setup.add(
            AccessRequest(
                id=request_id,
                employee_id=committed_employee.id,
                requester_principal="hr-requester",
                purpose=SubmissionPurpose.RANDOM_CHECK,
                period_start=date(2032, 3, 1),
                period_end=date(2032, 3, 31),
                view_type="work-time-record",
                status=AccessRequestStatus.PENDING,
            )
        )

    # A plain Session: binding a Session to a Connection that already has a
    # transaction makes SQLAlchemy join it in rollback_only mode, in which
    # commit() silently does not commit.
    decliner = Session(db_engine)
    decliner_pid = int(decliner.scalar(text("SELECT pg_backend_pid()")))
    outcome: list[str] = []

    def _decline() -> None:
        try:
            decline_access_request(decliner, principal, committed_employee, request_id)
            decliner.commit()
            outcome.append("declined")
        except HTTPException as exc:
            decliner.rollback()
            outcome.append(f"refused:{exc.status_code}")

    granter = Session(db_engine)
    try:
        request = granter.get(AccessRequest, request_id)
        assert request is not None
        request.status = AccessRequestStatus.GRANTED
        request.closed_at = datetime.now(UTC)
        granter.add(
            DisclosureGrant(
                employee_id=committed_employee.id,
                access_request_id=request_id,
                grantee_principal="hr-requester",
                purpose=SubmissionPurpose.RANDOM_CHECK,
                period_start=date(2032, 3, 1),
                period_end=date(2032, 3, 31),
                view_type="work-time-record",
                expires_at=None,
            )
        )
        # Uncommitted: the grant holds the request row lock.
        granter.flush()

        worker = threading.Thread(target=_decline)
        worker.start()
        blocked = _wait_until_blocked(db_engine, decliner_pid)
        # Release the lock so the decline decision can be applied.
        granter.commit()
        worker.join(timeout=30)
        assert not worker.is_alive(), "decline did not finish"
        assert blocked, "the decline never reached the request row lock"
        # Winning is allowed; refusing because the predicate no longer matches
        # is equally correct. Only the contradictory end state is forbidden.
        assert outcome in (["declined"], ["refused:409"]), outcome

        with Session(db_engine) as verify:
            status_now = verify.scalar(
                text("SELECT status FROM access_request WHERE id = :id"), {"id": request_id}
            )
            grants = verify.scalar(
                text("SELECT count(*) FROM disclosure_grant WHERE access_request_id = :id"),
                {"id": request_id},
            )
        assert not (status_now == "DECLINED" and grants == 1), (
            f"contradictory disclosure state: status={status_now!r} grants={grants}"
        )
        if outcome == ["declined"]:
            assert grants == 0, "declined with an active grant"
        else:
            assert status_now == "GRANTED" and grants == 1, "refused but the grant was lost"
    finally:
        granter.close()
        decliner.close()
        with Session(db_engine) as cleanup, cleanup.begin():
            cleanup.execute(
                delete(DisclosureGrant).where(DisclosureGrant.access_request_id == request_id)
            )
            cleanup.execute(delete(AccessRequest).where(AccessRequest.id == request_id))
