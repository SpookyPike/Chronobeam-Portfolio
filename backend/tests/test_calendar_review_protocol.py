"""F5 review invalidation and the calendar/employee lock protocol (EINV-15).

Two kinds of check, mirroring ``test_review_surface.py``'s split: BEHAVIOURAL
tests that a publication reopens exactly the months it changes, and CONCURRENCY
regressions with genuinely independent DB sessions that prove the stale-REVIEWED
race is impossible rather than merely unlikely. Plus the structural guards that
make a future bypass fail the suite instead of reopening the race silently.
"""

import ast
import threading
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

from app.calendar.models import CALENDAR_KEY, CalendarDay, CalendarVersion
from app.calendar.service import publish_calendar_edition
from app.db import lock_calendar_coordination, lock_employee_facts
from app.enums import (
    CalendarDayClass,
    MasterDataSource,
    PayGroupBucket,
    PeriodStatus,
    WorkType,
)
from app.identity.models import Employee
from app.policy.service import calculate_employee_period
from app.worktime.models import WorkPeriod
from app.worktime.service import create_interval_instant, review_month
from sqlalchemy import delete, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

MONTH = date(2032, 7, 1)
HOLIDAY = date(2032, 7, 6)  # a Tuesday, so the zero requirement is discriminating
START = datetime(2032, 7, 6, 8, 0, tzinfo=UTC)
END = datetime(2032, 7, 6, 16, 0, tzinfo=UTC)
UNRELATED_MONTH = date(2032, 9, 1)


def _employee(session: Session, subject: str = "employee") -> Employee:
    row = session.scalar(select(Employee).where(Employee.idp_subject == subject))
    assert row is not None
    return row


def _publish_overlay(session: Session, version: str, days: dict[date, CalendarDayClass]):
    return publish_calendar_edition(
        session,
        calendar_key=CALENDAR_KEY,
        version=version,
        valid_from=MONTH,
        valid_to=date(2032, 7, 31),
        source="TEST",
        days=days,
        actor_principal="TEST",
    )


def _period(
    session: Session, employee_id: uuid.UUID, month: date, status: PeriodStatus
) -> WorkPeriod:
    """Replace any period row for that employee/month (idempotent fixture)."""
    session.execute(
        delete(WorkPeriod).where(
            WorkPeriod.employee_id == employee_id, WorkPeriod.period_month == month
        )
    )
    session.flush()
    row = WorkPeriod(employee_id=employee_id, period_month=month, status=status)
    session.add(row)
    session.flush()
    return row


def _status(session: Session, employee_id: uuid.UUID, month: date = MONTH) -> str:
    session.expire_all()
    period = session.scalar(
        select(WorkPeriod).where(
            WorkPeriod.employee_id == employee_id, WorkPeriod.period_month == month
        )
    )
    return period.status.value if period is not None else "MISSING"


def _required(session: Session, employee: Employee, day: date) -> int:
    result = calculate_employee_period(session, employee, date(day.year, day.month, 1))
    return next(item for item in result.daily_results if item.day == day).required_minutes


# ---------------------------------------------------------------------------
# Reopening behaviour
# ---------------------------------------------------------------------------


def test_calendar_publication_reopens_the_months_of_the_days_it_writes(
    db_session: Session,
) -> None:
    employee = _employee(db_session)
    _period(db_session, employee.id, MONTH, PeriodStatus.REVIEWED)
    _publish_overlay(db_session, "reopen-write-v1", {HOLIDAY: CalendarDayClass.PUBLIC_HOLIDAY})
    db_session.flush()
    assert _status(db_session, employee.id) == "OPEN"


def test_calendar_publication_reopens_the_months_of_the_days_it_un_classifies(
    db_session: Session,
) -> None:
    """Case (b): the old classification's span that this edition erases."""
    employee = _employee(db_session)
    _publish_overlay(db_session, "reopen-base-v1", {HOLIDAY: CalendarDayClass.PUBLIC_HOLIDAY})
    _period(db_session, employee.id, MONTH, PeriodStatus.REVIEWED)
    # A bounded overlay with NO day rows un-classifies the day.
    publish_calendar_edition(
        db_session,
        calendar_key=CALENDAR_KEY,
        version="reopen-erase-v1",
        valid_from=HOLIDAY,
        valid_to=HOLIDAY,
        source="TEST",
        days={},
        actor_principal="TEST",
    )
    db_session.flush()
    assert _status(db_session, employee.id) == "OPEN"
    assert _required(db_session, employee, HOLIDAY) == 348


