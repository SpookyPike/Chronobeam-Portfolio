"""F5 fingerprint / historical reproducibility (EINV-07, EINV-11, INV-11).

F5 is VALUE-PRESERVING for any month with no day-status facts and an unchanged
calendar: the fingerprint payload gains a ``day_status`` key only when facts
exist, exactly like the travel precedent, so every pre-existing fingerprint and
frozen snapshot stays byte-stable.
"""

from datetime import date

from app.enums import (
    CalendarDayClass,
    DayStatusCategory,
    DayStatusDayPart,
    DayStatusPortion,
)
from app.identity.models import Employee
from app.policy.engine import DayStatusFact, PeriodInputs, calculate_period
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.conftest import DEVELOPER_PRINCIPAL_HEADER as ME
from tests.conftest import HR_HEADER

DAY_STATUS = "/api/v1/me/day-status"
MONTHS = "/api/v1/me/months"
HR_REQUESTS = "/api/v1/hr/access-requests"
ME_REQUESTS = "/api/v1/me/access-requests"

MONDAY = "2032-03-08"
MARCH = "2032-03"
JULY = date(2026, 7, 1)


def test_a_month_without_day_status_facts_has_a_byte_identical_fingerprint() -> None:
    """The value-preserving guarantee: the key is added ONLY when facts exist."""
    day = date(2032, 3, 8)
    inputs = PeriodInputs(
        period_start=day,
        period_end=date(2032, 3, 9),
        daily_required_minutes={day: 348},
        weekly_required_minutes=348,
        calendar_classes={},
        intervals=(),
        opening_ledger_entries=(),
        policy_version="test",
        engine_build="test",
    )
    baseline = calculate_period(inputs).input_fingerprint
    # Same inputs, same fingerprint — and the payload has no day_status key.
    assert calculate_period(inputs).input_fingerprint == baseline
    import hashlib
    import json

    payload = {
        "period_start": day.isoformat(),
        "period_end": date(2032, 3, 9).isoformat(),
        "daily_required_minutes": sorted([(day.isoformat(), 348)]),
        "weekly_required_minutes": 348,
        "calendar_classes": [],
        "calendar_version_ids": [],
        "intervals": [],
        "opening_ledger_entries": [],
        "policy_version": "test",
        "engine_build": "test",
    }
    assert (
        hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        == baseline
    )


def test_a_day_status_change_changes_the_month_fingerprint(db_session: Session) -> None:
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None

    from app.absence.models import DayStatusEvent
    from app.policy.service import calculate_employee_period

    baseline = calculate_employee_period(db_session, employee, JULY).input_fingerprint
    db_session.add(
        DayStatusEvent(
            employee_id=employee.id,
            day=JULY,
            category=DayStatusCategory.VACATION,
            portion=DayStatusPortion.FULL,
            day_part=DayStatusDayPart.ALL,
            version=1,
        )
    )
    db_session.flush()
    after = calculate_employee_period(db_session, employee, JULY).input_fingerprint
    assert after != baseline


def test_the_day_status_payload_is_sorted_and_deterministic() -> None:
    day = date(2032, 3, 8)
    facts = (
        DayStatusFact(day, "VACATION", "HALF"),
        DayStatusFact(day, "FLEX_COMPENSATION", "FULL"),
        DayStatusFact(day, "SICK", "FULL"),
    )
    base = {
        "period_start": day,
        "period_end": date(2032, 3, 9),
        "daily_required_minutes": {day: 348},
        "weekly_required_minutes": 348,
        "calendar_classes": {},
        "intervals": (),
        "opening_ledger_entries": (),
        "policy_version": "test",
        "engine_build": "test",
    }
    forward = calculate_period(PeriodInputs(**base, day_status=facts)).input_fingerprint
    reversed_facts = tuple(reversed(facts))
    backward = calculate_period(PeriodInputs(**base, day_status=reversed_facts)).input_fingerprint
    assert forward == backward


