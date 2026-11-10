"""P3 — the deterministic demo environment.

The seed is asserted as *effect*, not as row counts: a month view per persona, the
findings a persona is built to produce, the balance a carried-forward opening entry
produces, the day a travel top-up closes, and the frozen package the recipient can
actually read.

The calendar half pins the one thing the dev seed cannot offer: a statutory edition
that starts on 1 January, so no month of the horizon is left as ordinary working
days (the compromise ``just demo-reset`` + ``just demo-seed`` exists to avoid).
"""

from datetime import date, timedelta
from itertools import pairwise

import pytest
from app.absence.models import DayStatusEvent
from app.auth.dev_principals import DEV_PRINCIPALS
from app.auth.models import RoleAssignment
from app.balance.models import BalanceLedgerEntry
from app.calendar.holidays import SH_HORIZON_FROM, SH_HORIZON_TO, statutory_days
from app.calendar.models import CalendarDay, CalendarVersion
from app.demo_seed import (
    DEMO_CUTOVER_MONTH,
    DEMO_FROM,
    DEMO_PERSONAS,
    DEMO_TO,
    SHIFT_BLOCK_DAYS,
    SHIFT_WINDOWS,
    DemoSeedRefused,
    seed_demo,
)
from app.disclosure.models import SubmissionPackage
from app.enums import CalendarDayClass
from app.identity.models import Employee
from app.ocd_overtime.models import OcdAssignment, OvertimeAuthorization
from app.policy.service import calculate_employee_period
from app.shift.models import ShiftAssignment, ShiftDeviation
from app.travel.models import BusinessTrip, TravelSegment
from app.worktime.models import BreakInterval, WorkInterval
from app.worktime.walltime import BERLIN
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

MONTHS = "/api/v1/me/months"
INTERVALS = "/api/v1/me/work-intervals"
RECIPIENT_QUEUE = "/api/v1/recipient/submissions"
ONBOARDING = "/api/v1/me/onboarding"

DEMO_MONTH = "2026-09"
DEMO_SUBJECTS = tuple(str(spec["idp_subject"]) for spec in DEMO_PERSONAS)
ROLE_HOLDING = {"demo-admin": "APPLICATION_ADMIN", "demo-hr": "HR_REQUESTER"}
#: The persona that is DELIBERATELY fact-free, so the start-state form can be
#: demonstrated live: the clean-cutover guard refuses to establish an opening
#: balance for anyone who already holds pre-cutover facts (INV-13), and every other
#: persona holds September facts. Named here rather than excluded silently, so giving it
#: facts fails a test instead of quietly removing the demo's onboarding surface.
FACT_FREE_PERSONAS = ("demo-onboarding",)
#: Every persona that must have its own recorded time. Every OTHER one: a mapped+active
#: employee row carries the baseline ``OWN_LEDGER_*`` capabilities, and a role only
#: *adds* reach — it never takes away the person's own time tracking.
ALL_EMPLOYEE_PERSONAS = tuple(
    subject for subject in DEMO_SUBJECTS if subject not in FACT_FREE_PERSONAS
)
#: The three standard shift windows and the block length, written out rather than
#: derived from the seed: a check that reads the seed's own constant cannot catch
#: that constant being wrong. The pinning test below keeps the two in step.
STANDARD_SHIFT_WINDOWS = [("07:00", "15:30"), ("15:00", "23:30"), ("23:00", "07:30")]
SHIFT_BLOCK = 3


def _headers(subject: str) -> dict[str, str]:
    return {"X-Chronobeam-Dev-Principal": subject}


def _window(row: ShiftAssignment) -> tuple[str, str]:
    """A shift's wall window as ``(HH:MM, HH:MM)`` in Berlin, whatever the date."""
    return (
        row.planned_start_at.astimezone(BERLIN).strftime("%H:%M"),
        row.planned_end_at.astimezone(BERLIN).strftime("%H:%M"),
    )


