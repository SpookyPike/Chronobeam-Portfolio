"""Owner-scoped Shift/OCD list batching budgets and version-chain regressions."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from app.enums import (
    BreakKind,
    OcdAssignmentStatus,
    ShiftAssignmentStatus,
    ShiftDeviationType,
    WorkType,
)
from app.identity.models import Employee
from app.integrations.shift_plan import ShiftAssignmentSeed
from app.ocd_overtime.models import OcdAssignment, OcdCall, OcdCallMode, OcdCallStatus
from app.ocd_overtime.service import _ocd_out_for, _ocd_row_context, list_ocd
from app.shift.models import ShiftAssignment, ShiftDeviation
from app.shift.service import list_shifts, seed_shift_assignment
from app.worktime.models import BreakInterval, WorkInterval, WorkPeriod
from app.worktime.walltime import BERLIN
from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.orm import Session


@contextmanager
def _selects(session: Session) -> Iterator[list[str]]:
    statements: list[str] = []
    connection = session.connection()

    def capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(connection, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(connection, "before_cursor_execute", capture)


def _owner(session: Session) -> tuple[Employee, WorkPeriod]:
    employee = Employee(legal_name="Synthetic list performance employee")
    session.add(employee)
    session.flush()
    period = WorkPeriod(employee_id=employee.id, period_month=date(2026, 7, 1))
    session.add(period)
    session.flush()
    return employee, period


def _shift(session: Session, employee: Employee, day: int) -> ShiftAssignment:
    start = datetime(2026, 7, day, 8, tzinfo=BERLIN)
    return seed_shift_assignment(
        session,
        employee,
        ShiftAssignmentSeed(
            plan_key=f"list-batching-{employee.id}",
            plan_name="Synthetic list plan",
            planned_start_at=start,
            planned_end_at=start + timedelta(hours=8),
            shift_kind="DAY",
            source="TEST_SEED",
            source_ref="list-query-budget",
        ),
    )


def _interval(
    session: Session,
    employee: Employee,
    period: WorkPeriod,
    start: datetime,
    end: datetime,
    **values: Any,
) -> WorkInterval:
    row = WorkInterval(
        employee_id=employee.id,
        work_period_id=period.id,
        start_at=start,
        end_at=end,
        work_type=WorkType.SHIFT,
        **values,
    )
    session.add(row)
    session.flush()
    return row


@pytest.mark.parametrize("count", [1, 20])
def test_shift_list_five_queries_preserve_heads_breaks_status_and_owner(
    db_session: Session,
    count: int,
) -> None:
    employee, period = _owner(db_session)
    foreign, foreign_period = _owner(db_session)
    assignments = [_shift(db_session, employee, day) for day in range(1, count + 1)]
    first = assignments[0]
    start = first.planned_start_at
    # Include retained plan statuses: visibility is independent of writability.
    assignments[-1].status = ShiftAssignmentStatus.CANCELLED
    original = _interval(
        db_session,
        employee,
        period,
        start,
        start + timedelta(hours=8),
        linked_shift_assignment_id=first.id,
    )
    corrected = _interval(
        db_session,
        employee,
        period,
        start + timedelta(hours=1),
        start + timedelta(hours=7),
        linked_shift_assignment_id=first.id,
        supersedes_id=original.id,
        version=2,
    )
    _interval(
        db_session,
        foreign,
        foreign_period,
        start - timedelta(hours=4),
        start + timedelta(hours=12),
        linked_shift_assignment_id=first.id,
    )
    # A stale paid break corrected to UNPAID must not satisfy SHIFT-003.
    old_break = BreakInterval(
        employee_id=employee.id,
        work_period_id=period.id,
        start_at=start,
        end_at=start + timedelta(minutes=15),
        kind=BreakKind.PAID_SHIFT_BREAK,
        linked_shift_assignment_id=first.id,
    )
    db_session.add(old_break)
    db_session.flush()
    unpaid = BreakInterval(
        employee_id=employee.id,
        work_period_id=period.id,
        start_at=start,
        end_at=start + timedelta(minutes=15),
        kind=BreakKind.UNPAID,
        linked_shift_assignment_id=first.id,
        supersedes_id=old_break.id,
        version=2,
    )
    db_session.add(unpaid)
    paid = BreakInterval(
        employee_id=employee.id,
        work_period_id=period.id,
        start_at=start + timedelta(hours=2),
        end_at=start + timedelta(hours=2, minutes=15),
        kind=BreakKind.PAID_SHIFT_BREAK,
        linked_shift_assignment_id=first.id,
    )
    db_session.add(paid)
    deviation = ShiftDeviation(
        employee_id=employee.id,
        shift_assignment_id=first.id,
        deviation_type=ShiftDeviationType.SHORTENED,
        actual_start_at=start,
        actual_end_at=start + timedelta(hours=8),
        source_work_interval_ids=[str(original.id)],
        version=1,
    )
    db_session.add(deviation)
    db_session.flush()
    current_deviation = ShiftDeviation(
        employee_id=employee.id,
        shift_assignment_id=first.id,
        deviation_type=ShiftDeviationType.SHORTENED,
        actual_start_at=corrected.start_at,
        actual_end_at=corrected.end_at,
        source_work_interval_ids=[str(corrected.id)],
        supersedes_id=deviation.id,
        version=2,
    )
    db_session.add(current_deviation)
    _shift(db_session, foreign, 1)
    db_session.flush()
    with _selects(db_session) as statements:
        result = list_shifts(
            db_session,
            employee,
            datetime(2026, 7, 1, tzinfo=BERLIN),
            datetime(2026, 8, 1, tzinfo=BERLIN),
        )
    assert len(statements) == 5, statements
    assert [row.id for row in result] == [row.id for row in assignments]
    assert result[-1].status == ShiftAssignmentStatus.CANCELLED
    actual = result[0]
    assert actual.actual_start_at == corrected.start_at
    assert actual.actual_end_at == corrected.end_at
    assert actual.deviation is not None and actual.deviation.id == current_deviation.id
    assert actual.paid_break_status == "RECORDED"
    assert [row.id for row in actual.paid_breaks] == [paid.id]
    assert actual.approved_revision.id == first.approved_revision_id


@pytest.mark.parametrize("missing", [True, False])
def test_shift_list_refuses_missing_or_other_assignment_revision(
    db_session: Session,
    missing: bool,
) -> None:
    employee, _ = _owner(db_session)
    foreign, _ = _owner(db_session)
    assignment = _shift(db_session, employee, 1)
    other = _shift(db_session, foreign, 2)
    assignment.approved_revision_id = None if missing else other.approved_revision_id
    db_session.flush()
    with pytest.raises(HTTPException) as refused:
        list_shifts(
            db_session,
            employee,
            datetime(2026, 7, 1, tzinfo=BERLIN),
            datetime(2026, 8, 1, tzinfo=BERLIN),
        )
    assert refused.value.status_code == 422
    detail: Any = refused.value.detail
    assert detail["code"] == "POLICY_BLOCKED"


def _availability(session: Session, employee: Employee, day: int, **values: Any) -> OcdAssignment:
    start = datetime(2026, 7, day, tzinfo=BERLIN)
    row = OcdAssignment(
        employee_id=employee.id,
        start_at=start,
        end_at=start + timedelta(hours=8),
        source="TEST_SEED",
        status=OcdAssignmentStatus.SCHEDULED,
        **values,
    )
    session.add(row)
    session.flush()
    return row


@pytest.mark.parametrize("count", [1, 8])
def test_ocd_list_seven_queries_equal_point_rendering_and_keep_all_history_rest(
    db_session: Session,
    count: int,
) -> None:
    employee, period = _owner(db_session)
    foreign, foreign_period = _owner(db_session)
    predecessor = _availability(db_session, employee, 1)
    assignments = [_availability(db_session, employee, 1, supersedes_id=predecessor.id, version=2)]
    assignments += [_availability(db_session, employee, day) for day in range(2, count + 1)]
    current_interval_ids: set[uuid.UUID] = set()
    for assignment in assignments:
        for hour in (1, 3):
            old = OcdCall(
                employee_id=employee.id,
                ocd_assignment_id=assignment.id,
                call_start_at=assignment.start_at + timedelta(hours=hour),
                call_end_at=assignment.start_at + timedelta(hours=hour + 1),
                mode=OcdCallMode.REMOTE,
            )
            db_session.add(old)
            db_session.flush()
            call = OcdCall(
                employee_id=employee.id,
                ocd_assignment_id=assignment.id,
                call_start_at=old.call_start_at,
                call_end_at=old.call_end_at,
                mode=old.mode,
                supersedes_id=old.id,
                version=2,
                status=OcdCallStatus.VOIDED if hour == 3 else OcdCallStatus.RECORDED,
            )
            db_session.add(call)
            db_session.flush()
            old_interval = _interval(
                db_session,
                employee,
                period,
                old.call_start_at,
                old.call_end_at,
                linked_ocd_call_id=old.id,
            )
            current = _interval(
                db_session,
                employee,
                period,
                call.call_start_at,
                call.call_end_at,
                linked_ocd_call_id=call.id,
                supersedes_id=old_interval.id,
                version=2,
            )
            current_interval_ids.add(current.id)
            # Linked IDs alone never authorize loading another owner's facts.
            _interval(
                db_session,
                foreign,
                foreign_period,
                call.call_start_at,
                call.call_end_at,
                linked_ocd_call_id=call.id,
            )
    _availability(db_session, foreign, 1)
    # A prior-month correction must be used for rest context, even outside the list window.
    prior_start = datetime(2026, 6, 30, 8, tzinfo=BERLIN)
    old_prior = _interval(
        db_session, employee, period, prior_start, datetime(2026, 6, 30, 12, tzinfo=BERLIN)
    )
    _interval(
        db_session,
        employee,
        period,
        prior_start,
        datetime(2026, 6, 30, 23, tzinfo=BERLIN),
        supersedes_id=old_prior.id,
        version=2,
    )
    db_session.flush()
    start, end = datetime(2026, 7, 1, tzinfo=BERLIN), datetime(2026, 8, 1, tzinfo=BERLIN)
    context = _ocd_row_context(db_session, employee, start, end)
    expected = [_ocd_out_for(db_session, employee, row, context) for row in assignments]
    with _selects(db_session) as statements:
        result = list_ocd(db_session, employee, start, end)
    assert len(statements) == 7, statements
    assert [row.id for row in result] == [row.id for row in assignments]
    for actual, reference in zip(result, expected, strict=True):
        # Calls have no declared ordering in the API; compare their complete projections.
        actual.calls.sort(key=lambda row: row.id)
        reference.calls.sort(key=lambda row: row.id)
        assert actual == reference
        assert len(actual.calls) == 2
        assert {call.status for call in actual.calls} == {
            OcdCallStatus.RECORDED,
            OcdCallStatus.VOIDED,
        }
        assert all(call.linked_interval_id in current_interval_ids for call in actual.calls)
    assert any(finding.rule_id == "OCD-005" for finding in result[0].findings)
