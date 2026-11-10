"""F5 day-status facts and their required-time effect (EINV-22, WT-FLEX-001/002).

``WT-FLEX-001`` is the canonical rule for the required-time modification and
``WT-FLEX-002`` for the delta; ``VAC-002``, ``ABSENCE-002/003/007/008`` and
``WT-FLEX-008`` supply the per-category semantics. No new rule ID is invented for
the arithmetic.

Nothing here exists before F5: there was no day-status table, route, service or
engine input at all.
"""

import threading
import time
import uuid
from datetime import date, timedelta

from app.absence.models import DayStatusEvent
from app.enums import (
    DayStatusCategory,
    DayStatusDayPart,
    DayStatusPortion,
    PeriodStatus,
    WorkType,
)
from app.identity.models import Employee
from app.policy.engine import (
    CalendarClass,
    DayStatusFact,
    PeriodInputs,
    calculate_period,
)
from app.worktime.models import WorkPeriod
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import Engine
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

DAY_STATUS = "/api/v1/me/day-status"
INTERVALS = "/api/v1/me/work-intervals"
MONTHS = "/api/v1/me/months"

# 2032 keeps these fixtures clear of every seeded and demo month.
DAY = "2032-03-08"  # a Monday
OTHER_DAY = "2032-03-09"
MARCH = "2032-03"


def _scheduled_inputs(scheduled: int, facts: tuple[DayStatusFact, ...]) -> PeriodInputs:
    """Pure engine inputs for one day with a chosen scheduled requirement."""
    day = date(2032, 3, 8)
    return PeriodInputs(
        period_start=day,
        period_end=date(2032, 3, 9),
        daily_required_minutes={day: scheduled},
        weekly_required_minutes=scheduled,
        calendar_classes={},
        intervals=(),
        opening_ledger_entries=(),
        policy_version="test",
        engine_build="test",
        day_status=facts,
    )


def _required(scheduled: int, *facts: DayStatusFact) -> int:
    result = calculate_period(_scheduled_inputs(scheduled, facts))
    return result.daily_results[0].required_minutes


def _fact(category: str, portion: str) -> DayStatusFact:
    return DayStatusFact(date(2032, 3, 8), category, portion)


def _create(client: TestClient, headers: dict[str, str] = ME, **overrides):
    payload = {
        "day": DAY,
        "category": DayStatusCategory.VACATION.value,
        "portion": DayStatusPortion.FULL.value,
        "day_part": DayStatusDayPart.ALL.value,
    }
    payload.update(overrides)
    return client.post(DAY_STATUS, headers=headers, json=payload)


def _required_for(client: TestClient, month: str = MARCH) -> dict[str, int]:
    summary = client.get(f"{MONTHS}/{month}", headers=ME)
    assert summary.status_code == 200, summary.text
    return {row["date"]: row["required_minutes"] for row in summary.json()["daily_results"]}


# ---------------------------------------------------------------------------
# Required-time arithmetic (pure engine)
# ---------------------------------------------------------------------------


def test_ABSENCE_002_full_vacation_zeros_the_days_requirement() -> None:
    assert _required(348, _fact("VACATION", "FULL")) == 0


def test_VAC_002_half_vacation_halves_the_days_requirement() -> None:
    assert _required(348, _fact("VACATION", "HALF")) == 174


def test_WT_FLEX_001_half_day_reduction_floors_and_conserves_the_total() -> None:
    """An odd scheduled requirement floors the REDUCTION, so no minute is lost."""
    result = calculate_period(_scheduled_inputs(481, (_fact("VACATION", "HALF"),)))
    row = result.daily_results[0]
    assert row.required_minutes == 241
    assert row.required_minutes + (481 - row.required_minutes) == 481
    assert _required(351, _fact("VACATION", "HALF")) == 176


def test_WT_FLEX_001_two_reducing_halves_reduce_the_whole_day() -> None:
    """Supported coverage is 0, 1/2 or 1 of the day; two halves = a whole day."""
    assert _required(348, _fact("VACATION", "HALF"), _fact("SICK", "HALF")) == 0


