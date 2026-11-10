"""The shared employee-fact serialization primitive (``app.db``).

Every employee-fact writer takes ``lock_employee_facts`` before reading that
employee's current-version / overlap / ``max(version)+1`` state. These tests
prove the primitive itself engages, and prove the two properties the rest of the
batch depends on:

- two sessions on the same employee mutually exclude (observed lock wait, never
  a sleep);
- re-acquiring the same key inside one transaction is free, which is what makes
  nested locking (``split_interval``, ``save_deviation``, ``patch_call``) safe.

The racers use plain ``Session(db_engine)``: a ``Session`` bound to a
``Connection`` that already owns a transaction joins it in ``rollback_only``
mode and ``commit()`` would silently not commit.
"""

import threading
import time
from datetime import date

from app.db import constraint_name, lock_employee_facts
from app.identity.models import Employee
from app.worktime.models import WorkPeriod
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


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


def test_two_sessions_mutually_exclude_on_one_employee(db_engine: Engine) -> None:
    """The second session is observed blocked until the first releases."""
    with Session(db_engine) as probe:
        employee = probe.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None

    holder = Session(db_engine)
    waiter = Session(db_engine)
    waiter_pid = int(waiter.scalar(text("SELECT pg_backend_pid()")))
    acquired: list[str] = []

    def _acquire() -> None:
        lock_employee_facts(waiter, employee.id)
        waiter.commit()
        acquired.append("acquired")

    worker = threading.Thread(target=_acquire)
    try:
        lock_employee_facts(holder, employee.id)
        worker.start()
        blocked = _wait_until_blocked(db_engine, waiter_pid)
        holder.commit()
        worker.join(timeout=30)
        assert not worker.is_alive(), "the waiter never acquired the lock"
        assert blocked, "the waiter never reached the advisory lock"
        assert acquired == ["acquired"]
    finally:
        holder.close()
        waiter.close()


def test_reacquiring_the_lock_in_one_transaction_is_free(db_engine: Engine) -> None:
    """Nested acquisition in one transaction returns instead of deadlocking."""
    with Session(db_engine) as probe:
        employee = probe.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None

    nested = Session(db_engine)
    other = Session(db_engine)
    other_pid = int(other.scalar(text("SELECT pg_backend_pid()")))
    blocked = False
    try:
        lock_employee_facts(nested, employee.id)
        lock_employee_facts(nested, employee.id)  # free: same transaction, same key

        def _acquire() -> None:
            lock_employee_facts(other, employee.id)
            other.commit()

        worker = threading.Thread(target=_acquire)
        worker.start()
        # Still held by ``nested`` after two acquisitions, so the other session
        # must be blocked — proving the second call neither released nor failed.
        blocked = _wait_until_blocked(db_engine, other_pid)
        nested.commit()
        worker.join(timeout=30)
        assert not worker.is_alive()
        assert blocked, "the nested acquisition did not keep the lock"
    finally:
        nested.close()
        other.close()


def test_lock_keys_are_per_employee(db_engine: Engine) -> None:
    """Two different employees do not exclude each other."""
    with Session(db_engine) as probe:
        employees = probe.scalars(select(Employee).limit(2)).all()
    assert len(employees) == 2

    first = Session(db_engine)
    second = Session(db_engine)
    second_pid = int(second.scalar(text("SELECT pg_backend_pid()")))
    done: list[str] = []

    def _acquire() -> None:
        lock_employee_facts(second, employees[1].id)
        second.commit()
        done.append("acquired")

    worker = threading.Thread(target=_acquire)
    try:
        lock_employee_facts(first, employees[0].id)
        worker.start()
        worker.join(timeout=30)
        assert not worker.is_alive(), "different employees must not serialize"
        assert not _wait_until_blocked(db_engine, second_pid, timeout=0.5)
        assert done == ["acquired"]
    finally:
        first.close()
        second.close()


def test_constraint_name_identifies_the_violated_constraint(
    db_engine: Engine, db_session: Session
) -> None:
    """A real unique violation reports its constraint name."""
    with Session(db_engine) as probe:
        employee = probe.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None

    month = date(2033, 7, 1)
    db_session.add(WorkPeriod(employee_id=employee.id, period_month=month))
    db_session.flush()
    try:
        db_session.add(WorkPeriod(employee_id=employee.id, period_month=month))
        db_session.flush()
    except IntegrityError as exc:
        assert constraint_name(exc) == "uq_work_period_employee_month"
    else:  # pragma: no cover - the constraint must exist
        raise AssertionError("the duplicate work period was accepted")


def test_constraint_name_is_none_for_an_unidentifiable_error() -> None:
    """An unidentifiable violation reports None, so callers re-raise it."""
    assert constraint_name(IntegrityError("stmt", {}, ValueError("boom"))) is None
    assert constraint_name(IntegrityError("stmt", {}, RuntimeError("no diag"))) is None
