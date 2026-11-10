"""Carry-forward continuity tests (INV-08).

Discriminates the invariants of the read-time month-by-month carry:
an explicit ledger row belongs to the month containing its ``occurred_on``
and the displayed closing of every ELAPSED month equals the opening of the
next — with a seed, without a seed, across a credit, and across a year-end
settlement boundary.

The word ELAPSED is load-bearing. A month that has not elapsed is not
completed, so its days never reach the next month's opening: an un-lived
weekday is charged its full requirement against zero recorded work, and
threading that projection forward would open the next month with debt nobody
incurred (INV-08). An un-elapsed month's own DISPLAYED closing is still the
period-end projection — that figure is deliberately unchanged — so for the
current and future months the closing of M and the opening of M+1 are
expected to differ. ``test_an_un_elapsed_month_does_not_settle_into_the_next``
pins that boundary.
"""

import uuid
from datetime import date, datetime, time

import pytest
from app.balance.models import BalanceLedgerEntry
from app.identity.models import Employee
from app.policy.models import PolicyVersion
from app.policy.service import calculate_employee_period
from app.worktime.walltime import BERLIN
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from .conftest import DEVELOPER_PRINCIPAL_HEADER, OTHER_EMPLOYEE_HEADER

MONTHS = "/api/v1/me/months"
FINDINGS = "/api/v1/me/findings"


def _pin_today(monkeypatch: pytest.MonkeyPatch, day: date) -> None:
    """Pin the view's business date.

    ``calculate_employee_period`` holds the ONE clock read in the whole
    calculation, so replacing it there is the seam — no freezer, no global
    monkeypatching of ``datetime``. Midday, so the Berlin date is unambiguous.
    """
    import app.policy.service as policy_service

    monkeypatch.setattr(
        policy_service, "now_berlin", lambda: datetime.combine(day, time(12, 0), tzinfo=BERLIN)
    )


def _employee(session: Session, subject: str) -> Employee:
    employee = session.scalar(select(Employee).where(Employee.idp_subject == subject))
    assert employee is not None
    return employee


def _add_ledger(
    session: Session,
    employee: Employee,
    *,
    entry_type: str,
    occurred_on: date,
    minutes: int,
) -> None:
    policy = session.scalar(select(PolicyVersion).limit(1))
    assert policy is not None
    session.add(
        BalanceLedgerEntry(
            employee_id=employee.id,
            occurred_on=occurred_on,
            entry_type=entry_type,
            minutes=minutes,
            source_entity_type="TEST_SEED",
            policy_version_id=policy.id,
        )
    )
    session.flush()


def test_in_month_credit_belongs_to_its_own_month(
    db_client: TestClient, db_session: Session
) -> None:
    """Closing(M) must equal opening(M+1).

    The credit dated INSIDE August belongs to August: August's own view
    already includes it, so September continues from exactly that closing.
    """
    employee = _employee(db_session, "employee")
    _add_ledger(
        db_session,
        employee,
        entry_type="CUTOVER_SEED",
        occurred_on=date(2026, 7, 31),
        minutes=100,
    )
    _add_ledger(
        db_session,
        employee,
        entry_type="AUTHORIZED_CREDIT",
        occurred_on=date(2026, 8, 5),
        minutes=50,
    )
    august = db_client.get(f"{FINDINGS}?period=2026-08", headers=DEVELOPER_PRINCIPAL_HEADER)
    august_body = august.json()
    august_from_view = db_client.get(f"{MONTHS}/2026-08", headers=DEVELOPER_PRINCIPAL_HEADER)
    assert august_from_view.status_code == 200  # month view renders too
    september = db_client.get(f"{FINDINGS}?period=2026-09", headers=DEVELOPER_PRINCIPAL_HEADER)
    assert september.json()["opening_balance_minutes"] == august_body["closing_balance_minutes"]
    # The +50 credit is inside August's closing (compare via the engine:
    # no interval work, so August delta is calendar-only and identical for
    # both runs of the same session).
    direct = calculate_employee_period(db_session, employee, date(2026, 8, 1))
    assert direct.closing_balance_minutes == august_body["closing_balance_minutes"]
    without_credit = calculate_employee_period(db_session, employee, date(2026, 6, 1))
    # June has neither the seed (July 31) nor the credit; July chain is empty.
    assert without_credit.closing_balance_minutes < direct.closing_balance_minutes