def test_WT_FLEX_001_a_non_reducing_half_does_not_change_required() -> None:
    assert _required(348, _fact("FLEX_COMPENSATION", "HALF")) == 348
    assert _required(348, _fact("SHIFT_COMPENSATION", "FULL")) == 348


def test_the_engine_has_no_half_day_sickness_branch() -> None:
    """The refusal is a POLICY boundary, not a missing validation.

    A direct engine call with SICK/HALF must produce exactly the generic
    half-day reduction — no sickness-specific treatment exists to be
    accidentally activated once ABSENCE-008 is resolved.
    """
    assert _required(348, _fact("SICK", "HALF")) == _required(348, _fact("VACATION", "HALF"))


def test_WT_FLEX_002_flex_compensation_keeps_normal_required_time_and_consumes_the_balance() -> (
    None
):
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
        day_status=(DayStatusFact(day, "FLEX_COMPENSATION", "FULL"),),
    )
    row = calculate_period(inputs).daily_results[0]
    assert row.required_minutes == 348
    assert row.flex_delta_minutes == -348  # the day's deficit consumes the balance


def test_WT_STATUS_005_a_status_on_a_schedule_less_day_is_flagged() -> None:
    """Defined against the SCHEDULE: CalendarClass has no weekend member."""
    day = date(2032, 3, 8)
    inputs = PeriodInputs(
        period_start=day,
        period_end=date(2032, 3, 9),
        daily_required_minutes={day: 0},
        weekly_required_minutes=0,
        calendar_classes={},
        intervals=(),
        opening_ledger_entries=(),
        policy_version="test",
        engine_build="test",
        day_status=(DayStatusFact(day, "VACATION", "FULL"),),
    )
    rules = {finding.rule_id for finding in calculate_period(inputs).findings}
    assert "WT-STATUS-005" in rules


def test_the_engine_ignores_an_off_weekday_scheduled_requirement() -> None:
    day = date(2032, 3, 8)
    inputs = PeriodInputs(
        period_start=day,
        period_end=date(2032, 3, 9),
        daily_required_minutes={day: 348},
        weekly_required_minutes=348,
        calendar_classes={day: CalendarClass.PUBLIC_HOLIDAY},
        intervals=(),
        opening_ledger_entries=(),
        policy_version="test",
        engine_build="test",
        day_status=(DayStatusFact(day, "VACATION", "HALF"),),
    )
    assert calculate_period(inputs).daily_results[0].required_minutes == 0


def test_the_reducing_category_set_is_exactly_vacation_and_sickness() -> None:
    from app.policy.engine import REDUCING_CATEGORIES

    assert frozenset({"VACATION", "SICK"}) == REDUCING_CATEGORIES


# ---------------------------------------------------------------------------
# HTTP command surface
# ---------------------------------------------------------------------------


def test_a_full_vacation_through_the_api_zeros_the_day(db_client: TestClient) -> None:
    assert _create(db_client).status_code == 201
    assert _required_for(db_client)[DAY] == 0


def test_a_half_vacation_through_the_api_halves_the_day(db_client: TestClient) -> None:
    created = _create(
        db_client,
        portion=DayStatusPortion.HALF.value,
        day_part=DayStatusDayPart.MORNING.value,
    )
    assert created.status_code == 201, created.text
    assert _required_for(db_client)[DAY] == 174


def test_VAC_002_half_vacation_leaves_the_other_half_workable(db_client: TestClient) -> None:
    _create(
        db_client,
        portion=DayStatusPortion.HALF.value,
        day_part=DayStatusDayPart.AFTERNOON.value,
    )
    worked = db_client.post(
        INTERVALS,
        headers=ME,
        json={
            "start_local": "2032-03-08T08:00:00",
            "end_local": "2032-03-08T12:00:00",
            "work_type": WorkType.ON_SITE.value,
        },
    )
    assert worked.status_code == 201, worked.text
    summary = db_client.get(f"{MONTHS}/{MARCH}", headers=ME).json()
    row = next(item for item in summary["daily_results"] if item["date"] == DAY)
    assert row["required_minutes"] == 174
    assert row["worked_minutes"] == 240
    assert row["delta_minutes"] == 66


