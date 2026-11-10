"""F8.9 — the corrected ``TRAVEL-004`` base is an EDITION, not a constant.

``TRAVEL-004`` is ``PARTIAL``: the base is **resolved** (25 % of the TOTAL
non-chargeable travel time), the shipped v1 computes 25 % of the EXCESS — a
recorded executable divergence — and the pool / 7.8 h yardstick and the grant's
category remain open parameters of ``POL-08``.

F8-9 therefore delivers the two things that can be delivered without inventing an
institutional value:

1. the travel rule's constants become **edition parameters**, so the correction is
   expressible and executable (``TravelParameters``); and
2. the corrected edition ``phase-2-travel-v2`` is **designed but neither registered
   nor published** — no ``EDITIONS`` entry, no ``HISTORICAL_RULE_SETS`` entry, no
   ``policy_version`` row and no migration — because ``ARCH-POL-001`` requires the
   registry entry and its publishing migration to land **together**, at an effective
   boundary strictly later than v1's ``2026-06-01``. That boundary is the rollout
   month, which is not fixed.

The tests below pin both halves, and the guard that matters most is
``test_a_published_travel_edition_changes_the_credit_and_leaves_earlier_months_alone``:
it proves the mechanism actually works end to end (so the publication really is
the only missing piece) AND that a later edition cannot restate an earlier month.
"""

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from app.backup import _domain_checks
from app.errors import PolicyEditionUnavailable
from app.policy.editions import (
    DEFAULT_TRAVEL_PARAMETERS,
    EDITIONS,
    HISTORICAL_RULE_SETS,
    ORDINARY_RULE_SET_V1,
    SOURCE_MANIFEST_HASH,
    TOTAL_BASE_TRAVEL_PARAMETERS,
    TRAVEL_RULE_SET_V1,
    TRAVEL_RULE_SET_V2,
    PolicyEdition,
    TravelParameters,
    resolve_edition,
)
from app.policy.models import PolicyVersion, PolicyVersionStatus
from app.policy.service import TRAVEL_POLICY_KEY, travel_edition_for
from app.travel.engine import PassiveFact, TripFact, calculate_travel
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from .conftest import DEVELOPER_PRINCIPAL_HEADER

BERLIN_OFFSET = "+02:00"
#: A trip window whose 25 % credit is large enough that the two bases are
#: unmistakably different: 16 h eligible -> 60 min excess -> EXCESS grants 15 min,
#: TOTAL grants 240 min.
TRIP_START = "2026-05-01T06:00:00"
TRIP_END = "2026-05-02T22:00:00"
PASSIVE_START = "2026-05-01T08:00:00"
PASSIVE_END = "2026-05-02T00:00:00"  # 16 h of non-chargeable travel on 1 May


def _eligible_16h() -> tuple[TripFact, ...]:
    return (TripFact("trip", date(2026, 5, 1)),)


def _passive_16h() -> tuple[PassiveFact, ...]:
    start = datetime.fromisoformat(f"{PASSIVE_START}{BERLIN_OFFSET}")
    end = datetime.fromisoformat(f"{PASSIVE_END}{BERLIN_OFFSET}")
    return (PassiveFact("trip", start, end, "ACTUAL"),)


# ---------------------------------------------------------------------------
# The engine consumes the EDITION's parameters
# ---------------------------------------------------------------------------


def test_the_corrected_base_grants_25_percent_of_the_total() -> None:
    """``POL-08``'s own arithmetic: 16 h eligible -> 4 h granted, not 15 min.

    The shipped ``EXCESS`` formula grants 25 % of the amount ABOVE the 15 h
    threshold (60 min // 4 = 15 min), which is why the divergence is a *reduction*
    of the employee's credit and why ``INV-15`` treats it as a real defect rather
    than a rounding difference.
    """
    outcome = calculate_travel(
        trips=_eligible_16h(),
        passive_facts=_passive_16h(),
        work_facts=(),
        required_minutes={},
        parameters=TravelParameters(credit_base="TOTAL"),
        policy_edition=TRAVEL_RULE_SET_V2,
    )
    trip = outcome.trips[0]
    assert trip.eligible_minutes == 16 * 60
    assert trip.excess_minutes == 60
    assert trip.credit_minutes == 240
    assert outcome.credits[0].minutes == 240