def test_calendar_publication_does_not_reopen_months_whose_classification_is_unchanged(
    db_session: Session,
) -> None:
    """No over-reopen: re-publishing the same classification reopens nothing."""
    employee = _employee(db_session)
    _publish_overlay(db_session, "unchanged-base-v1", {HOLIDAY: CalendarDayClass.PUBLIC_HOLIDAY})
    _period(db_session, employee.id, MONTH, PeriodStatus.REVIEWED)
    # A later bounded overlay that repeats the SAME classification.
    publish_calendar_edition(
        db_session,
        calendar_key=CALENDAR_KEY,
        version="unchanged-overlay-v1",
        valid_from=date(2032, 7, 5),
        valid_to=date(2032, 7, 20),
        source="TEST",
        days={HOLIDAY: CalendarDayClass.PUBLIC_HOLIDAY},
        actor_principal="TEST",
    )
    db_session.flush()
    assert _status(db_session, employee.id) == "REVIEWED"


def test_forward_dated_calendar_publication_reopens_nothing_existing(db_session: Session) -> None:
    employee = _employee(db_session)
    _period(db_session, employee.id, MONTH, PeriodStatus.REVIEWED)
    publish_calendar_edition(
        db_session,
        calendar_key=CALENDAR_KEY,
        version="forward-v1",
        valid_from=date(2033, 1, 1),
        valid_to=None,
        source="TEST",
        days={date(2033, 1, 3): CalendarDayClass.PUBLIC_HOLIDAY},
        actor_principal="TEST",
    )
    db_session.flush()
    assert _status(db_session, employee.id) == "REVIEWED"


def test_calendar_publication_reopens_only_months_inside_the_governed_range(
    db_session: Session,
) -> None:
    """A bounded edition must not reopen months it does not govern."""
    employee = _employee(db_session)
    _period(db_session, employee.id, MONTH, PeriodStatus.REVIEWED)
    _period(db_session, employee.id, UNRELATED_MONTH, PeriodStatus.REVIEWED)
    _publish_overlay(db_session, "governed-range-v1", {HOLIDAY: CalendarDayClass.PUBLIC_HOLIDAY})
    db_session.flush()
    assert _status(db_session, employee.id, MONTH) == "OPEN"
    assert _status(db_session, employee.id, UNRELATED_MONTH) == "REVIEWED"


def test_calendar_publication_reopens_two_employees_and_leaves_a_third_and_closed_alone(
    db_session: Session,
) -> None:
    first = _employee(db_session, "employee")
    second = _employee(db_session, "employee-b")
    third = Employee(external_employee_id="emp-9001", legal_name="Third (synthetic)", active=True)
    db_session.add(third)
    db_session.flush()
    _period(db_session, first.id, MONTH, PeriodStatus.REVIEWED)
    _period(db_session, second.id, MONTH, PeriodStatus.REVIEWED)
    _period(db_session, third.id, UNRELATED_MONTH, PeriodStatus.REVIEWED)
    _period(db_session, third.id, MONTH, PeriodStatus.CLOSED)
    _publish_overlay(db_session, "multi-employee-v1", {HOLIDAY: CalendarDayClass.PUBLIC_HOLIDAY})
    db_session.flush()
    assert _status(db_session, first.id, MONTH) == "OPEN"
    assert _status(db_session, second.id, MONTH) == "OPEN"
    assert _status(db_session, third.id, UNRELATED_MONTH) == "REVIEWED"
    assert _status(db_session, third.id, MONTH) == "CLOSED"  # CLOSED never reopens


def test_contract_correction_reopens_exactly_its_effective_range(db_session: Session) -> None:
    """The F3 -> F5 hand-off: the contract writer is now covered."""
    from app.identity.models import EmploymentContract
    from app.identity.service import _reopen_contract_months

    employee = _employee(db_session)
    # A later-starting contract bounds the first row's effective range, so the
    # reopen is bounded rather than open-ended.
    db_session.execute(
        delete(EmploymentContract).where(
            EmploymentContract.employee_id == employee.id,
            EmploymentContract.valid_from >= MONTH,
        )
    )
    db_session.flush()
    db_session.add(
        EmploymentContract(
            employee_id=employee.id,
            valid_from=UNRELATED_MONTH,
            valid_to=None,
            fte=1.0,
            weekly_required_minutes=1740,
            pay_group_bucket=PayGroupBucket.LE_9B,
            source=MasterDataSource.MASTER,
            version=2,
        )
    )
    db_session.flush()
    _period(db_session, employee.id, MONTH, PeriodStatus.REVIEWED)
    _period(db_session, employee.id, UNRELATED_MONTH, PeriodStatus.REVIEWED)
    _reopen_contract_months(db_session, employee, MONTH)
    db_session.flush()
    assert _status(db_session, employee.id, MONTH) == "OPEN"
    assert _status(db_session, employee.id, UNRELATED_MONTH) == "REVIEWED"