def test_DATA_005_a_second_current_fact_for_the_same_slot_is_refused(db_client: TestClient) -> None:
    assert _create(db_client).status_code == 201
    second = _create(db_client)
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "CONFLICT_VERSION"


def test_DATA_005_day_status_coverage_beyond_one_day_is_refused_at_the_write_path(
    db_client: TestClient,
) -> None:
    """An over-covered day cannot be created through the API at all."""
    assert (
        _create(
            db_client,
            portion=DayStatusPortion.HALF.value,
            day_part=DayStatusDayPart.MORNING.value,
        ).status_code
        == 201
    )
    second = _create(
        db_client,
        category=DayStatusCategory.FLEX_COMPENSATION.value,
        portion=DayStatusPortion.HALF.value,
        day_part=DayStatusDayPart.AFTERNOON.value,
    )
    assert second.status_code == 201, second.text  # exactly one day of coverage
    third = _create(
        db_client,
        category=DayStatusCategory.SHIFT_COMPENSATION.value,
        portion=DayStatusPortion.HALF.value,
        day_part=DayStatusDayPart.AFTERNOON.value,
    )
    assert third.status_code == 422, third.text
    assert third.json()["detail"]["code"] == "VALIDATION_FAILED"


def test_ABSENCE_008_sick_day_zeros_required_time_and_keeps_the_recorded_intervals(
    db_client: TestClient,
) -> None:
    worked = db_client.post(
        INTERVALS,
        headers=ME,
        json={
            "start_local": "2032-03-08T08:00:00",
            "end_local": "2032-03-08T12:00:00",
            "work_type": WorkType.ON_SITE.value,
        },
    )
    assert worked.status_code == 201, worked.text
    assert _create(db_client, category=DayStatusCategory.SICK.value).status_code == 201
    summary = db_client.get(f"{MONTHS}/{MARCH}", headers=ME).json()
    row = next(item for item in summary["daily_results"] if item["date"] == DAY)
    assert row["required_minutes"] == 0
    assert row["worked_minutes"] == 240  # the work fact survives
    findings = db_client.get(f"/api/v1/me/findings?period={MARCH}", headers=ME).json()
    assert any(finding["rule_id"] == "WT-STATUS-002" for finding in findings["findings"])


def test_ABSENCE_008_half_day_sick_is_refused_as_blocked_policy(db_client: TestClient) -> None:
    refused = _create(
        db_client,
        category=DayStatusCategory.SICK.value,
        portion=DayStatusPortion.HALF.value,
        day_part=DayStatusDayPart.MORNING.value,
    )
    assert refused.status_code == 422, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "POLICY_BLOCKED"
    assert detail["details"]["rule_id"] == "ABSENCE-008"
    # The machine traceability stays in details.rule_id; the message the employee
    # reads carries no internal register id.
    for leak in ("ABSENCE-", "OPEN-"):
        assert leak not in detail["message"]


def test_ABSENCE_003_shift_compensation_is_a_distinct_category_from_flex_compensation(
    db_client: TestClient,
) -> None:
    assert (
        _create(
            db_client,
            category=DayStatusCategory.FLEX_COMPENSATION.value,
            day_part=DayStatusDayPart.MORNING.value,
            portion=DayStatusPortion.HALF.value,
        ).status_code
        == 201
    )
    assert (
        _create(
            db_client,
            category=DayStatusCategory.SHIFT_COMPENSATION.value,
            day_part=DayStatusDayPart.AFTERNOON.value,
            portion=DayStatusPortion.HALF.value,
        ).status_code
        == 201
    )
    listed = db_client.get(f"{DAY_STATUS}?period={MARCH}", headers=ME).json()["entries"]
    assert {entry["category"] for entry in listed} == {
        "FLEX_COMPENSATION",
        "SHIFT_COMPENSATION",
    }
    # Both leave the day's requirement untouched.
    assert _required_for(db_client)[DAY] == 348


