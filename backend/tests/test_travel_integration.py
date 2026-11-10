"""Travel -> balance-chain integration (TRAVEL-003/004 wiring).

Discriminates what the pure-engine and route tests cannot: employee-declared
travel facts must flow through the REAL read-time carry-forward as derived
proposals — a day top-up in the travel month, one 25% credit in the month
after the per-trip window, exact arithmetic through every later month, and
deterministic recalculation (never a duplicated ledger credit) after
corrections. Passive travel never becomes a WorkInterval; work during travel
is counted exactly once (no double counting).

Dates sit on/after 2026-06-01 because the ordinary-working-time policy
version (migration 0003) becomes effective then.
"""

from datetime import UTC, date, datetime, time, timedelta
from typing import cast

from app.enums import MasterDataSource, WorkType
from app.identity.models import Employee, WorkScheduleVersion
from app.ocd_overtime.models import (
    INSTITUTIONAL_SOURCE,
    OvertimeAuthorization,
    OvertimeAuthStatus,
)
from app.policy.service import calculate_employee_period
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .conftest import DEVELOPER_PRINCIPAL_HEADER, OTHER_EMPLOYEE_HEADER

MONTHS = "/api/v1/me/months"
TRIPS = "/api/v1/me/travel-trips"
SUMMARY = "/api/v1/me/travel-summary"


def _employee(session: Session, subject: str) -> Employee:
    employee = session.scalar(select(Employee).where(Employee.idp_subject == subject))
    assert employee is not None
    return employee


