"""F4.1/F4.3 registry identity: executable, immutable, append-only.

The registry is the CODE half of the policy hybrid (``policy_version`` owns
identity/effective dating, ``EDITIONS`` owns the frozen parameters). These tests
pin the properties that make a recorded edition *executable* and impossible to
mutate silently — EINV-09 at the code level. The DB half (every recorded
``engine_rule_set`` is registered) lives in ``test_policy_editions_db.py``.
"""

from dataclasses import FrozenInstanceError, fields

import pytest
from app.enums import WorkType
from app.policy.editions import (
    DEFAULT_ORDINARY_PARAMETERS,
    EDITIONS,
    HISTORICAL_RULE_SETS,
    ORDINARY_RULE_SET_V1,
    TRAVEL_RULE_SET_V1,
    OrdinaryTimeParameters,
    PolicyEdition,
    TravelParameters,
    UnknownPolicyEdition,
    resolve_edition,
)


def test_historical_rule_sets_are_still_registered() -> None:
    """F4 D3: an edition that ever shipped is never removed from the registry."""
    assert set(EDITIONS) >= HISTORICAL_RULE_SETS


def test_every_registered_edition_is_fully_described() -> None:
    """Every entry has a resolvable identity and honest provenance.

    A registry entry with a blank label or a mismatched key would make a
    recorded edition describable but not resolvable — the defect F4 closes.
    """
    for key, edition in EDITIONS.items():
        assert isinstance(edition, PolicyEdition)
        assert edition.rule_set == key
        assert edition.version
        assert edition.algorithm
        assert edition.note
        # A source-manifest hash is provenance, not decoration: it must be a real
        # sha256, and it must match what the DB row records (see
        # ``test_policy_editions_db.py::test_every_recorded_engine_rule_set_is_registered``).
        assert len(edition.source_manifest_hash) == 64
        int(edition.source_manifest_hash, 16)


def test_the_ordinary_edition_is_the_one_with_ordinary_parameters() -> None:
    """Only an ordinary-time edition drives ``calculate_period``; a sibling must
    not silently pretend to carry ordinary parameters (F8-9 generalised this: the
    travel editions carry ``TravelParameters``, so the check is by FAMILY rather
    than by ``is None``)."""
    ordinary = resolve_edition(ORDINARY_RULE_SET_V1)
    assert ordinary.parameters is DEFAULT_ORDINARY_PARAMETERS
    others = {key: edition for key, edition in EDITIONS.items() if key != ORDINARY_RULE_SET_V1}
    assert others, "the sibling editions must stay registered"
    assert all(
        not isinstance(edition.parameters, OrdinaryTimeParameters) for edition in others.values()
    )


def test_only_the_travel_edition_carries_travel_parameters() -> None:
    """The parameter set is family-scoped, so a mis-registered edition cannot
    execute on another family's parameters."""
    travel = {key: edition for key, edition in EDITIONS.items() if key == TRAVEL_RULE_SET_V1}
    assert set(travel) == {TRAVEL_RULE_SET_V1}
    for edition in travel.values():
        assert isinstance(edition.parameters, TravelParameters)
    identity_only = {
        key: edition
        for key, edition in EDITIONS.items()
        if key not in {ORDINARY_RULE_SET_V1, TRAVEL_RULE_SET_V1}
    }
    assert identity_only, "the identity-only siblings must stay registered"
    assert all(edition.parameters is None for edition in identity_only.values())


def test_unknown_rule_set_fails_closed() -> None:
    """F4 D5: never fall back to the newest edition."""
    with pytest.raises(UnknownPolicyEdition) as caught:
        resolve_edition("not-a-shipped-rule-set")
    assert caught.value.rule_set == "not-a-shipped-rule-set"


def test_default_parameters_cover_every_work_type() -> None:
    """The credit map is exhaustive, so the engine's ``.get(..., True)`` fallback
    is unreachable in production: a new work type cannot silently change credit."""
    assert set(DEFAULT_ORDINARY_PARAMETERS.credit_by_work_type) == {
        member.value for member in WorkType
    }


def test_v1_credit_rule_is_every_work_type_credits() -> None:
    """F4 does not change the credit rule: v1 is today's behaviour."""
    assert all(DEFAULT_ORDINARY_PARAMETERS.credit_by_work_type.values())


def test_edition_parameters_are_immutable() -> None:
    """Neither the parameter set nor its credit map can be edited in place."""
    with pytest.raises(FrozenInstanceError):
        DEFAULT_ORDINARY_PARAMETERS.qualifying_break_minutes = 20  # type: ignore[misc]
    with pytest.raises(TypeError):
        DEFAULT_ORDINARY_PARAMETERS.credit_by_work_type["OVERTIME_PAID"] = False  # type: ignore[index]


def test_the_registry_is_immutable() -> None:
    with pytest.raises(TypeError):
        EDITIONS["injected"] = resolve_edition(ORDINARY_RULE_SET_V1)  # type: ignore[index]


def test_parameter_defaults_are_the_pre_f4_constants() -> None:
    """The defaults ARE the constants the engine used before F4 — this is why the
    introduction of the parameter set changes no computed value."""
    parameters = OrdinaryTimeParameters()
    assert parameters.qualifying_break_minutes == 15
    assert parameters.six_hour_threshold_minutes == 360
    assert parameters.nine_hour_threshold_minutes == 540
    assert parameters.ten_hour_max_minutes == 600
    assert parameters.eleven_hour_rest_minutes == 660
    assert parameters.year_end_payout_cap_minutes == 2400
    assert parameters.corridor_start_minutes == 360
    assert parameters.corridor_end_minutes == 1260
    assert parameters.flex_warn_denominator == 2
    assert parameters.flex_critical_denominator == 1
    assert parameters.negative_balance_days == 2
    assert parameters.settlement_boundary_month == 1
    assert parameters.settlement_boundary_day == 1


def test_WT_FLEX_006_no_payout_month_is_encoded_in_the_parameters() -> None:
    """The settlement boundary is a PERIOD boundary, and nothing else may appear.

    The parameter is compared against the EXCLUSIVE ``period_end``, so 1/1 names
    the DECEMBER period. ``POL-01`` leaves the payout month open, so no month may
    be encoded — a ``payout_month`` parameter would silently resolve a conflict
    the sample policy itself leaves open. Mutation-proof: renaming the boundary back
    to a payout-month reading fails this test.
    """
    names = {field.name for field in fields(OrdinaryTimeParameters)}
    assert "settlement_boundary_month" in names
    assert "year_end_month" not in names, "a payout-month reading was reintroduced"
    assert "payout_month" not in names and "payout_day" not in names