def test_a_finite_ended_row_reopens_only_its_own_range(db_session: Session) -> None:
    """A row's OWN declared end bounds the reopened range (review R30).

    The master-data import may write a finite ``valid_to``. Reading only the
    SUCCESSOR bound made such a row report an open-ended range when it had no
    successor, so ``open_reviewed_month_range`` was handed ``end=None`` and
    cleared the review state of every month from the row's start onward — months
    the row does not govern at all. Reopening a reviewed month is a state change,
    not a recomputation, so this is the one that matters most.
    """
    from app.identity.models import WorkScheduleVersion
    from app.identity.service import _reopen_schedule_months

    employee = _employee(db_session)
    month_end = date(MONTH.year, MONTH.month, 28)
    db_session.add(
        WorkScheduleVersion(
            employee_id=employee.id,
            valid_from=MONTH,
            valid_to=month_end,
            version=1,
            source="MASTER",
            weekly_required_minutes=1740,
            mon_required_minutes=348,
            tue_required_minutes=348,
            wed_required_minutes=348,
            thu_required_minutes=348,
            fri_required_minutes=348,
            sat_required_minutes=0,
            sun_required_minutes=0,
        )
    )
    db_session.flush()
    _period(db_session, employee.id, MONTH, PeriodStatus.REVIEWED)
    _period(db_session, employee.id, UNRELATED_MONTH, PeriodStatus.REVIEWED)

    _reopen_schedule_months(db_session, employee, MONTH)
    db_session.flush()

    assert _status(db_session, employee.id, MONTH) == "OPEN"
    assert _status(db_session, employee.id, UNRELATED_MONTH) == "REVIEWED", (
        "a row that ends on the 28th reopened a month it does not govern"
    )


def test_a_finite_ended_contract_does_not_resurrect_its_predecessor(
    db_session: Session,
) -> None:
    """The RANGE resolver must not resurrect across a whole window (review R30).

    The batched resolver pre-filtered rows by ``valid_to >= start``, which was
    correct only under the per-row rule this branch replaced: under permanent
    shadowing an EXPIRED row is precisely what makes a later window ungoverned,
    so filtering it out let the earlier row win again.
    """
    from app.identity.models import EmploymentContract
    from app.identity.service import effective_contracts_by_day

    employee = _employee(db_session)
    later = date(MONTH.year, MONTH.month + 1, 1)
    later_end = date(MONTH.year, MONTH.month + 1, 28)
    db_session.execute(
        delete(EmploymentContract).where(EmploymentContract.employee_id == employee.id)
    )
    db_session.flush()
    for valid_from, valid_to in ((MONTH, None), (later, later_end)):
        db_session.add(
            EmploymentContract(
                employee_id=employee.id,
                valid_from=valid_from,
                valid_to=valid_to,
                fte=1.0,
                weekly_required_minutes=1740,
                pay_group_bucket=PayGroupBucket.LE_9B,
                source=MasterDataSource.MASTER,
                version=1,
            )
        )
    db_session.flush()

    # A window strictly AFTER the finite successor expired: nothing governs it,
    # and the July row must not come back. The window must START after the
    # successor's last day — starting ON it includes the successor under either
    # predicate and so proves nothing (this test was written that way first).
    after_start = date(MONTH.year, MONTH.month + 1, 29)
    after = effective_contracts_by_day(
        db_session, employee.id, after_start, date(MONTH.year, MONTH.month + 1, 30)
    )
    assert set(after) == {after_start}, "one day asked, one day answered"
    assert after[after_start] is None, "the expired successor must not resurrect July"
    # The window that CONTAINS the successor still resolves it, including its own
    # last day — the positive control for the assertion above.
    covered = effective_contracts_by_day(db_session, employee.id, MONTH, after_start)
    july = covered[MONTH]
    assert july is not None
    assert july.valid_from == MONTH
    assert covered[later_end] is not None, "the finite row governs its own last day"
    # NOTE: no consumer assertion here on purpose. The daily required minutes come
    # from the SCHEDULE, not the contract (the contract feeds the weekly
    # threshold), so ``_required`` is 0 for both days and would assert nothing.
    # The ledger-level proof for this resolver lives on the schedule side, in
    # tests/test_working_time_api.py::
    # test_a_later_arrangement_permanently_supersedes_an_earlier_one.


