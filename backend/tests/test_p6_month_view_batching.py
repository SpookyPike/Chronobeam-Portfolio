"""P6.2 batching equivalence guards.

The P6 performance pass replaced three shapes of per-day/per-month work in the
month view with range prefetch:

1. effective-dated schedule/contract resolution went from one statement per DAY
   to one per range (``effective_schedules_by_day`` / ``effective_contracts_by_day``);
2. the canonical interval and break windows went from one fetch per CHAINED month
   to one fetch per view, narrowed in memory;
3. a chained month's interval window is narrowed to the months it overlaps,
   because only its closing balance is read.

Each is a claim of EQUIVALENCE, and each is only worth the speed if the claim
holds. These tests are the guards: they fail if the range resolvers pick a
different version than the point resolvers, or if narrowing a chained month's
window ever moves a daily result. The point resolvers are the reference — they
were not changed.
"""

from dataclasses import replace
from datetime import date, datetime, time, timedelta

import pytest
from app.enums import WorkType
from app.identity.models import Employee, WorkScheduleVersion
from app.identity.service import (
    effective_contract,
    effective_contracts_by_day,
    effective_schedule,
    effective_schedules_by_day,
)
from app.ocd_overtime.models import (
    INSTITUTIONAL_SOURCE,
    OvertimeAuthorization,
    OvertimeAuthStatus,
)
from app.policy.engine import calculate_period
from app.policy.service import _carry_facts, _month_inputs
from app.worktime.queries import month_bounds
from app.worktime.service import create_interval_instant
from app.worktime.walltime import BERLIN
from sqlalchemy import event, select
from sqlalchemy.orm import Session


def _employee(session: Session, subject: str) -> Employee:
    employee = session.scalar(select(Employee).where(Employee.idp_subject == subject))
    assert employee is not None
    return employee


def test_range_resolvers_agree_with_the_point_resolvers(db_session: Session) -> None:
    """Every day of a range resolves to the same version as the point lookup.

    The range form prefetches with ``valid_from < end`` and
    ``valid_to IS NULL OR valid_to >= start``. If that window were incomplete —
    a row that covers a day in the middle of the range but falls outside the
    predicate — the day would silently resolve to the WRONG version, which is a
    schedule/contract change the employee never made. Splitting the schedule at a
    mid-range boundary is what makes the window's two edges observable.
    """
    employee = _employee(db_session, "employee")
    original = db_session.scalar(
        select(WorkScheduleVersion).where(WorkScheduleVersion.employee_id == employee.id)
    )
    assert original is not None
    # Split the schedule inside the probed range, so the range has a boundary to
    # get wrong in both directions (before it: `original`; after it: `successor`).
    boundary = date(2026, 7, 15)
    original.valid_to = boundary - timedelta(days=1)
    successor = WorkScheduleVersion(
        employee_id=employee.id,
        valid_from=boundary,
        valid_to=None,
        weekly_required_minutes=original.weekly_required_minutes,
        mon_required_minutes=original.mon_required_minutes,
        tue_required_minutes=original.tue_required_minutes,
        wed_required_minutes=original.wed_required_minutes,
        thu_required_minutes=original.thu_required_minutes,
        fri_required_minutes=original.fri_required_minutes,
        sat_required_minutes=original.sat_required_minutes,
        sun_required_minutes=original.sun_required_minutes,
        mon_typical_start=None,
        mon_typical_end=None,
        tue_typical_start=None,
        tue_typical_end=None,
        wed_typical_start=None,
        wed_typical_end=None,
        thu_typical_start=None,
        thu_typical_end=None,
        fri_typical_start=None,
        fri_typical_end=None,
        sat_typical_start=None,
        sat_typical_end=None,
        sun_typical_start=None,
        sun_typical_end=None,
        source=original.source,
        version=original.version + 1,
    )
    db_session.add(successor)
    db_session.flush()

    start, end = date(2026, 6, 1), date(2026, 9, 1)
    schedules = effective_schedules_by_day(db_session, employee.id, start, end)
    contracts = effective_contracts_by_day(db_session, employee.id, start, end)
    day = start
    while day < end:
        assert schedules[day] is effective_schedule(db_session, employee.id, day), day
        assert contracts[day] is effective_contract(db_session, employee.id, day), day
        day += timedelta(days=1)
    # The boundary is inside the range and both sides are represented: without
    # this the assertions above could pass over a range with no boundary in it.
    assert schedules[boundary - timedelta(days=1)] is original
    assert schedules[boundary] is successor


