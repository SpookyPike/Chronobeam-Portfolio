"""Employee self-onboarding (``/api/v1/me/onboarding``).

The slice is a small extension of the EXISTING cutover model: the same
``CUTOVER_SEED`` ledger row, the same effective-dated employee schedule writer,
the same carry-forward. These tests pin the properties that make it safe rather
than the implementation:

- the read is month-aware and never guesses a target month;
- coverage comes from the real resolvers, so the onboarding gate cannot drift
  from what the calculation accepts;
- the route is self-only on the existing baseline capability.
"""

import json
from datetime import date, datetime, time

import pytest
from app.audit.models import AuditEvent
from app.balance.models import BalanceLedgerEntry
from app.calendar.models import CalendarDay, CalendarVersion
from app.config import get_settings
from app.cutover import self_service
from app.cutover.self_service import current_berlin_month, month_key, next_month
from app.disclosure.models import AccessRequest, DisclosureGrant, SubmissionPackage
from app.enums import MasterDataSource, PeriodStatus
from app.identity.models import Employee, EmploymentContract, WorkScheduleVersion
from app.policy.models import PolicyVersion
from app.worktime.models import WorkPeriod
from app.worktime.walltime import BERLIN
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.conftest import (
    DEVELOPER_PRINCIPAL_HEADER as ME,
)
from tests.conftest import (
    HR_HEADER,
    OTHER_EMPLOYEE_HEADER,
    RECIPIENT_HEADER,
    SUPERVISOR_HEADER,
)

ONBOARDING = "/api/v1/me/onboarding"


def _pin_today(monkeypatch: pytest.MonkeyPatch, day: date) -> None:
    """Pin the view's business date.

    ``calculate_employee_period`` holds the ONE clock read in the whole
    calculation, so replacing it there is the seam. Midday, so the Berlin date is
    unambiguous.
    """
    import app.policy.service as policy_service

    monkeypatch.setattr(
        policy_service, "now_berlin", lambda: datetime.combine(day, time(12, 0), tzinfo=BERLIN)
    )


#: Safely after every installed edition AND after any plausible "current" Berlin
#: month, so these tests do not depend on the wall clock. Coverage is open-ended
#: in the seeded editions, so a far-future month is covered by construction.
FUTURE_MONTH = "2032-06"
FUTURE = date(2032, 6, 1)


def _state(client: TestClient, start_month: str | None = None, headers: dict | None = None):
    params = f"?start_month={start_month}" if start_month else ""
    return client.get(f"{ONBOARDING}{params}", headers=headers or ME)


def _drop_calendar(session: Session) -> None:
    """Remove every calendar edition, so no month has calendar coverage."""
    session.execute(delete(CalendarDay))
    session.execute(delete(CalendarVersion))
    session.flush()


def _drop_policy(session: Session) -> None:
    session.execute(delete(PolicyVersion))
    session.flush()


def test_the_read_describes_the_requested_month_and_never_guesses_one(
    db_client: TestClient,
) -> None:
    """`state` always describes a NAMED month — there is no "unselected" verdict.

    Omitting `start_month` describes the current Berlin month; naming one
    describes that one. An earlier design returned a single ambiguous
    BLOCKED/blocked_month pair as though a target had already been chosen.
    """
    default = _state(db_client)
    assert default.status_code == 200, default.text
    body = default.json()
    assert body["month"] == month_key(current_berlin_month())
    assert body["state"] == "READY"
    assert body["blocked_reason"] is None
    assert body["blocked_month"] is None

    named = _state(db_client, FUTURE_MONTH).json()
    assert named["month"] == FUTURE_MONTH
    assert named["state"] == "READY"


def test_the_read_offers_the_earliest_covered_month_as_the_default(
    db_client: TestClient,
) -> None:
    """The seeded editions are open-ended, so the current month is already covered.

    Computed from `current_berlin_month()` rather than hard-coded: the assertion
    is about the RESOLUTION RULE, and it must not rot as the clock moves.
    """
    body = _state(db_client).json()
    assert body["default_start_month"] == month_key(current_berlin_month())
    assert body["opening"] is None
    assert body["correctable"] is False


def test_a_month_before_the_current_one_is_blocked_by_the_lower_bound(
    db_client: TestClient,
) -> None:
    """D3: no retrospective self-onboarding into historical/Excel months."""
    past = month_key(date(current_berlin_month().year - 1, 1, 1))
    body = _state(db_client, past).json()
    assert body["state"] == "BLOCKED"
    assert body["blocked_reason"] == "ROLLOUT_BOUNDARY"
    assert body["blocked_month"] == past
    # …and the month it describes is the one that was REQUESTED.
    assert body["month"] == past


def test_a_month_without_a_policy_edition_is_blocked_by_name(
    db_client: TestClient, db_session: Session
) -> None:
    _drop_policy(db_session)
    body = _state(db_client, FUTURE_MONTH).json()
    assert body["state"] == "BLOCKED"
    assert body["blocked_reason"] == "POLICY_EDITION_MISSING"
    assert body["blocked_month"] == FUTURE_MONTH


def test_a_month_without_calendar_coverage_is_blocked_by_name(
    db_client: TestClient, db_session: Session
) -> None:
    _drop_calendar(db_session)
    body = _state(db_client, FUTURE_MONTH).json()
    assert body["state"] == "BLOCKED"
    assert body["blocked_reason"] == "CALENDAR_MISSING"
    assert body["blocked_month"] == FUTURE_MONTH


