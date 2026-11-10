"""F6.4 compensation-day METRICS and the calendar-class projection.

``WT-FLEX-008`` stays **recorded, not enforced** (Major 2): the supplied sources
state the limits but not how a HALF day counts against them, nor the precise
meaning of "per year" (``OPEN-13``). So F6 exposes read-only counts, names
both figures as *limits* marked ``enforced: false``, and emits **no finding at
any count**. The load-bearing guard is
``test_WT_FLEX_008_emits_no_finding_at_any_count``: removing it is how a
false-authority finding would creep back in.

``EINV-25``: a derived metric is not a calculation input — the metrics are
computed on read, never enter ``PeriodInputs``/the fingerprint/``EINV-15``'s
reopen set, and never gate ``review_month``.

The calendar-class half of this module covers the F5 hand-off: the employee must
be able to see WHY a day's requirement is zero, without the class ever reaching
the frozen disclosure payload.
"""

from datetime import date, timedelta
from typing import Any

from app.absence.models import DayStatusEvent
from app.calendar.models import CalendarDay, CalendarVersion
from app.calendar.service import publish_calendar_edition
from app.enums import CalendarDayClass, DayStatusCategory, DayStatusDayPart, DayStatusPortion
from app.identity.models import Employee
from app.policy.engine import CalendarClass
from app.policy.service import calculate_employee_period
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from tests.conftest import DEVELOPER_PRINCIPAL_HEADER as ME
from tests.conftest import HR_HEADER, OTHER_EMPLOYEE_HEADER

DAY_STATUS = "/api/v1/me/day-status"
FINDINGS = "/api/v1/me/findings"
MONTHS = "/api/v1/me/months"
MARCH = "2032-03"
APRIL = "2032-04"
#: A Monday inside the fixture month, and a Saturday (scheduled requirement 0).
MONDAY = date(2032, 3, 8)
SATURDAY = date(2032, 3, 13)


def _employee(session: Session, subject: str = "employee") -> Employee:
    row = session.scalar(select(Employee).where(Employee.idp_subject == subject))
    assert row is not None, subject
    return row


def _weekdays(start: date, count: int) -> list[date]:
    days: list[date] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _saturdays(start: date, count: int) -> list[date]:
    days: list[date] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() == 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _record(
    client: TestClient,
    days: list[date],
    category: DayStatusCategory = DayStatusCategory.FLEX_COMPENSATION,
    portion: DayStatusPortion = DayStatusPortion.FULL,
) -> None:
    for day in days:
        response = client.post(
            DAY_STATUS,
            headers=ME,
            json={
                "day": day.isoformat(),
                "category": category.value,
                "portion": portion.value,
                "day_part": (
                    DayStatusDayPart.ALL.value
                    if portion is DayStatusPortion.FULL
                    else DayStatusDayPart.MORNING.value
                ),
            },
        )
        assert response.status_code == 201, (day, response.text)


def _metrics(client: TestClient, period: str = MARCH) -> dict[str, Any]:
    response = client.get(DAY_STATUS, headers=ME, params={"period": period})
    assert response.status_code == 200, response.text
    return response.json()["compensation_metrics"]


def _rules(client: TestClient, period: str = MARCH) -> list[str]:
    response = client.get(FINDINGS, headers=ME, params={"period": period})
    assert response.status_code == 200, response.text
    return [item["rule_id"] for item in response.json()["findings"]]


# ---------------------------------------------------------------------------
# WT-FLEX-008 — metrics, never authority
# ---------------------------------------------------------------------------


def test_WT_FLEX_008_emits_no_finding_at_any_count(db_client: TestClient) -> None:
    """THE load-bearing guard: no count produces a ``WT-FLEX-008`` finding.

    0, 19, 20, 21 and 40 full days plus 30 half days — every interesting count —
    and the employee-private finding surface never carries the rule. The counts
    themselves are reported.
    """
    assert _metrics(db_client)["flex_compensation_full_days"] == 0

    weekdays = _weekdays(date(2032, 1, 5), 51)
    # Recorded INCREMENTALLY: one day carries one compensation fact, so the
    # boundary counts are reached by adding a day, not by re-recording the set.
    _record(db_client, weekdays[:19])
    assert _metrics(db_client)["flex_compensation_full_days"] == 19
    assert "WT-FLEX-008" not in _rules(db_client)
    _record(db_client, weekdays[19:20])
    assert _metrics(db_client)["flex_compensation_full_days"] == 20
    assert "WT-FLEX-008" not in _rules(db_client)
    _record(db_client, weekdays[20:21])
    assert _metrics(db_client)["flex_compensation_full_days"] == 21
    assert "WT-FLEX-008" not in _rules(db_client)

    # Half days are reported SEPARATELY, and 30 of them is still no finding.
    # They sit on their own days: one day carries one compensation fact.
    _record(db_client, weekdays[21:51], portion=DayStatusPortion.HALF)
    metrics = _metrics(db_client)
    assert metrics["flex_compensation_full_days"] == 21
    assert metrics["flex_compensation_half_days"] == 30
    assert "WT-FLEX-008" not in _rules(db_client)
    assert "WT-FLEX-008" not in _rules(db_client, APRIL)


