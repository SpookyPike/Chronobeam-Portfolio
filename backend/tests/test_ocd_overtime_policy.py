"""Pure allowance and OCD policy boundaries.

Golden cases follow WT-TEST §8/§9: ALLOW-T01..T08 and OCD-T01..T09 boundary
semantics, plus the ALLOW-008 highest-of rule and the OCD-002/003/005 golden
boundaries of the classification rules.
"""

from datetime import datetime, timedelta

import pytest
from app.enums import CalendarDayClass, PayGroupBucket, WorkType
from app.ocd_overtime.policy import (
    WorkSpan,
    classify_duty,
    classify_spans,
    monthly_assignment_findings,
    rest_findings,
)

BERLIN = "+02:00"


def span(start: str, end: str, work_type: WorkType = WorkType.ON_SITE) -> WorkSpan:
    return WorkSpan(datetime.fromisoformat(start), datetime.fromisoformat(end), work_type)


def comps(result: tuple[object, object], name: str) -> int:
    return next(
        (item.minutes for item in result[0] if item.component == name),  # type: ignore[union-attr]
        0,
    )


def has_finding(result: tuple[object, object], rule: str, status: str | None = None) -> bool:  # type: ignore[no-untyped-def]
    return any(
        item.rule_id == rule and (status is None or item.status == status)
        for item in result[1]  # type: ignore[union-attr]
    )


def names(result: tuple[object, object]) -> set[str]:
    components = result[0]
    return {item.component for item in components}  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        # ALLOW-T01: night window 21:00-06:00, both boundaries exact.
        ("2026-07-06T20:59:00+02:00", "2026-07-06T21:01:00+02:00", 1),
        ("2026-07-06T05:59:00+02:00", "2026-07-06T06:01:00+02:00", 1),
        ("2026-07-06T21:00:00+02:00", "2026-07-07T06:00:00+02:00", 540),
        # Cross-midnight night classification (OCD-T04): 23:30 -> 01:30.
        ("2026-07-06T23:30:00+02:00", "2026-07-07T01:30:00+02:00", 120),
    ],
)
def test_ALLOW_T01_night_boundaries(start: str, end: str, expected: int) -> None:
    result = classify_spans([span(start, end)], {}, PayGroupBucket.LE_9B, 1, 2340)
    assert comps(result, "NIGHT_20") == expected


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        # ALLOW-T02: Saturday qualifying-overtime window 13:00-21:00, boundaries exact.
        ("2026-07-04T12:59:00+02:00", "2026-07-04T13:01:00+02:00", 1),
        ("2026-07-04T20:59:00+02:00", "2026-07-04T21:01:00+02:00", 1),
        ("2026-07-04T13:00:00+02:00", "2026-07-04T21:00:00+02:00", 480),
    ],
)
def test_ALLOW_T02_saturday_overtime_boundaries(start: str, end: str, expected: int) -> None:
    result = classify_spans(
        [span(start, end, WorkType.OVERTIME_PAID)], {}, PayGroupBucket.GE_9C, 1, 2340
    )
    assert comps(result, "SATURDAY_20") == expected


def test_ALLOW_T02_shift_and_ordinary_work_have_no_saturday_allowance() -> None:
    shift = classify_spans(
        [span("2026-07-04T13:00:00+02:00", "2026-07-04T14:00:00+02:00", WorkType.SHIFT)],
        {},
        PayGroupBucket.GE_9C,
        1,
        2340,
    )
    ordinary = classify_spans(
        [span("2026-07-04T13:00:00+02:00", "2026-07-04T14:00:00+02:00", WorkType.ON_SITE)],
        {},
        PayGroupBucket.GE_9C,
        1,
        2340,
    )
    assert "SATURDAY_20" not in names(shift)
    assert "SATURDAY_20" not in names(ordinary)


def test_ALLOW_T03_sunday_classification_and_time_equivalent() -> None:
    result = classify_spans(
        [span("2026-07-05T10:00:00+02:00", "2026-07-05T12:00:00+02:00", WorkType.OVERTIME_PAID)],
        {},
        PayGroupBucket.LE_9B,
        1,
        2340,
    )
    assert comps(result, "SUNDAY_25") == 120
    assert next(x for x in result[0] if x.component == "SUNDAY_25").time_equivalent_minutes == 30