def _make_trip(client: TestClient, start: str, end: str) -> dict[str, object]:
    response = client.post(
        TRIPS,
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": f"{start}T06:00:00",
            "end_local": f"{end}T22:00:00",
            "start_origin": "WORKPLACE",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _make_segment(
    client: TestClient, trip_id: str, start_local: str, end_local: str, classification: str
) -> dict[str, object]:
    response = client.post(
        f"{TRIPS}/{trip_id}/segments",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": start_local,
            "end_local": end_local,
            "classification": classification,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_travel_credit_lands_once_in_effective_month_and_topup_in_travel_month(
    db_client: TestClient, db_session: Session
) -> None:
    employee = _employee(db_session, "employee")  # seeded Mon-Fri 348/day

    trip = _make_trip(db_client, "2026-06-15", "2026-06-16")
    _make_segment(
        db_client, str(trip["id"]), "2026-06-15T06:00:00", "2026-06-15T22:00:00", "PASSIVE_TRAVEL"
    )

    june = calculate_employee_period(db_session, employee, date(2026, 6, 1))
    july = calculate_employee_period(db_session, employee, date(2026, 7, 1))
    august = calculate_employee_period(db_session, employee, date(2026, 8, 1))

    # TRAVEL-003: Jun 15 (Mon, required 348, worked 0) topped to exactly 0
    # delta — never positive from passive travel alone.  June's whole-month
    # closing rises by exactly one day's required minutes over the 22-weekday
    # deficit the same view recomputes (the seeded employee works no
    # intervals, so every value here is fully deterministic).
    day15 = next(item for item in june.daily_results if item.day == date(2026, 6, 15))
    assert day15.worked_minutes == 0
    assert day15.travel_topup_minutes == 348
    assert day15.flex_delta_minutes == 0
    assert june.closing_balance_minutes == -7656 + 348

    # The window (Jun 15 -> Jul 15) is NOT the calendar month: no credit in
    # July, exactly one 15-minute TRAVEL_CREDIT effective Aug 1 (excess 1:00
    # -> 0:15, the ``POL-08`` worked example).
    #
    # Chain continuity (INV-08): July opens with June's topped-up closing —
    # exactly the value the June view showed, no manual carry.
    assert not [p for p in july.ledger_proposals if p.entry_type == "TRAVEL_CREDIT"]
    assert july.opening_balance_minutes == june.closing_balance_minutes
    assert july.closing_balance_minutes == june.closing_balance_minutes - 8004  # Jul: 23 wd
    credits = [p for p in august.ledger_proposals if p.entry_type == "TRAVEL_CREDIT"]
    assert [(p.minutes, str(p.occurred_on), p.source_entity_type) for p in credits] == [
        (15, "2026-08-01", "business_trip")
    ]
    assert credits[0].rule_id == "TRAVEL-004"
    assert credits[0].calculation == {
        "window_start": "2026-06-15",
        "window_end": "2026-07-15",
        "eligible_minutes": 960,
        "threshold_minutes": 900,
        "excess_minutes": 60,
    }
    assert august.opening_balance_minutes == july.closing_balance_minutes
    # Aug: 21 wd x 348 = 7308 deficit + the single 15-minute credit, once.
    assert august.closing_balance_minutes == july.closing_balance_minutes - 7308 + 15

    # The summary view exposes the same derived numbers for the credit month.
    august_summary = db_client.get(
        f"{SUMMARY}?period=2026-08", headers=DEVELOPER_PRINCIPAL_HEADER
    ).json()
    assert august_summary["credits"][0]["minutes"] == 15

    # Month summary API carries the top-up field for the travel day.
    month = db_client.get(f"{MONTHS}/2026-06", headers=DEVELOPER_PRINCIPAL_HEADER).json()
    row = next(item for item in month["daily_results"] if item["date"] == "2026-06-15")
    assert row["travel_topup_minutes"] == 348 and row["delta_minutes"] == 0


def test_correction_recalculates_deterministically_without_duplicate_credit(
    db_client: TestClient, db_session: Session
) -> None:
    employee = _employee(db_session, "employee")
    trip = _make_trip(db_client, "2026-06-15", "2026-06-16")
    segment = _make_segment(
        db_client, str(trip["id"]), "2026-06-15T06:00:00", "2026-06-15T22:00:00", "PASSIVE_TRAVEL"
    )
    august_with = calculate_employee_period(db_session, employee, date(2026, 8, 1))
    assert (
        sum(p.minutes for p in august_with.ledger_proposals if p.entry_type == "TRAVEL_CREDIT")
        == 15
    )

    # Employee corrects the leg to end 20:00 -> 840 eligible -> no excess.
    response = db_client.patch(
        f"/api/v1/me/travel-segments/{segment['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "version": segment["version"],
            "reason": "itinerary was shorter",
            "start_local": "2026-06-15T06:00:00",
            "end_local": "2026-06-15T20:00:00",
        },
    )
    assert response.status_code == 200, response.text
    august_short = calculate_employee_period(db_session, employee, date(2026, 8, 1))
    assert not [p for p in august_short.ledger_proposals if p.entry_type == "TRAVEL_CREDIT"]

    # Correcting back yields exactly ONE credit again — never a second row.
    corrected = response.json()
    again = db_client.patch(
        f"/api/v1/me/travel-segments/{corrected['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "version": corrected["version"],
            "reason": "restored full leg",
            "start_local": "2026-06-15T06:00:00",
            "end_local": "2026-06-15T22:00:00",
        },
    )
    assert again.status_code == 200, again.text
    august_back = calculate_employee_period(db_session, employee, date(2026, 8, 1))
    credits = [p for p in august_back.ledger_proposals if p.entry_type == "TRAVEL_CREDIT"]
    assert len(credits) == 1 and credits[0].minutes == 15


def test_driving_works_count_once_and_effective_schedule_applies_per_day(
    db_client: TestClient, db_session: Session
) -> None:
    employee = _employee(db_session, "employee")
    trip = _make_trip(db_client, "2026-06-15", "2026-06-20")
    # Driving 09:00-11:00 on Jun 19 IS working time (linked interval created).
    _make_segment(
        db_client, str(trip["id"]), "2026-06-19T09:00:00", "2026-06-19T11:00:00", "DRIVING"
    )
    june = calculate_employee_period(db_session, employee, date(2026, 6, 1))
    day19 = next(item for item in june.daily_results if item.day == date(2026, 6, 19))
    # Counted exactly once as actual work: no travel-side second addition.
    assert day19.worked_minutes == 120
    assert day19.travel_topup_minutes == 0
    assert day19.flex_delta_minutes == 120 - day19.required_minutes
    # And the ledger holds exactly one BUSINESS_TRIP_WORK interval (the link).
    day_view = db_client.get(
        f"{MONTHS}/2026-06/days/2026-06-19", headers=DEVELOPER_PRINCIPAL_HEADER
    ).json()
    assert [item["work_type"] for item in day_view["intervals"]] == [
        WorkType.BUSINESS_TRIP_WORK.value
    ]
    assert day_view["intervals"][0]["elapsed_seconds"] == 2 * 3600
    month = db_client.get(f"{MONTHS}/2026-06", headers=DEVELOPER_PRINCIPAL_HEADER).json()
    assert month["interval_count"] == 1