def test_the_shipped_default_is_still_the_excess_base() -> None:
    """The default parameters ARE v1, so a caller that resolves no edition
    reproduces the shipped behaviour byte for byte — the positive control for the
    test above."""
    outcome = calculate_travel(
        trips=_eligible_16h(),
        passive_facts=_passive_16h(),
        work_facts=(),
        required_minutes={},
    )
    assert outcome.trips[0].excess_minutes == 60
    assert outcome.trips[0].credit_minutes == 15


def test_the_threshold_conditions_the_total_base_too() -> None:
    """A month at or below the threshold grants nothing under EITHER base.

    The 15 h threshold is a CONDITION for the grant, not only a base: a month at
    or below it grants nothing. Crediting 25 % of a 14 h total would invent a grant
    the sample policy does not make.
    """
    start = datetime.fromisoformat("2026-05-01T08:00:00+02:00")
    outcome = calculate_travel(
        trips=_eligible_16h(),
        passive_facts=(PassiveFact("trip", start, start.replace(hour=22), "ACTUAL"),),
        work_facts=(),
        required_minutes={},
        parameters=TravelParameters(credit_base="TOTAL"),
        policy_edition=TRAVEL_RULE_SET_V2,
    )
    assert outcome.trips[0].eligible_minutes == 14 * 60
    assert outcome.trips[0].credit_minutes == 0
    assert outcome.credits == ()


def test_the_edition_parameters_move_the_input_fingerprint() -> None:
    """A travel-rule change can no longer move no fingerprint at all (F4.4).

    The identity is deliberately held CONSTANT here: the guard is that the
    fingerprint records the PARAMETERS themselves, so a frozen snapshot is
    self-describing (the numbers that produced it) rather than merely naming a rule
    set. Asserting on the rule-set id would pass even if the numbers were dropped.
    """
    shipped = calculate_travel(
        trips=_eligible_16h(),
        passive_facts=_passive_16h(),
        work_facts=(),
        required_minutes={},
        parameters=DEFAULT_TRAVEL_PARAMETERS,
        policy_edition=TRAVEL_RULE_SET_V1,
    )
    same_identity_other_numbers = calculate_travel(
        trips=_eligible_16h(),
        passive_facts=_passive_16h(),
        work_facts=(),
        required_minutes={},
        parameters=TravelParameters(credit_base="TOTAL"),
        policy_edition=TRAVEL_RULE_SET_V1,
    )
    assert shipped.input_fingerprint != same_identity_other_numbers.input_fingerprint


def test_travel_parameters_are_immutable() -> None:
    """A shipped edition's parameters cannot be edited in place (EINV-09)."""
    with pytest.raises(FrozenInstanceError):
        DEFAULT_TRAVEL_PARAMETERS.credit_base = "TOTAL"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# The registry: designed but NEITHER registered NOR published
# ---------------------------------------------------------------------------


def test_the_corrected_edition_is_designed_but_neither_registered_nor_published() -> None:
    """``ARCH-POL-001``: the registry entry and its publishing migration land TOGETHER.

    So while the rollout boundary is undecided the corrected edition must be
    *designed* — named, with its parameter set existing as a value — and
    **unregistered and unapplied**, exactly like ``phase-2-ordinary-v2``. Registering
    it here would put a rule set in ``EDITIONS`` ahead of the migration that makes it
    resolvable, which is what the rule forbids.
    """
    assert TRAVEL_RULE_SET_V2 not in EDITIONS, "no registry entry ahead of its migration"
    assert TRAVEL_RULE_SET_V2 not in HISTORICAL_RULE_SETS, "it has never shipped"
    assert TRAVEL_RULE_SET_V1 in HISTORICAL_RULE_SETS
    # ...but the corrected PARAMETERS are a first-class, reviewable value.
    assert TravelParameters(credit_base="TOTAL") == TOTAL_BASE_TRAVEL_PARAMETERS
    assert TOTAL_BASE_TRAVEL_PARAMETERS != DEFAULT_TRAVEL_PARAMETERS


def test_the_registered_travel_edition_carries_travel_parameters() -> None:
    edition = resolve_edition(TRAVEL_RULE_SET_V1)
    assert isinstance(edition.parameters, TravelParameters)
    assert edition.parameters == DEFAULT_TRAVEL_PARAMETERS


# ---------------------------------------------------------------------------
# Month-based resolution — the reproducibility anchor
# ---------------------------------------------------------------------------