def test_ALLOW_T04_holiday_classification_and_time_equivalent() -> None:
    result = classify_spans(
        [span("2026-07-05T10:00:00+02:00", "2026-07-05T11:00:00+02:00", WorkType.OVERTIME_PAID)],
        {datetime(2026, 7, 5).date(): CalendarDayClass.PUBLIC_HOLIDAY},
        PayGroupBucket.LE_9B,
        1,
        2340,
    )
    assert comps(result, "HOLIDAY_35") == 60
    assert next(x for x in result[0] if x.component == "HOLIDAY_35").time_equivalent_minutes == 21


@pytest.mark.parametrize(
    ("weekday", "holiday", "expected_components"),
    [
        # ALLOW-T05: Saturday + holiday -> only the highest (holiday 35 > saturday 20).
        (5, True, {"HOLIDAY_35"}),
        # ALLOW-T06: Sunday + holiday -> only the highest (holiday 35 > sunday 25).
        (6, True, {"HOLIDAY_35"}),
        # Sunday alone keeps SUNDAY_25; Saturday qualifies without holiday.
        (6, False, {"SUNDAY_25"}),
    ],
)
def test_ALLOW_T05_T06_highest_day_allowance_only(
    weekday: int, holiday: bool, expected_components: set[str]
) -> None:
    day = datetime(2026, 7, 4)  # a Saturday
    offset = (weekday - day.weekday()) % 7
    date = (day + timedelta(days=offset)).date()
    classes = {date: CalendarDayClass.PUBLIC_HOLIDAY} if holiday else {}
    result = classify_spans(
        [
            span(
                f"{date.isoformat()}T10:00:00+02:00",
                f"{date.isoformat()}T11:00:00+02:00",
                WorkType.OVERTIME_PAID,
            )
        ],
        classes,
        PayGroupBucket.LE_9B,
        1,
        2340,
    )
    present = {
        item.component for item in result[0] if item.component.endswith(("_25", "_20", "_35"))
    }
    assert present == expected_components


def test_ALLOW_T07_supplement_differs_by_pay_group_bucket() -> None:
    minute = span("2026-07-06T22:00:00+02:00", "2026-07-06T23:00:00+02:00", WorkType.OVERTIME_PAID)
    low = classify_spans([minute], {}, PayGroupBucket.LE_9B, 1, 2340)
    high = classify_spans([minute], {}, PayGroupBucket.GE_9C, 1, 2340)
    low_equiv = next(
        x for x in low[0] if x.component == "OVERTIME_SUPPLEMENT"
    ).time_equivalent_minutes
    high_equiv = next(
        x for x in high[0] if x.component == "OVERTIME_SUPPLEMENT"
    ).time_equivalent_minutes
    # 18 min/h for <=9b, 9 min/h for >=9c (ALLOW-002). The PUBLIC projection
    # strips these (they reveal the bucket); the pure classifier still computes
    # them for the employee's private classification.
    assert low_equiv == 18
    assert high_equiv == 9


def test_ALLOW_T08_mehrarbeit_for_part_time_and_blocked_without_contract() -> None:
    minute = span("2026-07-06T22:00:00+02:00", "2026-07-06T23:00:00+02:00", WorkType.OVERTIME_PAID)
    part_time = classify_spans([minute], {}, PayGroupBucket.LE_9B, 0.8, 1872)
    full_time = classify_spans([minute], {}, PayGroupBucket.LE_9B, 1.0, 2340)
    assert "MEHRARBEIT" in names(part_time)
    assert "OVERTIME_BASE" not in names(part_time)
    assert "OVERTIME_BASE" in names(full_time)
    assert "MEHRARBEIT" not in names(full_time)
    blocked = classify_spans([minute], {}, PayGroupBucket.LE_9B, None, None)
    assert has_finding(blocked, "ALLOW-001", "BLOCKED_POLICY")