def test_generic_worktime_paths_cannot_bypass_travel_linking(
    db_client: TestClient, db_session: Session
) -> None:
    employee = _employee(db_session, "employee")
    # Mid-window schedule change: from Jun 18 the employee works 300/day, so
    # Jun 19's requirement is effective-dated independently of the trip.
    db_session.add(
        WorkScheduleVersion(
            employee_id=employee.id,
            valid_from=date(2026, 6, 18),
            valid_to=None,
            weekly_required_minutes=1500,
            mon_required_minutes=300,
            tue_required_minutes=300,
            wed_required_minutes=300,
            thu_required_minutes=300,
            fri_required_minutes=300,
            sat_required_minutes=0,
            sun_required_minutes=0,
            source=MasterDataSource.EMPLOYEE,
            version=2,
        )
    )
    db_session.flush()

    trip = _make_trip(db_client, "2026-06-01", "2026-06-05")
    segment = _make_segment(
        db_client, str(trip["id"]), "2026-06-03T09:00:00", "2026-06-03T11:00:00", "ACTIVE_WORK"
    )
    june = calculate_employee_period(db_session, employee, date(2026, 6, 1))
    day3 = next(item for item in june.daily_results if item.day == date(2026, 6, 3))
    day19 = next(item for item in june.daily_results if item.day == date(2026, 6, 19))
    assert day3.required_minutes == 348  # old schedule still effective
    assert day19.required_minutes == 300  # new schedule effective from Jun 18

    interval_id = segment["work_interval_id"]
    assert interval_id is not None

    # 1. generic creation of business-trip work is rejected
    created = db_client.post(
        "/api/v1/me/work-intervals",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": "2026-06-04T09:00:00",
            "end_local": "2026-06-04T11:00:00",
            "work_type": "BUSINESS_TRIP_WORK",
        },
    )
    assert created.status_code == 422
    assert created.json()["detail"]["code"] == "VALIDATION_FAILED"

    # 2. direct patch/delete of the linked interval is rejected
    patched = db_client.patch(
        f"/api/v1/me/work-intervals/{interval_id}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 1, "end_local": "2026-06-03T12:00:00"},
    )
    assert patched.status_code == 422
    deleted = db_client.delete(
        f"/api/v1/me/work-intervals/{interval_id}?version=1", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert deleted.status_code == 422

    # 3. copy-previous must not duplicate business-trip work
    copied = db_client.post(
        f"{MONTHS}/2026-06/days/2026-06-04/copy-previous",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={},
    )
    assert copied.status_code == 200, copied.text
    day_view = db_client.get(
        f"{MONTHS}/2026-06/days/2026-06-04", headers=DEVELOPER_PRINCIPAL_HEADER
    ).json()
    assert day_view["intervals"] == []


def test_ordinary_sunday_work_never_produces_travel_findings(
    db_client: TestClient, db_session: Session
) -> None:
    """Reviewer F2: TRAVEL-SUNDAY/TRAVEL-LOCAL findings are travel-only.

    Ordinary Sunday work must not be labelled as Sunday TRAVEL work on either
    surface (/travel-summary and /me/findings), even while the employee has
    current travel facts that keep the travel engine active.
    """
    _employee(db_session, "employee")
    trip = _make_trip(db_client, "2026-06-19", "2026-06-22")
    _make_segment(
        db_client, str(trip["id"]), "2026-06-19T06:00:00", "2026-06-19T10:00:00", "PASSIVE_TRAVEL"
    )
    # Ordinary (non-travel) work on Sunday 2026-06-21 via the real API path.
    created = db_client.post(
        "/api/v1/me/work-intervals",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": "2026-06-21T09:00:00",
            "end_local": "2026-06-21T11:00:00",
            "work_type": "ON_SITE",
        },
    )
    assert created.status_code == 201, created.text

    june = calculate_employee_period(
        db_session, _employee(db_session, "employee"), date(2026, 6, 1)
    )
    travel_rules = {
        finding.rule_id for finding in june.findings if finding.rule_id.startswith("TRAVEL-")
    }
    assert travel_rules == set(), (
        f"ordinary Sunday work must not raise travel findings: {travel_rules}"
    )

    summary = db_client.get(f"{SUMMARY}?period=2026-06", headers=DEVELOPER_PRINCIPAL_HEADER).json()
    assert not any(finding["rule_id"].startswith("TRAVEL-") for finding in summary["findings"]), (
        summary["findings"]
    )

    findings = db_client.get(
        "/api/v1/me/findings?period=2026-06", headers=DEVELOPER_PRINCIPAL_HEADER
    ).json()
    assert not any(finding["rule_id"].startswith("TRAVEL-") for finding in findings["findings"]), (
        findings["findings"]
    )


