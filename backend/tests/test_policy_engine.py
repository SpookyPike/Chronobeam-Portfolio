"""Pure calculation tests: WT-CORE-* and WT-FLEX-* boundaries."""

from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta

from app.policy.engine import (
    BERLIN,
    CalendarClass,
    FindingSeverity,
    IntervalFact,
    LedgerFact,
    PeriodInputs,
    calculate_period,
)
from hypothesis import given
from hypothesis import strategies as st

DAY = date(2026, 7, 6)  # Monday


def interval(
    name: str, start_hour: int, start_minute: int, end_hour: int, end_minute: int = 0
) -> IntervalFact:
    return IntervalFact(
        id=name,
        start_at=datetime(2026, 7, 6, start_hour, start_minute, tzinfo=BERLIN),
        end_at=datetime(2026, 7, 6, end_hour, end_minute, tzinfo=BERLIN),
        work_type="ON_SITE",
    )


def inputs(
    intervals: tuple[IntervalFact, ...] = (),
    *,
    required: int = 468,
    weekly_required: int | None = None,
    opening: int = 0,
    calendar_class: CalendarClass = CalendarClass.WEEKDAY,
    end: date = date(2026, 7, 7),
) -> PeriodInputs:
    return PeriodInputs(
        period_start=DAY,
        period_end=end,
        daily_required_minutes={DAY: required},
        weekly_required_minutes=required if weekly_required is None else weekly_required,
        calendar_classes={DAY: calendar_class},
        intervals=intervals,
        opening_ledger_entries=(LedgerFact(date(2026, 6, 30), opening, "CUTOVER_SEED"),),
        policy_version="test-v1",
        engine_build="test-build",
    )


def rule_ids(result: object) -> set[str]:
    return {finding.rule_id for finding in result.findings}  # type: ignore[attr-defined]


def test_WT_FLEX_001_effective_part_time_requirement_is_not_hardcoded() -> None:
    result = calculate_period(inputs(required=351))
    assert result.daily_results[0].required_minutes == 351
    assert result.daily_results[0].flex_delta_minutes == -351


def test_DATA_004_two_intervals_and_WT_CORE_003_qualifying_break() -> None:
    result = calculate_period(inputs((interval("a", 8, 0, 12, 0), interval("b", 12, 30, 16, 18))))
    assert result.daily_results[0].worked_minutes == 468
    assert result.daily_results[0].qualifying_break_minutes == 30
    assert result.daily_results[0].flex_delta_minutes == 0
    assert not ({"WT-CORE-003", "WT-CORE-005"} & rule_ids(result))


def test_WT_CORE_003_and_004_break_boundaries() -> None:
    exactly_six = calculate_period(inputs((interval("a", 8, 0, 14, 0),)))
    six_oh_one = calculate_period(inputs((interval("a", 8, 0, 14, 1),)))
    nine_exact = calculate_period(inputs((interval("a", 8, 0, 17, 0),)))
    nine_oh_one = calculate_period(
        inputs((interval("a", 8, 0, 12, 0), interval("b", 12, 30, 17, 31)))
    )
    assert "WT-CORE-003" not in rule_ids(exactly_six)
    assert "WT-CORE-003" in rule_ids(six_oh_one)
    assert "WT-CORE-004" not in rule_ids(nine_exact)
    assert "WT-CORE-004" in rule_ids(nine_oh_one)


def test_WT_CORE_005_micro_gaps_do_not_break_an_uninterrupted_block() -> None:
    result = calculate_period(inputs((interval("a", 8, 0, 11, 0), interval("b", 11, 14, 14, 15))))
    assert "WT-CORE-005" in rule_ids(result)


def test_WT_CORE_006_and_008_daily_max_and_flex_corridor() -> None:
    result = calculate_period(inputs((interval("a", 5, 59, 16, 0),)))
    exact_corridor = calculate_period(inputs((interval("a", 6, 0, 21, 0),)))
    after_corridor = calculate_period(inputs((interval("a", 6, 0, 21, 1),)))
    assert {"WT-CORE-006", "WT-CORE-008"} <= rule_ids(result)
    assert "WT-CORE-008" not in rule_ids(exact_corridor)
    assert "WT-CORE-008" in rule_ids(after_corridor)