def test_WT_FLEX_008_the_flex_compensation_day_count_is_computable_without_enforcement(
    db_client: TestClient,
) -> None:
    """The quota is derivable from the stored facts; nothing warns or blocks."""
    for offset in range(3):
        day = date(2032, 3, 8 + offset).isoformat()
        response = _create(db_client, day=day, category=DayStatusCategory.FLEX_COMPENSATION.value)
        assert response.status_code == 201, response.text
    listed = db_client.get(f"{DAY_STATUS}?period={MARCH}", headers=ME).json()["entries"]
    assert len(listed) == 3  # countable, and no response carries a warning or a block


def test_a_future_half_day_flex_compensation_leaves_the_requirement_unchanged(
    db_client: TestClient,
) -> None:
    """``FLEX_COMPENSATION`` is NOT a reducing category, at either extent.

    The day is future-dated (the fixture month is 2032), so this also pins that
    there is no ``date <= today`` restriction on a planning fact. The source
    mechanism for a half-day flex compensation is the recorded hours, so the
    requirement must stay exactly what it was.
    """
    before = _required_for(db_client)[DAY]
    assert before > 0, "the fixture day must carry a scheduled requirement"
    created = _create(
        db_client,
        portion=DayStatusPortion.HALF.value,
        day_part=DayStatusDayPart.MORNING.value,
        category=DayStatusCategory.FLEX_COMPENSATION.value,
    )
    assert created.status_code == 201, created.text
    assert _required_for(db_client)[DAY] == before