#: The corrected edition as it WOULD be registered when its boundary is decided.
#: Injected by the tests below (never shipped in ``EDITIONS``), mirroring
#: ``test_policy_editions_db.py``'s ``_V2_EDITION`` for the ordinary edition.
_V2_EDITION = PolicyEdition(
    rule_set=TRAVEL_RULE_SET_V2,
    version="2026-06-guidance-v2",
    algorithm="travel-v1",
    parameters=TOTAL_BASE_TRAVEL_PARAMETERS,
    note="test registration of the corrected travel base",
)


def _publish_v2(session: Session, effective_from: date) -> None:
    """Publish the corrected edition the way a migration MUST: as a HANDOVER.

    A new edition does not simply appear beside its predecessor. The predecessor's
    ``effective_to`` is closed the day before the boundary, so the range set stays
    **non-overlapping and unambiguous** (`ARCH-POL-001`). Overlapping ranges are a
    real defect — the restore verifier reports them as ``overlapping effective
    ranges`` — so a fixture that modelled the overlap would teach the wrong
    migration. The assertion below pins the handover using that same verifier.
    """
    predecessor = session.scalar(
        select(PolicyVersion).where(
            PolicyVersion.policy_key == TRAVEL_POLICY_KEY,
            PolicyVersion.effective_to.is_(None),
        )
    )
    assert predecessor is not None, "the shipped travel edition must be open-ended"
    predecessor.effective_to = effective_from - timedelta(days=1)
    session.add(
        PolicyVersion(
            policy_key=TRAVEL_POLICY_KEY,
            version="2026-06-guidance-v2",
            effective_from=effective_from,
            effective_to=None,
            status=PolicyVersionStatus.PROVISIONAL,
            engine_rule_set=TRAVEL_RULE_SET_V2,
            source_manifest_hash=SOURCE_MANIFEST_HASH,
        )
    )
    session.flush()
    failures = _domain_checks(session)
    assert not any("overlapping effective ranges" in item for item in failures["policy"]), failures[
        "policy"
    ]


def test_the_travel_edition_resolves_from_the_month_not_from_the_newest_row(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publishing a correction must not reinterpret an earlier month.

    This is the whole reason the base had to become an edition parameter: an
    ungated constant change would silently restate every already-computed month
    (``EINV-09``/``INV-11``).
    """
    before = travel_edition_for(db_session, date(2026, 8, 1))
    assert before == (TRAVEL_RULE_SET_V1, DEFAULT_TRAVEL_PARAMETERS)

    # DESIGNED-BUT-UNREGISTERED-AND-UNPUBLISHED is the shipped state: v2 has no
    # `EDITIONS` entry and no row, so an uncovered month refuses rather than guessing.
    with pytest.raises(PolicyEditionUnavailable):
        travel_edition_for(db_session, date(2020, 1, 1))

    # Publishing means the registry entry AND the row together (ARCH-POL-001).
    monkeypatch.setattr(
        "app.policy.editions.EDITIONS", {**EDITIONS, TRAVEL_RULE_SET_V2: _V2_EDITION}
    )
    _publish_v2(db_session, date(2026, 9, 1))
    assert travel_edition_for(db_session, date(2026, 8, 1)) == (
        TRAVEL_RULE_SET_V1,
        DEFAULT_TRAVEL_PARAMETERS,
    ), "an August month must still resolve v1 after a September correction is published"
    assert travel_edition_for(db_session, date(2026, 9, 1)) == (
        TRAVEL_RULE_SET_V2,
        TravelParameters(credit_base="TOTAL"),
    )
    assert travel_edition_for(db_session, date(2027, 1, 1)) == (
        TRAVEL_RULE_SET_V2,
        TravelParameters(credit_base="TOTAL"),
    )


def test_an_unregistered_travel_rule_set_fails_closed(db_session: Session) -> None:
    """A row asserting an edition with no executable entry is a refusal, not a
    silent fall back to today's numbers (EINV-08)."""
    db_session.add(
        PolicyVersion(
            policy_key=TRAVEL_POLICY_KEY,
            version="2026-06-guidance-v9",
            effective_from=date(2030, 1, 1),
            effective_to=None,
            status=PolicyVersionStatus.PROVISIONAL,
            engine_rule_set="phase-2-travel-v9",
            source_manifest_hash=SOURCE_MANIFEST_HASH,
        )
    )
    db_session.flush()
    with pytest.raises(PolicyEditionUnavailable):
        travel_edition_for(db_session, date(2030, 1, 1))


def test_a_travel_row_pointing_at_a_non_travel_edition_fails_closed(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parameter set is family-scoped: a row that resolves to an edition
    carrying ORDINARY parameters must refuse, not execute on today's defaults."""
    db_session.add(
        PolicyVersion(
            policy_key=TRAVEL_POLICY_KEY,
            version="2026-06-guidance-v3",
            effective_from=date(2031, 1, 1),
            effective_to=None,
            status=PolicyVersionStatus.PROVISIONAL,
            engine_rule_set="test-mis-registered-ordinary",
            source_manifest_hash=SOURCE_MANIFEST_HASH,
        )
    )
    db_session.flush()
    monkeypatch.setattr(
        "app.policy.editions.EDITIONS",
        {
            **EDITIONS,
            "test-mis-registered-ordinary": PolicyEdition(
                rule_set="test-mis-registered-ordinary",
                version="2026-06-guidance-v3",
                algorithm="ordinary-v1",
                parameters=resolve_edition(ORDINARY_RULE_SET_V1).parameters,
                note="test-only mis-registration",
            ),
        },
    )
    with pytest.raises(PolicyEditionUnavailable):
        travel_edition_for(db_session, date(2031, 1, 1))


# ---------------------------------------------------------------------------
# End to end: publication is the only missing piece
# ---------------------------------------------------------------------------


def _trip_with_16h_passive(
    client: TestClient, *, trip_start: str, trip_end: str, passive_start: str, passive_end: str
) -> dict[str, Any]:
    """A trip plus 16 h of non-chargeable travel inside its credit window.

    A credit lands in the month AFTER the trip's one-calendar-month window ends
    (``following_month_start(window_end)``), so the caller picks the trip start to
    choose which month the credit is read from.
    """
    trip = client.post(
        "/api/v1/me/travel-trips",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"start_local": trip_start, "end_local": trip_end, "start_origin": "HOME"},
    )
    assert trip.status_code == 201, trip.text
    segment = client.post(
        f"/api/v1/me/travel-trips/{trip.json()['id']}/segments",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": passive_start,
            "end_local": passive_end,
            "classification": "PASSIVE_TRAVEL",
        },
    )
    assert segment.status_code == 201, segment.text
    return trip.json()


