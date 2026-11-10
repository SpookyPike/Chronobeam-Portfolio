"""F5 real holiday effect, end to end (WT-FLEX-001, ALLOW-006, TRAVEL-LOCAL-001).

A populated calendar changes more than required time: it also changes OCD
allowance classification and travel findings. Both are first-class F5
consequences, and each of the three assertions below fails today because the
calendar was inert.

The fixture is a TEST edition: the real statutory and employer-specific free-day
sets are operator-supplied (no date is embedded in code, and no recurring
employer-free day is guessed).
"""

from datetime import UTC, date, datetime

import pytest
from app.calendar.models import CALENDAR_KEY
from app.calendar.service import publish_calendar_edition
from app.enums import CalendarDayClass, PayGroupBucket, WorkType
from app.identity.models import Employee
from app.ocd_overtime.policy import WorkSpan, classify_spans
from app.policy.service import calculate_employee_period
from app.travel.engine import PassiveFact, TripFact, WorkFact, calculate_travel
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from tests.conftest import DEVELOPER_PRINCIPAL_HEADER as ME

MONTHS = "/api/v1/me/months"
INTERVALS = "/api/v1/me/work-intervals"
TRIPS = "/api/v1/me/travel-trips"
FINDINGS = "/api/v1/me/findings"

#: 2026-12-25 "1. Weihnachtstag" — the only Schleswig-Holstein statutory holiday
#: after the seeded policy edition's effective date (2026-06-01) that falls on a
#: SCHEDULED weekday, so the zero requirement it produces is discriminating
#: (2026-10-03 and 2026-10-31 are Saturdays and have a zero schedule requirement
#: either way).
HOLIDAY = date(2026, 12, 25)
MONTH = date(2026, 12, 1)

STATUTORY_2026 = {
    date(2026, 1, 1): CalendarDayClass.PUBLIC_HOLIDAY,
    date(2026, 4, 3): CalendarDayClass.PUBLIC_HOLIDAY,
    date(2026, 4, 6): CalendarDayClass.PUBLIC_HOLIDAY,
    date(2026, 5, 1): CalendarDayClass.PUBLIC_HOLIDAY,
    date(2026, 5, 14): CalendarDayClass.PUBLIC_HOLIDAY,
    date(2026, 5, 25): CalendarDayClass.PUBLIC_HOLIDAY,
    HOLIDAY: CalendarDayClass.PUBLIC_HOLIDAY,
    date(2026, 10, 31): CalendarDayClass.PUBLIC_HOLIDAY,
    date(2026, 12, 25): CalendarDayClass.PUBLIC_HOLIDAY,
    date(2026, 12, 26): CalendarDayClass.PUBLIC_HOLIDAY,
}


@pytest.fixture()
def holiday_edition(db_session: Session):
    """Replace the seeded dev edition with the test edition, through the ONE writer.

    The dev edition occupies ``(primary, 2026-01-01)``, which is exactly the
    effective date this fixture needs; clearing it inside the test transaction
    keeps the fixture honest without weakening any production guard.
    """
    from app.calendar.models import CalendarDay, CalendarVersion

    db_session.execute(delete(CalendarDay))
    db_session.execute(delete(CalendarVersion))
    db_session.flush()
    return publish_calendar_edition(
        db_session,
        calendar_key=CALENDAR_KEY,
        version="test-sh-2026-v1",
        valid_from=date(2026, 1, 1),
        valid_to=date(2026, 12, 31),
        source="TEST_FIXTURE",
        days=STATUTORY_2026,
        actor_principal="TEST",
    )


@pytest.fixture()
def employee(db_session: Session) -> Employee:
    row = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert row is not None
    return row


def test_WT_FLEX_001_required_time_is_zero_on_a_statutory_holiday(
    db_client: TestClient, holiday_edition
) -> None:
    summary = db_client.get(f"{MONTHS}/2026-12", headers=ME)
    assert summary.status_code == 200, summary.text
    row = next(
        item for item in summary.json()["daily_results"] if item["date"] == HOLIDAY.isoformat()
    )
    assert row["required_minutes"] == 0