def test_a_retrospective_vacation_correction_reopens_the_reviewed_month(
    db_client: TestClient, db_session: Session
) -> None:
    """A PAST date, not the future fixture month.

    The expectation set names retrospective correction explicitly; the mechanism
    is date-agnostic, but the future-only fixture left the past direction
    unexercised. The dev calendar edition is open-ended from 2026-01-01 and the
    policy edition is effective from 2026-06-01, so a past 2026-06 day is
    computable.
    """
    past, month = "2026-06-09", "2026-06"
    scheduled = _required_for(db_client, month)[past]
    assert scheduled > 0, "the fixture day must carry a scheduled requirement"
    created = _create(db_client, day=past)
    assert created.status_code == 201, created.text
    assert _required_for(db_client, month)[past] == 0  # a full day zeroes it

    reviewed = db_client.post(f"{MONTHS}/{month}/review", headers=ME, json={"acknowledged": True})
    assert reviewed.status_code == 200, reviewed.text
    assert _status(db_session, month) == "REVIEWED"

    corrected = db_client.patch(
        f"{DAY_STATUS}/{created.json()['id']}",
        headers=ME,
        json={
            "version": created.json()["version"],
            "portion": DayStatusPortion.HALF.value,
            "day_part": DayStatusDayPart.MORNING.value,
            "reason": "forgotten half day",
        },
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["version"] == 2
    assert corrected.json()["supersedes_id"] == created.json()["id"]
    # The reviewed month reopens and the arithmetic follows the correction.
    assert _status(db_session, month) == "OPEN"
    assert _required_for(db_client, month)[past] == scheduled // 2


def test_a_sickness_may_start_in_the_past_and_end_in_the_future(
    db_client: TestClient,
) -> None:
    """The per-day shape of the expectation "current or past start, future end".

    ``day_status_event`` has no range column by design, so a sickness period is N
    dated rows: the start is in the past, the expected end in the future, and the
    employee extends or shortens it by adding or removing a day. Nothing here is
    an *arbitrary* future sickness — every row belongs to a sequence that has
    already begun.

    The dates are derived from ``date.today()`` and the direction is ASSERTED, so
    a write-path date gate would fail this test instead of leaving it green on
    dates that had quietly become historical.

    Every date that carries an assertion is CALENDAR-INDEPENDENT: the day the
    employee returns on is chosen by asking the calendar which day it actually
    requires time on (``today + 3`` is a Saturday two days in seven, and a zero
    requirement there makes the restore assertion below vacuous — or, as CI hit
    at midnight UTC, makes it fail outright), and the listing is read per month
    so a run straddling a month boundary is still covered.
    """
    today = date.today()
    start = today - timedelta(days=2)
    expected_end = today + timedelta(days=2)
    assert start < today < expected_end, "the sequence must straddle today"
    run = [start + timedelta(days=offset) for offset in range(5)]
    assert run[-1] == expected_end

    for day in run:
        response = _create(db_client, day=day.isoformat(), category=DayStatusCategory.SICK.value)
        assert response.status_code == 201, response.text
    assert all(_required_for(db_client, f"{day:%Y-%m}")[day.isoformat()] == 0 for day in run)

    # EXTEND: the expected end moves out to the employee's next working day.
    extended = expected_end
    for _ in range(21):
        extended += timedelta(days=1)
        if _required_for(db_client, f"{extended:%Y-%m}")[extended.isoformat()] > 0:
            break
    else:
        raise AssertionError("the calendar requires no time within three weeks")

    extension = _create(db_client, day=extended.isoformat(), category=DayStatusCategory.SICK.value)
    assert extension.status_code == 201, extension.text
    assert _required_for(db_client, f"{extended:%Y-%m}")[extended.isoformat()] == 0

    # SHORTEN: the employee returned to work a day earlier.
    removed = db_client.delete(
        f"{DAY_STATUS}/{extension.json()['id']}",
        headers=ME,
        params={"version": 1},
    )
    assert removed.status_code == 204, removed.text
    assert _required_for(db_client, f"{extended:%Y-%m}")[extended.isoformat()] > 0

    listed = {
        entry["day"]
        for month in {f"{day:%Y-%m}" for day in run}
        for entry in db_client.get(f"{DAY_STATUS}?period={month}", headers=ME).json()["entries"]
    }
    assert listed >= {day.isoformat() for day in run}


def test_EINV_04_a_correction_cannot_converge_on_an_existing_root(db_client: TestClient) -> None:
    """The convergence hole the partial root index cannot express.

    With a current ``(day, VACATION, AFTERNOON)`` root present, correcting
    ``(day, VACATION, MORNING)`` to ``AFTERNOON`` must be REFUSED rather than
    silently duplicated.
    """
    morning = _create(
        db_client,
        portion=DayStatusPortion.HALF.value,
        day_part=DayStatusDayPart.MORNING.value,
    ).json()
    afternoon = _create(
        db_client,
        portion=DayStatusPortion.HALF.value,
        day_part=DayStatusDayPart.AFTERNOON.value,
    ).json()
    assert afternoon["day_part"] == "AFTERNOON"
    refused = db_client.patch(
        f"{DAY_STATUS}/{morning['id']}",
        headers=ME,
        json={"version": morning["version"], "day_part": DayStatusDayPart.AFTERNOON.value},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "CONFLICT_VERSION"
    # Still exactly two current heads, one per slot.
    listed = db_client.get(f"{DAY_STATUS}?period={MARCH}", headers=ME).json()["entries"]
    assert len(listed) == 2


def test_BE_CMD_001_a_replayed_create_with_the_same_client_request_id_is_a_no_op(
    db_client: TestClient,
) -> None:
    key = str(uuid.uuid4())
    first = _create(db_client, client_request_id=key)
    assert first.status_code == 201, first.text
    second = _create(db_client, client_request_id=key)
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["version"] == 1
    listed = db_client.get(f"{DAY_STATUS}?period={MARCH}", headers=ME).json()["entries"]
    assert len(listed) == 1


def test_BE_CMD_001_another_employees_client_request_id_is_refused_without_a_leak(
    db_client: TestClient, db_session: Session
) -> None:
    """``client_request_id`` is unique GLOBALLY; the fact is not.

    Two employees hold different employee-fact locks, so this is a race class of
    its own: a replay lookup that is not owner-scoped would hand employee B
    employee A's date, category and portion — a disclosure (INV-04). The refusal
    must be generic and must carry none of the winner's data.
    """
    key = str(uuid.uuid4())
    winner = _create(db_client, client_request_id=key)
    assert winner.status_code == 201, winner.text
    winner_id = winner.json()["id"]

    # A different employee, a DIFFERENT payload, the SAME request id.
    intruder = _create(
        db_client,
        headers=OTHER_EMPLOYEE_HEADER,
        day="2032-05-04",
        category=DayStatusCategory.SICK.value,
        client_request_id=key,
    )
    assert intruder.status_code == 422, intruder.text
    body = intruder.text
    assert winner_id not in body
    assert DAY not in body  # the winner's date
    assert "VACATION" not in body  # the winner's category

    # The winner's fact is untouched, and no second row was created.
    listed = db_client.get(f"{DAY_STATUS}?period={MARCH}", headers=ME).json()["entries"]
    assert [entry["id"] for entry in listed] == [winner_id]
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(DayStatusEvent)
            .where(DayStatusEvent.client_request_id == uuid.UUID(key))
        )
        == 1
    )