def test_the_metrics_split_full_and_half_and_never_combine_them(db_client: TestClient) -> None:
    """Combining them IS the unresolved weighting decision, so it cannot exist."""
    _record(db_client, _weekdays(date(2032, 3, 1), 6))
    _record(db_client, _weekdays(date(2032, 4, 1), 3), portion=DayStatusPortion.HALF)
    metrics = _metrics(db_client)
    assert metrics["flex_compensation_full_days"] == 6
    assert metrics["flex_compensation_half_days"] == 3
    assert not [key for key in metrics if "total" in key]


def test_the_metrics_exclude_zero_requirement_days_from_the_counts(
    db_client: TestClient,
) -> None:
    """ "A status row exists" is not "a policy day consumed".

    A ``FLEX_COMPENSATION`` row on a Saturday (the seeded schedule sets
    ``sat_required_minutes = 0``) is a record of intent: it is surfaced on its own
    and excluded from both counts — the ``WT-STATUS-005`` discriminator.
    """
    _record(db_client, [MONDAY])
    _record(db_client, [SATURDAY])
    metrics = _metrics(db_client)
    assert metrics["flex_compensation_full_days"] == 1
    assert metrics["flex_compensation_half_days"] == 0
    assert metrics["flex_compensation_zero_requirement_days"] == 1
    # The discriminator is the engine's own, so the day is flagged there too.
    assert "WT-STATUS-005" in _rules(db_client)


def test_the_limits_are_reported_as_limits_and_marked_unenforced(db_client: TestClient) -> None:
    limits = _metrics(db_client)["limits"]
    assert limits["max_days_per_year"] == 20
    assert limits["max_consecutive_days"] == 5
    assert limits["enforced"] is False
    assert "POL-01" in limits["source"]
    # The sample policy is the citation for BOTH figures, and it must say so
    # plainly: these are configuration values, never a legal requirement.
    assert "sample" in limits["source"].lower()
    assert "not a legal requirement" in limits["source"]


def test_the_response_names_only_the_still_unresolved_counting_treatment(
    db_client: TestClient,
) -> None:
    """The note names ONLY what is still open, in plain language.

    ``POL-01`` settles the year scope (20 days per calendar year), so still telling
    the employee that "the precise meaning of 'per year'" is being confirmed would
    be a stale claim about the current policy. The half-day weighting is the one
    detail that remains open, and the count must still be marked unenforced.

    The internal open-decision reference (``OPEN-13``) is NOT part of the
    user-facing note: it is traceability, and it belongs in the review material,
    not on an employee screen.

    Mutation-proof: this fails if the note regresses to the two-detail wording,
    or if the register reference leaks back into rendered copy.
    """
    note = _metrics(db_client)["counting_note"]
    assert "OPEN-13" not in note
    assert "internal register" not in note
    assert "half day" in note
    assert "not a limit we apply" in note
    assert "per year" not in note, "the resolved year scope is claimed unresolved again"


def test_the_longest_run_is_reported_without_a_verdict(db_client: TestClient) -> None:
    """Six consecutive full days is an OBSERVED run, not a violation.

    Six consecutive CALENDAR days can only all count if Saturday is a working
    day, so the employee's own schedule is set that way first — otherwise the run
    would stop at five and the test could not exceed the 5-day limit at all.
    """
    scheduled = db_client.put(
        "/api/v1/me/working-time",
        headers=ME,
        json={
            "valid_from": "2032-01-01",
            "weekly_required_minutes": 2880,
            "mon_required_minutes": 480,
            "tue_required_minutes": 480,
            "wed_required_minutes": 480,
            "thu_required_minutes": 480,
            "fri_required_minutes": 480,
            "sat_required_minutes": 480,
            "sun_required_minutes": 0,
        },
    )
    assert scheduled.status_code == 200, scheduled.text
    _record(db_client, [date(2032, 3, 1) + timedelta(days=offset) for offset in range(6)])
    metrics = _metrics(db_client)
    assert metrics["flex_compensation_longest_run_days"] == 6
    assert metrics["flex_compensation_zero_requirement_days"] == 0
    assert "WT-FLEX-008" not in _rules(db_client)