def test_narrowing_a_chained_month_window_preserves_its_daily_results(
    db_session: Session,
) -> None:
    """A chained month's narrowed window must not move a single daily number.

    ``_carried_opening`` reads a chained month for its CLOSING BALANCE alone and
    narrows that month's intervals to the ones it overlaps. The claim is that the
    daily allocation clips every interval to its own day, so pre-month intervals
    cannot contribute — this asserts it directly, on daily results rather than on
    the balance, so a future rule that starts reading pre-month context fails here
    rather than silently shifting every balance after it.
    """
    employee = _employee(db_session, "employee")
    # Work in the month BEFORE the probed one, so narrowing actually removes
    # intervals, and inside the probed month, so there is a result to preserve.
    for day, hours in ((date(2026, 6, 15), 8), (date(2026, 7, 6), 8), (date(2026, 7, 7), 6)):
        create_interval_instant(
            db_session,
            employee,
            start_at=datetime.combine(day, time(8, 0), tzinfo=BERLIN),
            end_at=datetime.combine(day, time(8 + hours, 0), tzinfo=BERLIN),
            work_type=WorkType.ON_SITE,
            comment=None,
        )
    db_session.flush()

    probed = date(2026, 7, 1)
    # The full window the view fetches: everything up to the REQUESTED month's end.
    facts = _carry_facts(db_session, employee, date(2026, 9, 1))
    probed_start_at = month_bounds(probed)[0]
    # Exactly the narrowing ``_carried_opening`` applies to a chained month.
    narrowed = replace(
        facts,
        intervals=[row for row in facts.intervals if row.end_at > probed_start_at],
        breaks=[row for row in facts.breaks if row.end_at > probed_start_at],
    )
    # The narrowing must actually have removed something, or this proves nothing.
    assert len(narrowed.intervals) < len(facts.intervals)

    wide = calculate_period(_month_inputs(db_session, employee, probed, (), facts=facts))
    narrow = calculate_period(_month_inputs(db_session, employee, probed, (), facts=narrowed))
    assert narrow.daily_results == wide.daily_results
    assert narrow.closing_balance_minutes == wide.closing_balance_minutes
    # And the probed month did produce work, so equality is not vacuous.
    assert sum(result.worked_minutes for result in narrow.daily_results) > 0