@pytest.fixture()
def demo(db_session: Session) -> Session:
    """Build the demo state in-transaction, from the schema ``just demo-reset`` leaves.

    The session-scoped test fixture has already published the *dev* seed's coverage
    edition on 2026-01-01, which the demo seed refuses to build over (one edition per
    effective date). Clearing the calendar inside the test transaction mirrors
    ``test_calendar_effects.py`` and keeps the assertion honest: the demo is built
    from exactly the empty schema ``just demo-reset`` provides.
    """
    db_session.execute(delete(CalendarDay))
    db_session.execute(delete(CalendarVersion))
    db_session.flush()
    seed_demo(db_session)
    return db_session


def _employee(session: Session, subject: str) -> Employee:
    row = session.scalar(select(Employee).where(Employee.idp_subject == subject))
    assert row is not None, subject
    return row


def _rules(session: Session, subject: str, month: date = date(2026, 9, 1)) -> set[str]:
    result = calculate_employee_period(session, _employee(session, subject), month)
    return {finding.rule_id for finding in result.findings}


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _count_where(session: Session, model: type, *criteria: object) -> int:
    statement = select(func.count()).select_from(model)
    for criterion in criteria:
        statement = statement.where(criterion)  # type: ignore[arg-type]
    return int(session.scalar(statement) or 0)


# ---------------------------------------------------------------------------
# The calendar
# ---------------------------------------------------------------------------


def test_the_demo_calendar_covers_the_horizon_from_the_first_of_january(demo: Session) -> None:
    """The whole point of ``just demo-reset``: no coverage-edition compromise.

    The dev seed publishes an overlay from 1 October because its own coverage edition
    already owns 2026-01-01; the demo has a fresh schema, so the statutory set starts
    on 1 January and *every* month of the horizon is classified.
    """
    editions = demo.scalars(select(CalendarVersion)).all()
    assert len(editions) == 1
    edition = editions[0]
    assert edition.valid_from == SH_HORIZON_FROM == date(2026, 1, 1)
    assert edition.valid_to == SH_HORIZON_TO == date(2035, 12, 31)
    assert "SFTG SH 2004 §2(1)" in edition.source

    rows = {
        row[0]
        for row in demo.execute(
            select(CalendarDay.day).where(CalendarDay.calendar_version_id == edition.id)
        ).all()
    }
    assert rows == set(statutory_days(SH_HORIZON_FROM, SH_HORIZON_TO))
    assert len(rows) == 100

    # The months the dev seed leaves ordinary are classified here — including the
    # 1 January and 1 May that a January-start horizon is the only way to cover.
    for day in (date(2026, 1, 1), date(2026, 4, 3), date(2026, 5, 1), date(2026, 5, 25)):
        assert day in rows, day
    # Bounded horizon, honestly: outside it the month view fails closed rather than
    # silently reusing a neighbouring edition's classification.
    assert date(2025, 12, 31) not in rows
    assert date(2036, 1, 1) not in rows


def test_the_demo_calendar_invents_no_employer_free_day(demo: Session) -> None:
    """``OPEN-12``: the employer-free days stay operator-supplied."""
    classes = {row[0] for row in demo.execute(select(CalendarDay.day_class)).all()}
    assert classes == {CalendarDayClass.PUBLIC_HOLIDAY}


def test_the_demo_seed_refuses_a_dev_seeded_calendar(db_session: Session) -> None:
    """The two seeds cannot share one database, and the refusal says so.

    ``db_session`` deliberately does NOT clear the calendar here: the dev seed's
    coverage edition is exactly the state the demo seed must refuse.
    """
    assert db_session.scalar(
        select(CalendarVersion).where(CalendarVersion.valid_from == SH_HORIZON_FROM)
    )
    with pytest.raises(DemoSeedRefused, match="demo-reset"):
        seed_demo(db_session)


# ---------------------------------------------------------------------------
# Personas and their own time
# ---------------------------------------------------------------------------


def test_every_persona_is_registered_and_tracks_its_own_time(
    demo: Session, db_client: TestClient
) -> None:
    """A persona the dev header seam cannot resolve would 401, so registration is load-bearing."""
    assert len(DEMO_PERSONAS) == 14
    for subject in ALL_EMPLOYEE_PERSONAS:
        assert subject in DEV_PRINCIPALS, subject
        employee = _employee(demo, subject)
        assert employee.active
        assert _count_where(demo, WorkInterval, WorkInterval.employee_id == employee.id) > 0, (
            f"{subject} has no recorded time"
        )
        response = db_client.get(f"{MONTHS}/{DEMO_MONTH}", headers=_headers(subject))
        assert response.status_code == 200, (subject, response.text)