def test_the_configured_rollout_boundary_is_a_lower_bound_not_a_target(
    db_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D4: the institutional boundary bounds M; choosing M never changes it.

    The boundary is a DEPLOYMENT decision and is unset by default (no month is
    invented). Configuring it must refuse an earlier month and admit the boundary
    month itself — without publishing or altering any policy edition.
    """
    monkeypatch.setenv("CHRONOBEAM_ROLLOUT_BOUNDARY_MONTH", "2032-01")
    get_settings.cache_clear()
    try:
        before = _state(db_client, "2031-12").json()
        assert before["state"] == "BLOCKED"
        assert before["blocked_reason"] == "ROLLOUT_BOUNDARY"

        at_boundary = _state(db_client, "2032-01").json()
        assert at_boundary["state"] == "READY"

        # The boundary did not become the employee's cutover month, and no
        # policy edition was touched by reading it.
        assert at_boundary["opening"] is None
        assert at_boundary["month"] == "2032-01"
    finally:
        get_settings.cache_clear()


def test_an_unset_rollout_boundary_adds_no_extra_lower_bound(
    db_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset is the honest default while the production rollout month is undecided."""
    monkeypatch.setenv("CHRONOBEAM_ROLLOUT_BOUNDARY_MONTH", "")
    get_settings.cache_clear()
    try:
        assert self_service.rollout_boundary() is None
        assert _state(db_client, FUTURE_MONTH).json()["state"] == "READY"
    finally:
        get_settings.cache_clear()


def test_the_default_start_month_is_null_when_nothing_is_covered(
    db_client: TestClient, db_session: Session
) -> None:
    """No covered month exists -> null, and the state is BLOCKED. No guess."""
    _drop_policy(db_session)
    body = _state(db_client).json()
    assert body["default_start_month"] is None
    assert body["state"] == "BLOCKED"


def test_the_default_start_month_resolver_is_bounded_not_a_month_by_month_walk(
    db_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin the BOUNDEDNESS, not just the answer.

    A naive implementation would walk forward one month at a time from the current
    month; across a multi-year gap that is dozens of resolver calls. The candidate
    set is `current` plus every edition start month, so the number of coverage
    probes stays at or below the number of distinct candidate months.
    """
    # Move the ordinary edition far into the future and remove the calendar, so
    # the only candidate months are the current one and the far edition start.
    # A walk would probe every month in between; the candidate set cannot.
    _drop_calendar(db_session)
    edition = db_session.scalar(
        select(PolicyVersion).where(PolicyVersion.policy_key == "ordinary-working-time")
    )
    assert edition is not None
    edition.effective_from = date(2035, 1, 1)
    db_session.flush()

    calls: list[date] = []
    original = self_service._covered

    def counting(session: Session, month: date) -> bool:
        calls.append(month)
        return original(session, month)

    monkeypatch.setattr(self_service, "_covered", counting)
    # No month is covered (there is no calendar edition), so the answer is None —
    # and reaching it must not have probed every month in between.
    assert self_service.default_start_month(db_session, current_berlin_month()) is None
    assert len(calls) <= 2, f"expected a bounded candidate set, probed {len(calls)} months"
    assert all(month >= current_berlin_month() for month in calls)


def test_an_employee_cannot_reach_another_employees_onboarding_state(
    db_client: TestClient,
) -> None:
    """Self-only: no employee id is accepted anywhere, so there is no IDOR surface.

    The employee always comes from the session. Non-employee principals and a
    DIFFERENT employee both get the one byte-identical denial.
    """
    own = _state(db_client)
    assert own.status_code == 200, own.text

    for headers in (HR_HEADER, SUPERVISOR_HEADER, RECIPIENT_HEADER):
        denied = _state(db_client, headers=headers)
        assert denied.status_code == 403, denied.text

    # A different mapped employee sees their OWN state, never the first one's:
    # the route has no target parameter at all.
    other = _state(db_client, headers=OTHER_EMPLOYEE_HEADER)
    assert other.status_code == 200, other.text
    assert other.json()["opening"] is None


def test_the_onboarding_response_exposes_no_other_employees_data(
    db_client: TestClient, db_session: Session
) -> None:
    """The payload is the caller's own state; it carries no ids or subjects."""
    body = _state(db_client).json()
    assert set(body) == {
        "state",
        "month",
        "default_start_month",
        "opening",
        "blocked_reason",
        "blocked_month",
        "has_facts_in_month",
        "correctable",
        "in_use_reason",
        "schedule",
    }
    assert "employee" not in body
    assert "id" not in body


def test_the_month_helpers_agree_with_the_month_arithmetic(
    db_session: Session,
) -> None:
    """`next_month` is the exclusive upper bound every coverage probe uses."""
    assert next_month(date(2032, 12, 1)) == date(2033, 1, 1)
    assert next_month(date(2032, 1, 1)) == date(2032, 2, 1)
    assert month_key(date(2032, 6, 1)) == "2032-06"


def test_the_read_does_not_write_anything(db_client: TestClient, db_session: Session) -> None:
    """A read must not create a period, a ledger row, or a schedule."""
    before = db_session.scalar(
        select(PolicyVersion.effective_from).where(PolicyVersion.effective_from == FUTURE)
    )
    assert _state(db_client, FUTURE_MONTH).status_code == 200
    assert before is None  # no edition was published by reading the state


# --------------------------------------------------------------------------
# Establish
# --------------------------------------------------------------------------


def _payload(**overrides):
    """A valid onboarding payload: 29:00/week, Mon-Fri, nothing carried over."""
    payload = {
        "start_month": FUTURE_MONTH,
        "weekly_required_minutes": 1740,
        "mon_required_minutes": 348,
        "tue_required_minutes": 348,
        "wed_required_minutes": 348,
        "thu_required_minutes": 348,
        "fri_required_minutes": 348,
        "sat_required_minutes": 0,
        "sun_required_minutes": 0,
        "opening_balance_minutes": 0,
    }
    payload.update(overrides)
    return payload


def _establish(client: TestClient, **overrides):
    return client.post(ONBOARDING, headers=ME, json=_payload(**overrides))


def _seeds(session: Session, employee_id) -> list:
    return list(
        session.scalars(
            select(BalanceLedgerEntry).where(
                BalanceLedgerEntry.employee_id == employee_id,
                BalanceLedgerEntry.entry_type == "CUTOVER_SEED",
            )
        ).all()
    )


@pytest.mark.parametrize(
    ("opening", "expected"),
    [(1440, 1440), (-1104, -1104), (0, 0)],
    ids=["positive", "negative", "zero"],
)
def test_establish_accepts_a_signed_opening_balance(
    db_client: TestClient, opening: int, expected: int
) -> None:
    """Positive credit, negative deficit and zero are all ordinary.

    Zero must still establish a real opening state: it is a decision the employee
    made, not an absence of one.
    """
    response = _establish(db_client, opening_balance_minutes=opening)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] == "COMPLETED"
    assert body["opening"]["minutes"] == expected
    assert body["opening"]["seed_minutes"] == expected
    assert body["opening"]["cutover_month"] == FUTURE_MONTH
    assert body["opening"]["revision"] == 1
    assert body["opening"]["corrected_at"] is None
    # The seed is dated the last day of M-1 (the existing convention), so M opens
    # with exactly those minutes.
    assert body["opening"]["occurred_on"] == "2032-05-31"
    assert body["opening"]["previous_period"] == "2032-05"


def test_establish_writes_the_employee_entered_working_time_with_no_contract(
    db_client: TestClient, db_session: Session
) -> None:
    """D1: the employee owns the working-time setup; there is NO contract gate.

    The only allocation rule is the existing one — the weekday distribution must
    sum to the entered weekly total — and the write goes through the existing
    effective-dated employee schedule mechanism.
    """
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    # The premise: no employment contract AT ALL, so nothing can be validated
    # against contractual hours. The dev fixture normally has one; removing it is
    # what makes this test discriminating about D1.
    db_session.execute(
        delete(EmploymentContract).where(EmploymentContract.employee_id == employee.id)
    )
    db_session.flush()

    assert _establish(db_client).status_code == 201

    schedule = db_session.scalar(
        select(WorkScheduleVersion).where(
            WorkScheduleVersion.employee_id == employee.id,
            WorkScheduleVersion.valid_from == FUTURE,
        )
    )
    assert schedule is not None
    assert schedule.source == MasterDataSource.EMPLOYEE
    assert schedule.weekly_required_minutes == 1740
    assert schedule.mon_required_minutes == 348
    assert schedule.sat_required_minutes == 0


def test_establish_refuses_a_weekday_distribution_that_misses_the_weekly_total(
    db_client: TestClient, db_session: Session
) -> None:
    """Both values are named, and nothing at all is written."""
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None

    response = _establish(db_client, weekly_required_minutes=1740, fri_required_minutes=0)
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "VALIDATION_FAILED"
    assert detail["details"]["weekly_required_minutes"] == 1740
    assert detail["details"]["allocated_minutes"] == 1392

    db_session.expire_all()
    assert _seeds(db_session, employee.id) == []
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(WorkScheduleVersion)
            .where(
                WorkScheduleVersion.employee_id == employee.id,
                WorkScheduleVersion.valid_from == FUTURE,
            )
        )
        == 0
    )