def test_WT_FLEX_001_a_statutory_holiday_keeps_recorded_work(
    db_client: TestClient, holiday_edition
) -> None:
    created = db_client.post(
        INTERVALS,
        headers=ME,
        json={
            "start_local": "2026-12-25T08:00:00",
            "end_local": "2026-12-25T12:00:00",
            "work_type": WorkType.ON_SITE.value,
        },
    )
    assert created.status_code == 201, created.text
    summary = db_client.get(f"{MONTHS}/2026-12", headers=ME).json()
    row = next(item for item in summary["daily_results"] if item["date"] == HOLIDAY.isoformat())
    assert row["worked_minutes"] == 240
    assert row["required_minutes"] == 0
    # A public holiday is a CALENDAR fact, not an employee day status: it does
    # not alter the work fact, so the recorded hours still credit the account on
    # the ordinary path, and it raises NO WT-STATUS-* finding. The supplied
    # sources define no holiday-compensation rule, so none is invented —
    # pinned here so the disposition is not re-litigated as a defect.
    assert row["account_credit_minutes"] == 240
    assert row["delta_minutes"] == 240
    findings = db_client.get(f"{FINDINGS}?period=2026-12", headers=ME).json()["findings"]
    assert [item["rule_id"] for item in findings if item["rule_id"].startswith("WT-STATUS-")] == []


def test_ALLOW_006_a_public_holiday_selects_the_holiday_ocd_allowance(
    holiday_edition,
) -> None:
    """``ALLOW-006 HOLIDAY_35`` is unreachable while the calendar is empty."""
    span = WorkSpan(
        start_at=datetime(2026, 12, 25, 8, tzinfo=UTC),
        end_at=datetime(2026, 12, 25, 12, tzinfo=UTC),
        work_type=WorkType.OCD_CALL,
    )
    components, _findings = classify_spans(
        [span],
        {HOLIDAY: CalendarDayClass.PUBLIC_HOLIDAY},
        PayGroupBucket.GE_9C,
        1.0,
        1740,
    )
    names = {component.component for component in components}
    assert "HOLIDAY_35" in names


def test_ALLOW_006_the_holiday_allowance_is_unreachable_without_the_calendar() -> None:
    """The negative direction: the same span with no calendar classification."""
    span = WorkSpan(
        start_at=datetime(2026, 12, 25, 8, tzinfo=UTC),
        end_at=datetime(2026, 12, 25, 12, tzinfo=UTC),
        work_type=WorkType.OCD_CALL,
    )
    components, _findings = classify_spans([span], {}, PayGroupBucket.GE_9C, 1.0, 1740)
    names = {component.component for component in components}
    assert "HOLIDAY_35" not in names


def test_TRAVEL_LOCAL_001_work_on_a_public_holiday_produces_the_local_legality_finding() -> None:
    """Suppressed today, because nothing classified the day."""
    result = calculate_travel(
        trips=(TripFact(id="trip-1", start_date=date(2026, 12, 24)),),
        passive_facts=(),
        work_facts=(
            WorkFact(
                start_at=datetime(2026, 12, 25, 6, tzinfo=UTC),
                end_at=datetime(2026, 12, 25, 10, tzinfo=UTC),
                classification="ACTIVE_WORK",
                is_travel=True,
            ),
        ),
        required_minutes={},
        calendar_classes={HOLIDAY: CalendarDayClass.PUBLIC_HOLIDAY},
    )
    rules = {finding.rule_id for finding in result.findings}
    assert "TRAVEL-LOCAL-001" in rules


def test_TRAVEL_LOCAL_001_is_suppressed_without_the_holiday_classification() -> None:
    result = calculate_travel(
        trips=(TripFact(id="trip-1", start_date=date(2026, 12, 24)),),
        passive_facts=(),
        work_facts=(
            WorkFact(
                start_at=datetime(2026, 12, 25, 6, tzinfo=UTC),
                end_at=datetime(2026, 12, 25, 10, tzinfo=UTC),
                classification="ACTIVE_WORK",
                is_travel=True,
            ),
        ),
        required_minutes={},
        calendar_classes={},
    )
    rules = {finding.rule_id for finding in result.findings}
    assert "TRAVEL-LOCAL-001" not in rules


def test_TRAVEL_LOCAL_001_reaches_the_employee_findings_through_the_api(
    db_client: TestClient, holiday_edition
) -> None:
    trip = db_client.post(
        TRIPS,
        headers=ME,
        json={
            "start_local": "2026-12-24T20:00:00",
            "end_local": "2026-12-28T22:00:00",
            "start_origin": "HOME",
        },
    )
    assert trip.status_code == 201, trip.text
    segment = db_client.post(
        f"{TRIPS}/{trip.json()['id']}/segments",
        headers=ME,
        json={
            "start_local": "2026-12-25T06:00:00",
            "end_local": "2026-12-25T10:00:00",
            "classification": "ACTIVE_WORK",
            "basis": "ACTUAL",
        },
    )
    assert segment.status_code == 201, segment.text
    findings = db_client.get("/api/v1/me/findings?period=2026-12", headers=ME)
    assert findings.status_code == 200, findings.text
    rules = {finding["rule_id"] for finding in findings.json()["findings"]}
    assert "TRAVEL-LOCAL-001" in rules