def test_WT_CORE_007_rest_boundaries() -> None:
    next_day = DAY + timedelta(days=1)
    exact = (
        IntervalFact(
            "a",
            datetime(2026, 7, 6, 18, tzinfo=BERLIN),
            datetime(2026, 7, 6, 20, tzinfo=BERLIN),
            "ON_SITE",
        ),
        IntervalFact(
            "b",
            datetime(2026, 7, 7, 7, tzinfo=BERLIN),
            datetime(2026, 7, 7, 9, tzinfo=BERLIN),
            "ON_SITE",
        ),
    )
    short = (
        exact[0],
        IntervalFact(
            "b",
            datetime(2026, 7, 7, 6, 59, 59, tzinfo=BERLIN),
            datetime(2026, 7, 7, 9, tzinfo=BERLIN),
            "ON_SITE",
        ),
    )
    exact_inputs = PeriodInputs(
        period_start=DAY,
        period_end=next_day + timedelta(days=1),
        daily_required_minutes={DAY: 0, next_day: 0},
        weekly_required_minutes=0,
        calendar_classes={},
        intervals=exact,
        opening_ledger_entries=(),
        policy_version="test-v1",
        engine_build="test-build",
    )
    short_inputs = PeriodInputs(
        period_start=DAY,
        period_end=next_day + timedelta(days=1),
        daily_required_minutes={DAY: 0, next_day: 0},
        weekly_required_minutes=0,
        calendar_classes={},
        intervals=short,
        opening_ledger_entries=(),
        policy_version="test-v1",
        engine_build="test-build",
    )
    assert "WT-CORE-007" not in rule_ids(calculate_period(exact_inputs))
    assert "WT-CORE-007" in rule_ids(calculate_period(short_inputs))


def test_WT_CORE_007_same_day_split_sessions_are_one_workday() -> None:
    """The documented ordinary-day split is not an 11-hour rest violation."""
    result = calculate_period(
        inputs((interval("morning", 8, 0, 10, 0), interval("evening", 20, 0, 22, 0)))
    )

    assert "WT-CORE-007" not in rule_ids(result)


def test_WT_CORE_overnight_work_uses_one_canonical_period_without_midnight_rest() -> None:
    next_day = DAY + timedelta(days=1)
    result = calculate_period(
        PeriodInputs(
            period_start=DAY,
            period_end=next_day + timedelta(days=1),
            daily_required_minutes={DAY: 0, next_day: 0},
            weekly_required_minutes=0,
            calendar_classes={},
            intervals=(
                IntervalFact(
                    "overnight",
                    datetime(2026, 7, 6, 19, tzinfo=BERLIN),
                    datetime(2026, 7, 7, 2, 30, tzinfo=BERLIN),
                    "ON_SITE",
                ),
            ),
            opening_ledger_entries=(),
            policy_version="test-v1",
            engine_build="test-build",
        )
    )
    assert {"WT-CORE-003", "WT-CORE-005"} <= rule_ids(result)
    assert "WT-CORE-007" not in rule_ids(result)


def test_WT_CORE_007_uses_prior_month_canonical_rest_context() -> None:
    july = date(2026, 7, 1)
    result = calculate_period(
        PeriodInputs(
            period_start=july,
            period_end=july + timedelta(days=1),
            daily_required_minutes={july: 0},
            weekly_required_minutes=0,
            calendar_classes={},
            intervals=(
                IntervalFact(
                    "june",
                    datetime(2026, 6, 30, 20, tzinfo=BERLIN),
                    datetime(2026, 6, 30, 22, tzinfo=BERLIN),
                    "ON_SITE",
                ),
                IntervalFact(
                    "july",
                    datetime(2026, 7, 1, 6, tzinfo=BERLIN),
                    datetime(2026, 7, 1, 8, tzinfo=BERLIN),
                    "ON_SITE",
                ),
            ),
            opening_ledger_entries=(),
            policy_version="test-v1",
            engine_build="test-build",
        )
    )
    findings = [finding for finding in result.findings if finding.rule_id == "WT-CORE-007"]
    assert [(finding.date, finding.rule_id) for finding in findings] == [(july, "WT-CORE-007")]