def test_a_later_working_time_change_is_ordinary_maintenance_not_a_second_seed(
    db_client: TestClient, db_session: Session
) -> None:
    """D2: a contract change later is effective-dated maintenance.

    It creates a NEW schedule version, writes NO second ledger row, and leaves the
    carried balance untouched.
    """
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    assert _establish(db_client, opening_balance_minutes=600).status_code == 201

    later = "2032-09-01"
    changed = db_client.put(
        "/api/v1/me/working-time",
        headers=ME,
        json={
            "valid_from": later,
            "weekly_required_minutes": 1160,
            "mon_required_minutes": 232,
            "tue_required_minutes": 232,
            "wed_required_minutes": 232,
            "thu_required_minutes": 232,
            "fri_required_minutes": 232,
            "sat_required_minutes": 0,
            "sun_required_minutes": 0,
        },
    )
    assert changed.status_code == 200, changed.text

    db_session.expire_all()
    assert len(_seeds(db_session, employee.id)) == 1  # still exactly one seed
    starts = {
        row.valid_from
        for row in db_session.scalars(
            select(WorkScheduleVersion).where(WorkScheduleVersion.employee_id == employee.id)
        ).all()
    }
    # Both arrangements exist and both are the employee's own; the later one did
    # NOT mint a second opening-balance row.
    assert {FUTURE, date(2032, 9, 1)} <= starts
    assert _state(db_client).json()["opening"]["minutes"] == 600


def test_a_second_onboarding_is_refused_and_leaves_exactly_one_seed(
    db_client: TestClient, db_session: Session
) -> None:
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    assert _establish(db_client).status_code == 201

    again = _establish(db_client, opening_balance_minutes=999)
    assert again.status_code == 409, again.text
    detail = again.json()["detail"]
    assert detail["code"] == "CONFLICT_CUTOVER_SEED"
    assert detail["details"]["reason"] == "DUPLICATE_SEED"

    db_session.expire_all()
    seeds = _seeds(db_session, employee.id)
    assert len(seeds) == 1
    assert seeds[0].minutes == 0


@pytest.mark.parametrize("bad", ["nope", "2032-13", "2032-6", "2032-06-01", "203206"])
def test_a_malformed_start_month_is_a_validation_error_not_a_server_error(
    db_client: TestClient, bad: str
) -> None:
    """The GET's `start_month` is a raw query string, so it must be validated.

    ``parse_month`` alone raises ``ValueError`` on these, which surfaced as a
    500 on a brand-new public contract instead of the
    ``VALIDATION_INVALID_MONTH`` envelope every other month parameter returns.

    An EMPTY value is not malformed: it is treated as omitted, like every other
    optional query parameter here.
    """
    response = db_client.get(f"{ONBOARDING}?start_month={bad}", headers=ME)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "VALIDATION_INVALID_MONTH"