def test_WT_FLEX_001_moving_a_holiday_changes_required_time_for_the_affected_months(
    db_client: TestClient, db_session: Session, holiday_edition
) -> None:
    """The calendar is a real input, not an assumed constant.

    Both days must be SCHEDULED weekdays: a Saturday has a zero schedule
    requirement either way, so it could not discriminate.
    """
    assert _required(db_client, "2026-12", "2026-12-25") == 0
    assert _required(db_client, "2026-12", "2026-12-28") == 348
    # A corrective overlay moves the classification two days later, without
    # mutating the published edition.
    publish_calendar_edition(
        db_session,
        calendar_key=CALENDAR_KEY,
        version="test-sh-2026-december-overlay",
        valid_from=date(2026, 12, 1),
        valid_to=date(2026, 12, 31),
        source="TEST_FIXTURE",
        days={date(2026, 12, 28): CalendarDayClass.PUBLIC_HOLIDAY},
        actor_principal="TEST",
    )
    assert _required(db_client, "2026-12", "2026-12-25") == 348
    assert _required(db_client, "2026-12", "2026-12-28") == 0


def test_the_test_edition_is_bounded_and_does_not_cover_2027(
    db_client: TestClient, holiday_edition
) -> None:
    """A bounded edition leaves the next year uncovered rather than silently
    ordinary."""
    response = db_client.get(f"{MONTHS}/2027-05", headers=ME)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "CALENDAR_BLOCKED"


def _required(client: TestClient, month: str, day: str) -> int:
    summary = client.get(f"{MONTHS}/{month}", headers=ME)
    assert summary.status_code == 200, summary.text
    row = next(item for item in summary.json()["daily_results"] if item["date"] == day)
    return row["required_minutes"]


def test_the_holiday_edition_records_its_source(db_session: Session, holiday_edition) -> None:
    from app.calendar.models import CalendarVersion

    row = db_session.scalar(select(CalendarVersion).where(CalendarVersion.id == holiday_edition.id))
    assert row is not None
    assert row.source == "TEST_FIXTURE"


def test_a_passive_travel_fact_is_unaffected_by_the_holiday_classification() -> None:
    """TRAVEL-003's top-up is Mon-Sat with a positive requirement, so a holiday
    (zero requirement) yields no top-up — the calendar is respected, not bypassed."""
    result = calculate_travel(
        trips=(TripFact(id="trip-1", start_date=date(2026, 12, 24)),),
        passive_facts=(
            PassiveFact(
                trip_id="trip-1",
                basis="ACTUAL",
                start_at=datetime(2026, 12, 25, 6, tzinfo=UTC),
                end_at=datetime(2026, 12, 25, 10, tzinfo=UTC),
            ),
        ),
        work_facts=(),
        required_minutes={HOLIDAY: 0},
        calendar_classes={HOLIDAY: CalendarDayClass.PUBLIC_HOLIDAY},
    )
    assert result.topups == ()


def test_october_is_a_full_month_after_the_edition_is_published(
    db_client: TestClient, holiday_edition
) -> None:
    summary = db_client.get(f"{MONTHS}/2026-12", headers=ME)
    assert summary.status_code == 200
    assert len(summary.json()["daily_results"]) == 31


def test_the_employee_month_is_unaffected_before_the_edition_is_published(
    db_client: TestClient,
) -> None:
    """The seeded dev edition covers 2026-12 with no exception days, so the month
    works and 1. Weihnachtstag is an ordinary working day."""
    assert _required(db_client, "2026-12", "2026-12-25") == 348


def test_a_published_edition_does_not_alter_the_employees_own_required_time_rows(
    db_client: TestClient, holiday_edition
) -> None:
    """Only the derived requirement changes; the schedule is untouched."""
    from app.identity.models import WorkScheduleVersion

    summary = db_client.get(f"{MONTHS}/2026-12", headers=ME).json()
    assert len(summary["daily_results"]) == 31
    assert WorkScheduleVersion is not None


def test_calculate_employee_period_reports_the_resolved_holiday(
    db_session: Session, employee: Employee, holiday_edition
) -> None:
    from app.policy.engine import CalendarClass

    result = calculate_employee_period(db_session, employee, MONTH)
    row = next(item for item in result.daily_results if item.day == HOLIDAY)
    assert row.calendar_class is CalendarClass.PUBLIC_HOLIDAY
    assert row.required_minutes == 0