def test_WT_CORE_008_treats_end_at_midnight_as_2400() -> None:
    next_day = DAY + timedelta(days=1)
    clean = calculate_period(
        inputs(
            (
                IntervalFact(
                    "clean",
                    datetime(2026, 7, 6, 20, tzinfo=BERLIN),
                    datetime(2026, 7, 7, 0, tzinfo=BERLIN),
                    "ON_SITE",
                ),
            ),
            end=next_day + timedelta(days=1),
        )
    )
    finding = calculate_period(
        inputs(
            (
                IntervalFact(
                    "finding",
                    datetime(2026, 7, 6, 21, tzinfo=BERLIN),
                    datetime(2026, 7, 7, 0, tzinfo=BERLIN),
                    "ON_SITE",
                ),
            ),
            end=next_day + timedelta(days=1),
        )
    )
    assert "WT-CORE-008" not in rule_ids(clean)
    assert "WT-CORE-008" in rule_ids(finding)


def test_WT_FLEX_thresholds_and_negative_two_day_threshold_are_strict() -> None:
    warning = calculate_period(inputs(opening=703))
    critical = calculate_period(inputs(opening=937))
    debt = calculate_period(inputs(opening=-469))
    assert "WT-FLEX-003" in rule_ids(warning)
    assert "WT-FLEX-004" in rule_ids(critical)
    assert "WT-FLEX-005" in rule_ids(debt)


def week_inputs(
    *,
    worked_minutes: Mapping[date, int] | None = None,
    required: int = 468,
    opening: int = 0,
) -> PeriodInputs:
    """One ordinary Mon-Fri week, so an as-of date inside it is meaningful.

    ``DAY`` (2026-07-06) is a Monday; the period is half-open at Saturday
    2026-07-11. Nothing is recorded unless ``worked_minutes`` says so, which is
    the point: an un-lived weekday is charged its full requirement against zero
    recorded work, and that is the projection the as-of trigger must not read.
    """
    days = [DAY + timedelta(days=offset) for offset in range(5)]
    return PeriodInputs(
        period_start=DAY,
        period_end=DAY + timedelta(days=5),
        daily_required_minutes=dict.fromkeys(days, required),
        weekly_required_minutes=required * len(days),
        calendar_classes=dict.fromkeys(days, CalendarClass.WEEKDAY),
        intervals=tuple(
            IntervalFact(
                id=f"w-{day.isoformat()}",
                start_at=datetime(day.year, day.month, day.day, 8, 0, tzinfo=BERLIN),
                end_at=datetime(day.year, day.month, day.day, 8, 0, tzinfo=BERLIN)
                + timedelta(minutes=minutes),
                work_type="ON_SITE",
            )
            for day, minutes in (worked_minutes or {}).items()
        ),
        opening_ledger_entries=(LedgerFact(date(2026, 6, 30), opening, "CUTOVER_SEED"),),
        policy_version="test-v1",
        engine_build="test-build",
    )


def test_WT_FLEX_005_reads_the_balance_through_the_as_of_date_not_the_period_end() -> None:
    """The rule's trigger is the "at any time" condition in its own definition.

    Every weekday is charged its full requirement, and nothing is recorded — the
    employee has simply not lived the later days yet. The threshold is
    ``negative_balance_days`` (2) x the individual day (468) = 936, strictly
    greater, so the table below pins the exact boundary as well as the fix:
    Monday is 468 of debt (clean), Tuesday reaches exactly 936 and must still be
    clean, and Wednesday crosses it.
    """
    monday = DAY
    tuesday = DAY + timedelta(days=1)
    wednesday = DAY + timedelta(days=2)
    friday = DAY + timedelta(days=4)
    assert "WT-FLEX-005" not in rule_ids(calculate_period(replace(week_inputs(), as_of=monday)))
    assert "WT-FLEX-005" not in rule_ids(calculate_period(replace(week_inputs(), as_of=tuesday)))
    assert "WT-FLEX-005" in rule_ids(calculate_period(replace(week_inputs(), as_of=wednesday)))
    assert "WT-FLEX-005" in rule_ids(calculate_period(replace(week_inputs(), as_of=friday)))
    # The regression pin: with no as-of date the rule reads the period end, which
    # is exactly the behaviour that warned employees who owed nothing. Without
    # this half the assertions above would pass on a rule that never fires.
    assert "WT-FLEX-005" in rule_ids(calculate_period(week_inputs()))