def test_active_only_trip_findings_surface_on_me_findings(
    db_client: TestClient, db_session: Session
) -> None:
    """Reviewer F3: a trip with only active/driving segments (no passive) must
    still surface its travel findings on /me/findings, matching
    /travel-summary — the passive-only early return hid them.
    """
    _employee(db_session, "employee")
    trip = _make_trip(db_client, "2026-06-19", "2026-06-22")
    _make_segment(
        db_client, str(trip["id"]), "2026-06-21T09:00:00", "2026-06-21T11:00:00", "DRIVING"
    )

    summary = db_client.get(f"{SUMMARY}?period=2026-06", headers=DEVELOPER_PRINCIPAL_HEADER).json()
    summary_rules = {item["rule_id"] for item in summary["findings"]}
    assert "TRAVEL-SUNDAY-002" in summary_rules

    findings = db_client.get(
        "/api/v1/me/findings?period=2026-06", headers=DEVELOPER_PRINCIPAL_HEADER
    ).json()
    rules = {item["rule_id"] for item in findings["findings"]}
    assert "TRAVEL-SUNDAY-002" in rules, "active-only travel findings must reach /me/findings"
    # No travel credit/top-up is invented for a passive-less trip.
    assert summary["credits"] == [] and summary["topups"] == []


def test_superseded_overtime_authorization_does_not_cover_sunday_travel(
    db_client: TestClient, db_session: Session
) -> None:
    """Reviewer F4: travel summary must apply the same current-version
    predicate as the policy loader — a superseded overtime authorization
    stops covering Sunday travel work on BOTH surfaces.
    """
    employee = _employee(db_session, "employee")
    trip = _make_trip(db_client, "2026-06-19", "2026-06-22")
    _make_segment(
        db_client, str(trip["id"]), "2026-06-21T09:00:00", "2026-06-21T11:00:00", "ACTIVE_WORK"
    )

    sunday = datetime.combine(date(2026, 6, 21), time.min, tzinfo=UTC)
    auth = OvertimeAuthorization(
        employee_id=employee.id,
        valid_from=sunday,
        valid_to=sunday + timedelta(days=1),
        status=OvertimeAuthStatus.APPROVED,
        source=INSTITUTIONAL_SOURCE,
        version=1,
    )
    db_session.add(auth)
    db_session.flush()

    covered = db_client.get(f"{SUMMARY}?period=2026-06", headers=DEVELOPER_PRINCIPAL_HEADER).json()
    assert not any(item["rule_id"] == "TRAVEL-SUNDAY-001" for item in covered["findings"]), (
        "current authorization must cover the Sunday travel work"
    )

    # A correction supersedes the authorization and narrows its window away
    # from Sunday: only the new (current) row counts, so coverage is gone.
    db_session.add(
        OvertimeAuthorization(
            employee_id=employee.id,
            valid_from=sunday + timedelta(days=7),
            valid_to=sunday + timedelta(days=8),
            status=OvertimeAuthStatus.APPROVED,
            source=INSTITUTIONAL_SOURCE,
            version=2,
            supersedes_id=auth.id,
        )
    )
    db_session.flush()

    superseded = db_client.get(
        f"{SUMMARY}?period=2026-06", headers=DEVELOPER_PRINCIPAL_HEADER
    ).json()
    assert any(item["rule_id"] == "TRAVEL-SUNDAY-001" for item in superseded["findings"]), (
        "superseded authorization must not cover Sunday travel work"
    )
    findings = db_client.get(
        "/api/v1/me/findings?period=2026-06", headers=DEVELOPER_PRINCIPAL_HEADER
    ).json()
    assert any(item["rule_id"] == "TRAVEL-SUNDAY-001" for item in findings["findings"]), (
        "summary and /me/findings must use the same current authorization heads"
    )


