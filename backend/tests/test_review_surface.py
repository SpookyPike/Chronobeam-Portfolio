"""F3 structural guards and the review/mutation ordering invariant.

Two kinds of check live here, mirroring the F1 ``test_identity_surface.py``
pattern: STRUCTURAL guards that keep an invariant holding as the code evolves
(they are tripwires with declared limits, not proofs), and one ORDERING test
that asserts the F2 lock actually serializes a review against a fact mutation.

The tripwires exist because the invariants they protect are "every writer must
remember to do X" invariants, which is exactly the kind that decays silently.
"""

import ast
import re
import threading
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from app.enums import WorkType
from app.identity.models import Employee
from app.policy.engine import PeriodResult
from app.worktime.models import Correction, WorkInterval, WorkPeriod
from app.worktime.service import create_interval_instant, review_month
from sqlalchemy import delete, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"

# Models whose construction creates a calculation input: writing one MUST
# reopen the review of the months it affects (EINV-15). Seeds are exempt: they
# run at test/dev bootstrap, before any review can exist, and are not employee
# commands. Models are exempt: ``class WorkInterval(Base)`` is not a write.
FACT_MODELS = frozenset(
    {
        "WorkInterval",
        "TravelSegment",
        "BusinessTrip",
        "OvertimeAuthorization",
        "BalanceLedgerEntry",
        "WorkScheduleVersion",
        # F5: calendar editions are a global calculation input, and a day-status
        # fact is a per-employee one. Adding them here is what makes the tripwire
        # see the new writers at all — they were invisible before.
        "CalendarVersion",
        "CalendarDay",
        "DayStatusEvent",
        "EmploymentContract",
    }
)
REVIEW_INVALIDATION_WRITERS = (
    "app/worktime/service.py",
    "app/ocd_overtime/service.py",
    "app/travel/service.py",
    "app/cutover/service.py",
    "app/identity/service.py",
    # F5 writers, each verified below to still write a calculation input.
    "app/calendar/service.py",
    "app/absence/service.py",
    "app/master_data_admin.py",
)
REVIEW_INVALIDATION_EXEMPT = (
    "app/worktime/models.py",
    "app/travel/models.py",
    "app/balance/models.py",
    "app/identity/models.py",
    "app/dev_seed.py",
    "app/demo_seed.py",
)
# Function-level exemptions, each with the reason it is NOT a calculation input.
REVIEW_INVALIDATION_EXEMPT_FUNCTIONS = {
    # A trip is a CONTAINER, not a fact in the calculation: creating one adds no
    # segment, no interval and no ledger row, and the travel context skips a
    # segment-less trip entirely. The credit/top-up effect arrives with the
    # segment commands, which do invalidate (and with patch_trip, which moves
    # the window).
    "app/travel/service.py::create_trip",
}
# Helpers that reopen a reviewed month, and the cross-module fact-writing
# chokepoints that already do so themselves.
INVALIDATORS = frozenset(
    {
        "open_reviewed_months",
        "open_reviewed_month_range",
        "_reopen_reviewed",
        # F5: cross-module helpers that reopen a reviewed month. Without them a
        # writer in another module reaches invalidation invisibly to the
        # intra-module call walk.
        "reopen_reviewed_months_in_publication",
        "_reopen_schedule_months",
        "_reopen_contract_months",
    }
)
INVALIDATING_CHOKEPOINTS = frozenset(
    {"create_interval_instant", "create_interval", "correct_interval", "split_interval"}
)
# The exact self-comparison that made a disclosure predicate inert.
SELF_COMPARING_PREDICATE = re.compile(r"(\w+)\.supersedes_id == \1\.id\b")

RACE_MONTH = date(2032, 7, 1)
RACE_START = datetime(2032, 7, 5, 8, 0, tzinfo=UTC)
RACE_END = datetime(2032, 7, 5, 16, 0, tzinfo=UTC)


def _called_names(node: ast.AST) -> set[str]:
    """Every bare function name called anywhere inside ``node``."""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
            names.add(child.func.id)
    return names