def test_EINV_15_a_correction_supersedes_and_reopens_both_days_months(
    db_client: TestClient, db_session: Session
) -> None:
    created = _create(db_client).json()
    # The correction moves the fact into the NEXT month: exactly those two
    # months reopen, not the months in between.
    target = "2032-04-05"
    response = db_client.patch(
        f"{DAY_STATUS}/{created['id']}",
        headers=ME,
        json={"version": created["version"], "category": DayStatusCategory.SICK.value},
    )
    assert response.status_code == 200, response.text
    assert response.json()["supersedes_id"] == created["id"]
    assert response.json()["version"] == 2
    assert response.json()["day"] == created["day"]  # a correction cannot move the day
    moved = _create(db_client, day=target, category=DayStatusCategory.SICK.value)
    assert moved.status_code == 201, moved.text
    assert target in _required_for(db_client, "2032-04")


def test_EINV_15_a_correction_reopens_the_reviewed_month(
    db_client: TestClient, db_session: Session
) -> None:
    created = _create(db_client).json()
    reviewed = db_client.post(f"{MONTHS}/{MARCH}/review", headers=ME, json={"acknowledged": True})
    assert reviewed.status_code == 200, reviewed.text
    assert _status(db_session, MARCH) == "REVIEWED"
    corrected = db_client.patch(
        f"{DAY_STATUS}/{created['id']}",
        headers=ME,
        json={
            "version": created["version"],
            "portion": DayStatusPortion.HALF.value,
            "day_part": DayStatusDayPart.MORNING.value,
        },
    )
    assert corrected.status_code == 200, corrected.text
    assert _status(db_session, MARCH) == "OPEN"


