"""Pure TRAVEL-001..004 calculation engine (POL-08).

No ORM, HTTP, or clock imports are allowed here.  Total work, including
``BUSINESS_TRIP_WORK``, remains subject to WT-CORE-006 in the ordinary month
engine; this module intentionally does not duplicate that daily-cap rule.

``TRAVEL-004``'s constants are an EDITION PARAMETER, not module literals (F8-9):
the caller passes the resolved edition's ``TravelParameters`` plus the rule-set
identifier that produced them, and the identifier is recorded in the input
fingerprint.  Correcting the credit base is therefore a new edition, never an
edit to a shipped one, and a month computed before the correction stays
reproducible (``EINV-09``/``INV-11``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.policy.editions import (
    DEFAULT_TRAVEL_PARAMETERS,
    TRAVEL_RULE_SET_V1,
    TravelParameters,
)
from app.policy.engine import Finding, FindingSeverity

BERLIN = ZoneInfo("Europe/Berlin")


@dataclass(frozen=True)
class TripFact:
    id: str
    start_date: date


@dataclass(frozen=True)
class PassiveFact:
    trip_id: str
    start_at: datetime
    end_at: datetime
    basis: str


@dataclass(frozen=True)
class WorkFact:
    start_at: datetime
    end_at: datetime
    classification: str = "ACTIVE_WORK"
    # Travel findings (Sunday legality etc.) apply ONLY to work performed
    # during business travel.  Ordinary intervals flow through the same work
    # union for overlap/top-up arithmetic but are not travel findings.
    # Deliberately NOT part of the input fingerprint: adding a flag field
    # would churn fingerprints for pre-travel employees for no semantic gain.
    is_travel: bool = True


@dataclass(frozen=True)
class TravelDailyTopup:
    day: date
    minutes: int


@dataclass(frozen=True)
class TravelCreditProposal:
    occurred_on: date
    minutes: int
    rule_id: str
    source_entity_type: str
    source_entity_id: str
    window_start: date
    window_end: date
    eligible_minutes: int
    threshold_minutes: int
    excess_minutes: int


@dataclass(frozen=True)
class TravelTripResult:
    trip_id: str
    window_start: date
    window_end: date
    eligible_minutes: int
    threshold_minutes: int
    excess_minutes: int
    credit_minutes: int
    credit_effective_on: date


@dataclass(frozen=True)
class TravelResult:
    topups: tuple[TravelDailyTopup, ...]
    credits: tuple[TravelCreditProposal, ...]
    trips: tuple[TravelTripResult, ...]
    findings: tuple[Finding, ...]
    input_fingerprint: str


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("travel engine facts must be aware instants")
    return value.astimezone(UTC)


def _elapsed_minutes(start: datetime, end: datetime) -> int:
    return int((_utc(end) - _utc(start)).total_seconds() // 60)


def _allocated_minutes(span_start: datetime, portion_start: datetime, portion_end: datetime) -> int:
    """Whole elapsed minutes of a span attributed to one of its day portions.

    Same rule as the ordinary-time engine: the span's elapsed time is
    partitioned into whole-minute slots anchored at ``span_start``, and a slot
    belongs to the portion containing its COMPLETION instant. Summing over a
    tiling of the span yields exactly ``floor(elapsed/60)`` completed minutes,
    so a midnight-crossing passive span or travel-work span can neither lose
    nor invent time (EINV-17) — and for minute-aligned facts the result is
    identical to flooring each portion independently.
    """
    base = _utc(span_start)
    return (
        int((_utc(portion_end) - base).total_seconds()) // 60
        - int((_utc(portion_start) - base).total_seconds()) // 60
    )


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=BERLIN)
    return start, start + timedelta(days=1)


def _merge(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    result: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals, key=lambda item: (_utc(item[0]), _utc(item[1]))):
        if end <= start:
            raise ValueError("travel fact end_at must be after start_at")
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def _subtract(
    source: list[tuple[datetime, datetime]], removed: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    result: list[tuple[datetime, datetime]] = []
    for start, end in _merge(source):
        parts = [(start, end)]
        for cut_start, cut_end in _merge(removed):
            next_parts: list[tuple[datetime, datetime]] = []
            for part_start, part_end in parts:
                if cut_end <= part_start or cut_start >= part_end:
                    next_parts.append((part_start, part_end))
                else:
                    if part_start < cut_start:
                        next_parts.append((part_start, cut_start))
                    if cut_end < part_end:
                        next_parts.append((cut_end, part_end))
            parts = next_parts
        result.extend(parts)
    return _merge(result)


def _split_days(intervals: list[tuple[datetime, datetime]]) -> dict[date, int]:
    result: dict[date, int] = {}
    for start, end in intervals:
        current = start.astimezone(BERLIN).date()
        final = (end - timedelta(microseconds=1)).astimezone(BERLIN).date()
        while current <= final:
            day_start, day_end = _day_bounds(current)
            portion_start, portion_end = max(start, day_start), min(end, day_end)
            if portion_end > portion_start:
                result[current] = result.get(current, 0) + _allocated_minutes(
                    start, portion_start, portion_end
                )
            current += timedelta(days=1)
    return result


def add_one_calendar_month_clamped(value: date) -> date:
    year = value.year + (value.month == 12)
    month = 1 if value.month == 12 else value.month + 1
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return date(year, month, min(value.day, (next_month - timedelta(days=1)).day))


def following_month_start(value: date) -> date:
    """First day of the month after ``value`` (the month a credit lands in)."""
    return date(value.year + 1, 1, 1) if value.month == 12 else date(value.year, value.month + 1, 1)


def _fingerprint(
    trips: tuple[TripFact, ...],
    passive: tuple[PassiveFact, ...],
    work: tuple[WorkFact, ...],
    required: Mapping[date, int],
    authorizations: tuple[tuple[datetime, datetime], ...],
    calendar_classes: Mapping[date, str],
    parameters: TravelParameters,
    policy_edition: str,
) -> str:
    value = {
        "policy_edition": policy_edition,
        # F8-9 — the EDITION PARAMETERS, not just the identity. A travel-rule
        # change can no longer move no fingerprint at all (F4.4), and two rule sets
        # that happen to differ only in a number are still distinguishable.
        "travel_parameters": {
            "passive_threshold_minutes": parameters.passive_threshold_minutes,
            "credit_denominator": parameters.credit_denominator,
            "credit_base": parameters.credit_base,
        },
        "trips": [(x.id, x.start_date.isoformat()) for x in sorted(trips, key=lambda x: x.id)],
        "passive": [
            (x.trip_id, _utc(x.start_at).isoformat(), _utc(x.end_at).isoformat(), x.basis)
            for x in sorted(
                passive, key=lambda x: (x.trip_id, _utc(x.start_at), _utc(x.end_at), x.basis)
            )
        ],
        "work": [
            (_utc(x.start_at).isoformat(), _utc(x.end_at).isoformat(), x.classification)
            for x in sorted(
                work, key=lambda x: (_utc(x.start_at), _utc(x.end_at), x.classification)
            )
        ],
        "required": sorted((x.isoformat(), y) for x, y in required.items()),
        "authorizations": sorted(
            (_utc(a).isoformat(), _utc(b).isoformat()) for a, b in authorizations
        ),
        "calendar_classes": sorted((x.isoformat(), str(y)) for x, y in calendar_classes.items()),
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def calculate_travel(
    *,
    trips: tuple[TripFact, ...],
    passive_facts: tuple[PassiveFact, ...],
    work_facts: tuple[WorkFact, ...],
    required_minutes: Mapping[date, int],
    authorization_windows: tuple[tuple[datetime, datetime], ...] = (),
    calendar_classes: Mapping[date, str] = {},
    parameters: TravelParameters = DEFAULT_TRAVEL_PARAMETERS,
    policy_edition: str = TRAVEL_RULE_SET_V1,
) -> TravelResult:
    """Calculate private travel top-ups, credit proposals, and warnings.

    ``parameters`` is the RESOLVED edition's frozen rule input and
    ``policy_edition`` is the rule set that supplied it; both are recorded in the
    input fingerprint. The defaults are v1's own constants, so a caller that does
    not resolve an edition reproduces the shipped behaviour exactly.
    """
    trip_ids = {trip.id for trip in trips}
    if any(item.trip_id not in trip_ids for item in passive_facts):
        raise ValueError("passive fact references an unknown trip")
    work_union = _merge([(item.start_at, item.end_at) for item in work_facts])
    # Passive overlap is prohibited by the service.  Deduping here makes the
    # pure function robust to stale/imported facts and prevents double credit.
    passive_union = _subtract([(item.start_at, item.end_at) for item in passive_facts], work_union)
    passive_by_day = _split_days(passive_union)
    worked_by_day = _split_days(work_union)
    topups = tuple(
        TravelDailyTopup(
            day, min(passive_by_day.get(day, 0), max(0, target - worked_by_day.get(day, 0)))
        )
        for day, target in sorted(required_minutes.items())
        if day.weekday() < 6
        and target > 0
        and min(passive_by_day.get(day, 0), max(0, target - worked_by_day.get(day, 0))) > 0
    )

    # Attribute deduped passive instants to one trip deterministically.  This
    # has no normal-user effect (the service rejects cross-trip overlap) but
    # prevents bad historical facts from multiplying TRAVEL-004 credit.
    attributed: dict[str, list[tuple[datetime, datetime]]] = {trip.id: [] for trip in trips}
    for interval in passive_union:
        owners = sorted(
            item.trip_id
            for item in passive_facts
            if item.start_at < interval[1] and item.end_at > interval[0]
        )
        if owners:
            attributed[owners[0]].append(interval)
    credits: list[TravelCreditProposal] = []
    trip_results: list[TravelTripResult] = []
    for trip in sorted(trips, key=lambda item: item.id):
        window_end = add_one_calendar_month_clamped(trip.start_date)
        eligible = sum(
            minutes
            for day, minutes in _split_days(attributed[trip.id]).items()
            if trip.start_date <= day <= window_end
        )
        excess = max(0, eligible - parameters.passive_threshold_minutes)
        # ``TRAVEL-004``'s base (F8-9). ``EXCESS`` — the shipped v1 — credits 25 %
        # of the amount ABOVE the threshold. ``TOTAL`` — what POL-08 describes —
        # credits 25 % of the whole non-chargeable total. The threshold is a
        # CONDITION in BOTH forms (a month at or below it grants nothing), which is
        # why ``excess`` gates the TOTAL form too and why ``excess`` is still
        # reported: it is what makes the proposal's own derivation readable.
        base = eligible if (excess and parameters.credit_base == "TOTAL") else excess
        credit = base // parameters.credit_denominator
        effective = following_month_start(window_end)
        trip_results.append(
            TravelTripResult(
                trip.id,
                trip.start_date,
                window_end,
                eligible,
                parameters.passive_threshold_minutes,
                excess,
                credit,
                effective,
            )
        )
        if credit:
            credits.append(
                TravelCreditProposal(
                    effective,
                    credit,
                    "TRAVEL-004",
                    "business_trip",
                    trip.id,
                    trip.start_date,
                    window_end,
                    eligible,
                    parameters.passive_threshold_minutes,
                    excess,
                )
            )

    findings: list[Finding] = []
    for work in sorted(
        work_facts, key=lambda item: (_utc(item.start_at), _utc(item.end_at), item.classification)
    ):
        if not work.is_travel:
            continue
        day_parts = _split_days([(work.start_at, work.end_at)])
        for day in sorted(day_parts):
            holiday = str(calendar_classes.get(day, "")) in {"PUBLIC_HOLIDAY", "EMPLOYER_FREE"}
            if day.weekday() != 6 and not holiday:
                continue
            day_start, day_end = _day_bounds(day)
            start, end = max(work.start_at, day_start), min(work.end_at, day_end)
            covered = any(
                start >= auth_start and end <= auth_end
                for auth_start, auth_end in authorization_windows
            )
            if day.weekday() == 6 and not covered:
                findings.append(
                    Finding(
                        "TRAVEL-SUNDAY-001",
                        FindingSeverity.WARNING,
                        day,
                        "Sunday travel work requires a prior overtime application.",
                    )
                )
            if day.weekday() == 6 and work.classification == "DRIVING":
                findings.append(
                    Finding(
                        "TRAVEL-SUNDAY-002",
                        FindingSeverity.WARNING,
                        day,
                        "Sunday driving is working time and is generally prohibited; avoid it.",
                    )
                )
            findings.append(
                Finding(
                    "TRAVEL-LOCAL-001",
                    FindingSeverity.WARNING,
                    day,
                    "Host-country Sunday/public-holiday legality is the employee's responsibility; "
                    "the service does not assess it.",
                )
            )
    findings.sort(key=lambda item: (item.date or date.min, item.rule_id, item.message))
    return TravelResult(
        topups,
        tuple(credits),
        tuple(trip_results),
        tuple(findings),
        _fingerprint(
            trips,
            passive_facts,
            work_facts,
            required_minutes,
            authorization_windows,
            calendar_classes,
            parameters,
            policy_edition,
        ),
    )