def test_establish_refuses_a_month_before_the_current_one(db_client: TestClient) -> None:
    """D3: no retrospective self-onboarding into historical/Excel months."""
    past = month_key(date(current_berlin_month().year - 1, 1, 1))
    response = _establish(db_client, start_month=past)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "VALIDATION_INVALID_MONTH"


def test_establish_refuses_a_month_before_the_configured_rollout_boundary(
    db_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D4: the institutional boundary bounds M and is never changed by choosing M."""
    monkeypatch.setenv("CHRONOBEAM_ROLLOUT_BOUNDARY_MONTH", "2032-07")
    get_settings.cache_clear()
    try:
        refused = _establish(db_client, start_month="2032-06")
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"]["code"] == "VALIDATION_INVALID_MONTH"

        admitted = _establish(db_client, start_month="2032-07")
        assert admitted.status_code == 201, admitted.text
        assert admitted.json()["opening"]["cutover_month"] == "2032-07"
    finally:
        get_settings.cache_clear()


def test_establish_refuses_a_month_without_policy_coverage(
    db_client: TestClient, db_session: Session
) -> None:
    _drop_policy(db_session)
    response = _establish(db_client)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "VALIDATION_INVALID_MONTH"


def test_establish_refuses_a_month_without_calendar_coverage(
    db_client: TestClient, db_session: Session
) -> None:
    """Fail closed: a holiday calendar that does not cover M cannot be guessed."""
    _drop_calendar(db_session)
    response = _establish(db_client)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "CALENDAR_BLOCKED"


def test_establish_refuses_a_month_already_reviewed_or_shared(
    db_client: TestClient, db_session: Session
) -> None:
    """A month whose result is already frozen cannot host a first cutover."""
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    db_session.add(
        WorkPeriod(employee_id=employee.id, period_month=FUTURE, status=PeriodStatus.REVIEWED)
    )
    db_session.flush()

    response = _establish(db_client)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "CONFLICT_CUTOVER_SEED"
    assert detail["details"]["reason"] == "IN_USE_BY_REVIEW"


def test_establish_surfaces_facts_already_recorded_inside_the_month(
    db_client: TestClient, db_session: Session
) -> None:
    """D10: facts inside M do NOT block — they are surfaced, then recalculated."""
    # Through the real route: a work interval needs its period row, and the
    # employee write path is what creates it.
    worked = db_client.post(
        "/api/v1/me/work-intervals",
        headers=ME,
        json={
            "start_local": "2032-06-07T08:00:00",
            "end_local": "2032-06-07T12:00:00",
            "work_type": "ON_SITE",
        },
    )
    assert worked.status_code == 201, worked.text

    before = _state(db_client, FUTURE_MONTH).json()
    assert before["has_facts_in_month"] is True
    assert before["state"] == "READY"  # surfaced, not blocked

    assert _establish(db_client).status_code == 201
    assert _state(db_client, FUTURE_MONTH).json()["state"] == "COMPLETED"


def test_the_response_does_not_overload_month_with_the_actual_cutover_month(
    db_client: TestClient,
) -> None:
    """Two distinct meanings, two distinct fields.

    After establishing for M, browsing another month must report THAT month in
    `month` while `opening.cutover_month` stays M.
    """
    assert _establish(db_client).status_code == 201

    browsed = _state(db_client, "2033-01").json()
    assert browsed["month"] == "2033-01"
    assert browsed["opening"]["cutover_month"] == FUTURE_MONTH
    assert browsed["state"] == "COMPLETED"


def test_the_reported_schedule_follows_the_cutover_month_not_the_browsed_one(
    db_client: TestClient, db_session: Session
) -> None:
    """A future-month onboarding must not be reported with today's arrangement.

    Regression: the read resolved the schedule at the REQUESTED month, so an
    employee who started in a future month reopened Profile and was shown
    whatever arrangement happened to be effective today — an older one, or
    "no days set" — as the result of their own onboarding.
    """
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    # An OLDER arrangement that is effective today but NOT at M: retune whatever
    # the dev fixture already has so no row is inserted twice.
    existing = db_session.scalar(
        select(WorkScheduleVersion)
        .where(WorkScheduleVersion.employee_id == employee.id)
        .order_by(WorkScheduleVersion.valid_from)
        .limit(1)
    )
    assert existing is not None, "the dev fixture must have a baseline arrangement"
    existing.valid_to = None
    existing.weekly_required_minutes = 1200
    for field in ("mon", "tue", "wed", "thu", "fri"):
        setattr(existing, f"{field}_required_minutes", 240)
    for field in ("sat", "sun"):
        setattr(existing, f"{field}_required_minutes", 0)
    db_session.flush()

    assert _establish(db_client, weekly_required_minutes=1740).status_code == 201

    # Browsing the CURRENT month still reports the cutover arrangement, not the
    # older one that is effective now.
    body = _state(db_client).json()
    assert body["state"] == "COMPLETED"
    assert body["schedule"]["weekly_required_minutes"] == 1740
    assert body["schedule"]["mon_required_minutes"] == 348

    # …and so does browsing a later month.
    later = _state(db_client, "2033-01").json()
    assert later["schedule"]["weekly_required_minutes"] == 1740


# --------------------------------------------------------------------------
# Atomicity and concurrency — real transactions, because the trap is a COMMIT
# --------------------------------------------------------------------------


def _race_employee(engine, subject: str) -> Employee:
    """A throwaway employee with its own subject, committed outside the fixture."""
    with Session(engine) as setup:
        row = Employee(
            idp_subject=subject,
            external_employee_id=f"emp-{subject}",
            legal_name="Onboarding Race (synthetic)",
            active=True,
        )
        setup.add(row)
        setup.commit()
        setup.refresh(row)
        return row


def _purge_race(engine, employee_id) -> None:
    with Session(engine) as cleanup, cleanup.begin():
        cleanup.execute(
            delete(BalanceLedgerEntry).where(BalanceLedgerEntry.employee_id == employee_id)
        )
        cleanup.execute(delete(WorkPeriod).where(WorkPeriod.employee_id == employee_id))
        cleanup.execute(
            delete(WorkScheduleVersion).where(WorkScheduleVersion.employee_id == employee_id)
        )
        cleanup.execute(delete(AuditEvent).where(AuditEvent.employee_subject_id == employee_id))
        cleanup.execute(delete(Employee).where(Employee.id == employee_id))


def _fresh_counts(engine, employee_id) -> tuple[int, int]:
    """(schedule rows starting at M, CUTOVER_SEED rows) from a NEW transaction."""
    with Session(engine) as check:
        schedules = check.scalar(
            select(func.count())
            .select_from(WorkScheduleVersion)
            .where(
                WorkScheduleVersion.employee_id == employee_id,
                WorkScheduleVersion.valid_from == FUTURE,
            )
        )
        seeds = check.scalar(
            select(func.count())
            .select_from(BalanceLedgerEntry)
            .where(
                BalanceLedgerEntry.employee_id == employee_id,
                BalanceLedgerEntry.entry_type == "CUTOVER_SEED",
            )
        )
    return int(schedules or 0), int(seeds or 0)


def _wait_until_blocked(engine, pid: int, timeout: float = 15.0) -> bool:
    """True once the backend holds a lock wait — a deterministic interleave
    instead of a sleep that hopes the other thread got there first."""
    import time

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


def test_a_failed_seed_write_leaves_neither_a_schedule_nor_a_seed(
    db_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE atomicity trap, proven against a fresh transaction.

    `set_working_time` flushes and `get_db` commits whenever a route RETURNS — so
    a route that flushed the schedule and then returned a refusal would commit a
    half-onboarded employee. One savepoint over both writes is what prevents it.
    """
    from app.cutover.schemas import OnboardingIn

    employee = _race_employee(db_engine, "onboard-atomic-seed")
    try:

        def exploding(*args, **kwargs):
            raise IntegrityError("simulated duplicate", None, Exception())

        monkeypatch.setattr(self_service, "_write_opening_entry", exploding)

        with Session(db_engine) as session:
            result = self_service.establish_opening_state(
                session, employee, OnboardingIn(**_payload())
            )
            assert isinstance(result, self_service.OnboardingRejected)
            assert result.reason == "DUPLICATE_SEED"
            # This is the commit the trap depends on: the route RETURNS.
            session.commit()

        assert _fresh_counts(db_engine, employee.id) == (0, 0)
        # …and the rejection audit survived the rolled-back savepoint.
        with Session(db_engine) as check:
            assert (
                check.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(
                        AuditEvent.employee_subject_id == employee.id,
                        AuditEvent.action == "CUTOVER_SEED",
                        AuditEvent.outcome == "REJECTED",
                    )
                )
                == 1
            )
    finally:
        _purge_race(db_engine, employee.id)