def test_no_seed_anchor_from_earliest_interval(db_client: TestClient) -> None:
    """The no-seed chain anchors at the first interval."""
    created = db_client.post(
        "/api/v1/me/work-intervals",
        headers=OTHER_EMPLOYEE_HEADER,
        json={
            "start_local": "2026-07-06T09:00:00",
            "end_local": "2026-07-06T12:00:00",
            "work_type": "ON_SITE",
            "client_request_id": str(uuid.uuid4()),
        },
    )
    assert created.status_code == 201, created.text
    july = db_client.get(f"{FINDINGS}?period=2026-07", headers=OTHER_EMPLOYEE_HEADER).json()
    assert july["opening_balance_minutes"] == 0  # chain starts at zero, no seed
    august = db_client.get(f"{FINDINGS}?period=2026-08", headers=OTHER_EMPLOYEE_HEADER).json()
    assert august["opening_balance_minutes"] == july["closing_balance_minutes"]
    september = db_client.get(f"{FINDINGS}?period=2026-09", headers=OTHER_EMPLOYEE_HEADER).json()
    assert september["opening_balance_minutes"] == august["closing_balance_minutes"]


def test_no_seed_anchor_ignores_a_superseded_interval_moved_forward(db_client: TestClient) -> None:
    """A correction must not create a stale-looking pre-cutover chain."""
    created = db_client.post(
        "/api/v1/me/work-intervals",
        headers=OTHER_EMPLOYEE_HEADER,
        json={
            "start_local": "2026-01-06T09:00:00",
            "end_local": "2026-01-06T12:00:00",
            "work_type": "ON_SITE",
            "client_request_id": str(uuid.uuid4()),
        },
    )
    assert created.status_code == 201, created.text
    corrected = db_client.patch(
        f"/api/v1/me/work-intervals/{created.json()['id']}",
        headers=OTHER_EMPLOYEE_HEADER,
        json={
            "version": 1,
            "start_local": "2026-07-06T09:00:00",
            "end_local": "2026-07-06T12:00:00",
            "reason": "synthetic correction",
        },
    )
    assert corrected.status_code == 200, corrected.text

    july = db_client.get(f"{FINDINGS}?period=2026-07", headers=OTHER_EMPLOYEE_HEADER).json()
    assert july["opening_balance_minutes"] == 0