def test_the_onboarding_persona_is_fact_free_and_can_establish_a_start_state(
    demo: Session, db_client: TestClient
) -> None:
    """The fact-free persona exists so onboarding can be demonstrated live.

    Onboarding is refused for anyone who already holds pre-cutover ledger rows or
    interval facts — an additive opening balance would double-count them (INV-13,
    clean cutover). Every other persona holds September facts, so without this one the
    start-state form cannot be shown for any month at or after the rollout
    boundary. Both halves are pinned here: give the persona facts and the demo's
    onboarding surface disappears, silently.

    The state is read for the CURRENT Berlin month rather than a fixed one, so the
    assertion does not expire as the calendar moves.
    """
    for subject in FACT_FREE_PERSONAS:
        employee = _employee(demo, subject)
        assert _count_where(demo, WorkInterval, WorkInterval.employee_id == employee.id) == 0, (
            f"{subject} is meant to be fact-free: onboarding is refused once it holds facts"
        )
        state = db_client.get(ONBOARDING, headers=_headers(subject)).json()
        assert state["state"] == "READY", state
        assert state["opening"] is None
        # The form prefills from the arrangement the start month resolves to, so it
        # must not be empty.
        assert state["schedule"] is not None, state


def test_a_role_widens_reach_without_removing_own_time_tracking(
    demo: Session, db_client: TestClient
) -> None:
    """Admin, HR and the recipient are employees first.

    Each holds a role *and* keeps the baseline employee capabilities, so each can
    read and write their own ledger; the role adds only what it names.
    """
    for subject, role in ROLE_HOLDING.items():
        employee = _employee(demo, subject)
        assigned = {
            row.role
            for row in demo.scalars(
                select(RoleAssignment).where(
                    RoleAssignment.employee_id == employee.id,
                    RoleAssignment.revoked_at.is_(None),
                )
            )
        }
        assert assigned == {role}, subject

    recipient = _employee(demo, "demo-recipient")
    assert {
        row.role
        for row in demo.scalars(
            select(RoleAssignment).where(
                RoleAssignment.employee_id == recipient.id,
                RoleAssignment.revoked_at.is_(None),
            )
        )
    } == {"SUBMISSION_RECIPIENT"}

    for subject in (*ROLE_HOLDING, "demo-recipient"):
        read = db_client.get(f"{MONTHS}/{DEMO_MONTH}", headers=_headers(subject))
        assert read.status_code == 200, (subject, read.text)
        write = db_client.post(
            INTERVALS,
            headers=_headers(subject),
            json={
                "start_local": "2026-10-01T18:00:00",
                "end_local": "2026-10-01T19:00:00",
                "work_type": "ON_SITE",
            },
        )
        assert write.status_code == 201, (subject, write.text)


def test_no_persona_holds_a_supervisor_role(demo: Session) -> None:
    """The deny boundary is the *absence* of a role, not a reduced capability set.

    ``app/auth/roles.py`` defines no SUPERVISOR role on purpose (ACCESS-012/013), so
    the supervisory persona is an ordinary employee with its own private ledger and
    nothing more.
    """
    roles = {row[0] for row in demo.execute(select(RoleAssignment.role)).all()}
    assert roles == {"APPLICATION_ADMIN", "HR_REQUESTER", "SUBMISSION_RECIPIENT"}
    supervisor = _employee(demo, "demo-supervisor")
    assert (
        demo.scalar(
            select(func.count())
            .select_from(RoleAssignment)
            .where(RoleAssignment.employee_id == supervisor.id)
        )
        == 0
    )


# ---------------------------------------------------------------------------
# Seeded scenarios
# ---------------------------------------------------------------------------


def test_the_findings_persona_produces_its_named_findings(demo: Session) -> None:
    rules = _rules(demo, "demo-findings")
    assert {"WT-CORE-003", "WT-CORE-004", "WT-CORE-005", "WT-CORE-006", "WT-STATUS-003"} <= rules