def test_the_publication_reopen_is_one_set_based_statement() -> None:
    """No employee enumeration, no per-employee lock: that is what removes the
    'a new work_period appears after the enumeration' window by construction."""
    source = (Path(__file__).resolve().parents[1] / "app" / "worktime" / "service.py").read_text(
        encoding="utf-8"
    )
    body = source[source.index("def reopen_reviewed_months_in_publication") :]
    body = body[: body.index("\ndef ")]
    assert "lock_employee_facts" not in body
    assert "update(WorkPeriod)" in body
    assert 'synchronize_session="fetch"' in body


# ---------------------------------------------------------------------------
# The concurrency protocol — independent DB sessions
# ---------------------------------------------------------------------------


def test_EINV_15_a_review_cannot_commit_against_a_superseded_calendar(
    db_engine: Engine,
) -> None:
    """The core case.

    T2 begins a publication of state B and does not commit. T1 calls
    ``review_month``; it must WAIT on the coordination lock, then compute against
    B. Against the unsafe protocol T1 would not block, would compute against A,
    and would commit a REVIEWED period whose reviewed calculation is A's.
    """
    with Session(db_engine) as setup:
        employee_id = _employee(setup).id
        _reset(setup, employee_id)
        setup.commit()
    try:
        publication = Session(db_engine)
        reviewer = Session(db_engine)
        try:
            reviewer_pid = int(reviewer.scalar(text("SELECT pg_backend_pid()")))
            publication.begin()
            _publish_overlay(publication, "race-b-v1", {HOLIDAY: CalendarDayClass.PUBLIC_HOLIDAY})

            outcome: dict[str, object] = {}

            def review() -> None:
                try:
                    summary = review_month(
                        reviewer, _reload(reviewer, employee_id), MONTH, acknowledged=True
                    )
                    outcome["required"] = next(
                        item.required_minutes
                        for item in summary.daily_results
                        if item.date == HOLIDAY
                    )
                    reviewer.commit()
                    outcome["committed"] = True
                except Exception as exc:  # pragma: no cover - failure path
                    reviewer.rollback()
                    outcome["error"] = f"{type(exc).__name__}: {exc}"

            thread = threading.Thread(target=review)
            thread.start()
            assert _wait_until_blocked(db_engine, reviewer_pid), (
                "the review did not wait on the calendar coordination lock"
            )
            publication.commit()
            thread.join(timeout=30)
            assert not thread.is_alive()
            assert outcome.get("committed") is True, outcome
            # The review computed against B, which IS authoritative.
            assert outcome["required"] == 0
            with Session(db_engine) as check:
                assert _status(check, employee_id) == "REVIEWED"
                assert _required(check, _employee(check), HOLIDAY) == 0
        finally:
            publication.close()
            reviewer.close()
    finally:
        _cleanup(db_engine, employee_id)


def test_EINV_15_a_work_period_created_during_a_publication_is_not_left_stale(
    db_engine: Engine,
) -> None:
    """The 'new work_period appears after affected-employee enumeration' case.

    The employee has NO ``work_period`` row for the month when the publication
    starts, so an enumeration-based protocol would miss the row the review then
    creates.
    """
    with Session(db_engine) as setup:
        employee_id = _employee(setup).id
        setup.execute(
            delete(WorkPeriod).where(
                WorkPeriod.employee_id == employee_id, WorkPeriod.period_month == MONTH
            )
        )
        setup.commit()
    try:
        publication = Session(db_engine)
        reviewer = Session(db_engine)
        try:
            reviewer_pid = int(reviewer.scalar(text("SELECT pg_backend_pid()")))
            publication.begin()
            _publish_overlay(
                publication, "race-newperiod-v1", {HOLIDAY: CalendarDayClass.PUBLIC_HOLIDAY}
            )
            outcome: dict[str, object] = {}

            def review() -> None:
                try:
                    summary = review_month(
                        reviewer, _reload(reviewer, employee_id), MONTH, acknowledged=True
                    )
                    outcome["required"] = next(
                        item.required_minutes
                        for item in summary.daily_results
                        if item.date == HOLIDAY
                    )
                    reviewer.commit()
                    outcome["committed"] = True
                except Exception as exc:  # pragma: no cover - failure path
                    reviewer.rollback()
                    outcome["error"] = f"{type(exc).__name__}: {exc}"

            thread = threading.Thread(target=review)
            thread.start()
            assert _wait_until_blocked(db_engine, reviewer_pid)
            publication.commit()
            thread.join(timeout=30)
            assert not thread.is_alive()
            assert outcome.get("committed") is True, outcome
            assert outcome["required"] == 0
            with Session(db_engine) as check:
                rows = check.scalars(
                    select(WorkPeriod).where(
                        WorkPeriod.employee_id == employee_id,
                        WorkPeriod.period_month == MONTH,
                    )
                ).all()
                assert len(rows) == 1
                assert rows[0].status is PeriodStatus.REVIEWED
        finally:
            publication.close()
            reviewer.close()
    finally:
        _cleanup(db_engine, employee_id)