def _credit_minutes(client: TestClient, period: str) -> int:
    summary = client.get(
        f"/api/v1/me/travel-summary?period={period}", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert summary.status_code == 200, summary.text
    credits = summary.json()["credits"]
    assert len(credits) == 1, credits
    return int(credits[0]["minutes"])


def test_a_published_travel_edition_changes_the_credit_and_leaves_earlier_months_alone(
    db_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end-to-end guard for F8-9.

    It proves three things at once: (1) the shipped behaviour is the EXCESS base;
    (2) once a corrected edition is PUBLISHED for a later month the credit becomes
    the TOTAL base through the ordinary product surface — so the publication really
    is the only missing piece; and (3) the month BEFORE the boundary is unchanged,
    which is the reproducibility property the edition mechanism exists for.

    Two trips are needed because a credit lands in the month AFTER the trip's
    window: the April trip credits June, the May trip credits July. Publishing the
    correction from July must therefore leave June's number alone.
    """
    _trip_with_16h_passive(
        db_client,
        trip_start="2026-04-01T06:00:00",
        trip_end="2026-05-02T22:00:00",
        passive_start="2026-04-01T08:00:00",
        passive_end="2026-04-02T00:00:00",
    )
    _trip_with_16h_passive(
        db_client,
        trip_start=TRIP_START,
        trip_end=TRIP_END,
        passive_start=PASSIVE_START,
        passive_end=PASSIVE_END,
    )
    assert _credit_minutes(db_client, "2026-06") == 15, "the shipped base is 25 % of the EXCESS"
    assert _credit_minutes(db_client, "2026-07") == 15

    # Designed-but-unregistered is the real state; the registry entry is what the test
    # adds, and it is added only here, never in the shipped registry.
    monkeypatch.setattr(
        "app.policy.editions.EDITIONS", {**EDITIONS, TRAVEL_RULE_SET_V2: _V2_EDITION}
    )
    _publish_v2(db_session, date(2026, 7, 1))

    assert _credit_minutes(db_client, "2026-06") == 15, (
        "a June credit computed under v1 must not be restated by a July correction"
    )
    assert _credit_minutes(db_client, "2026-07") == 240, "25 % of the TOTAL (16 h) is 4 h"