def test_night_coexists_with_highest_day_allowance() -> None:
    # A Sunday night hour is both SUNDAY_25 and NIGHT_20 (different components;
    # ALLOW-008 only forbids stacking day allowances).
    result = classify_spans(
        [span("2026-07-05T22:00:00+02:00", "2026-07-05T23:00:00+02:00", WorkType.OVERTIME_PAID)],
        {},
        PayGroupBucket.LE_9B,
        1,
        2340,
    )
    assert comps(result, "SUNDAY_25") == 60
    assert comps(result, "NIGHT_20") == 60


def test_DST_fall_back_elapsed_minutes_are_classified_once_each() -> None:
    # 2026-10-25 01:30+02:00 -> 03:30+01:00 is 3 elapsed hours; the repeated
    # local 02:00-03:00 hour exists twice and must count twice (180 night min).
    result = classify_spans(
        [span("2026-10-25T01:30:00+02:00", "2026-10-25T03:30:00+01:00", WorkType.ON_SITE)],
        {},
        PayGroupBucket.LE_9B,
        1,
        2340,
    )
    assert comps(result, "NIGHT_20") == 180


def test_OCD_T08_duty_hourly_vs_lump_sum_boundaries() -> None:
    start = datetime(
        2026, 7, 6, 8, 0, tzinfo=datetime.fromisoformat(f"2026-07-06T08:00:00{BERLIN}").tzinfo
    )
    twelve = start + timedelta(hours=12)
    ge, _ = classify_duty(start, twelve, None, {})
    assert ge.duty_kind == "DUTY_GE_12H"  # exactly 12h qualifies (>=12h)
    lt, findings = classify_duty(start, twelve - timedelta(minutes=1), None, {})
    assert lt.duty_kind == "DUTY_LT_12H" and lt.basis_points == 1250
    assert any(x.rule_id == "OCD-007" for x in findings)


def test_OCD_T08_duty_weekend_and_holiday_units() -> None:
    start = datetime.fromisoformat("2026-07-04T08:00:00+02:00")  # Saturday
    weekend, _ = classify_duty(start, start + timedelta(hours=13), None, {})
    assert weekend.units == 4
    weekday = datetime.fromisoformat("2026-07-06T08:00:00+02:00")  # Monday
    plain, findings = classify_duty(weekday, weekday + timedelta(hours=13), None, {})
    # A weekday absent from the sparse holiday map is an ordinary day (the map
    # is authoritative for its range): units stay 2 and nothing is blocked.
    assert plain.units == 2
    assert not has_finding(([], findings), "OCD-007", "BLOCKED_POLICY")
    holiday, _ = classify_duty(
        weekday,
        weekday + timedelta(hours=13),
        None,
        {weekday.date(): CalendarDayClass.PUBLIC_HOLIDAY},
    )
    assert holiday.units == 4


def test_OCD_T09_short_notice_72h_boundary() -> None:
    start = datetime.fromisoformat("2026-07-06T08:00:00+02:00")
    exactly, _ = classify_duty(start, start + timedelta(hours=8), start - timedelta(hours=72), {})
    assert exactly.short_notice is False
    under, findings = classify_duty(
        start, start + timedelta(hours=8), start - timedelta(hours=71, minutes=59), {}
    )
    assert under.short_notice is True
    assert any(x.rule_id == "OCD-002" and x.status == "WARNING" for x in findings)


def test_OCD_002_notice_four_week_boundary() -> None:
    start = datetime.fromisoformat("2026-07-06T08:00:00+02:00")
    _, findings = classify_duty(start, start + timedelta(hours=8), start - timedelta(weeks=4), {})
    assert any(x.rule_id == "OCD-002" and x.status == "INFO" for x in findings)
    _, warning = classify_duty(
        start, start + timedelta(hours=8), start - timedelta(weeks=4) + timedelta(minutes=1), {}
    )
    assert any(x.rule_id == "OCD-002" and x.status == "WARNING" for x in warning)