def test_the_as_of_balance_is_the_opening_before_the_period_starts() -> None:
    """Nothing has been lived yet, so nothing has accumulated."""
    before = DAY - timedelta(days=1)
    result = calculate_period(replace(week_inputs(), as_of=before))
    assert "WT-FLEX-005" not in rule_ids(result)
    # The projection is still computed and still shown — only the TRIGGER moved.
    assert result.closing_balance_minutes == -468 * 5


def test_a_fully_elapsed_period_is_unchanged_by_the_as_of_date() -> None:
    """INV-11: for a completed period the date filter is the identity.

    Whole-dataclass equality, not field-by-field: any difference at all — a
    finding, a proposal, a fingerprint — fails here.
    """
    inputs_with_work = week_inputs(worked_minutes={DAY: 400})
    plain = calculate_period(inputs_with_work)
    for as_of in (DAY + timedelta(days=4), DAY + timedelta(days=5), DAY + timedelta(days=400)):
        dated = calculate_period(replace(inputs_with_work, as_of=as_of))
        assert dated == plain
        assert dated.snapshot_json() == plain.snapshot_json()


def test_the_as_of_date_is_not_a_fingerprint_input() -> None:
    """A clock reading is not a policy fact (INV-11).

    ``PeriodCalculationSnapshot.input_fingerprint`` is what proves a stored
    disclosure can be reproduced. If it moved with the date, a snapshot written
    yesterday could not be verified today.
    """
    base = week_inputs(worked_minutes={DAY: 400})
    fingerprints = {
        calculate_period(replace(base, as_of=as_of)).input_fingerprint
        for as_of in (None, DAY, DAY + timedelta(days=3), DAY + timedelta(days=400))
    }
    assert len(fingerprints) == 1


def test_WT_FLEX_006_positive_year_end_and_blocked_negative_settlement() -> None:
    december = inputs(opening=3000, end=date(2027, 1, 1))
    positive = calculate_period(december)
    negative = calculate_period(inputs(opening=-1, end=date(2027, 1, 1)))
    assert [proposal.entry_type for proposal in positive.ledger_proposals[-2:]] == [
        "YEAR_END_PAYOUT",
        "YEAR_END_FORFEIT",
    ]
    assert positive.closing_balance_minutes == 0
    assert any(finding.severity == FindingSeverity.BLOCKED_POLICY for finding in negative.findings)


def test_WT_FLEX_007_negative_year_end_names_its_rule_id_not_only_the_severity() -> None:
    """The blocked negative settlement must be identifiable BY RULE.

    Asserting only ``FindingSeverity.BLOCKED_POLICY`` (as the test above does)
    stays green if a regression emits a *different* blocked rule for the same
    condition, so the rule id itself is pinned here.
    """
    negative = calculate_period(inputs(opening=-1, end=date(2027, 1, 1)))
    assert "WT-FLEX-007" in rule_ids(negative)
    blocked = [
        finding
        for finding in negative.findings
        if finding.severity == FindingSeverity.BLOCKED_POLICY
    ]
    assert [finding.rule_id for finding in blocked] == ["WT-FLEX-007"]


def test_WT_FLEX_006_the_settlement_fires_only_at_the_year_end_boundary() -> None:
    """The boundary names a PERIOD, not a payout month.

    The parameter is compared against the EXCLUSIVE ``period_end``, so 1/1 is the
    DECEMBER period: December settles, January does not. ``POL-01`` leaves the
    payout month open, so no payout month may be encoded or asserted anywhere.
    """
    december = [
        proposal.entry_type
        for proposal in calculate_period(
            inputs(opening=3000, end=date(2027, 1, 1))
        ).ledger_proposals
    ]
    january = [
        proposal.entry_type
        for proposal in calculate_period(
            inputs(opening=3000, end=date(2027, 2, 1))
        ).ledger_proposals
    ]
    assert "YEAR_END_PAYOUT" in december
    assert "YEAR_END_PAYOUT" not in january, "the settlement fired outside the year-end boundary"