def test_the_carry_chain_applies_the_narrowing(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production chain must PASS a narrowed window to each chained month.

    The equivalence test above proves the narrowing is SAFE. This one proves it is
    APPLIED: it is the only guard that fails if someone deletes the narrowing in
    ``_carried_opening`` while keeping the helpers, which would silently restore the
    O(history) engine cost this pass exists to remove. It asserts the exact fact that
    makes the optimization real — an interval lying wholly before a chained month is
    absent from that month's inputs, and present in the requested month's.
    """
    import app.policy.service as service

    employee = _employee(db_session, "employee")
    early = create_interval_instant(
        db_session,
        employee,
        start_at=datetime.combine(date(2026, 7, 6), time(8, 0), tzinfo=BERLIN),
        end_at=datetime.combine(date(2026, 7, 6), time(16, 0), tzinfo=BERLIN),
        work_type=WorkType.ON_SITE,
        comment=None,
    )[0]
    create_interval_instant(
        db_session,
        employee,
        start_at=datetime.combine(date(2026, 9, 6), time(8, 0), tzinfo=BERLIN),
        end_at=datetime.combine(date(2026, 9, 6), time(16, 0), tzinfo=BERLIN),
        work_type=WorkType.ON_SITE,
        comment=None,
    )
    db_session.flush()

    requested = date(2026, 9, 1)
    chained = date(2026, 8, 1)
    seen: dict[date, set[object]] = {}
    original = service._month_inputs

    def spy(session, employee_arg, month, opening, *, facts=None, as_of=None):
        assert facts is not None, "the chain must pass its prefetched facts"
        seen[month] = {row.id for row in facts.intervals}
        return original(session, employee_arg, month, opening, facts=facts, as_of=as_of)

    monkeypatch.setattr(service, "_month_inputs", spy)
    service.calculate_employee_period(db_session, employee, requested)

    assert requested in seen and chained in seen, f"expected both months, saw {sorted(seen)}"
    # The requested month keeps the full prior-interval context its findings need.
    assert early.id in seen[requested]
    # The chained month must NOT be handed an interval that ended before it began:
    # that exclusion is the whole optimization.
    assert early.id not in seen[chained]


def test_the_travel_facts_are_fetched_once_for_a_whole_carry_chain(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A multi-month view must fetch the month-independent travel facts EXACTLY once.

    This is the half that guards the optimization rather than its safety. The
    earlier version of this guard asserted only that ``prefetch`` was PASSED, which
    stays true even if the loader ignores it and calls
    ``month_independent_travel_facts()`` anyway — i.e. it could not catch the exact
    N-per-chain regression it claimed to guard. Counting the FETCHES instead makes
    the claim falsifiable: dropping the prefetch wiring turns one call into one per
    chained month.

    The chain is real (work in June, requested month September), and the counter is
    proven non-vacuous by a standalone month fetch in the same test.
    """
    import app.policy.service as service
    import app.travel.context as travel_context

    employee = _employee(db_session, "employee")
    db_session.add(
        OvertimeAuthorization(
            employee_id=employee.id,
            valid_from=datetime.combine(date(2026, 7, 6), time.min, tzinfo=BERLIN),
            valid_to=datetime.combine(date(2026, 7, 7), time.min, tzinfo=BERLIN),
            status=OvertimeAuthStatus.APPROVED,
            source=INSTITUTIONAL_SOURCE,
            version=1,
        )
    )
    # The chain's anchor is the employee's earliest current interval; this puts it
    # in June, so requesting September walks June, July, August and September.
    create_interval_instant(
        db_session,
        employee,
        start_at=datetime.combine(date(2026, 6, 15), time(8, 0), tzinfo=BERLIN),
        end_at=datetime.combine(date(2026, 6, 15), time(16, 0), tzinfo=BERLIN),
        work_type=WorkType.ON_SITE,
        comment=None,
    )
    db_session.flush()

    calls: list[object] = []
    real = travel_context.month_independent_travel_facts

    def counting(session, employee_id):
        calls.append(employee_id)
        return real(session, employee_id)

    # Both names must be patched: ``_carry_facts`` reaches it through
    # ``policy.service``'s module namespace, and the loader through its own.
    monkeypatch.setattr(service, "month_independent_travel_facts", counting)
    monkeypatch.setattr(travel_context, "month_independent_travel_facts", counting)

    # NON-VACUOUS: a standalone month computation really does fetch them, so a
    # zero-or-one result below cannot come from a counter that never fires.
    _month_inputs(db_session, employee, date(2026, 9, 1), ())
    assert len(calls) == 1, "the standalone month fetch must call the helper"

    calls.clear()
    statements: list[str] = []

    def record_calculation_sql(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ):
        statements.append(statement)

    bind = db_session.get_bind()
    event.listen(bind, "before_cursor_execute", record_calculation_sql)
    try:
        service.calculate_employee_period(db_session, employee, date(2026, 9, 1))
    finally:
        event.remove(bind, "before_cursor_execute", record_calculation_sql)

    assert len(calls) == 1, (
        "the whole carry chain must fetch the month-independent travel facts ONCE, "
        f"not once per chained month (got {len(calls)})"
    )
    assert sum("FROM work_interval" in statement for statement in statements) == 1


def test_the_travel_prefetch_is_equivalent_to_fetching_it_per_month(
    db_session: Session,
) -> None:
    """Passing the prefetch must yield the same travel inputs as fetching fresh.

    The prefetched row sets carry no month predicate, so this is the whole reason
    the optimization is safe. The approval makes the inputs non-vacuous: it enters
    the travel fingerprint, so an empty result could not pass by accident.
    """
    employee = _employee(db_session, "employee")
    db_session.add(
        OvertimeAuthorization(
            employee_id=employee.id,
            valid_from=datetime.combine(date(2026, 7, 6), time.min, tzinfo=BERLIN),
            valid_to=datetime.combine(date(2026, 7, 7), time.min, tzinfo=BERLIN),
            status=OvertimeAuthStatus.APPROVED,
            source=INSTITUTIONAL_SOURCE,
            version=1,
        )
    )
    db_session.flush()

    month = date(2026, 7, 1)
    facts = _carry_facts(db_session, employee, month)

    shared = _month_inputs(db_session, employee, month, (), facts=facts)
    fresh = _month_inputs(db_session, employee, month, ())

    assert shared.travel_input_fingerprint == fresh.travel_input_fingerprint
    assert shared.travel_credits == fresh.travel_credits
    assert shared.travel_topups == fresh.travel_topups
    # Not vacuous: the approval above must have reached the inputs.
    assert facts.travel.authorizations, "the prefetch must carry the approval"
    assert fresh.travel_input_fingerprint


def test_travel_prefetch_keeps_ancestry_currentness_and_full_history(db_session: Session) -> None:
    """One segment/interval fetch must preserve the original travel inputs."""
    from app.travel.context import load_travel_calculation
    from app.travel.models import (
        BusinessTrip,
        TravelSegment,
        TravelSegmentBasis,
        TravelSegmentClassification,
        TripStartOrigin,
    )
    from sqlalchemy import event

    employee = _employee(db_session, "employee")
    work = create_interval_instant(
        db_session,
        employee,
        start_at=datetime(2026, 6, 29, 8, tzinfo=BERLIN),
        end_at=datetime(2026, 6, 29, 12, tzinfo=BERLIN),
        work_type=WorkType.ON_SITE,
        comment=None,
    )[0]

    def trip(owner, start, end, **kw):
        row = BusinessTrip(
            employee_id=owner.id,
            start_at=start,
            end_at=end,
            start_origin=TripStartOrigin.HOME,
            version=1,
            **kw,
        )
        db_session.add(row)
        db_session.flush()
        return row

    root = trip(
        employee, datetime(2026, 6, 30, 8, tzinfo=BERLIN), datetime(2026, 7, 3, 20, tzinfo=BERLIN)
    )
    head = trip(employee, root.start_at, root.end_at + timedelta(hours=1), supersedes_id=root.id)
    head.version = 2
    empty = trip(
        employee, datetime(2026, 8, 1, 8, tzinfo=BERLIN), datetime(2026, 8, 1, 20, tzinfo=BERLIN)
    )
    trip(_employee(db_session, "employee-b"), root.start_at, root.end_at)

    def segment(parent, start, end, **kw):
        row = TravelSegment(
            trip_id=parent.id,
            start_at=start,
            end_at=end,
            classification=TravelSegmentClassification.PASSIVE_TRAVEL,
            basis=TravelSegmentBasis.ACTUAL,
            version=1,
            **kw,
        )
        db_session.add(row)
        db_session.flush()
        return row

    original = segment(
        root, datetime(2026, 6, 30, 20, tzinfo=BERLIN), datetime(2026, 7, 1, 2, tzinfo=BERLIN)
    )
    corrected = segment(
        root, original.start_at + timedelta(hours=1), original.end_at, supersedes_id=original.id
    )
    corrected.version = 2
    later = segment(
        head, datetime(2026, 7, 2, 8, tzinfo=BERLIN), datetime(2026, 7, 2, 12, tzinfo=BERLIN)
    )
    db_session.flush()
    statements: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    bind = db_session.get_bind()
    event.listen(bind, "before_cursor_execute", record)
    try:
        facts = _carry_facts(db_session, employee, date(2026, 9, 1))
        shared = {
            month: _month_inputs(
                db_session, employee, month, (), facts=replace(facts, intervals=[])
            )
            for month in (date(2026, 6, 1), date(2026, 7, 1), date(2026, 9, 1))
        }
    finally:
        event.remove(bind, "before_cursor_execute", record)
    assert sum("\nFROM work_interval \n" in sql for sql in statements) == 1
    assert sum("\nFROM travel_segment " in sql for sql in statements) == 1
    assert sum("\nFROM employment_contract " in sql for sql in statements) == 1
    assert sum("\nFROM work_schedule_version " in sql for sql in statements) == 1
    assert sum("\nFROM policy_version " in sql for sql in statements) == 1
    assert {row.id for row in facts.travel.trips} == {head.id, empty.id}
    assert [row.id for row in facts.travel.segments_by_trip[head.id]] == [corrected.id, later.id]
    assert facts.travel.segments_by_trip[empty.id] == ()
    assert facts.travel.intervals is not None
    assert work.id in {row.id for row in facts.travel.intervals}
    for month, inputs in shared.items():
        fresh = _month_inputs(db_session, employee, month, ())
        assert inputs.travel_input_fingerprint == fresh.travel_input_fingerprint
        assert inputs.travel_credits == fresh.travel_credits
        assert inputs.travel_topups == fresh.travel_topups
        context = load_travel_calculation(
            db_session, employee, month, {}, {}, prefetch=facts.travel
        )
        expected = [corrected] if month.month == 6 else [corrected, later]
        assert [row.id for row in context.segments_by_trip[head.id]] == [row.id for row in expected]

    import app.policy.service as policy_service

    statements: list[str] = []

    def record_calculation(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    bind = db_session.get_bind()
    event.listen(bind, "before_cursor_execute", record_calculation)
    try:
        policy_service.calculate_employee_period(db_session, employee, date(2026, 9, 1))
    finally:
        event.remove(bind, "before_cursor_execute", record_calculation)
    assert sum("FROM work_interval" in statement for statement in statements) == 1
    assert sum("FROM travel_segment" in statement for statement in statements) == 1