def _constructs_fact(node: ast.AST) -> bool:
    """True when ``node`` calls a fact model's constructor."""
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id in FACT_MODELS
        for child in ast.walk(node)
    )


def _functions_without_invalidation(source: str) -> list[str]:
    """Functions that build a calculation input without reopening a review.

    Function-level rather than module-level: a module that keeps ONE
    invalidation call while a second writer forgets it must fail. A function
    satisfies the guard when it reaches an invalidation helper downward
    (through the module's own helpers or a cross-module chokepoint that
    invalidates itself), or when EVERY caller above it does — a private builder
    is not the writer; the command that calls it is.
    """
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    calls = {name: _called_names(node) for name, node in functions.items()}
    callers: dict[str, set[str]] = {name: set() for name in functions}
    for name, direct in calls.items():
        for callee in direct & functions.keys():
            callers[callee].add(name)

    def invalidates(name: str, seen: frozenset[str]) -> bool:
        if name in INVALIDATORS:
            return True
        if name in seen or name not in functions:
            return False
        direct = calls[name]
        if direct & (INVALIDATORS | INVALIDATING_CHOKEPOINTS):
            return True
        return any(invalidates(callee, seen | {name}) for callee in direct)

    def covered(name: str, seen: frozenset[str]) -> bool:
        if invalidates(name, frozenset()):
            return True
        if name in seen:
            return False  # cycle with no invalidation on it: report it
        above = callers.get(name, set())
        if not above:
            return False  # nothing above ever invalidates
        return all(covered(caller, seen | {name}) for caller in above)

    return sorted(
        name
        for name, node in functions.items()
        if _constructs_fact(node) and not covered(name, frozenset())
    )