def test_calendar_holiday_and_employer_free_zero_only_required_time() -> None:
    holiday = calculate_period(
        inputs((interval("a", 8, 0, 12, 0),), calendar_class=CalendarClass.PUBLIC_HOLIDAY)
    )
    employer_free = calculate_period(inputs(calendar_class=CalendarClass.EMPLOYER_FREE))
    assert holiday.daily_results[0].required_minutes == 0
    assert holiday.daily_results[0].worked_minutes == 240
    assert employer_free.daily_results[0].required_minutes == 0


def test_DATA_009_dst_elapsed_uses_instants() -> None:
    day = date(2026, 3, 29)
    result = calculate_period(
        PeriodInputs(
            period_start=day,
            period_end=day + timedelta(days=1),
            daily_required_minutes={day: 0},
            weekly_required_minutes=0,
            calendar_classes={},
            intervals=(
                IntervalFact(
                    "dst",
                    datetime(2026, 3, 29, 1, 30, tzinfo=BERLIN),
                    datetime(2026, 3, 29, 3, 30, tzinfo=BERLIN),
                    "SHIFT",
                ),
            ),
            opening_ledger_entries=(),
            policy_version="test-v1",
            engine_build="test-build",
        )
    )
    assert result.daily_results[0].worked_minutes == 60


def test_DATA_009_fall_back_dst_day_allocation_uses_elapsed_instants() -> None:
    day = date(2026, 10, 25)
    result = calculate_period(
        PeriodInputs(
            period_start=day,
            period_end=day + timedelta(days=1),
            daily_required_minutes={day: 0},
            weekly_required_minutes=0,
            calendar_classes={},
            intervals=(
                IntervalFact(
                    "fall-back",
                    datetime(2026, 10, 25, 0, tzinfo=BERLIN),
                    datetime(2026, 10, 25, 3, tzinfo=BERLIN),
                    "SHIFT",
                ),
            ),
            opening_ledger_entries=(),
            policy_version="test-v1",
            engine_build="test-build",
        )
    )
    assert result.daily_results[0].worked_minutes == 240