def test_the_clean_persona_has_no_findings_and_a_zero_balance(demo: Session) -> None:
    """The negative control for the findings persona."""
    result = calculate_employee_period(demo, _employee(demo, "demo-clean"), date(2026, 9, 1))
    assert {finding.rule_id for finding in result.findings} == set()
    assert result.closing_balance_minutes == 0


def test_the_parttime_persona_has_its_own_lower_requirement(demo: Session) -> None:
    result = calculate_employee_period(demo, _employee(demo, "demo-parttime"), date(2026, 9, 1))
    assert result.closing_balance_minutes == 0
    full_time = calculate_employee_period(demo, _employee(demo, "demo-clean"), date(2026, 9, 1))
    assert sum(row.worked_minutes for row in result.daily_results) < sum(
        row.worked_minutes for row in full_time.daily_results
    )


def test_the_overtime_persona_carries_a_forward_balance_with_linked_overtime(demo: Session) -> None:
    """A ledger-derived balance, not a stored one: the opening entry plus overtime."""
    employee = _employee(demo, "demo-overtime")
    result = calculate_employee_period(demo, employee, date(2026, 9, 1))
    assert result.closing_balance_minutes == 480

    ledger = demo.scalars(
        select(BalanceLedgerEntry).where(BalanceLedgerEntry.employee_id == employee.id)
    ).all()
    assert [entry.entry_type for entry in ledger] == ["CUTOVER_SEED"]
    assert ledger[0].minutes == 240

    # The overtime intervals link to the *decided* authorization, which is what
    # ``_validate_overtime_link`` accepts: an approval, not the employee's request.
    linked = demo.scalars(
        select(WorkInterval).where(
            WorkInterval.employee_id == employee.id,
            WorkInterval.overtime_authorization_id.is_not(None),
        )
    ).all()
    assert len(linked) == 2
    for interval in linked:
        authorization = demo.get(OvertimeAuthorization, interval.overtime_authorization_id)
        assert authorization is not None
        assert authorization.status == "APPROVED"
        assert authorization.source == "INSTITUTIONAL_APPROVAL"


def test_the_travel_persona_shows_a_topup_and_a_credit(demo: Session) -> None:
    """A trip alone has no effect (``travel/context.py`` drops segment-less trips)."""
    employee = _employee(demo, "demo-travel")
    assert _count(demo, BusinessTrip) >= 1
    assert _count(demo, TravelSegment) >= 2

    september = calculate_employee_period(demo, employee, date(2026, 9, 1))
    by_day = {row.day: row for row in september.daily_results}
    # The travel days carry no ordinary intervals, so the passive-travel top-up is
    # what closes them — the visible effect of the trip.
    for day in (date(2026, 9, 7), date(2026, 9, 9)):
        assert by_day[day].travel_topup_minutes == 348, day
        assert by_day[day].worked_minutes == 0
    assert september.closing_balance_minutes == 0

    # The credit is dated the month after the trip's one-month assessment window
    # closes: the window ends on 7 October, so the credit is effective 1 November.
    november = calculate_employee_period(demo, employee, date(2026, 11, 1))
    credits = [
        proposal
        for proposal in november.ledger_proposals
        if proposal.entry_type == "TRAVEL_CREDIT" and proposal.minutes
    ]
    assert [(proposal.minutes, proposal.occurred_on) for proposal in credits] == [
        (75, date(2026, 11, 1))
    ]


def test_the_day_status_persona_reduces_only_where_the_rule_says_so(demo: Session) -> None:
    """Vacation and sickness reduce the requirement; the compensation categories do not.

    ``REDUCING_CATEGORIES`` is ``{VACATION, SICK}``, so a FLEX_COMPENSATION or
    SHIFT_COMPENSATION day is recorded and reported but leaves ``required`` alone.
    The test pins that boundary rather than implying an effect the engine does not have.
    """
    employee = _employee(demo, "demo-flex")
    assert _count(demo, DayStatusEvent) >= 5
    by_day = {
        row.day: row
        for row in calculate_employee_period(demo, employee, date(2026, 9, 1)).daily_results
    }
    for day in (date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 21)):
        assert by_day[day].required_minutes == 0, day
    # The compensation categories are captured facts with no requirement effect.
    assert by_day[date(2026, 9, 22)].required_minutes == 348
    assert by_day[date(2026, 9, 23)].required_minutes == 348
    assert _rules(demo, "demo-flex") == set()