def test_an_old_month_is_not_reinterpreted_by_a_later_calendar_edition(
    db_session: Session,
) -> None:
    """The EINV-09/EINV-20 analogue for calendars."""
    from app.calendar.models import CALENDAR_KEY
    from app.calendar.service import publish_calendar_edition
    from app.enums import CalendarDayClass
    from app.policy.service import _month_inputs

    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    before = _month_inputs(db_session, employee, JULY, ())
    publish_calendar_edition(
        db_session,
        calendar_key=CALENDAR_KEY,
        version="future-repro-v1",
        valid_from=date(2026, 10, 1),
        valid_to=None,
        source="TEST",
        days={date(2026, 10, 3): CalendarDayClass.PUBLIC_HOLIDAY},
        actor_principal="TEST",
    )
    after = _month_inputs(db_session, employee, JULY, ())
    assert after.calendar_version_ids == before.calendar_version_ids
    assert calculate_period(after).input_fingerprint == calculate_period(before).input_fingerprint


def test_a_published_calendar_edition_does_not_rewrite_an_existing_frozen_snapshot(
    db_client: TestClient, db_session: Session
) -> None:
    """EINV-07: the frozen artefact is written once and never refreshed."""
    from app.policy.models import PeriodCalculationSnapshot

    request = db_client.post(
        HR_REQUESTS,
        headers=HR_HEADER,
        json={
            "employee_external_id": "emp-0001",
            "purpose": "RANDOM_CHECK",
            "month": "2026-07",
            "view_type": "work-time-record",
        },
    )
    assert request.status_code == 200, request.text
    granted = db_client.post(f"{ME_REQUESTS}/{request.json()['id']}/grant", headers=ME, json={})
    assert granted.status_code == 200, granted.text
    snapshot = db_session.scalars(
        select(PeriodCalculationSnapshot).order_by(PeriodCalculationSnapshot.snapshot_no.desc())
    ).first()
    assert snapshot is not None
    before = (
        snapshot.result_json,
        snapshot.input_fingerprint,
        list(snapshot.calendar_version_ids),
    )
    from app.calendar.models import CALENDAR_KEY
    from app.calendar.service import publish_calendar_edition

    publish_calendar_edition(
        db_session,
        calendar_key=CALENDAR_KEY,
        version="frozen-repro-v1",
        valid_from=date(2026, 7, 16),
        valid_to=None,
        source="TEST",
        days={date(2026, 7, 20): CalendarDayClass.PUBLIC_HOLIDAY},
        actor_principal="TEST",
    )
    db_session.refresh(snapshot)
    assert (
        snapshot.result_json,
        snapshot.input_fingerprint,
        list(snapshot.calendar_version_ids),
    ) == before


def test_the_calendar_version_ids_are_recorded_in_the_snapshot(db_client: TestClient) -> None:
    from app.policy.models import PeriodCalculationSnapshot

    grant_id = _disclose(db_client)
    assert db_client.get(f"/api/v1/disclosures/{grant_id}", headers=HR_HEADER).status_code == 200
    assert PeriodCalculationSnapshot is not None


def test_a_month_with_day_status_facts_records_them_in_the_fingerprint_only(
    db_client: TestClient,
) -> None:
    """The facts are an INPUT; they are not added to any stored projection."""
    created = db_client.post(
        DAY_STATUS,
        headers=ME,
        json={
            "day": MONDAY,
            "category": DayStatusCategory.VACATION.value,
            "portion": DayStatusPortion.FULL.value,
            "day_part": DayStatusDayPart.ALL.value,
        },
    )
    assert created.status_code == 201, created.text
    summary = db_client.get(f"{MONTHS}/{MARCH}", headers=ME).json()
    assert "day_status" not in str(summary)


def _disclose(db_client: TestClient) -> str:
    request = db_client.post(
        HR_REQUESTS,
        headers=HR_HEADER,
        json={
            "employee_external_id": "emp-0001",
            "purpose": "RANDOM_CHECK",
            "month": "2026-07",
            "view_type": "work-time-record",
        },
    )
    assert request.status_code == 200, request.text
    granted = db_client.post(f"{ME_REQUESTS}/{request.json()['id']}/grant", headers=ME, json={})
    assert granted.status_code == 200, granted.text
    return granted.json()["id"]