def test_shift_compensation_days_are_counted_separately(db_client: TestClient) -> None:
    """Never deducted from the flex figures (``POL-05``, explicit)."""
    _record(db_client, _weekdays(date(2032, 3, 1), 2))
    _record(
        db_client,
        _weekdays(date(2032, 4, 1), 30),
        category=DayStatusCategory.SHIFT_COMPENSATION,
    )
    metrics = _metrics(db_client)
    assert metrics["shift_compensation_days"] == 30
    assert metrics["flex_compensation_full_days"] == 2


def test_WT_FLEX_008_the_validation_never_gates_the_review(db_client: TestClient) -> None:
    """``review_month`` refuses while ``result.findings`` is non-empty, so a
    year-scoped metric must never become a finding — 21 full days still review."""
    _record(db_client, _weekdays(date(2032, 3, 1), 21))
    response = db_client.post(f"{MONTHS}/{MARCH}/review", headers=ME, json={"acknowledged": True})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "REVIEWED"


def test_EINV_25_the_metrics_are_not_a_calculation_input(
    db_client: TestClient, db_session: Session
) -> None:
    """A YEAR-scoped count must not make one month depend on other months' facts.

    This is the discriminating direction: the facts are recorded in APRIL, so if
    the year-scoped count were an engine input, March's fingerprint would move.
    (Day-status facts inside the month ARE inputs — F5 — which is a different,
    intended thing.)
    """
    employee = _employee(db_session)
    before = calculate_employee_period(db_session, employee, date(2032, 3, 1))
    _record(db_client, _weekdays(date(2032, 4, 1), 5))
    after = calculate_employee_period(db_session, employee, date(2032, 3, 1))
    assert after.input_fingerprint == before.input_fingerprint
    assert after.closing_balance_minutes == before.closing_balance_minutes
    # ...while the metric itself does see them, so the guard is not vacuous.
    assert _metrics(db_client, APRIL)["flex_compensation_full_days"] == 5


def test_the_metrics_are_employee_scoped(db_client: TestClient) -> None:
    _record(db_client, _weekdays(date(2032, 3, 1), 4))
    other = db_client.get(DAY_STATUS, headers=OTHER_EMPLOYEE_HEADER, params={"period": MARCH})
    assert other.status_code == 200, other.text
    assert other.json()["compensation_metrics"]["flex_compensation_full_days"] == 0


def test_WT_FLEX_008_the_response_never_claims_an_entitlement(db_client: TestClient) -> None:
    """No "remaining"/"entitled"/"exceeded" field or label anywhere.

    The 20 is a policy LIMIT, not an entitlement (the F5 verification report),
    and no limit is enforced.
    """
    payload = db_client.get(DAY_STATUS, headers=ME, params={"period": MARCH}).json()
    text = str(payload).lower()
    for forbidden in ("remaining", "entitle", "exceed", "available"):
        assert forbidden not in text, forbidden


# ---------------------------------------------------------------------------
# Calendar classification reaches the employee (F5 hand-off, E8)
# ---------------------------------------------------------------------------


def _holiday_edition(session: Session, holiday: date) -> None:
    """Replace the seeded edition with one that classifies ``holiday``.

    The dev edition occupies ``(primary, 2026-01-01)`` open-ended; clearing
    it inside the test transaction keeps the fixture honest without weakening any
    production guard (the ``test_calendar_effects`` pattern).
    """
    from app.calendar.models import CALENDAR_KEY

    session.execute(delete(CalendarDay))
    session.execute(delete(CalendarVersion))
    session.flush()
    publish_calendar_edition(
        session,
        calendar_key=CALENDAR_KEY,
        version="test-2032-v1",
        valid_from=date(2032, 1, 1),
        valid_to=date(2032, 12, 31),
        source="TEST_FIXTURE",
        days={holiday: CalendarDayClass.PUBLIC_HOLIDAY},
        actor_principal="TEST",
    )


def _daily(db_client: TestClient, period: str = MARCH) -> dict[str, Any]:
    body = db_client.get(f"{MONTHS}/{period}", headers=ME).json()
    return {row["date"]: row for row in body["daily_results"]}