def test_EINV_15_a_review_committed_before_a_publication_is_reopened(db_engine: Engine) -> None:
    """The other half of the protocol: blocking alone is not enough.

    A protocol that excludes concurrent reviews but forgets to reopen the
    already-committed one would pass the two tests above and fail this one.
    """
    with Session(db_engine) as setup:
        employee_id = _employee(setup).id
        _reset(setup, employee_id)
        setup.commit()
    try:
        reviewer = Session(db_engine)
        try:
            reviewer.begin()
            review_month(reviewer, _reload(reviewer, employee_id), MONTH, acknowledged=True)
            reviewer.commit()
        finally:
            reviewer.close()
        with Session(db_engine) as check:
            assert _status(check, employee_id) == "REVIEWED"
        publication = Session(db_engine)
        try:
            publication.begin()
            _publish_overlay(
                publication, "race-after-v1", {HOLIDAY: CalendarDayClass.PUBLIC_HOLIDAY}
            )
            publication.commit()
        finally:
            publication.close()
        with Session(db_engine) as check:
            assert _status(check, employee_id) == "OPEN"
            assert _required(check, _employee(check), HOLIDAY) == 0
    finally:
        _cleanup(db_engine, employee_id)


def test_EINV_15_the_coordination_lock_is_released_by_commit_and_rollback(
    db_engine: Engine,
) -> None:
    """The lock cannot leak or wedge the service."""
    with Session(db_engine) as setup:
        employee_id = _employee(setup).id
        _reset(setup, employee_id)
        setup.commit()
    try:
        for outcome in ("commit", "rollback"):
            session = Session(db_engine)
            try:
                session.begin()
                lock_calendar_coordination(session)
                lock_employee_facts(session, employee_id)
                if outcome == "commit":
                    session.commit()
                else:
                    session.rollback()
            finally:
                session.close()
            # The next acquisition must succeed immediately.
            follower = Session(db_engine)
            try:
                follower.begin()
                lock_calendar_coordination(follower)
                lock_employee_facts(follower, employee_id)
                follower.rollback()
            finally:
                follower.close()
    finally:
        _cleanup(db_engine, employee_id)


def test_a_publication_and_an_employee_mutation_do_not_deadlock(db_engine: Engine) -> None:
    """The publication takes no employee lock, so the two cannot form a cycle."""
    with Session(db_engine) as setup:
        employee_id = _employee(setup).id
        _reset(setup, employee_id)
        setup.commit()
    try:
        publication = Session(db_engine)
        worker = Session(db_engine)
        try:
            worker_pid = int(worker.scalar(text("SELECT pg_backend_pid()")))
            publication.begin()
            _publish_overlay(
                publication, "race-nolock-v1", {HOLIDAY: CalendarDayClass.PUBLIC_HOLIDAY}
            )
            outcome: list[str] = []

            def mutate() -> None:
                try:
                    create_interval_instant(
                        worker,
                        _reload(worker, employee_id),
                        start_at=START,
                        end_at=END,
                        work_type=WorkType.ON_SITE,
                        comment=None,
                    )
                    worker.commit()
                    outcome.append("committed")
                except Exception as exc:  # pragma: no cover - failure path
                    worker.rollback()
                    outcome.append(f"error:{type(exc).__name__}")

            thread = threading.Thread(target=mutate)
            thread.start()
            # An employee command is NOT excluded by the publication.
            thread.join(timeout=15)
            assert not thread.is_alive(), "an employee mutation deadlocked with a publication"
            assert outcome == ["committed"], outcome
            assert _wait_until_blocked(db_engine, worker_pid) is False
            publication.commit()
        finally:
            publication.close()
            worker.close()
    finally:
        _cleanup(db_engine, employee_id)