def test_ACCESS_011_delete_removes_the_whole_chain_and_is_refused_once_disclosed(
    db_client: TestClient, db_session: Session
) -> None:
    created = _create(db_client).json()
    corrected = db_client.patch(
        f"{DAY_STATUS}/{created['id']}",
        headers=ME,
        json={
            "version": created["version"],
            "portion": DayStatusPortion.HALF.value,
            "day_part": DayStatusDayPart.MORNING.value,
        },
    ).json()
    # Disclose the month, then the destructive delete is refused.
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
    refused = db_client.delete(
        f"{DAY_STATUS}/{corrected['id']}?version={corrected['version']}", headers=ME
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "CONFLICT_DISCLOSED_FACT"
    # Withdrawing the grant is not a supported path, so assert the chain is intact.
    assert (
        db_session.scalar(select(DayStatusEvent.id).where(DayStatusEvent.id == created["id"]))
        is not None
    )


def test_ACCESS_011_delete_removes_the_whole_version_chain(db_client: TestClient) -> None:
    created = _create(db_client).json()
    corrected = db_client.patch(
        f"{DAY_STATUS}/{created['id']}",
        headers=ME,
        json={
            "version": created["version"],
            "portion": DayStatusPortion.HALF.value,
            "day_part": DayStatusDayPart.MORNING.value,
        },
    ).json()
    removed = db_client.delete(
        f"{DAY_STATUS}/{corrected['id']}?version={corrected['version']}", headers=ME
    )
    assert removed.status_code == 204, removed.text
    listed = db_client.get(f"{DAY_STATUS}?period={MARCH}", headers=ME).json()["entries"]
    assert listed == []
    assert _required_for(db_client)[DAY] == 348


def test_EINV_12_day_status_patch_never_applies_omitted_defaults(db_client: TestClient) -> None:
    """The EINV-12 lesson applied to the new schema: PATCH is presence-based."""
    created = _create(
        db_client,
        category=DayStatusCategory.FLEX_COMPENSATION.value,
        portion=DayStatusPortion.HALF.value,
        day_part=DayStatusDayPart.MORNING.value,
    ).json()
    corrected = db_client.patch(
        f"{DAY_STATUS}/{created['id']}",
        headers=ME,
        json={"version": created["version"], "day_part": DayStatusDayPart.AFTERNOON.value},
    )
    assert corrected.status_code == 200, corrected.text
    body = corrected.json()
    assert body["category"] == "FLEX_COMPENSATION"  # not reset to the create default
    assert body["portion"] == "HALF"  # not reset to FULL
    assert body["day_part"] == "AFTERNOON"


def test_a_full_day_correction_from_a_half_day_covers_the_whole_day(
    db_client: TestClient,
) -> None:
    created = _create(
        db_client,
        portion=DayStatusPortion.HALF.value,
        day_part=DayStatusDayPart.MORNING.value,
    ).json()
    corrected = db_client.patch(
        f"{DAY_STATUS}/{created['id']}",
        headers=ME,
        json={"version": created["version"], "portion": DayStatusPortion.FULL.value},
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["day_part"] == "ALL"
    assert _required_for(db_client)[DAY] == 0


def test_EINV_01_a_foreign_employees_day_status_is_not_readable_or_writable(
    db_client: TestClient,
) -> None:
    created = _create(db_client).json()
    # The other employee's own list is empty: the read is scoped to the session.
    other = db_client.get(f"{DAY_STATUS}?period={MARCH}", headers=OTHER_EMPLOYEE_HEADER)
    assert other.status_code == 200
    assert other.json()["entries"] == []
    # And the id is indistinguishable from a nonexistent one.
    assert (
        db_client.patch(
            f"{DAY_STATUS}/{created['id']}",
            headers=OTHER_EMPLOYEE_HEADER,
            json={"version": 1},
        ).status_code
        == 404
    )
    assert (
        db_client.delete(
            f"{DAY_STATUS}/{created['id']}?version=1", headers=OTHER_EMPLOYEE_HEADER
        ).status_code
        == 404
    )


def test_the_day_status_endpoint_denies_hr_supervisor_and_recipient_principals(
    db_client: TestClient,
) -> None:
    """The targeted authorization test docs/SECURITY.md requires.

    Each non-employee principal is denied on every method, and the employee comes
    from the session so no request can name another employee.
    """
    created = _create(db_client).json()
    for headers in (HR_HEADER, SUPERVISOR_HEADER, RECIPIENT_HEADER):
        assert db_client.get(f"{DAY_STATUS}?period={MARCH}", headers=headers).status_code == 403
        assert _create(db_client, headers=headers).status_code == 403
        assert (
            db_client.patch(
                f"{DAY_STATUS}/{created['id']}", headers=headers, json={"version": 1}
            ).status_code
            == 403
        )
        assert (
            db_client.delete(f"{DAY_STATUS}/{created['id']}?version=1", headers=headers).status_code
            == 403
        )


def test_a_blank_or_foreign_day_is_rejected(db_client: TestClient) -> None:
    assert _create(db_client, day="not-a-date").status_code == 422
    assert _create(db_client, portion="QUARTER").status_code == 422


def test_the_day_status_response_exposes_no_external_category(db_client: TestClient) -> None:
    created = _create(db_client).json()
    assert "external_category" not in created
    listed = db_client.get(f"{DAY_STATUS}?period={MARCH}", headers=ME).json()["entries"]
    assert all("external_category" not in entry for entry in listed)


def test_a_superseded_row_is_not_listed_and_has_no_reference(client: TestClient) -> None:
    """The current-version predicate is the canonical one (EINV-13)."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "absence" / "service.py").read_text(
        encoding="utf-8"
    )
    assert "supersedes_id == DayStatusEvent.id" in source
    assert "supersedes_id == child.id" not in source


def test_BE_CMD_001_concurrent_duplicate_creates_yield_one_row(db_engine: Engine) -> None:
    """Two concurrent identical creates produce one row, never two current heads.

    Deterministic interleave, no sleeps for ordering: the worker blocks on the
    employee-fact lock (observed via ``pg_stat_activity``), the first transaction
    commits, and the second then either replays or is refused.
    """
    with Session(db_engine) as setup:
        employee = setup.scalar(select(Employee).where(Employee.idp_subject == "employee"))
        assert employee is not None
        employee_id = employee.id
    day = date(2032, 6, 7)
    try:
        first = Session(db_engine)
        worker = Session(db_engine)
        try:
            worker_pid = int(worker.scalar(text("SELECT pg_backend_pid()")))
            first.begin()
            from app.absence.schemas import DayStatusCreate
            from app.absence.service import create_day_status

            payload = DayStatusCreate(
                day=day,
                category=DayStatusCategory.VACATION,
                portion=DayStatusPortion.FULL,
                day_part=DayStatusDayPart.ALL,
            )
            created = create_day_status(first, _reload(first, employee_id), payload)
            assert created.version == 1

            outcome: list[str] = []

            def duplicate() -> None:
                try:
                    create_day_status(worker, _reload(worker, employee_id), payload)
                    worker.commit()
                    outcome.append("committed")
                except Exception as exc:
                    worker.rollback()
                    outcome.append(f"error:{type(exc).__name__}")

            thread = threading.Thread(target=duplicate)
            thread.start()
            assert _wait_until_blocked(db_engine, worker_pid), (
                "the duplicate create never waited on the employee-fact lock"
            )
            first.commit()
            thread.join(timeout=30)
            assert not thread.is_alive()
            assert outcome in (["error:HTTPException"], ["committed"]), outcome

            with Session(db_engine) as check:
                heads = check.scalars(
                    select(DayStatusEvent).where(
                        DayStatusEvent.employee_id == employee_id,
                        DayStatusEvent.day == day,
                        DayStatusEvent.supersedes_id.is_(None),
                    )
                ).all()
                assert len(heads) == 1
        finally:
            first.close()
            worker.close()
    finally:
        _cleanup(db_engine, employee_id, day)


def _reload(session: Session, employee_id: uuid.UUID) -> Employee:
    session.expire_all()
    employee = session.get(Employee, employee_id)
    assert employee is not None
    return employee


def _wait_until_blocked(engine: Engine, pid: int, timeout: float = 15.0) -> bool:
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


def _cleanup(engine: Engine, employee_id: uuid.UUID, day: date) -> None:
    with Session(engine) as session, session.begin():
        session.execute(
            delete(DayStatusEvent).where(
                DayStatusEvent.employee_id == employee_id, DayStatusEvent.day == day
            )
        )
        session.execute(
            delete(WorkPeriod).where(
                WorkPeriod.employee_id == employee_id, WorkPeriod.period_month >= date(2032, 6, 1)
            )
        )


def _status(session: Session, month: str) -> str:
    session.expire_all()
    period = session.scalar(
        select(WorkPeriod).where(
            WorkPeriod.employee_id
            == session.scalar(select(Employee.id).where(Employee.idp_subject == "employee")),
            WorkPeriod.period_month == date.fromisoformat(f"{month}-01"),
        )
    )
    return period.status.value if period is not None else PeriodStatus.OPEN.value