def test_OCD_T06_T07_monthly_limit_boundaries() -> None:
    month = datetime.fromisoformat("2026-07-01T08:00:00+02:00")
    day = datetime.fromisoformat("2026-07-01T08:00:00+02:00")

    def assignments(count: int, weekend: bool = False) -> list[tuple[datetime, datetime, str]]:
        start = day
        rows = []
        for index in range(count):
            candidate = start + timedelta(days=index)
            if weekend:
                while candidate.weekday() < 5:
                    candidate += timedelta(days=1)
            rows.append((candidate, candidate + timedelta(hours=8), "SCHEDULED"))
        return rows

    assert monthly_assignment_findings(assignments(16), month.date().replace(day=1)) == []
    assert any(
        x.rule_id == "OCD-003" and x.status == "WARNING"
        for x in monthly_assignment_findings(assignments(17), month.date().replace(day=1))
    )
    saturday = datetime.fromisoformat("2026-07-04T08:00:00+02:00")
    two_weekends = [
        (saturday, saturday + timedelta(hours=8), "SCHEDULED"),
        (saturday + timedelta(days=7), saturday + timedelta(days=7, hours=8), "SCHEDULED"),
    ]
    assert monthly_assignment_findings(two_weekends, month.date().replace(day=1)) == []
    three_weekends = [
        *two_weekends,
        (saturday + timedelta(days=14), saturday + timedelta(days=14, hours=8), "SCHEDULED"),
    ]
    assert any(
        x.rule_id == "OCD-003" and x.status == "CRITICAL"
        for x in monthly_assignment_findings(three_weekends, month.date().replace(day=1))
    )
    # CANCELLED/SUPERSEDED assignments never count towards limits.
    cancelled = [(saturday, saturday + timedelta(hours=8), "CANCELLED"), *two_weekends]
    assert monthly_assignment_findings(cancelled, month.date().replace(day=1)) == []


def test_OCD_T05_rest_golden_boundaries() -> None:
    # Exactly 11h of rest -> no finding (passes).
    clean = rest_findings(
        [span("2026-07-02T09:00:00+02:00", "2026-07-02T10:00:00+02:00")],
        [span("2026-07-01T21:00:00+02:00", "2026-07-01T22:00:00+02:00")],
    )
    assert clean == []
    # 10h59m -> critical finding; the floor of the 60s shortfall is 1 minute.
    short = rest_findings(
        [span("2026-07-02T08:59:00+02:00", "2026-07-02T09:59:00+02:00")],
        [span("2026-07-01T21:00:00+02:00", "2026-07-01T22:00:00+02:00")],
    )
    assert short and short[0].rule_id == "OCD-005" and short[0].status == "CRITICAL"
    assert short[0].minutes == 1
    # A 10h59m59s sub-minute shortfall still fires the finding (floor 0).
    sub_minute = rest_findings(
        [span("2026-07-02T08:59:59+02:00", "2026-07-02T09:59:59+02:00")],
        [span("2026-07-01T21:00:00+02:00", "2026-07-01T22:00:00+02:00")],
    )
    assert sub_minute and sub_minute[0].rule_id == "OCD-005"
    # Non-OCD ordinary work after a call also restores the 11h clock.
    prior = [span("2026-07-01T23:00:00+02:00", "2026-07-02T00:30:00+02:00", WorkType.OCD_CALL)]
    next_morning = [
        span("2026-07-02T08:00:00+02:00", "2026-07-02T09:00:00+02:00", WorkType.ON_SITE)
    ]
    assert any(x.rule_id == "OCD-005" for x in rest_findings(next_morning, prior))


def test_OCD_T01_assignment_without_call_has_no_effective_work_component() -> None:
    # Pure counterpart: an assignment alone classifies duty (availability) but
    # contributes no call component; the API test asserts no interval row.
    duty, findings = classify_duty(
        datetime.fromisoformat("2026-07-19T19:00:00+02:00"),
        datetime.fromisoformat("2026-07-20T08:00:00+02:00"),
        datetime.fromisoformat("2026-06-01T10:00:00+02:00"),
        {datetime(2026, 7, 19).date(): CalendarDayClass.PUBLIC_HOLIDAY},
    )
    assert duty.duty_kind == "DUTY_GE_12H" and duty.units == 4  # weekend + holiday
    assert not has_finding(([], findings), "OCD-005")