def test_every_calculation_input_writer_reopens_reviewed_months() -> None:
    """A new fact writer must reopen the review of the months it touches.

    Honest limit: this is a static tripwire, not a proof — it reasons about
    intra-module calls, a small cross-module chokepoint list and the callers of
    the module under inspection, so a writer that reaches invalidation through
    an unlisted module-level helper is a false positive to be resolved by
    extending the lists. It also cannot see PARTIAL invalidation: a command that
    already reopens some months but stops covering one of its inputs still
    passes here. That case is pinned behaviourally (the create/correct/delete
    cases in ``test_f3_state_invariants.py`` each assert the affected month
    reopens), which is why the behavioural tests are the load-bearing checks.
    Its job is to force a reviewer to look when a new writer appears, exactly
    like the F1 mutation-surface guard.
    """
    offenders: list[str] = []
    for path in sorted((BACKEND / "app").rglob("*.py")):
        relative = str(path.relative_to(BACKEND))
        if relative in REVIEW_INVALIDATION_EXEMPT:
            continue
        for function in _functions_without_invalidation(path.read_text(encoding="utf-8")):
            key = f"{relative}::{function}"
            if key in REVIEW_INVALIDATION_EXEMPT_FUNCTIONS:
                continue
            offenders.append(key)
    assert offenders == [], (
        "these functions write a calculation input without reopening the review "
        f"of the affected months (EINV-15): {offenders}"
    )
    # The allowed list must stay honest: every entry still writes a fact.
    for relative in REVIEW_INVALIDATION_WRITERS:
        tree = ast.parse((BACKEND / relative).read_text(encoding="utf-8"))
        assert any(
            _constructs_fact(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ), f"{relative} no longer writes a fact; drop it from the allowed list"


def test_no_self_comparing_supersedes_predicate() -> None:
    """A current-version predicate must alias the successor table.

    ``X.supersedes_id == X.id`` compares a row to itself, so it is always
    false: the ``NOT EXISTS`` becomes inert and the "current" filter silently
    matches every row. This is the shape the disclosure grant path had.
    """
    offenders = sorted(
        f"{path.relative_to(BACKEND)}"
        for path in (BACKEND / "app").rglob("*.py")
        if SELF_COMPARING_PREDICATE.search(path.read_text(encoding="utf-8"))
    )
    assert offenders == [], f"self-comparing supersedes predicate in: {offenders}"


def test_review_and_mutation_cannot_interleave(
    db_engine: Engine,
) -> None:
    """A review and a fact mutation for one employee are serialized.

    Deterministic, no sleeps for ordering: transaction A reviews a month and
    holds the employee-fact lock; transaction B runs the real create command in
    a worker thread and blocks; the test waits for B's OBSERVED lock wait, then
    A commits. Whichever way the interleave resolves, the committed state must
    never be "REVIEWED over facts it did not cover" — and here B, running after
    A, must reopen the month.
    """
    with Session(db_engine) as setup:
        employee = setup.scalar(select(Employee).where(Employee.idp_subject == "employee"))
        assert employee is not None
        employee_id = employee.id
    try:
        reviewer = Session(db_engine)
        worker = Session(db_engine)
        try:
            worker_pid = int(worker.scalar(text("SELECT pg_backend_pid()")))
            reviewer.begin()
            review_month(reviewer, _reload(reviewer, employee_id), RACE_MONTH, acknowledged=True)
            assert _status(reviewer, employee_id, RACE_MONTH) == "REVIEWED"

            outcome: list[str] = []

            def mutate() -> None:
                try:
                    create_interval_instant(
                        worker,
                        _reload(worker, employee_id),
                        start_at=RACE_START,
                        end_at=RACE_END,
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
            assert _wait_until_blocked(db_engine, worker_pid), (
                "the mutation never waited on the employee-fact lock"
            )
            reviewer.commit()
            thread.join(timeout=30)
            assert not thread.is_alive()
            assert outcome == ["committed"], outcome

            with Session(db_engine) as check:
                assert _status(check, employee_id, RACE_MONTH) == "OPEN", (
                    "a committed fact mutation left the month asserting REVIEWED"
                )
        finally:
            reviewer.close()
            worker.close()
    finally:
        _cleanup(db_engine, employee_id)


def test_review_response_reuses_the_review_calculation(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The private review response must not calculate the same month twice."""
    import app.policy.service as policy_service

    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    month = date(2034, 5, 1)
    calls: list[PeriodResult] = []
    original = policy_service.calculate_employee_period

    def counted(session: Session, person: Employee, period: date) -> PeriodResult:
        result = original(session, person, period)
        calls.append(result)
        return result

    monkeypatch.setattr(policy_service, "calculate_employee_period", counted)
    summary = review_month(db_session, employee, month, acknowledged=True)

    assert len(calls) == 1
    assert summary.closing_balance_minutes == calls[0].closing_balance_minutes


def _reload(session: Session, employee_id: uuid.UUID) -> Employee:
    session.expire_all()
    employee = session.get(Employee, employee_id)
    assert employee is not None
    return employee


def _status(session: Session, employee_id: uuid.UUID, month: date) -> str:
    session.expire_all()
    period = session.scalar(
        select(WorkPeriod).where(
            WorkPeriod.employee_id == employee_id, WorkPeriod.period_month == month
        )
    )
    return period.status.value if period is not None else "OPEN"


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


def _cleanup(engine: Engine, employee_id: uuid.UUID) -> None:
    """Remove only the rows this test can have written.

    ``seed_dev`` commits no intervals or periods, so a scoped delete is enough;
    it is scoped anyway so a future committed fixture cannot be destroyed here.
    """
    with Session(engine) as session, session.begin():
        interval_ids = session.scalars(
            select(WorkInterval.id).where(
                WorkInterval.employee_id == employee_id,
                WorkInterval.start_at >= RACE_START,
            )
        ).all()
        if interval_ids:
            session.execute(
                delete(Correction).where(
                    Correction.entity_type == "WORK_INTERVAL",
                    Correction.entity_id.in_(interval_ids),
                )
            )
        session.execute(
            delete(WorkInterval).where(
                WorkInterval.employee_id == employee_id,
                WorkInterval.start_at >= RACE_START,
            )
        )
        session.execute(
            delete(WorkPeriod).where(
                WorkPeriod.employee_id == employee_id,
                WorkPeriod.period_month >= RACE_MONTH,
            )
        )