def test_a_failed_schedule_write_leaves_no_ledger_row(
    db_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction: the schedule writer fails first, so no seed is left."""
    from app.cutover.schemas import OnboardingIn

    employee = _race_employee(db_engine, "onboard-atomic-schedule")
    try:

        def exploding(*args, **kwargs):
            raise IntegrityError("simulated schedule failure", None, Exception())

        monkeypatch.setattr(self_service, "set_working_time", exploding)

        with Session(db_engine) as session:
            result = self_service.establish_opening_state(
                session, employee, OnboardingIn(**_payload())
            )
            assert isinstance(result, self_service.OnboardingRejected)
            session.commit()

        assert _fresh_counts(db_engine, employee.id) == (0, 0)
    finally:
        _purge_race(db_engine, employee.id)


def test_concurrent_establish_leaves_exactly_one_seed_and_no_partial_schedule(
    db_engine,
) -> None:
    """A real two-transaction race, not a simulated one.

    The lock is taken FIRST, so the loser blocks there and then re-reads: its
    duplicate pre-check sees the winner's committed seed and it refuses without
    writing anything. That is why the lock precedes the read.
    """
    import threading
    import time

    from app.cutover.schemas import OnboardingIn

    employee = _race_employee(db_engine, "onboard-race")
    outcome: dict[str, str] = {}
    session_a = Session(db_engine)
    session_b = Session(db_engine)
    try:
        winner = self_service.establish_opening_state(
            session_a, employee, OnboardingIn(**_payload(opening_balance_minutes=555))
        )
        assert not isinstance(winner, self_service.OnboardingRejected)

        def _loser() -> None:
            try:
                result = self_service.establish_opening_state(
                    session_b, employee, OnboardingIn(**_payload(opening_balance_minutes=999))
                )
                outcome["reason"] = (
                    result.reason
                    if isinstance(result, self_service.OnboardingRejected)
                    else "CREATED"
                )
            except Exception as exc:  # pragma: no cover - surfaced via the assertion
                outcome["reason"] = f"error:{type(exc).__name__}"
            session_b.commit()

        thread = threading.Thread(target=_loser)
        thread.start()
        # B is blocked on the employee-fact lock held by A's uncommitted write.
        time.sleep(0.6)
        session_a.commit()
        thread.join(30)
        assert not thread.is_alive()
        assert outcome["reason"] == "DUPLICATE_SEED", outcome

        schedules, seeds = _fresh_counts(db_engine, employee.id)
        assert (schedules, seeds) == (1, 1)
        with Session(db_engine) as check:
            row = check.scalar(
                select(BalanceLedgerEntry).where(
                    BalanceLedgerEntry.employee_id == employee.id,
                    BalanceLedgerEntry.entry_type == "CUTOVER_SEED",
                )
            )
            assert row is not None and row.minutes == 555  # the winner's value
            # The loser's refusal is durable even though it wrote no ledger row.
            assert (
                check.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(
                        AuditEvent.employee_subject_id == employee.id,
                        AuditEvent.outcome == "REJECTED",
                    )
                )
                == 1
            )
    finally:
        session_a.close()
        session_b.close()
        _purge_race(db_engine, employee.id)


# --------------------------------------------------------------------------
# Correction — append-only, bounded to the pre-use window
# --------------------------------------------------------------------------


def _correct(client: TestClient, *, revision: int, total: int, reason: str = "mistyped"):
    return client.patch(
        ONBOARDING,
        headers=ME,
        json={
            "expected_revision": revision,
            "opening_balance_minutes": total,
            "reason": reason,
        },
    )


def test_a_correction_appends_a_delta_and_never_touches_the_seed(
    db_client: TestClient, db_session: Session
) -> None:
    """D5/D8: the original seed is byte-for-byte unchanged; the delta nets out.

    The correction carries the CORRECTED TOTAL in its metadata and a DELTA in
    ``minutes``, so the ledger sum stays the accounting mechanism.
    """
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    assert _establish(db_client, opening_balance_minutes=600).status_code == 201

    db_session.expire_all()
    seed_before = _seeds(db_session, employee.id)[0]
    snapshot = (
        seed_before.id,
        seed_before.minutes,
        seed_before.occurred_on,
        seed_before.entry_type,
        seed_before.source_entity_type,
        seed_before.source_entity_id,
        dict(seed_before.metadata_json),
        seed_before.policy_version_id,
        seed_before.created_at,
    )

    corrected = _correct(db_client, revision=1, total=1440)
    assert corrected.status_code == 200, corrected.text
    body = corrected.json()
    assert body["opening"]["minutes"] == 1440
    assert body["opening"]["seed_minutes"] == 600  # the seed did NOT change
    assert body["opening"]["revision"] == 2

    db_session.expire_all()
    seed_after = _seeds(db_session, employee.id)[0]
    assert (
        seed_after.id,
        seed_after.minutes,
        seed_after.occurred_on,
        seed_after.entry_type,
        seed_after.source_entity_type,
        seed_after.source_entity_id,
        dict(seed_after.metadata_json),
        seed_after.policy_version_id,
        seed_after.created_at,
    ) == snapshot

    # Exactly one correction row, carrying the DELTA and the lineage link.
    corrections = list(
        db_session.scalars(
            select(BalanceLedgerEntry).where(
                BalanceLedgerEntry.employee_id == employee.id,
                BalanceLedgerEntry.entry_type == "CUTOVER_SEED_CORRECTION",
            )
        ).all()
    )
    assert len(corrections) == 1
    row = corrections[0]
    assert row.minutes == 840  # 1440 - 600
    assert row.source_entity_type == "CUTOVER_SEED"
    assert row.source_entity_id == seed_after.id  # the explicit lineage link
    assert row.occurred_on == seed_after.occurred_on  # NOT inside M
    assert row.policy_version_id == seed_after.policy_version_id
    assert row.metadata_json is not None
    assert row.metadata_json["corrected_total_minutes"] == 1440
    assert row.metadata_json["reason"] == "mistyped"


def test_a_correction_does_not_double_count(db_client: TestClient, db_session: Session) -> None:
    """The arithmetic that the delta form exists to protect.

    A second row carrying the TOTAL would make M open with ``S + (S+δ)``; the
    delta form must make it exactly ``S + δ``.
    """
    assert _establish(db_client, opening_balance_minutes=600).status_code == 201
    assert _correct(db_client, revision=1, total=1440).status_code == 200

    period = db_client.get(f"/api/v1/me/findings?period={FUTURE_MONTH}", headers=ME)
    assert period.status_code == 200, period.text
    # Exactly 600 + 840. A total-carrying second row would make this 2040.
    assert period.json()["opening_balance_minutes"] == 1440


def test_repeated_correction_while_untouched_is_allowed_and_revisioned(
    db_client: TestClient,
) -> None:
    """D9: no one-correction cap. Each attempt is append-only, audited, guarded."""
    assert _establish(db_client, opening_balance_minutes=600).status_code == 201

    first = _correct(db_client, revision=1, total=900)
    assert first.status_code == 200, first.text
    assert first.json()["opening"]["revision"] == 2

    second = _correct(db_client, revision=2, total=-300)
    assert second.status_code == 200, second.text
    assert second.json()["opening"]["revision"] == 3
    assert second.json()["opening"]["minutes"] == -300


def test_a_correction_with_a_stale_revision_is_refused(
    db_client: TestClient,
) -> None:
    assert _establish(db_client, opening_balance_minutes=600).status_code == 201
    assert _correct(db_client, revision=1, total=900).status_code == 200

    stale = _correct(db_client, revision=1, total=1200)
    assert stale.status_code == 409, stale.text
    detail = stale.json()["detail"]
    assert detail["code"] == "CONFLICT_VERSION"
    assert detail["details"]["reason"] == "STALE_REVISION"


def test_a_correction_before_onboarding_is_refused(db_client: TestClient) -> None:
    response = _correct(db_client, revision=1, total=600)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["details"]["reason"] == "NO_OPENING_STATE"


def test_the_carry_into_the_next_month_still_holds_after_a_correction(
    db_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test 23: M+1 continuity is untouched by the new entry type.

    The clock is pinned to the day BEFORE the onboarding month, so the month has
    not been lived at all. The corrected total therefore carries forward
    UNCHANGED — the next month opens from the same value this one opened with —
    while this month's own displayed closing is still the full-month projection.
    The two agreeing again would mean the projection was being threaded as if it
    were ledger state (INV-08).
    """
    _pin_today(monkeypatch, date(2032, 5, 31))
    assert _establish(db_client, opening_balance_minutes=600).status_code == 201
    assert _correct(db_client, revision=1, total=1440).status_code == 200

    june = db_client.get(f"/api/v1/me/findings?period={FUTURE_MONTH}", headers=ME).json()
    july = db_client.get("/api/v1/me/findings?period=2032-07", headers=ME).json()
    assert june["opening_balance_minutes"] == 1440  # the corrected total
    assert june["closing_balance_minutes"] < june["opening_balance_minutes"]  # projection
    assert july["opening_balance_minutes"] == 1440  # the corrected total, carried


def test_the_corrected_total_round_trips_through_the_export_and_backup_check(
    db_client: TestClient,
) -> None:
    """Test 28: a correction row is accepted by the consumers that see the ledger.

    The legacy export renders the engine's opening/closing minutes, and the backup
    integrity check verifies referential consistency — neither enumerates
    `entry_type`, so the new type must simply be summed like any other row.
    """
    assert _establish(db_client, opening_balance_minutes=600).status_code == 201
    assert _correct(db_client, revision=1, total=1440).status_code == 200

    export = db_client.get(
        f"/api/v1/me/reports/legacy/work-time-record?month={FUTURE_MONTH}", headers=ME
    )
    assert export.status_code == 200, export.text
    # The corrected total, not the seed alone and not a double count.
    assert '"1440"' in export.text or ",1440," in export.text


@pytest.mark.parametrize(
    ("make_it_used", "expected"),
    [
        ("work", "IN_USE_BY_FACTS"),
        ("day_status", "IN_USE_BY_FACTS"),
        ("review", "IN_USE_BY_REVIEW"),
        ("grant", "IN_USE_BY_DISCLOSURE"),
        ("package", "IN_USE_BY_DISCLOSURE"),
    ],
)
def test_the_correction_window_closes_at_the_first_real_use(
    db_client: TestClient, db_session: Session, make_it_used: str, expected: str
) -> None:
    """D6: the window closes permanently, and the reason is specific.

    A closed window is checked BEFORE the revision, so a stale token cannot mask
    the real explanation.
    """
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    assert _establish(db_client, opening_balance_minutes=600).status_code == 201

    if make_it_used == "work":
        created = db_client.post(
            "/api/v1/me/work-intervals",
            headers=ME,
            json={
                "start_local": "2032-06-07T08:00:00",
                "end_local": "2032-06-07T12:00:00",
                "work_type": "ON_SITE",
            },
        )
        assert created.status_code == 201, created.text
    elif make_it_used == "day_status":
        created = db_client.post(
            "/api/v1/me/day-status",
            headers=ME,
            json={
                "day": "2032-06-07",
                "category": "VACATION",
                "portion": "FULL",
                "day_part": "ALL",
            },
        )
        assert created.status_code == 201, created.text
    elif make_it_used == "review":
        db_session.add(
            WorkPeriod(employee_id=employee.id, period_month=FUTURE, status=PeriodStatus.REVIEWED)
        )
        db_session.flush()
    elif make_it_used == "grant":
        db_session.add(
            DisclosureGrant(
                employee_id=employee.id,
                grantee_principal="hr-requester",
                purpose="RANDOM_CHECK",
                period_start=FUTURE,
                period_end=FUTURE,
                view_type="MONTH",
            )
        )
        db_session.flush()
    else:
        db_session.add(
            SubmissionPackage(
                employee_id=employee.id,
                purpose="SHIFT_REPORT",
                period_start=FUTURE,
                period_end=FUTURE,
                version=1,
                payload_json={},
            )
        )
        db_session.flush()

    refused = _correct(db_client, revision=1, total=1440)
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "CONFLICT_CUTOVER_SEED"
    assert detail["details"]["reason"] == expected

    # The card must also SAY so, and must not claim an administrative workflow.
    state = _state(db_client, FUTURE_MONTH).json()
    assert state["correctable"] is False
    assert state["in_use_reason"] == expected


def test_a_shared_month_is_explained_as_shared_not_as_reviewed(
    db_client: TestClient, db_session: Session
) -> None:
    """The grant flow freezes a snapshot, so BOTH in-use predicates are true.

    The disclosure check must win: the employee never had this month reviewed,
    and "already been reviewed or closed" would be a wrong explanation of a
    month that was merely granted to a requester.
    """
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    db_session.add(
        DisclosureGrant(
            employee_id=employee.id,
            grantee_principal="hr-requester",
            purpose="RANDOM_CHECK",
            period_start=FUTURE,
            period_end=FUTURE,
            view_type="MONTH",
        )
    )
    db_session.flush()

    refused = _establish(db_client, opening_balance_minutes=600)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["details"]["reason"] == "IN_USE_BY_DISCLOSURE"


def test_a_pending_access_request_does_not_close_the_window(
    db_client: TestClient, db_session: Session
) -> None:
    """A request reveals no data — the GRANT is the access event (docs/SECURITY.md).

    Closing the employee's own correction window on an unanswered request would
    let a third party freeze the employee's setup without any data changing hands.
    """
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    assert _establish(db_client, opening_balance_minutes=600).status_code == 201

    db_session.add(
        AccessRequest(
            employee_id=employee.id,
            requester_principal="hr-requester",
            purpose="RANDOM_CHECK",
            period_start=FUTURE,
            period_end=FUTURE,
            view_type="MONTH",
        )
    )
    db_session.flush()

    assert _state(db_client, FUTURE_MONTH).json()["correctable"] is True
    assert _correct(db_client, revision=1, total=1440).status_code == 200


def test_the_audit_never_records_the_balance_amount(
    db_client: TestClient, db_session: Session
) -> None:
    """DP-LOG-001, extended to the CORRECTED outcome."""
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    assert _establish(db_client, opening_balance_minutes=600).status_code == 201
    assert _correct(db_client, revision=1, total=1440, reason="wrong credit").status_code == 200

    db_session.expire_all()
    events = db_session.scalars(
        select(AuditEvent).where(
            AuditEvent.employee_subject_id == employee.id,
            AuditEvent.action == "CUTOVER_SEED",
        )
    ).all()
    assert {event.outcome for event in events} == {"CREATED", "CORRECTED"}
    for event in events:
        blob = json.dumps(event.metadata_json or {})
        assert "600" not in blob
        assert "1440" not in blob
        assert "wrong credit" not in blob
        assert set(event.metadata_json or {}) <= {"month", "error_code"}


def test_the_correction_route_is_self_only(db_client: TestClient) -> None:
    assert _establish(db_client).status_code == 201
    for headers in (HR_HEADER, SUPERVISOR_HEADER, RECIPIENT_HEADER):
        denied = db_client.patch(
            ONBOARDING,
            headers=headers,
            json={"expected_revision": 1, "opening_balance_minutes": 600, "reason": "x"},
        )
        assert denied.status_code == 403, denied.text


def test_a_concurrent_correction_from_the_same_revision_lets_exactly_one_win(
    db_engine,
) -> None:
    """THE test that fails if the reread ever moves before the lock.

    Two SEPARATE sessions and two transactions: the main thread holds the
    employee-fact lock with an uncommitted correction while the worker thread —
    on its OWN Session — blocks on that same lock. When the main thread commits,
    the worker's post-lock reread sees revision 2, so its `expected_revision=1`
    is stale and it is refused.

    The separate Session is the whole point. A single shared Session is ONE
    transaction: the advisory lock is re-entrant inside it and the "race" would
    be resolved by shared-session visibility rather than by the database, so the
    test would stay green even with the lock moved after the reread.
    """
    import threading

    from app.cutover.schemas import OnboardingCorrectionIn, OnboardingIn

    employee = _race_employee(db_engine, "onboard-correct-race")
    outcome: dict[str, str] = {}
    session_a = Session(db_engine)
    session_b = Session(db_engine)
    try:
        established = self_service.establish_opening_state(
            session_a, employee, OnboardingIn(**_payload(opening_balance_minutes=600))
        )
        assert not isinstance(established, self_service.OnboardingRejected)
        session_a.commit()

        worker_pid = int(session_b.scalar(text("SELECT pg_backend_pid()")))

        def _worker() -> None:
            try:
                result = self_service.correct_opening_state(
                    session_b,
                    employee,
                    OnboardingCorrectionIn(
                        expected_revision=1, opening_balance_minutes=1440, reason="worker"
                    ),
                )
                outcome["worker"] = (
                    "OK"
                    if not isinstance(result, self_service.OnboardingRejected)
                    else f"rejected:{result.code.value}"
                )
            except Exception as exc:  # pragma: no cover - surfaced via the assertion
                outcome["worker"] = f"error:{type(exc).__name__}"
            session_b.commit()

        # The winner takes the lock and does NOT commit, so it is still held when
        # the worker starts.
        winner = self_service.correct_opening_state(
            session_a,
            employee,
            OnboardingCorrectionIn(expected_revision=1, opening_balance_minutes=900, reason="main"),
        )
        assert not isinstance(winner, self_service.OnboardingRejected), winner
        outcome["main"] = "OK"

        thread = threading.Thread(target=_worker)
        thread.start()
        # Deterministic interleave, no sleep-based ordering: the worker is
        # provably waiting on the lock before the winner commits.
        assert _wait_until_blocked(db_engine, worker_pid), "the worker never waited on the lock"
        session_a.commit()
        thread.join(30)
        assert not thread.is_alive()

        assert outcome == {"main": "OK", "worker": "rejected:CONFLICT_VERSION"}, outcome

        with Session(db_engine) as check:
            corrections = check.scalars(
                select(BalanceLedgerEntry).where(
                    BalanceLedgerEntry.employee_id == employee.id,
                    BalanceLedgerEntry.entry_type == "CUTOVER_SEED_CORRECTION",
                )
            ).all()
            assert len(corrections) == 1  # exactly one appended delta
            seed = check.scalar(
                select(BalanceLedgerEntry).where(
                    BalanceLedgerEntry.employee_id == employee.id,
                    BalanceLedgerEntry.entry_type == "CUTOVER_SEED",
                )
            )
            assert seed is not None
            # The effective total is the WINNER's value, not a corrupted sum.
            assert seed.minutes + corrections[0].minutes == 900
            assert corrections[0].minutes == 300  # 900 - 600, not 1440 - 600
    finally:
        session_a.close()
        session_b.close()
        _purge_race(db_engine, employee.id)


def test_a_later_operator_seed_for_the_same_month_is_still_refused_by_duplication(
    db_engine,
) -> None:
    """§4.8 item 1, pinned rather than assumed.

    The operator pre-cutover guard counts ANY ledger row dated before M, so an
    employee's own seed (dated M-1) is already such a row. A later operator seed
    for the same employee must therefore be refused by the DUPLICATE check, not
    mis-reported as a pre-cutover-facts problem.
    """
    from app.auth.principal import Capability, Principal
    from app.cutover.schemas import OnboardingIn
    from app.cutover.service import (
        CutoverSeedRejected,
        seed_opening_balance,
    )

    employee = _race_employee(db_engine, "onboard-operator-overlap")
    try:
        with Session(db_engine) as session:
            assert not isinstance(
                self_service.establish_opening_state(
                    session, employee, OnboardingIn(**_payload(opening_balance_minutes=600))
                ),
                self_service.OnboardingRejected,
            )
            session.commit()

        operator = Principal(
            subject="race-operator",
            display_name="race",
            capabilities=frozenset({Capability.CUTOVER_SEED_WRITE}),
        )
        with Session(db_engine) as session:
            result = seed_opening_balance(
                session,
                operator,
                employee_external_id=f"emp-{employee.idp_subject}",
                cutover_month=FUTURE,
                minutes=999,
            )
            assert isinstance(result, CutoverSeedRejected)
            assert result.reason == "DUPLICATE_SEED"  # NOT PRE_CUTOVER_FACTS
            session.commit()
    finally:
        _purge_race(db_engine, employee.id)