def test_year_end_settlement_threads_into_january(
    db_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chain applies the engine's settlement rule.

    A large positive seeded carry-in is settled at the December boundary
    (>40h forfeited beyond the paid-out tranche), so January 2027 opens from
    the SETTLED December closing, not the raw one.

    The clock is pinned PAST December, because the settlement is a boundary the
    year has to REACH: before December has elapsed there is no settlement to
    thread, and January must open from the raw carry-in instead. Both directions
    are asserted below, so the rule cannot be silently disabled by a change that
    makes the settlement never apply at all.
    """
    employee = _employee(db_session, "employee")
    _add_ledger(
        db_session,
        employee,
        entry_type="CUTOVER_SEED",
        occurred_on=date(2026, 11, 30),
        minutes=12000,
    )
    # Direction 1 — December 2026 has not elapsed, so it settles NOTHING into
    # January. Its own displayed closing still projects (it is the requested
    # month, whose period-end semantics are deliberately unchanged).
    before = db_client.get(f"{FINDINGS}?period=2026-12", headers=DEVELOPER_PRINCIPAL_HEADER).json()
    assert before["closing_balance_minutes"] == 0
    january_before = db_client.get(
        f"{FINDINGS}?period=2027-01", headers=DEVELOPER_PRINCIPAL_HEADER
    ).json()
    assert january_before["opening_balance_minutes"] == 12000  # the raw carry-in

    # Direction 2 — with the clock past the boundary, December IS settled and
    # January opens from the settled closing.
    _pin_today(monkeypatch, date(2027, 2, 15))
    december = db_client.get(f"{FINDINGS}?period=2026-12", headers=DEVELOPER_PRINCIPAL_HEADER)
    december_body = december.json()
    # balance_before_settlement > 0 (12000 - December weekday requirements):
    # settlement pays out min(balance, 40h) and forfeits the rest -> 0.
    assert december_body["closing_balance_minutes"] == 0
    january = db_client.get(f"{FINDINGS}?period=2027-01", headers=DEVELOPER_PRINCIPAL_HEADER)
    january_body = january.json()
    assert january_body["opening_balance_minutes"] == 0
    assert january_body["closing_balance_minutes"] < 0  # January accrues deficits


def test_a_row_inside_an_un_started_month_still_applies_in_it(
    db_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The skipped month still applies its OWN recorded rows — exactly once.

    This is the only test that fails if the skipped-month branch stops adding its
    in-month rows. Such a row has no later home: the engine is never called for a
    month that has not started, and no later month's half-open window contains the
    date, so dropping it would silently lose recorded minutes rather than defer
    them. Asserting BOTH months pins the other half too — the row must not be
    applied twice either.
    """
    employee = _employee(db_session, "employee")
    _add_ledger(
        db_session,
        employee,
        entry_type="CUTOVER_SEED",
        occurred_on=date(2026, 9, 30),
        minutes=600,
    )
    # Dated INSIDE November, which has not started on 30 September.
    _add_ledger(
        db_session,
        employee,
        entry_type="AUTHORIZED_CREDIT",
        occurred_on=date(2026, 11, 10),
        minutes=240,
    )
    _pin_today(monkeypatch, date(2026, 9, 30))

    october = db_client.get(f"{FINDINGS}?period=2026-10", headers=DEVELOPER_PRINCIPAL_HEADER).json()
    november = db_client.get(
        f"{FINDINGS}?period=2026-11", headers=DEVELOPER_PRINCIPAL_HEADER
    ).json()
    december = db_client.get(
        f"{FINDINGS}?period=2026-12", headers=DEVELOPER_PRINCIPAL_HEADER
    ).json()
    # October's own window excludes the November row.
    assert october["opening_balance_minutes"] == 600
    # November APPLIES it ...
    assert november["opening_balance_minutes"] == 840
    # ... and December must not see it a second time: November carried 840 forward.
    assert december["opening_balance_minutes"] == 840


def test_an_un_elapsed_month_does_not_project_into_the_next_opening(
    db_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The carry never threads a PROJECTION forward (INV-08).

    The clock sits on the day before the month starts, so not one day of it has
    been lived. The next month must then open from the SAME value this one opened
    with — while this month's own DISPLAYED closing is still the full-month
    projection, which charges every un-lived weekday its requirement against zero
    recorded work. That difference is the defect: threading the projection would
    open the next month with debt nobody has incurred.
    """
    employee = _employee(db_session, "employee")
    _add_ledger(
        db_session,
        employee,
        entry_type="CUTOVER_SEED",
        occurred_on=date(2026, 9, 30),
        minutes=600,
    )
    _pin_today(monkeypatch, date(2026, 9, 30))
    october = db_client.get(f"{FINDINGS}?period=2026-10", headers=DEVELOPER_PRINCIPAL_HEADER).json()
    november = db_client.get(
        f"{FINDINGS}?period=2026-11", headers=DEVELOPER_PRINCIPAL_HEADER
    ).json()
    assert october["opening_balance_minutes"] == 600
    # The un-lived month still shows its projection ...
    assert october["closing_balance_minutes"] < october["opening_balance_minutes"]
    # ... and the next month opens from what actually accumulated: nothing.
    assert november["opening_balance_minutes"] == 600