# ---------------------------------------------------------------------------
# The recipient side
# ---------------------------------------------------------------------------


def test_the_recipient_can_read_the_seeded_package_and_only_that(
    demo: Session, db_client: TestClient
) -> None:
    """A frozen, minimized package addressed to the demo recipient."""
    assert _count(demo, SubmissionPackage) == 1

    queue = db_client.get(RECIPIENT_QUEUE, headers=_headers("demo-recipient"))
    assert queue.status_code == 200, queue.text
    entries = queue.json()
    assert len(entries) == 1
    assert entries[0]["purpose"] == "SHIFT_REPORT"
    assert "payload" not in entries[0]

    detail = db_client.get(
        f"{RECIPIENT_QUEUE}/{entries[0]['id']}", headers=_headers("demo-recipient")
    )
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["payload"]
    # Employee-private findings are never part of a recipient projection (ACCESS-003).
    assert "findings" not in detail.text
    assert "recipients" not in detail.text

    # The negative controls: another persona's queue is empty, and the same package
    # is invisible to anyone it was not addressed to.
    assert db_client.get(RECIPIENT_QUEUE, headers=_headers("demo-employee")).status_code == 403
    foreign = db_client.get(f"{RECIPIENT_QUEUE}/{entries[0]['id']}", headers=_headers("demo-admin"))
    assert foreign.status_code in (403, 404)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_re_running_the_seed_duplicates_nothing(demo: Session) -> None:
    """Idempotent by construction: every fact carries a stable key.

    ``create_interval_instant`` has no replay path (its key is only a uniqueness
    guard) and ``seed_opening_balance`` is not an upsert, so both need the explicit
    check-then-skip this test protects.
    """
    tracked = (
        CalendarVersion,
        CalendarDay,
        Employee,
        WorkInterval,
        DayStatusEvent,
        BusinessTrip,
        TravelSegment,
        OvertimeAuthorization,
        BalanceLedgerEntry,
        SubmissionPackage,
    )
    before = {model.__name__: _count(demo, model) for model in tracked}
    assert before["WorkInterval"] > 0 and before["CalendarDay"] == 100

    seed_demo(demo)

    assert {model.__name__: _count(demo, model) for model in tracked} == before


# ---------------------------------------------------------------------------
# The September demo state (owner request, 2026-10-08)
# ---------------------------------------------------------------------------
#
# The demo tells a different story from the July one it used to: the service went
# live on 1 September, every persona was onboarded then, and the recorded facts run
# through the current day so the month IN PROGRESS is full. These pin the three
# properties that make that story work, because each one is invisible from the
# outside until it is wrong.


def test_every_persona_but_the_fact_free_one_is_onboarded_in_september(demo: Session) -> None:
    """One start state per persona, at the August boundary, and exactly one exception.

    The onboarding persona must NOT have one: its whole purpose is that the
    start-state form can be demonstrated live, and the clean-cutover guard refuses
    to establish one for an employee who already holds facts.
    """
    seeded = {
        row.employee_id: row
        for row in demo.scalars(
            select(BalanceLedgerEntry).where(BalanceLedgerEntry.entry_type == "CUTOVER_SEED")
        )
    }
    boundary = date(2026, 8, 31)  # DEMO_CUTOVER_MONTH - 1 day
    assert DEMO_CUTOVER_MONTH == "2026-09-01"
    for subject in DEMO_SUBJECTS:
        employee = _employee(demo, subject)
        if subject in FACT_FREE_PERSONAS:
            assert employee.id not in seeded, subject
            continue
        assert employee.id in seeded, subject
        assert seeded[employee.id].occurred_on == boundary, subject


