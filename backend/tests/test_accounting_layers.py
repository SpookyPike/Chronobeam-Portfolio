"""Accounting-layer separation: FACT, account credit and remuneration basis.

A synthetic day discriminates the three layers. The employee works 540 minutes,
of which 72 are paid overtime. The contractual requirement for the day is 360
minutes.

* **Fact.** 540 minutes were worked, and the factual column says so.
* **Account credit.** Under v1 every work type credits the account, so the credit
  is also 540 and the flex delta is 540 - 360 = 180.
* **Remuneration basis.** A paid interval is remunerated rather than credited.
  Under the v2 parameter set the credit is 540 - 72 = 468 and the flex delta is
  108, while the factual layer still reads 540.

The separation is structural: v1 and v2 differ only in the credit layer, and the
v2 parameter set below is a test-only edition that proves the mechanism can move
that layer alone.

What this test deliberately does NOT do is "fix" v1. Changing a shipped edition
would silently restate every historical recomputation (EINV-09/INV-11); a
corrected credit rule is a NEW edition.
"""

from datetime import date, datetime

from app.policy.editions import (
    DEFAULT_ORDINARY_PARAMETERS,
    ORDINARY_RULE_SET_V1,
    OrdinaryTimeParameters,
)
from app.policy.engine import (
    BERLIN,
    CalendarClass,
    IntervalFact,
    PeriodInputs,
    calculate_period,
)

DAY = date(2026, 7, 6)  # Monday
REQUIRED_MINUTES = 360
WEEKLY_REQUIRED_MINUTES = 2340


def _layered_day(parameters: OrdinaryTimeParameters) -> PeriodInputs:
    """The synthetic shape: 540 min worked, 72 of them paid overtime."""
    return PeriodInputs(
        period_start=DAY,
        period_end=date(2026, 7, 7),
        daily_required_minutes={DAY: REQUIRED_MINUTES},
        weekly_required_minutes=WEEKLY_REQUIRED_MINUTES,
        calendar_classes={DAY: CalendarClass.WEEKDAY},
        intervals=(
            IntervalFact(
                "ordinary-morning",
                datetime(2026, 7, 6, 8, 0, tzinfo=BERLIN),
                datetime(2026, 7, 6, 12, 0, tzinfo=BERLIN),
                "ON_SITE",
            ),
            IntervalFact(
                "ordinary-afternoon",
                datetime(2026, 7, 6, 12, 30, tzinfo=BERLIN),
                datetime(2026, 7, 6, 16, 18, tzinfo=BERLIN),
                "ON_SITE",
            ),
            IntervalFact(
                "paid-overtime",
                datetime(2026, 7, 6, 16, 18, tzinfo=BERLIN),
                datetime(2026, 7, 6, 17, 30, tzinfo=BERLIN),
                "OVERTIME_PAID",
            ),
        ),
        opening_ledger_entries=(),
        policy_version="2026-06-ordinary-v1",
        engine_build=ORDINARY_RULE_SET_V1,
        parameters=parameters,
    )


#: A test-only edition that differs from v1 in exactly ONE parameter: paid
#: overtime does not credit the working-time account (WT-FLEX-009, F6).
_V2_PARAMETERS = OrdinaryTimeParameters(
    credit_by_work_type={**DEFAULT_ORDINARY_PARAMETERS.credit_by_work_type, "OVERTIME_PAID": False}
)


def test_v1_credits_every_work_type_so_the_account_layer_equals_the_factual_layer() -> None:
    """F4 introduces the layer WITHOUT moving the number: v1 is unchanged."""
    result = calculate_period(_layered_day(DEFAULT_ORDINARY_PARAMETERS))
    day = result.daily_results[0]
    assert day.worked_minutes == 540
    assert day.account_credit_minutes == 540  # identical to the factual layer
    assert day.flex_delta_minutes == 180  # 540 - 360, v1 engine
    assert result.closing_balance_minutes == 180