# ---------------------------------------------------------------------------
# Structural guards
# ---------------------------------------------------------------------------


def test_only_the_two_approved_callers_take_the_calendar_coordination_lock() -> None:
    """The mutation-surface tripwire for the new primitive."""
    backend = Path(__file__).resolve().parents[1]
    callers: list[str] = []
    for path in sorted((backend / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "lock_calendar_coordination"
            ):
                callers.append(f"{path.relative_to(backend)}")
    # ``app/db.py`` defines it; exactly two modules call it.
    assert sorted(set(callers)) == ["app/calendar/service.py", "app/worktime/service.py"]


def test_the_calendar_publication_takes_no_employee_fact_lock() -> None:
    """It must not acquire an employee lock while holding the global one."""
    source = (Path(__file__).resolve().parents[1] / "app" / "calendar" / "service.py").read_text(
        encoding="utf-8"
    )
    assert "lock_employee_facts" not in source


def test_the_calendar_coordination_lock_is_always_acquired_before_the_employee_fact_lock() -> None:
    """An AST walk: any function reaching BOTH must reach the calendar lock first.

    The rule is a global order, so a future path that inverts it fails here
    rather than in production.
    """
    backend = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in sorted((backend / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            order: list[str] = []
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                    if child.func.id == "lock_calendar_coordination":
                        order.append("calendar")
                    elif child.func.id == "lock_employee_facts":
                        order.append("employee")
            if (
                "calendar" in order
                and "employee" in order
                and order.index("calendar") > order.index("employee")
            ):
                offenders.append(f"{path.name}::{node.name}")
    assert offenders == [], f"calendar-lock/employee-lock inversion in: {offenders}"


def test_the_review_month_is_the_only_reviewed_transition() -> None:
    """The invariant behind the whole design."""
    backend = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in sorted((backend / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            assigns_reviewed = any(
                isinstance(child, ast.Assign)
                and isinstance(child.value, ast.Attribute)
                and child.value.attr == "REVIEWED"
                for child in ast.walk(node)
            )
            if not assigns_reviewed:
                continue
            calls = {
                child.func.id
                for child in ast.walk(node)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
            }
            if "lock_calendar_coordination" not in calls:
                offenders.append(f"{path.name}::{node.name}")
    assert offenders == [], (
        "these functions commit a period as REVIEWED without participating in the "
        f"calendar protocol: {offenders}"
    )


def _reload(session: Session, employee_id: uuid.UUID) -> Employee:
    session.expire_all()
    employee = session.get(Employee, employee_id)
    assert employee is not None
    return employee


def _wait_until_blocked(engine: Engine, pid: int, timeout: float = 15.0) -> bool:
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


def _reset(engine: Session, employee_id: uuid.UUID) -> None:
    """Clear this suite's rows so a repeated run is deterministic."""
    from app.worktime.models import WorkInterval

    engine.execute(
        delete(WorkInterval).where(
            WorkInterval.employee_id == employee_id, WorkInterval.start_at >= START
        )
    )
    engine.execute(
        delete(WorkPeriod).where(
            WorkPeriod.employee_id == employee_id, WorkPeriod.period_month >= MONTH
        )
    )
    engine.execute(
        delete(CalendarDay).where(
            CalendarDay.calendar_version_id.in_(
                select(CalendarVersion.id).where(CalendarVersion.source == "TEST")
            )
        )
    )
    engine.execute(delete(CalendarVersion).where(CalendarVersion.source == "TEST"))
    engine.flush()


def _cleanup(engine: Engine, employee_id: uuid.UUID) -> None:
    """Remove only the rows this suite can have written."""
    with Session(engine) as session, session.begin():
        from app.worktime.models import WorkInterval

        session.execute(
            delete(WorkInterval).where(
                WorkInterval.employee_id == employee_id, WorkInterval.start_at >= START
            )
        )
        session.execute(
            delete(WorkPeriod).where(
                WorkPeriod.employee_id == employee_id, WorkPeriod.period_month >= MONTH
            )
        )
        session.execute(
            delete(CalendarDay).where(
                CalendarDay.calendar_version_id.in_(
                    select(CalendarVersion.id).where(CalendarVersion.source == "TEST")
                )
            )
        )
        session.execute(delete(CalendarVersion).where(CalendarVersion.source == "TEST"))