def test_every_working_persona_has_facts_across_the_whole_demo_range(demo: Session) -> None:
    """September 1 through 8 October, so no month view is half-empty."""
    for subject in ALL_EMPLOYEE_PERSONAS:
        employee = _employee(demo, subject)
        days = {
            row.start_at.astimezone(BERLIN).date()
            for row in demo.scalars(
                select(WorkInterval).where(WorkInterval.employee_id == employee.id)
            )
        }
        assert days, subject
        assert min(days) <= DEMO_FROM + timedelta(days=7), (subject, min(days))
        # The demo range ends on the day the state was authored; the last recorded
        # day must sit in its final week, not months earlier.
        assert max(days) >= DEMO_TO - timedelta(days=7), (subject, max(days))


def test_the_shift_personas_carry_blocks_cancellations_and_breaks(demo: Session) -> None:
    """Blocks of three, a cancelled shift, and the compensated break inside a shift.

    All three are load-bearing for the demo. A cancelled shift is a PLAN kept
    ``PLANNED`` plus a separate ``CANCELLED`` deviation (INV-09) — never a status on
    the plan. And a shift with no recorded break is 8:30 of uninterrupted work,
    which the break rule refuses and SHIFT-003 asks the employee to confirm, so the
    break is part of the shift being correctly represented.
    """
    # The seed's own constants must agree with the literal windows above, or the
    # assertions below would be checking the seed against itself.
    assert list(SHIFT_WINDOWS) == STANDARD_SHIFT_WINDOWS
    assert SHIFT_BLOCK_DAYS == SHIFT_BLOCK
    for subject in ("demo-shift", "demo-hr", "demo-supervisor"):
        employee = _employee(demo, subject)
        shifts = list(
            demo.scalars(select(ShiftAssignment).where(ShiftAssignment.employee_id == employee.id))
        )
        assert len(shifts) >= 20, (subject, len(shifts))
        # Blocks of THREE IDENTICAL shifts in a row, then the next window — the
        # whole point of the roster, and invisible from a shift count. Only the
        # standard windows take part: `demo-shift` carries one extra scripted plan
        # on a day the rotation deliberately steps around.
        rotating = sorted(
            (row for row in shifts if _window(row) in STANDARD_SHIFT_WINDOWS),
            key=lambda row: row.planned_start_at,
        )
        assert rotating, subject
        runs: list[list[tuple[str, str]]] = []
        for row in rotating:
            window = _window(row)
            if runs and runs[-1][0] == window:
                runs[-1].append(window)
            else:
                runs.append([window])
        assert all(len(set(run)) == 1 for run in runs), (subject, runs)
        lengths = [len(run) for run in runs]
        assert all(length == SHIFT_BLOCK for length in lengths[:-1]), (subject, lengths)
        assert lengths[-1] <= SHIFT_BLOCK, (subject, lengths)
        # ... and the blocks rotate through all three windows in order.
        order = [run[0] for run in runs]
        assert set(order) == set(STANDARD_SHIFT_WINDOWS), (subject, order)
        for before, after in pairwise(order):
            expected = STANDARD_SHIFT_WINDOWS[
                (STANDARD_SHIFT_WINDOWS.index(before) + 1) % len(STANDARD_SHIFT_WINDOWS)
            ]
            assert expected == after, (subject, before, after)

        cancellations = demo.scalars(
            select(ShiftDeviation).where(
                ShiftDeviation.shift_assignment_id.in_([row.id for row in shifts]),
                ShiftDeviation.deviation_type == "CANCELLED",
            )
        ).all()
        assert len(cancellations) >= 1, subject
        # The plan is KEPT: a cancellation is a deviation, not a plan status.
        assert all(row.status == "PLANNED" for row in shifts), subject

        breaks = demo.scalars(
            select(BreakInterval).where(
                BreakInterval.employee_id == employee.id,
                BreakInterval.kind == "PAID_SHIFT_BREAK",
            )
        ).all()
        assert len(breaks) >= 20, (subject, len(breaks))
        linked = {row.id for row in shifts}
        assert all(row.linked_shift_assignment_id in linked for row in breaks), subject


def test_on_call_duties_are_spread_over_a_few_employees(demo: Session) -> None:
    """OCD is not one persona's exclusive story."""
    holders = {
        row.idp_subject
        for row in demo.scalars(select(Employee))
        if _count_where(demo, OcdAssignment, OcdAssignment.employee_id == row.id)
    }
    assert len(holders) >= 3, holders