def test_a_foreign_successor_does_not_retire_an_overtime_authorization(
    db_client: TestClient, db_session: Session
) -> None:
    """The mirror of the case above: ``supersedes_id`` is a plain self-FK, so a
    restored or imported row belonging to ANOTHER employee must not retire this
    employee's approval and silently restore the Sunday finding. The anti-join
    must be employee-scoped, exactly like the chain walk in the preview.
    """
    employee = _employee(db_session, "employee")
    other = _employee(db_session, "employee-b")
    trip = _make_trip(db_client, "2026-06-19", "2026-06-22")
    _make_segment(
        db_client, str(trip["id"]), "2026-06-21T09:00:00", "2026-06-21T11:00:00", "ACTIVE_WORK"
    )

    sunday = datetime.combine(date(2026, 6, 21), time.min, tzinfo=UTC)
    auth = OvertimeAuthorization(
        employee_id=employee.id,
        valid_from=sunday,
        valid_to=sunday + timedelta(days=1),
        status=OvertimeAuthStatus.APPROVED,
        source=INSTITUTIONAL_SOURCE,
        version=1,
    )
    db_session.add(auth)
    db_session.flush()

    covered = db_client.get(f"{SUMMARY}?period=2026-06", headers=DEVELOPER_PRINCIPAL_HEADER).json()
    assert not any(item["rule_id"] == "TRAVEL-SUNDAY-001" for item in covered["findings"]), (
        "current authorization must cover the Sunday travel work"
    )

    # Another employee's row supersedes it. `EINV-26` now refuses that edge at
    # write time, so it is forged by dropping the FK inside the test transaction
    # (transactional DDL) — which is exactly the restored-database shape the
    # employee-scoped anti-join still has to survive. It must change nothing here.
    db_session.execute(
        text(
            "ALTER TABLE overtime_authorization "
            "DROP CONSTRAINT fk_overtime_authorization_supersedes_same_owner"
        )
    )
    db_session.add(
        OvertimeAuthorization(
            employee_id=other.id,
            valid_from=sunday + timedelta(days=7),
            valid_to=sunday + timedelta(days=8),
            status=OvertimeAuthStatus.APPROVED,
            source=INSTITUTIONAL_SOURCE,
            version=2,
            supersedes_id=auth.id,
        )
    )
    db_session.flush()

    still_covered = db_client.get(
        f"{SUMMARY}?period=2026-06", headers=DEVELOPER_PRINCIPAL_HEADER
    ).json()
    assert not any(item["rule_id"] == "TRAVEL-SUNDAY-001" for item in still_covered["findings"]), (
        "another employee's row retired this employee's approval"
    )


def test_travel_is_employee_private_surface_only(
    db_client: TestClient, db_session: Session
) -> None:
    _employee(db_session, "employee")
    paths = cast(FastAPI, db_client.app).openapi()["paths"]
    travel_paths = [path for path in paths if "travel" in path]
    assert travel_paths, "travel endpoints must exist"
    assert all(path.startswith("/api/v1/me/") for path in travel_paths)

    trip = _make_trip(db_client, "2026-06-15", "2026-06-16")
    _make_segment(
        db_client, str(trip["id"]), "2026-06-15T06:00:00", "2026-06-15T22:00:00", "PASSIVE_TRAVEL"
    )
    # Another employee cannot see or touch the trip (IDOR-proof by construction).
    other_trips = db_client.get(TRIPS, headers=OTHER_EMPLOYEE_HEADER).json()
    assert other_trips["trips"] == []
    foreign = db_client.patch(
        f"{TRIPS}/{trip['id']}",
        headers=OTHER_EMPLOYEE_HEADER,
        json={"version": 1, "reason": "nope"},
    )
    assert foreign.status_code == 404
    # The derived view answers for the owner only.
    mine = db_client.get(SUMMARY + "?period=2026-06", headers=DEVELOPER_PRINCIPAL_HEADER)
    assert mine.status_code == 200
    assert mine.json()["trips"][0]["eligible_minutes"] == 960
