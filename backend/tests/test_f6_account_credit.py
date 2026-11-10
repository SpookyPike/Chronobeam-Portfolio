"""F6.3 — the account-credit correction, MEASURED here and NOT published.

The corrected parameter set lives in this module, deliberately **not** in
``app/policy/editions.py``: publishing it would move real account balances, and
the effective month is an operator decision (``EINV-20``). This module measures
the divergence and pins the rule; the flip lands only when an operator selects
the boundary and publishes the edition together with its migration.
"""

from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta
from types import MappingProxyType

from app.enums import WorkType
from app.policy.editions import DEFAULT_ORDINARY_PARAMETERS, OrdinaryTimeParameters
from app.policy.engine import (
    BERLIN,
    CalendarClass,
    IntervalFact,
    LedgerFact,
    PeriodInputs,
    PeriodResult,
    calculate_period,
)

#: The corrected credit rule: EVERY work type still credits the account except
#: ``OVERTIME_PAID``, whose compensation is money (``POL-04``).
#: ``OVERTIME_TIME_COMP`` is a DISTINCT work type, so the employee's election is
#: already carried by the interval itself and the flip is a pure parameter change
#: — no new engine input, no fingerprint key, no ``EINV-15`` change.
CORRECTED_CREDIT_BY_WORK_TYPE: Mapping[str, bool] = MappingProxyType(
    {
        **{member.value: True for member in WorkType},
        WorkType.OVERTIME_PAID.value: False,
    }
)

CORRECTED_PARAMETERS = replace(
    DEFAULT_ORDINARY_PARAMETERS, credit_by_work_type=CORRECTED_CREDIT_BY_WORK_TYPE
)

WEEK_START = date(2026, 7, 6)  # a Monday
REQUIRED = 468  # the synthetic 5:48 day used across the policy tests
PAID_OVERTIME_MINUTES = 120
TIME_COMP_OVERTIME_MINUTES = 120
DAY_MINUTES = 480


def _interval(name: str, day_offset: int, start_hour: int, minutes: int, work_type: WorkType):
    start = datetime.combine(WEEK_START + timedelta(days=day_offset), datetime.min.time(), BERLIN)
    return IntervalFact(
        id=name,
        start_at=start.replace(hour=start_hour),
        end_at=start.replace(hour=start_hour) + timedelta(minutes=minutes),
        work_type=work_type.value,
    )


def _week_inputs(parameters: OrdinaryTimeParameters) -> PeriodInputs:
    """Five ordinary days, one of them with paid overtime and one with time-comp.

    ``worked_minutes`` is identical across editions by construction — the point of
    the fixture is that only the ACCOUNT layer moves.
    """
    intervals: list[IntervalFact] = []
    for offset in range(5):
        intervals.append(_interval(f"on-site-{offset}", offset, 8, DAY_MINUTES, WorkType.ON_SITE))
    intervals.append(
        _interval("paid-overtime", 0, 18, PAID_OVERTIME_MINUTES, WorkType.OVERTIME_PAID)
    )
    intervals.append(
        _interval(
            "time-comp-overtime", 1, 18, TIME_COMP_OVERTIME_MINUTES, WorkType.OVERTIME_TIME_COMP
        )
    )
    days = [WEEK_START + timedelta(days=offset) for offset in range(5)]
    return PeriodInputs(
        period_start=WEEK_START,
        period_end=WEEK_START + timedelta(days=7),
        daily_required_minutes={day: REQUIRED for day in days},
        weekly_required_minutes=REQUIRED * 5,
        calendar_classes={day: CalendarClass.WEEKDAY for day in days},
        intervals=tuple(intervals),
        opening_ledger_entries=(LedgerFact(date(2026, 6, 30), 0, "CUTOVER_SEED"),),
        policy_version="f6-measurement",
        engine_build="phase-2-ordinary-v1",
        parameters=parameters,
    )


def _single_day(work_type: WorkType, minutes: int, parameters: OrdinaryTimeParameters):
    """One interval on one day with NO requirement, to isolate the credit rule."""
    return calculate_period(
        PeriodInputs(
            period_start=WEEK_START,
            period_end=WEEK_START + timedelta(days=1),
            daily_required_minutes={WEEK_START: 0},
            weekly_required_minutes=0,
            calendar_classes={WEEK_START: CalendarClass.WEEKDAY},
            intervals=(_interval("solo", 0, 8, minutes, work_type),),
            opening_ledger_entries=(),
            policy_version="f6-measurement",
            engine_build="phase-2-ordinary-v1",
            parameters=parameters,
        )
    )


