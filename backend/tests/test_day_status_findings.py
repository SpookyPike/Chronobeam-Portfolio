"""F5 day-status findings (WT-STATUS-001..005) — one discriminating case each.

Findings are employee-private: they live in the derived result, are never part of
a disclosed payload, and they gate ``REVIEWED`` (which is intended).
"""

from datetime import date

from app.absence.models import DayStatusEvent
from app.enums import (
    DayStatusCategory,
    DayStatusDayPart,
    DayStatusPortion,
    WorkType,
)
from app.policy.engine import (
    DayStatusFact,
    PeriodInputs,
    calculate_period,
)
from app.worktime.models import WorkInterval
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.conftest import DEVELOPER_PRINCIPAL_HEADER as ME

DAY_STATUS = "/api/v1/me/day-status"
INTERVALS = "/api/v1/me/work-intervals"
FINDINGS = "/api/v1/me/findings"
MONTHS = "/api/v1/me/months"

MONDAY = "2032-03-08"
TUESDAY = "2032-03-09"
SATURDAY = "2032-03-13"
MARCH = "2032-03"


def _create(client: TestClient, **overrides):
    payload = {
        "day": MONDAY,
        "category": DayStatusCategory.VACATION.value,
        "portion": DayStatusPortion.FULL.value,
        "day_part": DayStatusDayPart.ALL.value,
    }
    payload.update(overrides)
    return client.post(DAY_STATUS, headers=ME, json=payload)


def _work(client: TestClient, day: str = MONDAY) -> None:
    response = client.post(
        INTERVALS,
        headers=ME,
        json={
            "start_local": f"{day}T08:00:00",
            "end_local": f"{day}T12:00:00",
            "work_type": WorkType.ON_SITE.value,
        },
    )
    assert response.status_code == 201, response.text


def _rules(client: TestClient, month: str = MARCH) -> list[str]:
    body = client.get(f"{FINDINGS}?period={month}", headers=ME).json()
    return [finding["rule_id"] for finding in body["findings"]]


def test_WT_STATUS_001_work_on_a_full_vacation_day(db_client: TestClient) -> None:
    _work(db_client)
    assert _create(db_client).status_code == 201
    assert "WT-STATUS-001" in _rules(db_client)


def test_WT_STATUS_002_work_on_a_sickness_day(db_client: TestClient) -> None:
    _work(db_client)
    assert _create(db_client, category=DayStatusCategory.SICK.value).status_code == 201
    assert "WT-STATUS-002" in _rules(db_client)


def test_WT_STATUS_002_message_is_honest_about_unresolved_accounting(
    db_client: TestClient,
) -> None:
    """The finding must not contradict the figures rendered beside it.

    Regression from the unexpected-work browser acceptance pass: the message
    asserted the earlier hours were "not reported as worked time" while the same
    day panel showed WORKED 3:00 / CREDITED 3:00, and the engine does credit them
    on the ordinary path. The wording now states preservation plus unresolved
    accounting, and leaks no internal register id to the employee.
    """
    _work(db_client)
    assert _create(db_client, category=DayStatusCategory.SICK.value).status_code == 201
    findings = db_client.get(f"{FINDINGS}?period={MARCH}", headers=ME).json()["findings"]
    message = next(item for item in findings if item["rule_id"] == "WT-STATUS-002")["message"]

    # Recorded work is preserved and remains visible as worked time.
    assert "kept" in message
    assert "shown as worked time" in message
    # The final accounting treatment is openly unresolved, not asserted settled.
    assert "still being confirmed" in message
    # …and the finding points the employee at the two facts to review.
    assert "interval" in message and "sickness day" in message
    # No internal register id, engine vocabulary or raw enum reaches the employee.
    for leak in ("OPEN-", "ABSENCE-", "WT-STATUS-", "BLOCKED_POLICY", "SICK", "POLICY_BLOCKED"):
        assert leak not in message

    # The arithmetic the message sits beside is UNCHANGED (goldens): a full sick
    # day zeroes the requirement, keeps the work fact, and credits it ordinarily.
    row = next(
        item
        for item in db_client.get(f"{MONTHS}/{MARCH}", headers=ME).json()["daily_results"]
        if item["date"] == MONDAY
    )
    assert row["required_minutes"] == 0
    assert row["worked_minutes"] == 240
    assert row["account_credit_minutes"] == 240
    assert row["delta_minutes"] == 240


def test_WT_STATUS_003_work_on_a_full_flex_compensation_day(db_client: TestClient) -> None:
    _work(db_client)
    assert _create(db_client, category=DayStatusCategory.FLEX_COMPENSATION.value).status_code == 201
    assert "WT-STATUS-003" in _rules(db_client)