def test_the_month_projection_carries_the_resolved_calendar_class(
    db_client: TestClient, db_session: Session
) -> None:
    _holiday_edition(db_session, MONDAY)
    days = _daily(db_client)
    assert days[MONDAY.isoformat()]["calendar_class"] == CalendarClass.PUBLIC_HOLIDAY.value
    assert days[MONDAY.isoformat()]["required_minutes"] == 0
    # The complement: an ordinary working day inside the same edition.
    assert days[date(2032, 3, 9).isoformat()]["calendar_class"] == CalendarClass.WEEKDAY.value
    assert days[date(2032, 3, 9).isoformat()]["required_minutes"] > 0


def test_the_projection_explains_a_credited_but_required_day(db_client: TestClient) -> None:
    """``account_credit_minutes`` makes the credit rule legible.

    ``delta_minutes`` IS the flex delta, so without the credit field a corrected
    credit rule shows as an unexplained arithmetic difference.
    """
    response = db_client.post(
        "/api/v1/me/work-intervals",
        headers=ME,
        json={
            "start_local": f"{date(2032, 3, 9).isoformat()}T08:00:00",
            "end_local": f"{date(2032, 3, 9).isoformat()}T12:00:00",
            "work_type": "ON_SITE",
        },
    )
    assert response.status_code == 201, response.text
    day = _daily(db_client)[date(2032, 3, 9).isoformat()]
    assert day["worked_minutes"] == 240
    assert day["account_credit_minutes"] == 240
    assert day["delta_minutes"] == day["account_credit_minutes"] - day["required_minutes"]


def test_the_disclosed_payload_still_excludes_the_calendar_class(
    db_client: TestClient, db_session: Session
) -> None:
    """The frozen recipient payload must not gain the class (the F5 residual)."""
    from app.disclosure.models import SubmissionPackage

    _holiday_edition(db_session, MONDAY)
    response = db_client.post(
        "/api/v1/me/work-intervals",
        headers=ME,
        json={
            "start_local": f"{MONDAY.isoformat()}T08:00:00",
            "end_local": f"{MONDAY.isoformat()}T12:00:00",
            "work_type": "ON_SITE",
        },
    )
    assert response.status_code == 201, response.text
    # The real disclosure flow: HR requests a month, the employee grants it, HR
    # reads the frozen projection. That projection is where a leak would show.
    request = db_client.post(
        "/api/v1/hr/access-requests",
        headers=HR_HEADER,
        json={
            "employee_external_id": "emp-0001",
            "purpose": "RANDOM_CHECK",
            "month": MARCH,
            "view_type": "work-time-record",
        },
    )
    assert request.status_code == 200, request.text
    granted = db_client.post(
        f"/api/v1/me/access-requests/{request.json()['id']}/grant", headers=ME, json={}
    )
    assert granted.status_code == 200, granted.text
    view = db_client.get(f"/api/v1/disclosures/{granted.json()['id']}", headers=HR_HEADER)
    assert view.status_code == 200, view.text
    for row in view.json()["snapshot"]["daily"]:
        assert set(row) == {"date", "worked_minutes", "required_minutes", "delta_minutes"}
    assert "calendar_class" not in view.text
    assert "account_credit_minutes" not in view.text
    assert "PUBLIC_HOLIDAY" not in view.text
    assert db_session.scalars(select(SubmissionPackage)).all() == []


def test_the_calendar_class_is_not_writable_by_any_employee_route(
    db_client: TestClient, db_session: Session
) -> None:
    """EINV-10/EINV-21: the employee layer can never rewrite the classification."""
    _holiday_edition(db_session, MONDAY)
    before = db_session.scalar(select(CalendarDay).where(CalendarDay.day == MONDAY))
    assert before is not None and before.day_class is CalendarDayClass.PUBLIC_HOLIDAY

    # A day-status fact ON the holiday day, plus one on the next working day:
    # neither can rewrite the classification.
    for day in (MONDAY, date(2032, 3, 9)):
        response = db_client.post(
            DAY_STATUS,
            headers=ME,
            json={
                "day": day.isoformat(),
                "category": "FLEX_COMPENSATION",
                "portion": "FULL",
            },
        )
        assert response.status_code == 201, response.text

    db_session.expire_all()
    after = db_session.scalar(select(CalendarDay).where(CalendarDay.day == MONDAY))
    assert after is not None and after.day_class is CalendarDayClass.PUBLIC_HOLIDAY
    assert _daily(db_client)[MONDAY.isoformat()]["calendar_class"] == (
        CalendarClass.PUBLIC_HOLIDAY.value
    )
    # The day-status facts are a SEPARATE object; both exist.
    categories = db_session.scalars(
        select(DayStatusEvent.category).where(DayStatusEvent.day == MONDAY)
    ).all()
    assert set(categories) == {DayStatusCategory.FLEX_COMPENSATION}