@given(
    start=st.integers(min_value=0, max_value=600), duration=st.integers(min_value=2, max_value=300)
)
def test_PROP_002_split_adjacent_intervals_preserves_work_duration(
    start: int, duration: int
) -> None:
    base = datetime(2026, 7, 6, 0, tzinfo=BERLIN) + timedelta(minutes=start)
    end = base + timedelta(minutes=duration)
    midpoint = base + timedelta(minutes=duration // 2)
    one = calculate_period(inputs((IntervalFact("one", base, end, "ON_SITE"),), required=0))
    split = calculate_period(
        inputs(
            (
                IntervalFact("left", base, midpoint, "ON_SITE"),
                IntervalFact("right", midpoint, end, "REMOTE"),
            ),
            required=0,
        )
    )
    assert one.daily_results[0].worked_minutes == split.daily_results[0].worked_minutes


@given(gap=st.integers(min_value=1, max_value=180))
def test_PROP_004_gap_reduces_duration_by_exact_gap(gap: int) -> None:
    start = datetime(2026, 7, 6, 8, tzinfo=BERLIN)
    continuous = calculate_period(
        inputs((IntervalFact("one", start, start + timedelta(hours=8), "ON_SITE"),), required=0)
    )
    gapped = calculate_period(
        inputs(
            (
                IntervalFact("one", start, start + timedelta(hours=4), "ON_SITE"),
                IntervalFact(
                    "two",
                    start + timedelta(hours=4 + gap / 60),
                    start + timedelta(hours=8),
                    "ON_SITE",
                ),
            ),
            required=0,
        )
    )
    assert (
        continuous.daily_results[0].worked_minutes - gapped.daily_results[0].worked_minutes == gap
    )


@given(opening=st.integers(min_value=-5000, max_value=5000))
def test_PROP_006_balance_equals_opening_plus_ledger_proposals(opening: int) -> None:
    result = calculate_period(inputs(opening=opening, required=0))
    assert result.closing_balance_minutes == opening + sum(
        proposal.minutes for proposal in result.ledger_proposals
    )


def test_PROP_001_same_inputs_have_deterministic_snapshot() -> None:
    source = inputs((interval("a", 8, 0, 12, 0), interval("b", 12, 30, 16, 18)))
    assert calculate_period(source) == calculate_period(source)
    assert calculate_period(source).snapshot_json() == calculate_period(source).snapshot_json()


def _month_result(start: date, end: date, span: tuple[datetime, datetime]) -> tuple[int, ...]:
    """Worked minutes per day for a month containing one interval span."""
    result = calculate_period(
        PeriodInputs(
            period_start=start,
            period_end=end,
            daily_required_minutes={},
            weekly_required_minutes=0,
            calendar_classes={},
            intervals=(IntervalFact("span", span[0], span[1], "ON_SITE"),),
            opening_ledger_entries=(),
            policy_version="test-v1",
            engine_build="test-build",
        )
    )
    return tuple(item.worked_minutes for item in result.daily_results)


def test_EINV_17_engine_allocation_preserves_elapsed_time_across_midnight() -> None:
    """EINV-17: splitting a span across midnight preserves the elapsed total.

    The engine-level invariant, independent of the command boundary. The F0
    reproduction asserted it through the API with a sub-minute span; F3.8's
    minute-resolution contract now REJECTS that input, so the invariant is
    pinned here, where the allocation actually happens, for any stored instants.
    """
    start = datetime(2032, 3, 31, 23, 59, 30, tzinfo=BERLIN)
    end = datetime(2032, 4, 1, 0, 0, 30, tzinfo=BERLIN)
    march = _month_result(date(2032, 3, 1), date(2032, 4, 1), (start, end))
    april = _month_result(date(2032, 4, 1), date(2032, 5, 1), (start, end))
    elapsed = int((end - start).total_seconds()) // 60
    assert sum(march) + sum(april) == elapsed == 1


def test_EINV_17_straddling_minute_is_attributed_by_its_completion_boundary() -> None:
    """The attribution convention, frozen.

    A span's elapsed time is partitioned into whole-minute slots anchored at
    the span's own start; a slot belongs to the day containing its COMPLETION
    instant. So ``23:59:30 -> 00:00:30`` credits the single completed minute to
    April 1 (its completion instant is 00:00:30), not to March 31. That is the
    attribution which makes the per-day counts telescope; any future
    re-implementation must make the same choice deliberately.
    """
    start = datetime(2032, 3, 31, 23, 59, 30, tzinfo=BERLIN)
    end = datetime(2032, 4, 1, 0, 0, 30, tzinfo=BERLIN)
    march = _month_result(date(2032, 3, 1), date(2032, 4, 1), (start, end))
    april = _month_result(date(2032, 4, 1), date(2032, 5, 1), (start, end))
    assert march[-1] == 0
    assert april[0] == 1


@given(
    minutes_before=st.integers(min_value=1, max_value=120),
    minutes_after=st.integers(min_value=1, max_value=120),
)
def test_PROP_003_minute_aligned_cross_midnight_allocation_is_exact(
    minutes_before: int, minutes_after: int
) -> None:
    """Minute-aligned spans keep the pre-F3 arithmetic (compatibility guard).

    Every legitimate command input is minute-aligned, and for those the new
    attribution is numerically identical to flooring each portion: the parts
    still sum to the elapsed minutes, and each day keeps exactly its own whole
    minutes.
    """
    midnight = datetime(2032, 4, 1, 0, 0, tzinfo=BERLIN)
    start = midnight - timedelta(minutes=minutes_before)
    end = midnight + timedelta(minutes=minutes_after)
    march = _month_result(date(2032, 3, 1), date(2032, 4, 1), (start, end))
    april = _month_result(date(2032, 4, 1), date(2032, 5, 1), (start, end))
    assert march[-1] == minutes_before
    assert april[0] == minutes_after
    assert sum(march) + sum(april) == minutes_before + minutes_after