def test_a_credit_edition_moves_only_the_account_layer() -> None:
    """v1 vs v2: same facts, same legal treatment, a different credit number.

    The v2 expectation follows from the layer rule: the paid 72 minutes are
    remunerated, not credited, so the balance is +108, a DIFFERENT
    number, not a renamed field.
    """
    v1 = calculate_period(_layered_day(DEFAULT_ORDINARY_PARAMETERS))
    v2 = calculate_period(_layered_day(_V2_PARAMETERS))

    # FACT layer: untouched by the credit rule.
    assert v1.daily_results[0].worked_minutes == v2.daily_results[0].worked_minutes == 540
    assert v1.daily_results[0].required_minutes == v2.daily_results[0].required_minutes == 360
    # Legal/compliance layer: byte-identical findings under both editions.
    assert v1.findings == v2.findings

    # Account layer: moves by exactly the paid interval's allocated minutes.
    assert v1.daily_results[0].account_credit_minutes == 540
    assert v2.daily_results[0].account_credit_minutes == 468
    assert v1.daily_results[0].flex_delta_minutes == 180
    assert v2.daily_results[0].flex_delta_minutes == 108
    assert (
        v1.daily_results[0].flex_delta_minutes - v2.daily_results[0].flex_delta_minutes
        == 72  # the paid interval's own minutes
    )
    assert v1.closing_balance_minutes == 180
    assert v2.closing_balance_minutes == 108


def test_the_credit_edition_does_not_move_a_day_without_paid_time() -> None:
    """The rule is targeted: ordinary work is unaffected by the credit flip."""
    inputs = _layered_day(DEFAULT_ORDINARY_PARAMETERS)
    ordinary_only = PeriodInputs(
        **{
            **inputs.__dict__,
            "intervals": tuple(
                item for item in inputs.intervals if item.work_type != "OVERTIME_PAID"
            ),
        }
    )
    v1 = calculate_period(ordinary_only)
    v2 = calculate_period(PeriodInputs(**{**ordinary_only.__dict__, "parameters": _V2_PARAMETERS}))
    assert v1 == v2


def test_default_parameters_are_the_v1_edition_parameters() -> None:
    """The pure engine's default IS v1, so a caller that omits an edition keeps
    today's behaviour (that is what makes the whole pre-F4 suite pass unchanged)."""
    inputs = _layered_day(DEFAULT_ORDINARY_PARAMETERS)
    without_edition = PeriodInputs(
        **{k: v for k, v in inputs.__dict__.items() if k != "parameters"}
    )
    assert calculate_period(without_edition) == calculate_period(inputs)


def test_the_canonical_day_is_byte_stable_under_v1() -> None:
    """Golden pin: with v1 resolved, the engine's numbers and its input
    fingerprint are exactly what they were before F4.

    The fingerprint payload was deliberately NOT extended with the new account
    field — it covers INPUTS only, and the edition identity is already in it as
    ``policy_version``/``engine_build`` — so this literal also proves F4 added no
    hidden calculation input.
    """
    result = calculate_period(_layered_day(DEFAULT_ORDINARY_PARAMETERS))
    assert result.closing_balance_minutes == 180
    assert result.findings == ()
    assert result.input_fingerprint == (
        "b2cfe3b6158c9309ff7e1cbf7447d467e98761f300f630b8e348c5786fcccb30"
    )


def test_account_credit_is_zero_for_a_day_with_no_credited_work() -> None:
    """A day whose only work type does not credit contributes nothing to the
    account — while still being reported as factual work."""
    inputs = PeriodInputs(
        period_start=DAY,
        period_end=date(2026, 7, 7),
        daily_required_minutes={DAY: 480},
        weekly_required_minutes=2400,
        calendar_classes={DAY: CalendarClass.WEEKDAY},
        intervals=(
            IntervalFact(
                "paid",
                datetime(2026, 7, 6, 8, 0, tzinfo=BERLIN),
                datetime(2026, 7, 6, 10, 0, tzinfo=BERLIN),
                "OVERTIME_PAID",
            ),
        ),
        opening_ledger_entries=(),
        policy_version="2026-06-ordinary-v1",
        engine_build=ORDINARY_RULE_SET_V1,
        parameters=_V2_PARAMETERS,
    )
    day = calculate_period(inputs).daily_results[0]
    assert day.worked_minutes == 120
    assert day.account_credit_minutes == 0
    assert day.flex_delta_minutes == -480
