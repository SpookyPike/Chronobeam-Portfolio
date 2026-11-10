"""Employee-maintained working time API (BE §8.1).

Covers DATA-011..014: employee-written effective-dated schedules, exact-sum
validation, authoritative-row protection, same-date employee correction, and
historical resolution. Engine weekly thresholds (WT-FLEX-003/004) read the
employee-maintained schedule value.
"""

from datetime import date

import pytest
from app.identity.models import WorkScheduleVersion
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.conftest import DEVELOPER_PRINCIPAL_HEADER as ME
from tests.conftest import HR_HEADER, OTHER_EMPLOYEE_HEADER

WORKING_TIME = "/api/v1/me/working-time"
PROFILE = "/api/v1/me/profile"
FINDINGS = "/api/v1/me/findings"
INTERVALS = "/api/v1/me/work-intervals"


def _payload(
    valid_from: str = "2026-10-01",
    weekly: int = 2340,  # 39:00
    days: dict[str, int] | None = None,
) -> dict[str, int | str | float]:
    base = {
        f"{day}_required_minutes": 0 for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    }
    if days:
        base.update({f"{day}_required_minutes": value for day, value in days.items()})
    return {"valid_from": valid_from, "weekly_required_minutes": weekly, **base}


def _schedule_rows(db_session: Session, employee_id: object) -> list[WorkScheduleVersion]:
    return list(
        db_session.scalars(
            select(WorkScheduleVersion)
            .where(WorkScheduleVersion.employee_id == employee_id)
            .order_by(WorkScheduleVersion.valid_from, WorkScheduleVersion.version)
        )
    )


