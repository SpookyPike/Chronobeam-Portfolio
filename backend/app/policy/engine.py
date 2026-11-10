"""Pure ordinary working-time calculation core.

The module deliberately accepts only frozen typed inputs.  It never imports an
ORM model, session, HTTP type, or clock; this keeps BE-POL-001/002 testable and
makes snapshots reproducible.  Shift, absence, OCD, travel and allowance
semantics live in their own modules.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from itertools import pairwise
from zoneinfo import ZoneInfo

from app.policy.editions import DEFAULT_ORDINARY_PARAMETERS, OrdinaryTimeParameters

BERLIN = ZoneInfo("Europe/Berlin")


class CalendarClass(StrEnum):
    WEEKDAY = "WEEKDAY"
    PUBLIC_HOLIDAY = "PUBLIC_HOLIDAY"
    EMPLOYER_FREE = "EMPLOYER_FREE"


class FindingSeverity(StrEnum):
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    BLOCKED_POLICY = "BLOCKED_POLICY"


#: The only source-backed REDUCING day-status categories (WT-FLEX-001 promises
#: "required minutes are ... modified by valid day-status events"; VAC-002,
#: ABSENCE-002 and ABSENCE-008 supply the per-category effect). FLEX_COMPENSATION and
#: SHIFT_COMPENSATION leave the day's requirement untouched: the existing
#: ``delta = account_credit - required`` arithmetic already consumes the balance
#: when the employee does not work.
REDUCING_CATEGORIES = frozenset({"VACATION", "SICK"})
#: A day has two supported halves; coverage beyond that is refused at the write
#: path and clamped here as a backstop for restored or imported data.
DAY_HALVES = 2
PORTION_HALVES: Mapping[str, int] = {"FULL": 2, "HALF": 1}


@dataclass(frozen=True)
class IntervalFact:
    id: str
    start_at: datetime
    end_at: datetime
    work_type: str


@dataclass(frozen=True)
class LedgerFact:
    occurred_on: date
    minutes: int
    entry_type: str


@dataclass(frozen=True)
class TravelTopupFact:
    """TRAVEL-003: pure-engine-applied daily travel top-up input.

    Produced by ``app.travel.engine.calculate_travel`` from employee-declared
    passive-travel facts; never actual worked time and never a fabricated
    ``WorkInterval`` (INV-02/TRAVEL-001). The month engine re-clamps the value
    against the day's deficit, so an inconsistent input cannot manufacture
    positive flex.
    """

    day: date
    minutes: int


@dataclass(frozen=True)
class DayStatusFact:
    """One canonical day-status fact as the engine consumes it (F5).

    Only the category and the portion matter to the arithmetic: the engine has
    no half-day *sickness* branch, no diagnosis, no source and no comment — the
    fact carries none of them in the first place.
    """

    day: date
    category: str
    portion: str


@dataclass(frozen=True)
class BreakFact:
    """An explicit factual break (WT-BACKEND §5.3 ``break_interval``).

    Distinct from a gap between intervals: a gap is *inferred* non-work, while
    an explicit break is a recorded fact that may sit INSIDE one interval span.
    ``kind`` decides whether it reduces worked time — see
    ``NON_DEDUCTING_BREAK_KINDS``.
    """

    id: str
    start_at: datetime
    end_at: datetime
    kind: str


#: Break kinds that do NOT reduce worked time. ``PAID_SHIFT_BREAK`` is the
#: compensated statutory shift break (SHIFT-003): remunerated presence, not a
#: gap. ``OTHER`` is deliberately here too — the supplied sources establish no
#: unpaid semantics for it, and deducting it would make the result stricter than
#: the regime (INV-15). Only ``UNPAID`` deducts.
NON_DEDUCTING_BREAK_KINDS = frozenset({"PAID_SHIFT_BREAK", "OTHER"})


@dataclass(frozen=True)
class PeriodInputs:
    period_start: date
    period_end: date  # exclusive
    daily_required_minutes: Mapping[date, int]
    weekly_required_minutes: int
    calendar_classes: Mapping[date, CalendarClass]
    intervals: tuple[IntervalFact, ...]
    opening_ledger_entries: tuple[LedgerFact, ...]
    policy_version: str
    engine_build: str
    calendar_version_ids: tuple[str, ...] = ()
    # Travel integration (pure derived inputs; empty = pre-travel
    # behavior bit-for-bit, keeping historical recomputation stable).
    travel_topups: tuple[TravelTopupFact, ...] = ()
    travel_credits: tuple[LedgerProposal, ...] = ()
    travel_findings: tuple[Finding, ...] = ()
    travel_input_fingerprint: str = ""
    # The resolved policy edition's frozen parameters (F4, ARCH-POL-001). The
    # pure engine reads them and NEVER performs a registry lookup, so it keeps
    # no hidden global dependency; the adapter resolves the edition by effective
    # date and passes the parameter set in. Defaulted to v1 so a caller that
    # supplies no edition (tests, offline tooling) keeps today's behaviour.
    parameters: OrdinaryTimeParameters = DEFAULT_ORDINARY_PARAMETERS
    # F5 day-status inputs (canonical employee-owned facts). Empty = pre-F5
    # behavior bit-for-bit: the fingerprint gains a ``day_status`` key ONLY when
    # this is non-empty, so every pre-existing fingerprint and snapshot stays
    # byte-stable (INV-11), exactly like the travel precedent below.
    day_status: tuple[DayStatusFact, ...] = ()
    # Explicit breaks (WT-BACKEND §5.3). Empty = pre-break behaviour bit-for-bit:
    # the fingerprint gains a ``breaks`` key ONLY when this is non-empty, so every
    # pre-existing fingerprint and snapshot stays byte-stable (INV-11) — the same
    # precedent as travel and day-status above.
    breaks: tuple[BreakFact, ...] = ()
    # The business date the as-of trigger is evaluated against (WT-FLEX-005).
    # A CLOCK READING, not a policy fact: it changes only WHICH projection is
    # reported, never the arithmetic, so it is deliberately absent from
    # ``_fingerprint`` — a fingerprint that moved at midnight could not verify a
    # stored snapshot (INV-11). ``None`` = the period-end reading, i.e. exactly
    # the pre-as-of behaviour, which is what every direct engine caller and every
    # pure engine test keeps.
    as_of: date | None = None


@dataclass(frozen=True)
class Finding:
    rule_id: str
    severity: FindingSeverity
    date: date | None
    message: str


@dataclass(frozen=True)
class DailyResult:
    day: date
    calendar_class: CalendarClass
    worked_minutes: int
    required_minutes: int
    flex_delta_minutes: int
    qualifying_break_minutes: int
    # TRAVEL-003: minutes of employee-declared PASSIVE travel credited
    # to close the day's deficit. Derived, never worked time, never editable
    # truth (INVARIANTS.md: fact and derived result are separate).
    travel_topup_minutes: int = 0
    # F4.5 layer separation: minutes that CREDIT the working-time account.
    # ``worked_minutes`` is the factual/legal layer (actual work, all intervals,
    # excluding breaks — the statutory definition of working time); this is the
    # account layer,
    # whose rule may later differ from the factual one (WT-FLEX-009, F6). Under
    # v1 every work type credits, so the two are equal and every number is
    # unchanged.
    account_credit_minutes: int = 0


@dataclass(frozen=True)
class LedgerProposal:
    occurred_on: date
    minutes: int
    entry_type: str
    # TRAVEL-004 provenance (audit/reproduction): only travel-derived
    # credit proposals populate these; ordinary proposals stay None so all
    # pre-travel payloads and fingerprints are unchanged.
    rule_id: str | None = None
    source_entity_type: str | None = None
    source_entity_id: str | None = None
    calculation: Mapping[str, object] | None = None


@dataclass(frozen=True)
class PeriodResult:
    daily_results: tuple[DailyResult, ...]
    ledger_proposals: tuple[LedgerProposal, ...]
    findings: tuple[Finding, ...]
    opening_balance_minutes: int
    closing_balance_minutes: int
    input_fingerprint: str
    calendar_version_ids: tuple[str, ...]

    def snapshot_json(self) -> dict[str, object]:
        """A structured serializable result suitable for snapshot storage."""
        return {
            "daily_results": [
                {
                    **asdict(item),
                    "day": item.day.isoformat(),
                    "calendar_class": item.calendar_class.value,
                }
                for item in self.daily_results
            ],
            "ledger_proposals": [
                # Disclosure minimization: TRAVEL_CREDIT
                # freezes the rule identity but never trip ids or raw travel
                # facts — itinerary detail stays employee-private.
                {
                    **{
                        key: value
                        for key, value in asdict(item).items()
                        if not (
                            item.entry_type == "TRAVEL_CREDIT"
                            and key in {"source_entity_type", "source_entity_id", "calculation"}
                        )
                    },
                    "occurred_on": item.occurred_on.isoformat(),
                }
                for item in self.ledger_proposals
            ],
            "findings": [
                {
                    **asdict(item),
                    "date": item.date.isoformat() if item.date else None,
                    "severity": item.severity.value,
                }
                for item in self.findings
            ],
            "opening_balance_minutes": self.opening_balance_minutes,
            "closing_balance_minutes": self.closing_balance_minutes,
            "input_fingerprint": self.input_fingerprint,
            "calendar_version_ids": list(self.calendar_version_ids),
        }


def _elapsed_minutes(start_at: datetime, end_at: datetime) -> int:
    return int((end_at.astimezone(UTC) - start_at.astimezone(UTC)).total_seconds() // 60)


def _allocated_minutes(
    span_start: datetime, span_end: datetime, portion_start: datetime, portion_end: datetime
) -> int:
    """Whole elapsed minutes of a span ATTRIBUTED to one of its day portions.

    The span's elapsed time is partitioned into whole-minute slots anchored at
    ``span_start``: slot *k* is ``[span_start + 60k, span_start + 60(k+1))``.
    A slot belongs to the day portion containing its COMPLETION instant, which
    is what makes the per-day counts telescope: summing over a tiling of the
    span yields exactly ``floor(elapsed/60)`` completed minutes, so a
    midnight-crossing span can neither lose nor invent time (EINV-17). A
    trailing partial minute is not a completed minute and is not allocated.

    For minute-aligned spans (the command contract) this is numerically
    identical to flooring each portion independently, so every pre-existing
    result is unchanged; it only differs for stored sub-minute instants, which
    arrive by restore/import/direct write rather than through the API.
    """
    base = span_start.astimezone(UTC)
    return (
        int((portion_end.astimezone(UTC) - base).total_seconds()) // 60
        - int((portion_start.astimezone(UTC) - base).total_seconds()) // 60
    )


def _overlap_minutes(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> int:
    """Whole elapsed minutes shared by two instant spans (0 when disjoint).

    Unlike ``_allocated_minutes`` this makes no containment assumption: a break
    may start before, inside, or extend past a work span, so the intersection is
    computed directly. Minute-aligned inputs (the command contract) make the
    floor exact.
    """
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    if end <= start:
        return 0
    return int((end.astimezone(UTC) - start.astimezone(UTC)).total_seconds()) // 60


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=BERLIN)
    return start, start + timedelta(days=1)


def _days(start: date, end: date) -> list[date]:
    result: list[date] = []
    current = start
    while current < end:
        result.append(current)
        current += timedelta(days=1)
    return result


def _portion_spans_for_day(
    intervals: tuple[IntervalFact, ...], day: date
) -> list[tuple[IntervalFact, datetime, datetime]]:
    """Each interval's clipped portion of ``day``, paired with its owning span.

    The owning span is carried so the day's worked minutes can be attributed
    against the interval's own start instant rather than against the portion
    (see ``_allocated_minutes``).
    """
    start, end = _day_bounds(day)
    return sorted(
        (
            (interval, max(interval.start_at, start), min(interval.end_at, end))
            for interval in intervals
            if interval.start_at < end and interval.end_at > start
        ),
        key=lambda item: item[1],
    )


def _breaks_overlapping(
    spans: list[tuple[datetime, datetime]], breaks: tuple[BreakFact, ...]
) -> list[tuple[datetime, datetime]]:
    """The explicit breaks that fall inside these work spans, in time order.

    A break that lies wholly in a gap is deliberately NOT returned: that time is
    already outside ``spans``, and counting it again would double the
    interruption.
    """
    return sorted(
        (fact.start_at, fact.end_at)
        for fact in breaks
        if any(_overlap_minutes(fact.start_at, fact.end_at, start, end) > 0 for start, end in spans)
    )


def _remove_breaks(
    spans: list[tuple[datetime, datetime]], breaks: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    """Working time with every explicit break cut out of it.

    An explicit break is the same factual event as an inferred gap, only entered
    rather than implied, so it must interrupt work exactly as a gap does. This is
    the case ``break_interval`` exists for (WT-BACKEND §5.3): a break INSIDE one
    interval, which ``_working_groups`` cannot see because it has no gap to
    observe. With no breaks this returns ``spans`` unchanged, so every
    pre-existing number and fingerprint stays identical (INV-11).
    """
    result: list[tuple[datetime, datetime]] = []
    for start, end in spans:
        cursor = start
        for break_start, break_end in breaks:
            if break_end <= cursor or break_start >= end:
                continue
            if break_start > cursor:
                result.append((cursor, min(break_start, end)))
            cursor = max(cursor, break_end)
        if cursor < end:
            result.append((cursor, end))
    return result


def _qualifying_break_minutes(
    spans: list[tuple[datetime, datetime]],
    parameters: OrdinaryTimeParameters,
    breaks: list[tuple[datetime, datetime]] | None = None,
) -> int:
    """Qualifying break time: the gaps between work spans, plus explicit breaks.

    The two terms are disjoint — a gap is outside the spans and an explicit break
    is measured by its overlap with them — so a break sitting in a gap is counted
    once, by the gap. A Ruhepause is not working time whatever it pays (ArbZG §2,
    and SHIFT-003 makes a paid shift break remunerated *presence*), so a recorded
    break qualifies here regardless of ``kind``.
    """
    qualifying = 0
    for previous, current in pairwise(spans):
        gap = _elapsed_minutes(previous[1], current[0])
        if gap >= parameters.qualifying_break_minutes:
            qualifying += gap
    for break_start, break_end in breaks or ():
        overlap = sum(_overlap_minutes(break_start, break_end, start, end) for start, end in spans)
        if overlap >= parameters.qualifying_break_minutes:
            qualifying += overlap
    return qualifying


def _uninterrupted(
    spans: list[tuple[datetime, datetime]], parameters: OrdinaryTimeParameters
) -> bool:
    """Whether any continuous working run exceeded the 6-hour threshold.

    Call this with the break-free spans, so a recorded break resets the run
    exactly like a qualifying gap does.
    """
    if not spans:
        return False
    block_start = spans[0][0]
    for previous, current in pairwise(spans):
        # Test the run that ENDS at ``previous`` before a qualifying gap resets the
        # block. A break that arrives after a long run must not erase the violation:
        # 08:00-15:00 followed by 15:30-16:30 is a seven-hour uninterrupted run and a
        # finding, even though the total break time also passes the aggregate check.
        if _elapsed_minutes(block_start, previous[1]) > parameters.six_hour_threshold_minutes:
            return True
        if _elapsed_minutes(previous[1], current[0]) >= parameters.qualifying_break_minutes:
            block_start = current[0]
    return _elapsed_minutes(block_start, spans[-1][1]) > parameters.six_hour_threshold_minutes


def _working_groups(
    intervals: tuple[IntervalFact, ...],
) -> list[tuple[date, list[tuple[datetime, datetime]]]]:
    """Return canonical work groups without introducing a midnight boundary.

    Adjacent intervals are one factual working period, even when midnight lies
    between their endpoints. Non-adjacent sessions entered on the same local date
    form one ordinary working day, because rest is measured between working
    periods and an intra-day interruption is not daily rest (LAW-01 §5,
    WT-CORE-007). The group date is the local date on which that work day began.
    """
    merged: list[tuple[datetime, datetime]] = []
    for interval in sorted(intervals, key=lambda item: (item.start_at, item.end_at, item.id)):
        if merged and interval.start_at <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], interval.end_at))
        else:
            merged.append((interval.start_at, interval.end_at))

    groups: list[tuple[date, list[tuple[datetime, datetime]]]] = []
    for block in merged:
        local_day = block[0].astimezone(BERLIN).date()
        if groups and groups[-1][0] == local_day:
            groups[-1][1].append(block)
        else:
            groups.append((local_day, [block]))
    return groups


def _group_minutes(blocks: list[tuple[datetime, datetime]]) -> int:
    return sum(_elapsed_minutes(start, end) for start, end in blocks)


def _group_date(group_start: date, period_start: date) -> date:
    """Attribute a contextual cross-month finding to its visible period."""
    return max(group_start, period_start)


def _fingerprint(inputs: PeriodInputs) -> str:
    payload = {
        "period_start": inputs.period_start.isoformat(),
        "period_end": inputs.period_end.isoformat(),
        "daily_required_minutes": sorted(
            (day.isoformat(), value) for day, value in inputs.daily_required_minutes.items()
        ),
        "weekly_required_minutes": inputs.weekly_required_minutes,
        "calendar_classes": sorted(
            (day.isoformat(), value.value) for day, value in inputs.calendar_classes.items()
        ),
        "calendar_version_ids": list(inputs.calendar_version_ids),
        "intervals": [
            (
                item.id,
                item.start_at.astimezone(UTC).isoformat(),
                item.end_at.astimezone(UTC).isoformat(),
                item.work_type,
            )
            for item in sorted(
                inputs.intervals, key=lambda interval: (interval.start_at, interval.id)
            )
        ],
        "opening_ledger_entries": [
            (item.occurred_on.isoformat(), item.minutes, item.entry_type)
            for item in sorted(
                inputs.opening_ledger_entries,
                key=lambda entry: (entry.occurred_on, entry.entry_type, entry.minutes),
            )
        ],
        "policy_version": inputs.policy_version,
        "engine_build": inputs.engine_build,
    }
    # Day-status inputs extend the fingerprint only when present, so a month
    # with no day-status fact keeps a byte-identical fingerprint (INV-11) —
    # the exact travel precedent below.
    if inputs.day_status:
        payload["day_status"] = sorted(
            (fact.day.isoformat(), fact.category, fact.portion) for fact in inputs.day_status
        )
    # Explicit breaks extend the fingerprint only when present, so a month with
    # no break fact keeps a byte-identical fingerprint (INV-11).
    if inputs.breaks:
        payload["breaks"] = sorted(
            (
                fact.id,
                fact.start_at.astimezone(UTC).isoformat(),
                fact.end_at.astimezone(UTC).isoformat(),
                fact.kind,
            )
            for fact in inputs.breaks
        )
    # Travel inputs extend the fingerprint only when present, so
    # pre-travel fingerprints/snapshots remain byte-stable (INV-11).
    if inputs.travel_topups or inputs.travel_credits or inputs.travel_input_fingerprint:
        payload["travel_topups"] = sorted(
            (item.day.isoformat(), item.minutes) for item in inputs.travel_topups
        )
        payload["travel_credits"] = [
            (
                credit.occurred_on.isoformat(),
                credit.minutes,
                credit.source_entity_id,
                json.dumps(credit.calculation, sort_keys=True) if credit.calculation else None,
            )
            for credit in sorted(
                inputs.travel_credits, key=lambda c: (c.occurred_on, str(c.source_entity_id))
            )
        ]
        payload["travel_input_fingerprint"] = inputs.travel_input_fingerprint
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def calculate_period(inputs: PeriodInputs) -> PeriodResult:
    """Return deterministic ordinary-time calculations for one half-open period.

    A public holiday/employer-free day has no ordinary required-time target;
    the calendar fact does not alter any recorded work interval.  All warnings
    remain in this derived result and have no disclosure side effect.
    """
    if inputs.period_end <= inputs.period_start:
        raise ValueError("period_end must be after period_start")
    if any(interval.end_at <= interval.start_at for interval in inputs.intervals):
        raise ValueError("interval end_at must be after start_at")

    findings: list[Finding] = []
    daily_results: list[DailyResult] = []
    proposals: list[LedgerProposal] = []
    # The resolved edition's frozen parameters (F4). Read-only and immutable;
    # this is the ONLY place policy numbers enter the calculation.
    parameters = inputs.parameters
    travel_topups = {item.day: max(0, item.minutes) for item in inputs.travel_topups}
    day_status: dict[date, list[DayStatusFact]] = {}
    for fact in inputs.day_status:
        day_status.setdefault(fact.day, []).append(fact)
    period_start_at, period_end_at = (
        _day_bounds(inputs.period_start)[0],
        _day_bounds(inputs.period_end)[0],
    )

    # Break, uninterrupted-work, daily-maximum, and rest rules are evaluated
    # from canonical work boundaries, not their local-date display portions.
    # Day portions below remain solely for daily allocation and flex deltas.
    # Explicit breaks (WT-BACKEND §5.3) are cut out FIRST, so a recorded break
    # interrupts these rules exactly as an inferred gap does. A Ruhepause is not
    # working time whatever it pays (ArbZG §2; SHIFT-003 makes a paid shift break
    # remunerated *presence*), so the statutory thresholds measure break-free
    # working time even though the ledger still credits a paid break.
    groups = _working_groups(inputs.intervals)
    for group_day, blocks in groups:
        group_start, group_end = blocks[0][0], blocks[-1][1]
        if group_end <= period_start_at or group_start >= period_end_at:
            continue
        group_breaks = _breaks_overlapping(blocks, inputs.breaks)
        work_segments = _remove_breaks(blocks, group_breaks)
        worked = _group_minutes(work_segments)
        qualifying_break = _qualifying_break_minutes(blocks, parameters, group_breaks)
        uninterrupted = _uninterrupted(work_segments, parameters)
        finding_day = _group_date(group_day, inputs.period_start)
        if worked > parameters.nine_hour_threshold_minutes and qualifying_break < 45:
            findings.append(
                Finding(
                    "WT-CORE-004",
                    FindingSeverity.CRITICAL,
                    finding_day,
                    "More than 9 hours requires 45 minutes of qualifying break.",
                )
            )
        elif worked > parameters.six_hour_threshold_minutes and qualifying_break < 30:
            findings.append(
                Finding(
                    "WT-CORE-003",
                    FindingSeverity.CRITICAL,
                    finding_day,
                    "More than 6 hours requires 30 minutes of qualifying break.",
                )
            )
        if uninterrupted:
            findings.append(
                Finding(
                    "WT-CORE-005",
                    FindingSeverity.CRITICAL,
                    finding_day,
                    "Work continued for more than 6 hours without a qualifying break.",
                )
            )
        if worked > parameters.ten_hour_max_minutes:
            findings.append(
                Finding(
                    "WT-CORE-006",
                    FindingSeverity.CRITICAL,
                    finding_day,
                    "Daily working time exceeds 10 hours.",
                )
            )

    for (_previous_day, previous_blocks), (current_day, current_blocks) in pairwise(groups):
        rest = _elapsed_minutes(previous_blocks[-1][1], current_blocks[0][0])
        if (
            rest < parameters.eleven_hour_rest_minutes
            and period_start_at <= current_blocks[0][0] < period_end_at
        ):
            findings.append(
                Finding(
                    "WT-CORE-007",
                    FindingSeverity.CRITICAL,
                    _group_date(current_day, inputs.period_start),
                    "Rest between working periods is less than 11 hours.",
                )
            )

    for day in _days(inputs.period_start, inputs.period_end):
        calendar_class = inputs.calendar_classes.get(day, CalendarClass.WEEKDAY)
        portion_spans = _portion_spans_for_day(inputs.intervals, day)
        portions = [(start, end) for _interval, start, end in portion_spans]
        worked = sum(
            _allocated_minutes(interval.start_at, interval.end_at, start, end)
            for interval, start, end in portion_spans
        )
        # F4.5 account layer: the same allocation, filtered by which work types
        # CREDIT the working-time account. Under v1 every work type credits, so
        # this equals ``worked`` and no existing number moves.
        account_credit = sum(
            _allocated_minutes(interval.start_at, interval.end_at, start, end)
            for interval, start, end in portion_spans
            if parameters.credit_by_work_type.get(interval.work_type, True)
        )
        # Explicit breaks (WT-BACKEND §5.3). Only a NON-compensated break
        # reduces worked time, and only the part of it overlapping a work span:
        # a break already sitting in a gap changes nothing, because that time is
        # already excluded from ``worked``. PAID_SHIFT_BREAK is remunerated
        # presence (SHIFT-003) and is deliberately NOT deducted.
        for break_fact in inputs.breaks:
            if break_fact.kind in NON_DEDUCTING_BREAK_KINDS:
                continue
            for interval, span_start, span_end in portion_spans:
                overlap = _overlap_minutes(
                    break_fact.start_at, break_fact.end_at, span_start, span_end
                )
                if overlap <= 0:
                    continue
                worked = max(0, worked - overlap)
                if parameters.credit_by_work_type.get(interval.work_type, True):
                    account_credit = max(0, account_credit - overlap)
        # F5 day-status effect. Halves are summed PER DAY, over the REDUCING
        # categories only, and clamped at the supported day coverage of 2. The
        # reduction floors, so ``reduced + required == scheduled`` exactly and
        # no minute is lost or invented (EINV-17's spirit).
        day_facts = day_status.get(day, [])
        scheduled = inputs.daily_required_minutes.get(day, 0)
        reducing_halves = sum(
            PORTION_HALVES.get(fact.portion, 0)
            for fact in day_facts
            if fact.category in REDUCING_CATEGORIES
        )
        covered_halves = min(reducing_halves, DAY_HALVES)
        reduced = covered_halves * scheduled // DAY_HALVES
        required = 0 if calendar_class != CalendarClass.WEEKDAY else scheduled - reduced
        qualifying_break = _qualifying_break_minutes(
            portions, parameters, _breaks_overlapping(portions, inputs.breaks)
        )

        if day_facts:
            # Employee-private findings. ``WT-STATUS-005`` is defined against the
            # SCHEDULED requirement, not the calendar class: CalendarClass has no
            # weekend member (weekends are ordinary calendar days with a zero
            # schedule requirement), so a class-based rule would fire on every
            # weekend or never. This covers weekends, holidays, employer-free days
            # and schedule-less days in one honest condition.
            if scheduled == 0:
                findings.append(
                    Finding(
                        "WT-STATUS-005",
                        FindingSeverity.WARNING,
                        day,
                        "A day status is recorded on a day with no scheduled requirement.",
                    )
                )
            if sum(PORTION_HALVES.get(fact.portion, 0) for fact in day_facts) > DAY_HALVES:
                findings.append(
                    Finding(
                        "WT-STATUS-004",
                        FindingSeverity.WARNING,
                        day,
                        "Day-status coverage exceeds one day for this date.",
                    )
                )
            full_categories = {
                fact.category
                for fact in day_facts
                if fact.portion == "FULL" and fact.category in REDUCING_CATEGORIES
            }
            if worked > 0 and full_categories:
                for category, rule_id, message in (
                    (
                        "VACATION",
                        "WT-STATUS-001",
                        "Work is recorded on a full vacation day. Correct or remove the "
                        "recorded interval, or change the day status.",
                    ),
                    (
                        "SICK",
                        "WT-STATUS-002",
                        # Truthful about what the engine actually did: the work fact
                        # is preserved and credited on the ordinary path, and the
                        # only unresolved thing is how a partly-worked sickness day
                        # should FINALLY be accounted for. The earlier wording
                        # ("not reported as worked time") contradicted the WORKED
                        # and CREDITED figures shown beside it. The blocked rule id
                        # stays out of employee-facing copy; the spec's
                        # cross-reference carries that traceability instead.
                        "Work is recorded on a sickness day. Your recorded hours are "
                        "kept and shown as worked time; how they should finally be "
                        "counted is still being confirmed. Review the recorded "
                        "interval and the sickness day.",
                    ),
                ):
                    if category in full_categories:
                        findings.append(Finding(rule_id, FindingSeverity.CRITICAL, day, message))
            if worked > 0 and any(
                fact.category == "FLEX_COMPENSATION" and fact.portion == "FULL"
                for fact in day_facts
            ):
                findings.append(
                    Finding(
                        "WT-STATUS-003",
                        FindingSeverity.CRITICAL,
                        day,
                        "Work is recorded on a full flexible-time-compensation day.",
                    )
                )

        for interval in inputs.intervals:
            if interval.work_type in {"SHIFT", "OCD_CALL", "OVERTIME_PAID", "OVERTIME_TIME_COMP"}:
                continue
            start, end = _day_bounds(day)
            if interval.start_at < end and interval.end_at > start:
                local_start = max(interval.start_at, start).astimezone(BERLIN)
                local_end = min(interval.end_at, end).astimezone(BERLIN)
                start_minutes = local_start.hour * 60 + local_start.minute
                # A local end at this day's next midnight is 24:00, not 00:00.
                end_minutes = (
                    24 * 60 if local_end == end else local_end.hour * 60 + local_end.minute
                )
                ends_after_corridor = end_minutes > parameters.corridor_end_minutes and not (
                    end_minutes == 24 * 60 and start_minutes < parameters.corridor_end_minutes
                )
                if start_minutes < parameters.corridor_start_minutes or ends_after_corridor:
                    findings.append(
                        Finding(
                            "WT-CORE-008",
                            FindingSeverity.WARNING,
                            day,
                            "Ordinary trusted-flex work is outside the 06:00-21:00 corridor.",
                        )
                    )
                    break

        # The balance is the ACCOUNT layer, not the factual one: it is driven by
        # credited minutes. ``worked`` still drives every WT-CORE finding above.
        delta = account_credit - required
        # TRAVEL-003: employee-declared passive travel may close the day's
        # deficit down to (never below) zero; passive travel alone can never
        # produce positive flex (POL-08). The clamp makes an
        # inconsistent top-up input harmless.
        declared = travel_topups.get(day, 0)
        # TRAVEL-003's clamp deliberately stays on the FACTUAL layer in F4: how
        # the top-up interacts with a non-crediting work type is a travel-rule
        # question (F6/F8), not an accounting-layer rename, and re-pointing it
        # here would change a travel number under a future edition as a side
        # effect of the layer split.
        topup = min(declared, max(0, required - worked)) if declared else 0
        delta += topup
        daily_results.append(
            DailyResult(
                day,
                calendar_class,
                worked,
                required,
                delta,
                qualifying_break,
                topup,
                account_credit,
            )
        )
        proposals.append(LedgerProposal(day, delta, "FLEX_DELTA"))

    # TRAVEL-004: pure derived credit proposals (25% of the excess over
    # the 15 h per-trip assessment window). They join the balance exactly like
    # any other ledger proposal — read-time derived, never persisted as
    # editable truth, and the owning trip id is carried for audit/fingerprint.
    for credit in inputs.travel_credits:
        if inputs.period_start <= credit.occurred_on < inputs.period_end:
            proposals.append(credit)
    findings.extend(inputs.travel_findings)

    opening = sum(entry.minutes for entry in inputs.opening_ledger_entries)
    balance_before_settlement = opening + sum(proposal.minutes for proposal in proposals)
    # WT-FLEX-005's trigger is the rule's own "at any time" condition
    # (``docs/RULE_CATALOGUE.md`` WT-FLEX-005), so the balance it tests is
    # the one accumulated THROUGH the business date — not the period-end closing,
    # which charges every un-lived weekday its full requirement against zero
    # recorded work and warns employees who owe nothing.
    #
    # Captured ABOVE the year-end settlement branch on purpose: the payout/forfeit
    # pair is a boundary decision, not an elapsed amount, so it must not enter the
    # as-of sum. ``opening`` stays un-filtered for the mirror-image reason: those
    # entries are recorded decisions already applied under the caller's window
    # contract, and only day-derived amounts are projections.
    #
    # ceiling: an in-month explicit row dated after ``as_of`` would still count.
    # No writer produces one — the cutover seed is dated at ``previous_end`` and
    # corrections reuse the seed's date — so a date filter on the opening entries
    # would be dead code. Add one if a writer ever dates a row inside its own month.
    balance_as_of = balance_before_settlement
    if inputs.as_of is not None:
        balance_as_of = opening + sum(
            proposal.minutes for proposal in proposals if proposal.occurred_on <= inputs.as_of
        )
    weekly_minutes = inputs.weekly_required_minutes
    if balance_before_settlement > weekly_minutes // parameters.flex_critical_denominator:
        findings.append(
            Finding(
                "WT-FLEX-004",
                FindingSeverity.CRITICAL,
                None,
                "Balance exceeds 100% of contractual weekly hours.",
            )
        )
    elif balance_before_settlement > weekly_minutes // parameters.flex_warn_denominator:
        findings.append(
            Finding(
                "WT-FLEX-003",
                FindingSeverity.WARNING,
                None,
                "Balance exceeds 50% of contractual weekly hours.",
            )
        )

    individual_days = [value for value in inputs.daily_required_minutes.values() if value > 0]
    # WT-FLEX-003/004 above deliberately keep the PERIOD-END figure: they are the
    # same projection class as the defect this rule just shed, and moving them is
    # a separate decision (see the feature document's Out of Scope). Do not "fix"
    # them here by accident.
    if individual_days and balance_as_of < -(
        parameters.negative_balance_days * max(individual_days)
    ):
        findings.append(
            Finding(
                "WT-FLEX-005",
                FindingSeverity.WARNING,
                None,
                "Negative balance exceeds two individual working days.",
            )
        )

    closing = balance_before_settlement
    # The boundary is matched against the EXCLUSIVE ``period_end``, so it names the
    # period that ENDS at the year boundary (December). It is not a payout month:
    # the sample policy leaves the payout month open, so nothing here may assert it.
    if (
        inputs.period_end.month == parameters.settlement_boundary_month
        and inputs.period_end.day == parameters.settlement_boundary_day
    ):
        if closing > 0:
            payout = min(closing, parameters.year_end_payout_cap_minutes)
            proposals.append(
                LedgerProposal(inputs.period_end - timedelta(days=1), -payout, "YEAR_END_PAYOUT")
            )
            if closing > parameters.year_end_payout_cap_minutes:
                proposals.append(
                    LedgerProposal(
                        inputs.period_end - timedelta(days=1),
                        -(closing - parameters.year_end_payout_cap_minutes),
                        "YEAR_END_FORFEIT",
                    )
                )
            closing = 0
        elif closing < 0:
            findings.append(
                Finding(
                    "WT-FLEX-007",
                    FindingSeverity.BLOCKED_POLICY,
                    None,
                    "Negative year-end settlement is blocked pending policy confirmation.",
                )
            )

    return PeriodResult(
        daily_results=tuple(daily_results),
        ledger_proposals=tuple(proposals),
        findings=tuple(findings),
        opening_balance_minutes=opening,
        closing_balance_minutes=closing,
        input_fingerprint=_fingerprint(inputs),
        calendar_version_ids=inputs.calendar_version_ids,
    )