def test_WT_FLEX_009_v2_paid_overtime_does_not_credit_the_account() -> None:
    """The flip moves the ACCOUNT layer only — and by exactly the paid minutes.

    The closing balance must equal the accumulation the layer rule implies: each
    day contributes its account credit minus its required minutes, so paid
    overtime is excluded from what accumulates.
    """
    v1 = calculate_period(_week_inputs(DEFAULT_ORDINARY_PARAMETERS))
    v2 = calculate_period(_week_inputs(CORRECTED_PARAMETERS))

    assert [row.worked_minutes for row in v1.daily_results] == [
        row.worked_minutes for row in v2.daily_results
    ], "worked_minutes is the factual/legal layer and must not move"
    assert [row.required_minutes for row in v1.daily_results] == [
        row.required_minutes for row in v2.daily_results
    ]
    assert [row.account_credit_minutes for row in v1.daily_results] == [
        row.worked_minutes for row in v1.daily_results
    ], "v1 credits every work type"

    paid_day = v1.daily_results[0]
    assert paid_day.worked_minutes == DAY_MINUTES + PAID_OVERTIME_MINUTES
    assert paid_day.account_credit_minutes == DAY_MINUTES + PAID_OVERTIME_MINUTES
    assert v2.daily_results[0].worked_minutes == DAY_MINUTES + PAID_OVERTIME_MINUTES
    assert v2.daily_results[0].account_credit_minutes == DAY_MINUTES

    assert v1.closing_balance_minutes - v2.closing_balance_minutes == PAID_OVERTIME_MINUTES
    assert v2.closing_balance_minutes == v2.opening_balance_minutes + sum(
        row.account_credit_minutes - row.required_minutes for row in v2.daily_results
    ), "the closing balance must follow the account-credit accumulation"
    # The two editions disagree, so the assertion above is not vacuous.
    assert v1.closing_balance_minutes != v2.closing_balance_minutes


def test_WT_FLEX_009_v2_time_compensated_overtime_still_credits() -> None:
    """The complement — the flip must not be a blanket exclusion of overtime."""
    for work_type, expected in (
        (WorkType.OVERTIME_TIME_COMP, 60),
        (WorkType.OVERTIME_PAID, 0),
        (WorkType.ON_SITE, 60),
        (WorkType.REMOTE, 60),
        (WorkType.SHIFT, 60),
        (WorkType.OCD_CALL, 60),
        (WorkType.BUSINESS_TRIP_WORK, 60),
    ):
        result = _single_day(work_type, 60, CORRECTED_PARAMETERS)
        row = result.daily_results[0]
        assert row.worked_minutes == 60, work_type
        assert row.account_credit_minutes == expected, work_type


def test_WT_FLEX_009_the_legal_findings_are_identical_across_editions() -> None:
    """``WT-CORE-*`` reads the legal layer, never the credit, so it cannot move."""

    def legal(result: PeriodResult) -> list[tuple[str, object, str]]:
        return sorted(
            (finding.rule_id, finding.date, finding.message)
            for finding in result.findings
            if finding.rule_id.startswith("WT-CORE")
        )

    assert legal(calculate_period(_week_inputs(DEFAULT_ORDINARY_PARAMETERS))) == legal(
        calculate_period(_week_inputs(CORRECTED_PARAMETERS))
    )


def test_the_corrected_parameter_set_is_a_credit_only_delta() -> None:
    """The measurement must not quietly change any other rule input.

    A parameter that moved would make the divergence report describe a different
    policy change than the one the unpublished edition is gated on.
    """
    assert CORRECTED_PARAMETERS.credit_by_work_type[WorkType.OVERTIME_PAID.value] is False
    for member in WorkType:
        if member is WorkType.OVERTIME_PAID:
            continue
        assert CORRECTED_PARAMETERS.credit_by_work_type[member.value] is True, member
    for name, value in vars(DEFAULT_ORDINARY_PARAMETERS).items():
        if name == "credit_by_work_type":
            continue
        assert getattr(CORRECTED_PARAMETERS, name) == value, name