def test_put_working_time_creates_employee_schedule_version(db_client: TestClient) -> None:
    payload = _payload(
        weekly=1740, days={"mon": 360, "tue": 360, "wed": 360, "thu": 360, "fri": 300}
    )
    response = db_client.put(WORKING_TIME, headers=ME, json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid_from"] == "2026-10-01"
    assert body["valid_to"] is None
    assert body["weekly_required_minutes"] == 1740
    assert body["mon_required_minutes"] == 360
    assert body["sun_required_minutes"] == 0
    assert body["source"] == "EMPLOYEE"
    assert body["version"] == 2  # seeded ADMIN_SEED row is version 1


def test_put_working_time_sum_mismatch_is_422(db_client: TestClient) -> None:
    payload = _payload(
        weekly=1740, days={"mon": 360, "tue": 360, "wed": 360, "thu": 360, "fri": 300}
    )
    payload["weekly_required_minutes"] = 1740
    payload["fri_required_minutes"] = 240  # allocated 24:00, target 29:00
    response = db_client.put(WORKING_TIME, headers=ME, json=payload)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "VALIDATION_FAILED"
    assert detail["details"]["weekly_required_minutes"] == 1740
    assert detail["details"]["allocated_minutes"] == 1680


def test_put_working_time_day_range_and_weekly_negative_are_422(db_client: TestClient) -> None:
    over_max = _payload(weekly=1740, days={"mon": 1500})
    response = db_client.put(WORKING_TIME, headers=ME, json=over_max)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VALIDATION_FAILED"

    negative_day = _payload(weekly=1740, days={"mon": -5})
    response = db_client.put(WORKING_TIME, headers=ME, json=negative_day)
    assert response.status_code == 422

    negative_weekly = _payload(weekly=-1)
    response = db_client.put(WORKING_TIME, headers=ME, json=negative_weekly)
    assert response.status_code == 422


def test_put_working_time_rejects_float_minutes(db_client: TestClient) -> None:
    """Minutes are integer facts; whole-valued JSON floats are not accepted."""
    float_weekly = _payload(
        weekly=1740,
        days={"mon": 348, "tue": 348, "wed": 348, "thu": 348, "fri": 348},
    )
    float_weekly["weekly_required_minutes"] = 1740.0
    response = db_client.put(WORKING_TIME, headers=ME, json=float_weekly)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VALIDATION_FAILED"

    float_day = _payload(
        weekly=1740,
        days={"mon": 348, "tue": 348, "wed": 348, "thu": 348, "fri": 348},
    )
    float_day["mon_required_minutes"] = 348.0
    response = db_client.put(WORKING_TIME, headers=ME, json=float_day)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VALIDATION_FAILED"


def test_put_working_time_protects_authoritative_row(db_client: TestClient) -> None:
    """DATA-014: an ADMIN_SEED/MASTER schedule date is never rewritten."""
    payload = _payload(
        valid_from="2026-01-01",
        weekly=1740,
        days={"mon": 348, "tue": 348, "wed": 348, "thu": 348, "fri": 348},
    )
    response = db_client.put(WORKING_TIME, headers=ME, json=payload)
    assert response.status_code == 422
    assert "authoritative source" in response.json()["detail"]["message"]

    profile = db_client.get(f"{PROFILE}?at=2026-06-15", headers=ME).json()
    assert profile["schedule"]["source"] == "ADMIN_SEED"
    assert profile["schedule"]["weekly_required_minutes"] == 1740
    assert profile["schedule"]["version"] == 1


def test_put_working_time_same_date_employee_correction_in_place(db_client: TestClient) -> None:
    """An employee row on the same valid_from is corrected in place (version bumped)."""
    first = _payload(weekly=1740, days={"mon": 348, "tue": 348, "wed": 348, "thu": 348, "fri": 348})
    response = db_client.put(WORKING_TIME, headers=ME, json=first)
    assert response.status_code == 200
    assert response.json()["version"] == 2

    second = _payload(
        weekly=1755, days={"mon": 351, "tue": 351, "wed": 351, "thu": 351, "fri": 351}
    )
    response = db_client.put(WORKING_TIME, headers=ME, json=second)
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 3
    assert body["weekly_required_minutes"] == 1755
    assert body["mon_required_minutes"] == 351


def test_put_working_time_effective_dated_history_preserved(db_client: TestClient) -> None:
    """INV-07/INV-11: dates before valid_from keep resolving to the old schedule."""
    payload = _payload(
        valid_from="2026-10-01",
        weekly=2340,
        days={"mon": 468, "tue": 468, "wed": 468, "thu": 468, "fri": 468},
    )
    response = db_client.put(WORKING_TIME, headers=ME, json=payload)
    assert response.status_code == 200, response.text

    before = db_client.get(f"{PROFILE}?at=2026-09-30", headers=ME).json()
    assert before["schedule"]["source"] == "ADMIN_SEED"
    assert before["schedule"]["weekly_required_minutes"] == 1740

    on_change = db_client.get(f"{PROFILE}?at=2026-10-01", headers=ME).json()
    assert on_change["schedule"]["source"] == "EMPLOYEE"
    assert on_change["schedule"]["weekly_required_minutes"] == 2340
    assert on_change["schedule"]["mon_required_minutes"] == 468

    ancient = db_client.get(f"{PROFILE}?at=2026-06-15", headers=ME).json()
    assert ancient["schedule"]["source"] == "ADMIN_SEED"


def test_put_working_time_past_effective_date_leaves_earlier_dates_alone(
    db_client: TestClient,
) -> None:
    payload = _payload(
        valid_from="2026-03-01",
        weekly=870,
        days={"mon": 174, "tue": 174, "wed": 174, "thu": 174, "fri": 174},
    )
    response = db_client.put(WORKING_TIME, headers=ME, json=payload)
    assert response.status_code == 200, response.text

    earlier = db_client.get(f"{PROFILE}?at=2026-02-15", headers=ME).json()
    assert earlier["schedule"]["weekly_required_minutes"] == 1740
    after = db_client.get(f"{PROFILE}?at=2026-03-15", headers=ME).json()
    assert after["schedule"]["weekly_required_minutes"] == 870
    assert after["schedule"]["source"] == "EMPLOYEE"


def test_put_working_time_hr_denied_and_employees_isolated(
    db_client: TestClient,
    db_session: Session,
) -> None:
    """Employee-only endpoint: HR gets the single 403; other employees stay isolated."""
    hr = db_client.put(WORKING_TIME, headers=HR_HEADER, json=_payload())
    assert hr.status_code == 403
    assert hr.json() == {
        "detail": {
            "code": "AUTH_FORBIDDEN",
            "message": "Access to this resource is restricted.",
        }
    }

    response = db_client.put(
        WORKING_TIME,
        headers=OTHER_EMPLOYEE_HEADER,
        json=_payload(
            valid_from="2026-10-01",
            weekly=1740,
            days={"mon": 348, "tue": 348, "wed": 348, "thu": 348, "fri": 348},
        ),
    )
    assert response.status_code == 200

    from app.identity.models import Employee

    employee_a = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    employee_b = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee-b"))
    assert employee_a is not None and employee_b is not None
    a_rows = _schedule_rows(db_session, employee_a.id)
    b_rows = _schedule_rows(db_session, employee_b.id)
    assert len(a_rows) == 1  # employee A untouched
    assert a_rows[0].source.value == "ADMIN_SEED"
    assert len(b_rows) == 2
    assert b_rows[1].source.value == "EMPLOYEE"


def test_weekly_threshold_uses_employee_maintained_schedule(
    db_client: TestClient,
) -> None:
    """WT-FLEX-003/004 thresholds follow the employee schedule, not the contract."""
    # Employee-maintained: 60 min/week, Monday 60 min. Contract says 1740.
    payload = _payload(valid_from="2026-07-01", weekly=60, days={"mon": 60})
    response = db_client.put(WORKING_TIME, headers=ME, json=payload)
    assert response.status_code == 200, response.text

    # Work 100 minutes on each of the four Mondays of July 2026 (delta +40/day
    # -> balance +160). With weekly 60 that fires WT-FLEX-003/004; with the
    # contract 1740 it would not.
    for monday in ("2026-07-06", "2026-07-13", "2026-07-20", "2026-07-27"):
        created = db_client.post(
            INTERVALS,
            headers=ME,
            json={
                "start_local": f"{monday}T08:00:00",
                "end_local": f"{monday}T09:40:00",  # 100 minutes
                "work_type": "ON_SITE",
            },
        )
        assert created.status_code == 201, created.text

    findings = db_client.get(f"{FINDINGS}?period=2026-07", headers=ME).json()
    rule_ids = {finding["rule_id"] for finding in findings["findings"]}
    # WT-FLEX-004 is the 100%-of-weekly threshold (WT-FLEX-003 is its elif);
    # with the contract 1740 the +160 balance would not fire either.
    assert "WT-FLEX-004" in rule_ids
    assert "WT-FLEX-003" not in rule_ids


def test_db_rejects_schedule_sum_violation(db_session: Session) -> None:
    """DATA-012: the storage invariant holds even below the service layer."""
    from app.identity.models import Employee

    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    db_session.add(
        WorkScheduleVersion(
            employee_id=employee.id,
            valid_from=date(2026, 11, 1),
            valid_to=None,
            weekly_required_minutes=1740,
            mon_required_minutes=348,
            tue_required_minutes=348,
            wed_required_minutes=348,
            thu_required_minutes=348,
            fri_required_minutes=348,
            sat_required_minutes=100,  # sum is now 1840 != 1740
            sun_required_minutes=0,
            source="EMPLOYEE",
            version=99,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_profile_exposes_schedule_weekly_and_source(db_client: TestClient) -> None:
    profile = db_client.get(f"{PROFILE}?at=2026-06-15", headers=ME).json()
    assert profile["schedule"]["weekly_required_minutes"] == 1740
    assert profile["schedule"]["source"] == "ADMIN_SEED"
    assert profile["schedule"]["mon_required_minutes"] == 348


def test_read_reports_the_effective_range_end_so_arrangements_are_enumerable(
    db_client: TestClient,
    db_session: Session,
) -> None:
    """Slice 5 (backend half): ``valid_to`` is the last day the row GOVERNS.

    Every row the application writes carries ``valid_to = NULL``, so the raw
    column reports "open-ended" for an arrangement that a later one supersedes —
    and the later arrangement is undiscoverable from the one in force. The read
    path must report the same derived range end the WRITE path already assumes
    (``_reopen_schedule_months`` reopens exactly ``[valid_from,
    _schedule_range_end]``), so ranges become contiguous and an arrangement is
    reachable through ``GET /me/profile?at=<its valid_from>`` with its OWN values.

    No column is written: this is a read-time projection.
    """
    october = _payload(
        valid_from="2026-10-01",
        weekly=1740,
        days={"mon": 348, "tue": 348, "wed": 348, "thu": 348, "fri": 348},
    )
    created = db_client.put(WORKING_TIME, headers=ME, json=october)
    assert created.status_code == 200, created.text
    # No later arrangement exists yet: the range is genuinely open-ended.
    assert created.json()["valid_to"] is None

    january = _payload(
        valid_from="2027-01-01",
        weekly=2040,  # 34:00
        days={"mon": 420, "tue": 420, "wed": 420, "thu": 420, "fri": 360},
    )
    created = db_client.put(WORKING_TIME, headers=ME, json=january)
    assert created.status_code == 200, created.text
    # The LAST arrangement stays open-ended.
    assert created.json()["valid_to"] is None

    at_october = db_client.get(f"{PROFILE}?at=2026-10-01", headers=ME).json()["schedule"]
    assert at_october["valid_from"] == "2026-10-01"
    assert at_october["valid_to"] == "2026-12-31"  # the day before January starts
    assert at_october["weekly_required_minutes"] == 1740
    assert at_october["mon_required_minutes"] == 348
    assert at_october["version"] == 2

    # The date the maintenance editor's chain step offers (``valid_to + 1 day``)
    # resolves the NEXT arrangement with its own values.
    at_january = db_client.get(f"{PROFILE}?at=2027-01-01", headers=ME).json()["schedule"]
    assert at_january["valid_from"] == "2027-01-01"
    assert at_january["valid_to"] is None
    assert at_january["weekly_required_minutes"] == 2040
    assert at_january["mon_required_minutes"] == 420
    assert at_january["version"] == 3

    # The superseded arrangement is still resolvable inside its own range, and
    # the seeded one now ends the day before October.
    seeded = db_client.get(f"{PROFILE}?at=2026-06-15", headers=ME).json()["schedule"]
    assert seeded["source"] == "ADMIN_SEED"
    assert seeded["valid_to"] == "2026-09-30"

    # …and NOTHING was written: the stored column is still NULL for every row.
    from app.identity.models import Employee

    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    assert {row.valid_from: row.valid_to for row in _schedule_rows(db_session, employee.id)} == {
        date(2026, 1, 1): None,
        date(2026, 10, 1): None,
        date(2027, 1, 1): None,
    }


def test_a_later_arrangement_permanently_supersedes_an_earlier_one(
    db_client: TestClient,
    db_session: Session,
) -> None:
    """Effective dating is PERMANENT SHADOWING, not per-row validity (review R29).

    The master-data import may write a FINITE ``valid_to`` on a schedule row
    (``MasterDataRow.valid_to`` is a writable column). Under a per-row
    ``valid_to >= at`` predicate, such a row expires and the arrangement it
    superseded becomes effective AGAIN — while ``GET /me/profile`` reports that
    superseded arrangement as having ended the day before the later one started.
    The projection then contradicts the schedule the ledger actually uses.

    So the later-starting row owns its day outright, and its own ``valid_to``
    decides whether anything governs: once it expires there is NO arrangement
    until another row starts. That is the rule the rest of the codebase already
    assumes (``master_data_admin``'s ``[valid_from, min(valid_to,
    next_valid_from - 1)]``, ``_effective_range_end``, ``_reopen_schedule_months``).
    """
    from app.identity.models import Employee
    from app.identity.service import effective_schedule

    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None

    # A: the seeded open-ended arrangement from 2026-01-01. B: a master-imported
    # arrangement that starts 2026-10-01 and ENDS 2026-10-31.
    db_session.add(
        WorkScheduleVersion(
            employee_id=employee.id,
            valid_from=date(2026, 10, 1),
            valid_to=date(2026, 10, 31),
            version=2,
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

    # Inside B's own range, B governs.
    inside = effective_schedule(db_session, employee.id, date(2026, 10, 15))
    assert inside is not None
    assert inside.valid_from == date(2026, 10, 1)

    # After B expires, NOTHING governs — A does not resume.
    assert effective_schedule(db_session, employee.id, date(2026, 11, 1)) is None
    # …and A's own reported range still ends the day before B started, so the
    # projection and the resolution agree instead of contradicting each other.
    seeded = db_client.get(f"{PROFILE}?at=2026-06-15", headers=ME).json()["schedule"]
    assert seeded["valid_from"] == "2026-01-01"
    assert seeded["valid_to"] == "2026-09-30"

    # The read path answers for the expired range the same way the resolver does.
    after = db_client.get(f"{PROFILE}?at=2026-11-01", headers=ME).json()
    assert after["schedule"] is None
    in_october = db_client.get(f"{PROFILE}?at=2026-10-15", headers=ME).json()["schedule"]
    assert in_october["valid_from"] == "2026-10-01"
    assert in_october["valid_to"] == "2026-10-31"

    # The RANGE resolver must agree with the point resolver. Its window predicate
    # used to drop rows with `valid_to < start` on the reasoning that a winner
    # must cover the day — true of the per-row rule, false under permanent
    # shadowing, where an expired row is precisely what makes a day ungoverned.
    # With the old predicate this window fetched only A and resurrected it.
    from app.identity.service import effective_schedules_by_day

    window = effective_schedules_by_day(
        db_session, employee.id, date(2026, 11, 1), date(2026, 12, 1)
    )
    assert set(window) == {date(2026, 11, day) for day in range(1, 31)}
    assert all(row is None for row in window.values()), (
        "the expired successor must leave November ungoverned, not resurrect January"
    )
    # …and the window that CONTAINS the successor still resolves it.
    covered = effective_schedules_by_day(
        db_session, employee.id, date(2026, 10, 1), date(2026, 11, 1)
    )
    inside_october = covered[date(2026, 10, 15)]
    assert inside_october is not None
    assert inside_october.valid_from == date(2026, 10, 1)
    assert covered[date(2026, 10, 31)] is not None

    # The LEDGER must agree, and this is the consumer that matters: the daily
    # required minutes ARE a schedule read, so the defect was never a wrong dict
    # but a wrong balance. Same weekday on both sides, so the only difference is
    # which row governs. (The contract resolver has no equivalent here — required
    # minutes come from the SCHEDULE; the contract feeds the weekly threshold.)
    from app.policy.service import calculate_employee_period

    def _required(day: date) -> int:
        result = calculate_employee_period(db_session, employee, date(day.year, day.month, 1))
        return next(item for item in result.daily_results if item.day == day).required_minutes

    governed = date(2026, 6, 15)
    ungoverned = date(2026, 11, 2)
    assert governed.weekday() == ungoverned.weekday(), "compare like with like"
    assert _required(governed) > 0, "the seeded arrangement requires time in June"
    assert _required(ungoverned) != _required(governed), (
        "the ledger resurrected the expired arrangement's required time"
    )
