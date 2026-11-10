"""Pure OCD/allowance classification.  Results are private derived overlays."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from app.enums import CalendarDayClass, FindingStatus, PayGroupBucket, WorkType
from app.worktime.walltime import BERLIN


@dataclass(frozen=True, slots=True)
class WorkSpan:
    start_at: datetime
    end_at: datetime
    work_type: WorkType
    source: str = "WORK_INTERVAL"


@dataclass(frozen=True, slots=True)
class PolicyFinding:
    rule_id: str
    status: FindingStatus
    message: str
    minutes: int | None = None


@dataclass(frozen=True, slots=True)
class ClassifiedComponent:
    rule_id: str
    component: str
    minutes: int
    time_equivalent_minutes: int


@dataclass(frozen=True, slots=True)
class DutyClassification:
    duty_minutes: int
    duty_kind: Literal["DUTY_GE_12H", "DUTY_LT_12H"]
    units: int | None
    basis_points: int | None
    short_notice: bool | None


def _elapsed_minutes(start: datetime, end: datetime) -> int:
    return int((end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() // 60)


def _add(
    out: list[ClassifiedComponent], rule: str, component: str, minutes: int, equivalent: int
) -> None:
    if minutes:
        out.append(ClassifiedComponent(rule, component, minutes, equivalent))


def classify_spans(
    spans: list[WorkSpan],
    calendar_classes: dict[date, CalendarDayClass],
    pay_group_bucket: PayGroupBucket,
    fte: float | None,
    contract_weekly_required_minutes: int | None,
) -> tuple[list[ClassifiedComponent], list[PolicyFinding]]:
    """Classify each elapsed minute by its Berlin local calendar/wall clock.

    Minute-granular components match the documented allowance units and keep UTC
    iteration DST-safe: a fall-back hour has two distinct elapsed minute slots.
    """
    components: list[ClassifiedComponent] = []
    findings: list[PolicyFinding] = []
    counts: dict[tuple[str, str], int] = {}
    for span in spans:
        cursor = span.start_at.astimezone(UTC)
        end = span.end_at.astimezone(UTC)
        overtime = span.work_type in {WorkType.OVERTIME_PAID, WorkType.OVERTIME_TIME_COMP}
        while cursor < end:
            next_minute = min(cursor + timedelta(minutes=1), end)
            # Partial minutes are conservatively not represented; factual intervals
            # are minute-resolution command inputs.
            if (next_minute - cursor).total_seconds() < 60:
                break
            local = cursor.astimezone(BERLIN)
            day = local.date()
            day_class = calendar_classes.get(day)

            def inc(rule: str, name: str) -> None:
                counts[(rule, name)] = counts.get((rule, name), 0) + 1

            if local.hour >= 21 or local.hour < 6:
                inc("ALLOW-003", "NIGHT_20")
            candidates: list[tuple[int, str, str]] = []
            if overtime and day.weekday() == 5 and 13 <= local.hour < 21:
                candidates.append((20, "ALLOW-004", "SATURDAY_20"))
            if day.weekday() == 6:
                candidates.append((25, "ALLOW-005", "SUNDAY_25"))
            if day_class == CalendarDayClass.PUBLIC_HOLIDAY:
                candidates.append((35, "ALLOW-006", "HOLIDAY_35"))
            if candidates:
                _, rule, name = max(candidates)
                inc(rule, name)
            if overtime:
                if fte is None or contract_weekly_required_minutes is None:
                    findings.append(
                        PolicyFinding(
                            "ALLOW-001",
                            "BLOCKED_POLICY",
                            "Overtime base cannot be classified without effective contract data.",
                        )
                    )
                else:
                    inc(
                        "ALLOW-007" if fte < 1 else "ALLOW-001",
                        "MEHRARBEIT" if fte < 1 else "OVERTIME_BASE",
                    )
                if pay_group_bucket == PayGroupBucket.UNKNOWN:
                    findings.append(
                        PolicyFinding(
                            "ALLOW-002",
                            "BLOCKED_POLICY",
                            "Overtime supplement cannot be classified without a pay-group bucket.",
                        )
                    )
                else:
                    inc("ALLOW-002", "OVERTIME_SUPPLEMENT")
            cursor = next_minute
    for (rule, name), minutes in counts.items():
        rate = {
            "NIGHT_20": 12,
            "SATURDAY_20": 12,
            "SUNDAY_25": 15,
            "HOLIDAY_35": 21,
            "OVERTIME_SUPPLEMENT": 18 if pay_group_bucket == PayGroupBucket.LE_9B else 9,
        }.get(name, 60)
        _add(components, rule, name, minutes, minutes * rate // 60)
    # Same uncertainty is a span finding, not one per minute.
    unique = {(x.rule_id, x.status, x.message): x for x in findings}
    return components, list(unique.values())


def classify_duty(
    start_at: datetime,
    end_at: datetime,
    scheduled_at: datetime | None,
    calendar_classes: dict[date, CalendarDayClass],
) -> tuple[DutyClassification, list[PolicyFinding]]:
    minutes = _elapsed_minutes(start_at, end_at)
    findings: list[PolicyFinding] = []
    if minutes >= 12 * 60:
        days: set[date] = set()
        cursor = start_at.astimezone(BERLIN).date()
        last = (end_at.astimezone(BERLIN) - timedelta(microseconds=1)).date()
        while cursor <= last:
            days.add(cursor)
            cursor += timedelta(days=1)
        weekend_or_holiday = False
        for day in days:
            if day.weekday() >= 5:
                weekend_or_holiday = True
            if calendar_classes.get(day) == CalendarDayClass.PUBLIC_HOLIDAY:
                weekend_or_holiday = True
        duty = DutyClassification(
            minutes, "DUTY_GE_12H", 4 if weekend_or_holiday else 2, None, None
        )
    else:
        duty = DutyClassification(minutes, "DUTY_LT_12H", None, 1250, None)
    if scheduled_at is None:
        findings.append(
            PolicyFinding(
                "OCD-007",
                "INFO",
                "Short-notice classification is unavailable because scheduled time is absent.",
            )
        )
        duty = DutyClassification(
            duty.duty_minutes, duty.duty_kind, duty.units, duty.basis_points, None
        )
    else:
        short = (start_at.astimezone(UTC) - scheduled_at.astimezone(UTC)) < timedelta(hours=72)
        duty = DutyClassification(
            duty.duty_minutes, duty.duty_kind, duty.units, duty.basis_points, short
        )
        findings.append(
            PolicyFinding(
                "OCD-002",
                "INFO"
                if start_at.astimezone(UTC) - scheduled_at.astimezone(UTC) >= timedelta(weeks=4)
                else "WARNING",
                "OCD assignment notice meets four-week expectation."
                if not short
                and start_at.astimezone(UTC) - scheduled_at.astimezone(UTC) >= timedelta(weeks=4)
                else "OCD assignment was scheduled with less than four weeks notice.",
            )
        )
    return duty, findings


def monthly_assignment_findings(
    assignments: list[tuple[datetime, datetime, str]], month: date
) -> list[PolicyFinding]:
    days: set[date] = set()
    weekend_days: set[date] = set()
    for start, end, status in assignments:
        if status != "SCHEDULED":
            continue
        cursor = start.astimezone(BERLIN).date()
        last = (end.astimezone(BERLIN) - timedelta(microseconds=1)).date()
        while cursor <= last:
            if cursor.year == month.year and cursor.month == month.month:
                days.add(cursor)
                if cursor.weekday() >= 5:
                    weekend_days.add(cursor)
            cursor += timedelta(days=1)
    # OCD-003: max 16 OCD calendar days AND max 2 weekends per calendar month.
    # A "weekend" is a maximal run of consecutive weekend duty days (a full
    # Sat+Sun duty is one weekend; two separate Saturdays are two). Counting
    # individual weekend DAYS would flag two allowed weekends (4 days) as a
    # violation — the boundary is 2 versus 3 weekends.
    weekends = 0
    previous: date | None = None
    for day in sorted(weekend_days):
        if previous is None or (day - previous).days > 1:
            weekends += 1
        previous = day
    result: list[PolicyFinding] = []
    if len(days) > 16:
        result.append(
            PolicyFinding(
                "OCD-003", "WARNING", "More than 16 OCD calendar days are scheduled this month."
            )
        )
    if weekends > 2:
        result.append(
            PolicyFinding(
                "OCD-003", "CRITICAL", "More than 2 OCD weekends are scheduled this month."
            )
        )
    return result


def rest_findings(calls: list[WorkSpan], prior_spans: list[WorkSpan]) -> list[PolicyFinding]:
    result: list[PolicyFinding] = []
    for call in calls:
        previous = [
            span
            for span in prior_spans
            if span.end_at.astimezone(UTC) <= call.start_at.astimezone(UTC)
        ]
        if previous:
            last = max(previous, key=lambda span: span.end_at.astimezone(UTC))
            gap = call.start_at.astimezone(UTC) - last.end_at.astimezone(UTC)
            if gap < timedelta(hours=11):
                result.append(
                    PolicyFinding(
                        "OCD-005",
                        "CRITICAL",
                        "OCD call disrupted the 11-hour rest period.",
                        max(0, int((timedelta(hours=11) - gap).total_seconds() // 60)),
                    )
                )
    return result