def test_shift_compensation_with_work_raises_no_finding_by_design(
    db_client: TestClient,
) -> None:
    """Drift guard for an OPEN policy question, not a defect.

    The spec is internally ambiguous about shift compensation: ABSENCE-003 cites
    ``WT-STATUS-003`` for BOTH compensation categories, while ``WT-STATUS-003``'s
    own definition is FLEX-only and ABSENCE-007's reconciliation list names only
    001/002/003. The engine implements the narrow reading. Resolving the
    ambiguity is an HR/WoCo decision, and adding a CRITICAL finding where none is
    specified would be a tightening (INV-15) — so the current behaviour is pinned
    rather than changed.
    """
    # The contrast that makes the guard meaningful: FLEX fires on its own day.
    _work(db_client, TUESDAY)
    assert (
        _create(
            db_client,
            day=TUESDAY,
            category=DayStatusCategory.FLEX_COMPENSATION.value,
        ).status_code
        == 201
    )
    _work(db_client)
    assert (
        _create(db_client, category=DayStatusCategory.SHIFT_COMPENSATION.value).status_code == 201
    )

    rules = _rules(db_client)
    assert "WT-STATUS-003" in rules  # the FLEX day still fires
    # The SHIFT day contributes nothing: only one status finding exists, and it
    # belongs to the FLEX day.
    status_findings = [
        item
        for item in db_client.get(f"{FINDINGS}?period={MARCH}", headers=ME).json()["findings"]
        if item["rule_id"].startswith("WT-STATUS-")
    ]
    assert [item["date"] for item in status_findings] == [TUESDAY]


def test_WT_STATUS_004_day_status_coverage_exceeding_one_day(db_session: Session) -> None:
    """The engine clamp is the BACKSTOP for restored or imported data.

    The API refuses an over-covered day outright, so this condition is only
    reachable through a restore/import — which is exactly why the engine must
    still clamp and flag it.
    """
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
        day_status=(
            DayStatusFact(day, "VACATION", "HALF"),
            DayStatusFact(day, "VACATION", "HALF"),
            DayStatusFact(day, "SICK", "FULL"),
        ),
    )
    result = calculate_period(inputs)
    rules = {finding.rule_id for finding in result.findings}
    assert "WT-STATUS-004" in rules
    # Clamped: never more than a whole day is reduced.
    assert result.daily_results[0].required_minutes == 0


def test_WT_STATUS_005_day_status_on_a_non_working_day(db_client: TestClient) -> None:
    """Defined against the SCHEDULE (a weekend has a zero requirement)."""
    assert _create(db_client, day=SATURDAY).status_code == 201
    assert "WT-STATUS-005" in _rules(db_client)


def test_WT_STATUS_005_does_not_fire_on_a_scheduled_working_day(db_client: TestClient) -> None:
    assert _create(db_client).status_code == 201
    assert "WT-STATUS-005" not in _rules(db_client)


def test_VAC_002_a_half_day_vacation_is_not_treated_as_a_full_day_absence(
    db_client: TestClient,
) -> None:
    """A half day of vacation counts as half a day (VAC-002)."""
    _work(db_client)
    assert (
        _create(
            db_client,
            portion=DayStatusPortion.HALF.value,
            day_part=DayStatusDayPart.MORNING.value,
        ).status_code
        == 201
    )
    rules = _rules(db_client)
    assert "WT-STATUS-001" not in rules  # the finding is a FULL-day rule
    summary = db_client.get(f"{MONTHS}/{MARCH}", headers=ME).json()
    row = next(item for item in summary["daily_results"] if item["date"] == MONDAY)
    assert row["required_minutes"] == 174
    assert row["worked_minutes"] == 240


def test_ABSENCE_007_the_reconciliation_finding_never_deletes_the_work_fact(
    db_client: TestClient, db_session: Session
) -> None:
    """Imported/recorded status must never delete employee-entered actual work."""
    _work(db_client)
    assert _create(db_client, category=DayStatusCategory.SICK.value).status_code == 201
    assert "WT-STATUS-002" in _rules(db_client)
    rows = db_session.scalars(
        select(WorkInterval).where(WorkInterval.employee_id.is_not(None))
    ).all()
    assert rows, "the recorded interval was removed by the reconciliation"
    summary = db_client.get(f"{MONTHS}/{MARCH}", headers=ME).json()
    row = next(item for item in summary["daily_results"] if item["date"] == MONDAY)
    assert row["worked_minutes"] == 240


def test_findings_are_never_part_of_the_month_summary(db_client: TestClient) -> None:
    _create(db_client)
    summary = db_client.get(f"{MONTHS}/{MARCH}", headers=ME).json()
    assert "findings" not in summary


def test_a_status_finding_gates_the_reviewed_transition(db_client: TestClient) -> None:
    """WT-STATUS-* gate ``REVIEWED``; they never block a submission."""
    _work(db_client)
    _create(db_client, category=DayStatusCategory.SICK.value)
    refused = db_client.post(f"{MONTHS}/{MARCH}/review", headers=ME, json={"acknowledged": False})
    assert refused.status_code == 422, refused.text
    accepted = db_client.post(f"{MONTHS}/{MARCH}/review", headers=ME, json={"acknowledged": True})
    assert accepted.status_code == 200, accepted.text


def test_a_day_status_row_never_carries_a_comment_or_diagnosis(db_session: Session) -> None:
    columns = {column.name for column in DayStatusEvent.__table__.columns}
    assert "comment" not in columns
    assert "reason" not in columns
    assert "diagnosis" not in columns
