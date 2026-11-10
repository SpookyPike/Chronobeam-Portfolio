"""Executable policy editions (``ARCH-POL-001``, ``EINV-09``).

A historical calculation must resolve the edition that applied to its own effective date and stay
reproducible afterwards. The *identity* of an edition is data and lives in ``policy_version``
(effective dating, status, provenance); its *parameters* are code, because a policy number that
can change without code review and without a test is a runtime rules engine by the back door.

The registry is the code half of that hybrid:

* ``policy_version`` (DB) — identity, effective dating, provenance.
* ``EDITIONS`` (code)      — the frozen typed parameter set for one rule set.

This module is pure: no ORM, no session, no clock. ``calculate_period`` reads
``PeriodInputs.parameters`` and never performs a lookup, so the pure engine keeps no hidden global
dependency. The single lookup lives in the persistence adapter
(``app.policy.service._month_inputs``), which is the only place that reads a ``policy_version`` row
and can therefore validate its identity.

**Edition immutability (``EINV-20``).** Never re-point an existing ``rule_set`` at different
parameters or a different implementation, and never remove an entry from ``HISTORICAL_RULE_SETS``.
A changed constant that can move an observable historical number is a NEW edition with a NEW
``effective_from`` — never an edit to a shipped one, because that would silently restate
already-accounted history (``EINV-09`` / ``INV-11``).

**What the parameters are.** The ordinary-time defaults are the statutory thresholds of
``LAW-01`` (ArbZG) and are not configurable in any meaningful sense: eight and ten hours, thirty
and forty-five minutes of break, eleven hours of rest. The travel and flex parameters are
``POL-01``/``POL-08`` sample policy. See ``docs/POLICY_SOURCES.md`` and
``docs/RULE_CATALOGUE.md``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from app.enums import WorkType

#: The provenance generation these editions were built against: the SHA-256 of
#: ``docs/POLICY_SOURCES.md`` as published in this repository.  Recorded provenance only — the
#: value that governs a calculation is the ``policy_version.source_manifest_hash`` row.
#: A changed register is a new generation, and an edition that changes with it is a new edition
#: with a new effective date (``EINV-20``).
SOURCE_MANIFEST_HASH = "e45fb742f9f2aaa53b8e1e6ad0538ba1a631f9adb02ab717608815d5cd850b9a"

#: The public build has ONE register, so every edition shares one provenance generation.  The
#: constant is kept separate because the shift edition is seeded by its own migration and a
#: future split into per-domain registers would change only this value.
SHIFT_SOURCE_MANIFEST_HASH = SOURCE_MANIFEST_HASH

#: v1 credit rule: every recorded work type credits the time account.  This is today's behaviour,
#: which the edition mechanism must not change (see ``credit_by_work_type``).
_ALL_WORK_TYPES_CREDIT: Mapping[str, bool] = MappingProxyType(
    {member.value: True for member in WorkType}
)


@dataclass(frozen=True)
class OrdinaryTimeParameters:
    """Frozen rule inputs of one ordinary-time edition (``WT-CORE`` / ``WT-FLEX``).

    Every default IS today's constant, so ``DEFAULT_ORDINARY_PARAMETERS`` is the single source of
    truth and an edition that changes none of them reproduces the pre-edition engine exactly.
    """

    # LAW-01 §4: a gap of at least this many minutes is a qualifying break (the statute's
    # minimum segment length, not an employer choice).
    qualifying_break_minutes: int = 15
    six_hour_threshold_minutes: int = 6 * 60
    nine_hour_threshold_minutes: int = 9 * 60
    # LAW-01 §3: eight hours, extendable to ten.
    ten_hour_max_minutes: int = 10 * 60
    # LAW-01 §5(1): minimum rest between working periods.
    eleven_hour_rest_minutes: int = 11 * 60
    # POL-01: maximum year-end payout.
    year_end_payout_cap_minutes: int = 40 * 60
    # POL-01: the ordinary trusted-flex corridor.
    corridor_start_minutes: int = 6 * 60
    corridor_end_minutes: int = 21 * 60
    # POL-01: warning (1/2) and critical (1/1) balance thresholds, expressed as denominators of
    # the contractual weekly requirement.
    flex_warn_denominator: int = 2
    flex_critical_denominator: int = 1
    # POL-01: negative-balance warning, in individual working days.
    negative_balance_days: int = 2
    # POL-01: the YEAR-END SETTLEMENT BOUNDARY — the period whose EXCLUSIVE ``period_end`` is the
    # year boundary, i.e. the DECEMBER period.  These name a PERIOD, not a payout month.  The
    # payout month is a sample-policy choice and is deliberately not encoded: nothing in the
    # engine may assert which month pays out a positive balance.
    settlement_boundary_month: int = 1
    settlement_boundary_day: int = 1
    # POL-01 layer separation — which work types CREDIT the working-time account.  v1: all of
    # them, i.e. ``account_credit_minutes == worked_minutes`` and the balance is byte-identical to
    # the pre-edition engine.  Whether paid overtime should credit the account is a policy
    # question (``WT-FLEX-009``), so the layer is separated without moving the number.
    credit_by_work_type: Mapping[str, bool] = field(default_factory=lambda: _ALL_WORK_TYPES_CREDIT)


DEFAULT_ORDINARY_PARAMETERS = OrdinaryTimeParameters()

#: ``TRAVEL-004``'s credit BASE. ``EXCESS`` credits 25 % of the amount above the monthly
#: threshold; ``TOTAL`` credits 25 % of the whole non-chargeable total once the threshold is
#: exceeded. ``POL-08`` describes the TOTAL form; the shipped v1 computes ``EXCESS`` — a recorded
#: executable divergence.  Both forms are expressible, so correcting the base is a NEW edition
#: rather than an edit to a shipped one (``EINV-20``/``INV-11``), and no already-computed month is
#: restated.
TravelCreditBase = Literal["EXCESS", "TOTAL"]


@dataclass(frozen=True)
class TravelParameters:
    """Frozen rule inputs of one travel-credit edition (``TRAVEL-003``/``004``).

    Every default IS the shipped v1 constant, so ``DEFAULT_TRAVEL_PARAMETERS`` reproduces the
    original travel engine byte for byte — the same property ``OrdinaryTimeParameters`` gives the
    ordinary edition.  These are ``POL-08`` sample policy, not statutory values.
    """

    # TRAVEL-004: the monthly non-chargeable-travel threshold the credit is conditioned on
    # (POL-08: 15 hours).
    passive_threshold_minutes: int = 15 * 60
    # TRAVEL-004: the credit rate as a denominator of the base (POL-08: 25 %).
    credit_denominator: int = 4
    # TRAVEL-004: which base the rate applies to. See ``TravelCreditBase``.
    credit_base: TravelCreditBase = "EXCESS"


DEFAULT_TRAVEL_PARAMETERS = TravelParameters()


@dataclass(frozen=True)
class PolicyEdition:
    """One immutable, resolvable policy edition.

    ``rule_set`` is the registry key and equals ``policy_version.engine_rule_set`` — the string a
    persisted result, snapshot or package records.  ``version`` is the display label
    (``policy_version.version``) that recipient-visible previews show, so the literals that
    already exist inside frozen payloads come from one place instead of being re-typed per call
    site.
    """

    rule_set: str
    version: str
    algorithm: str
    parameters: OrdinaryTimeParameters | TravelParameters | None
    note: str
    source_manifest_hash: str = SOURCE_MANIFEST_HASH


class UnknownPolicyEdition(Exception):
    """A recorded ``engine_rule_set`` has no executable registry entry.

    Fail closed: never fall back to the newest edition — that would silently restate a historical
    month under today's rules (``EINV-08``).
    """

    def __init__(self, rule_set: str) -> None:
        self.rule_set = rule_set
        super().__init__(f"no executable policy edition is registered for rule set {rule_set!r}")


ORDINARY_RULE_SET_V1 = "phase-2-ordinary-v1"
SHIFT_RULE_SET_V1 = "phase-5-shift-shadow-v1"
OCD_RULE_SET_V1 = "phase-2-ocd-v1"
OVERTIME_RULE_SET_V1 = "phase-2-overtime-v1"
TRAVEL_RULE_SET_V1 = "phase-2-travel-v1"
#: ``TRAVEL-004``'s CORRECTED-base edition: the TOTAL base described by ``POL-08``.
#:
#: **DESIGNED, NOT REGISTERED, NOT PUBLISHED — deliberately.** The rule set is named and its
#: parameter set exists as a first-class value, so the correction is expressible, reviewable and
#: testable; but there is **no ``EDITIONS`` entry, no ``HISTORICAL_RULE_SETS`` entry, no
#: ``policy_version`` row and no publishing migration**.  That is the form ``ARCH-POL-001``
#: requires while a rollout boundary is undecided: the registry entry and its publishing
#: migration land **together, in one change**, so ``HISTORICAL_RULE_SETS`` can never contain a
#: rule set that was never published in the database.  ``phase-2-ordinary-v2`` (the paid-overtime
#: correction) is in exactly the same state.
#:
#: Publishing it needs an effective boundary strictly later than ``phase-2-travel-v1``'s
#: ``2026-06-01`` (``EINV-20`` + ``uq_policy_version_effective_from``), and that boundary is the
#: operator's rollout month — a deployment decision, not a value this repository may invent.  So
#: the publication half stays GATED; the shipped default remains the ``EXCESS`` base,
#: ``TRAVEL-004`` stays ``PARTIAL``, and it never becomes ``STATUTORY`` while its pool/yardstick
#: question is open.
TRAVEL_RULE_SET_V2 = "phase-2-travel-v2"

#: The corrected parameter set itself: ``POL-08``'s base is the **total** non-chargeable travel
#: time, not the excess above the threshold.  Exported as a value so the correction is a one-line
#: registry+row change when its boundary is decided — and so nothing about it is hidden inside a
#: test module.
TOTAL_BASE_TRAVEL_PARAMETERS = TravelParameters(credit_base="TOTAL")

EDITIONS: Mapping[str, PolicyEdition] = MappingProxyType(
    {
        ORDINARY_RULE_SET_V1: PolicyEdition(
            rule_set=ORDINARY_RULE_SET_V1,
            version="2026-06-ordinary-v1",
            algorithm="ordinary-v1",
            parameters=DEFAULT_ORDINARY_PARAMETERS,
            note=(
                "Initial ordinary working-time edition (WT-CORE / WT-FLEX), effective "
                "2026-06-01. Statutory thresholds from LAW-01; flex parameters from POL-01. "
                "Every work type credits the time account."
            ),
        ),
        SHIFT_RULE_SET_V1: PolicyEdition(
            rule_set=SHIFT_RULE_SET_V1,
            version="2026-06-shadow-v1",
            algorithm="shift-v1",
            parameters=None,
            note=(
                "Shadow-mode shift-work edition. Identity only: its constants are not "
                "parameterised yet."
            ),
            source_manifest_hash=SHIFT_SOURCE_MANIFEST_HASH,
        ),
        OCD_RULE_SET_V1: PolicyEdition(
            rule_set=OCD_RULE_SET_V1,
            version="2026-ocd-v1",
            algorithm="ocd-v1",
            parameters=None,
            note=(
                "OCD compensation projection edition. Identity only: the preview literal was "
                "previously an unresolvable phantom edition."
            ),
        ),
        OVERTIME_RULE_SET_V1: PolicyEdition(
            rule_set=OVERTIME_RULE_SET_V1,
            version="2026-ot-v1",
            algorithm="overtime-v1",
            parameters=None,
            note=(
                "Overtime projection edition. Identity only: the preview literal was previously "
                "an unresolvable phantom edition."
            ),
        ),
        TRAVEL_RULE_SET_V1: PolicyEdition(
            rule_set=TRAVEL_RULE_SET_V1,
            version="2026-06-travel-v1",
            algorithm="travel-v1",
            parameters=DEFAULT_TRAVEL_PARAMETERS,
            note=(
                "Travel top-up / credit edition (TRAVEL-003/004). Its constants are "
                "parameterised and the defaults reproduce the shipped behaviour exactly: "
                "credit = 25 % of the amount ABOVE the 15 h monthly threshold, which POL-08 "
                "records as a DIVERGENCE (it grants 25 % of the TOTAL)."
            ),
        ),
    }
)

#: Append-only allowlist: every rule set that has EVER SHIPPED.  An entry is never removed, so
#: deleting a live registry entry breaks the build instead of silently making a recorded edition
#: unexecutable.
#:
#: ``TRAVEL_RULE_SET_V2`` is deliberately ABSENT, and so is every other designed-but-unpublished
#: rule set: nothing here has shipped, so claiming it had would be false.  The containment test is
#: deliberately ONE-WAY (``EDITIONS ⊇ HISTORICAL_RULE_SETS``): the allowlist may never lose an
#: entry, while the registry is free to gain one at the moment its publishing migration lands
#: (``ARCH-POL-001``).
HISTORICAL_RULE_SETS: frozenset[str] = frozenset(
    {
        ORDINARY_RULE_SET_V1,
        SHIFT_RULE_SET_V1,
        OCD_RULE_SET_V1,
        OVERTIME_RULE_SET_V1,
        TRAVEL_RULE_SET_V1,
    }
)


def resolve_edition(rule_set: str) -> PolicyEdition:
    """Return the executable edition for a recorded rule-set identifier."""
    edition = EDITIONS.get(rule_set)
    if edition is None:
        raise UnknownPolicyEdition(rule_set)
    return edition
