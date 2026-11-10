"""Narrow persistence adapter around the pure policy engine."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session, aliased

if TYPE_CHECKING:
    # Imported for the prefetch carrier's annotation only: `app.absence.models`
    # is imported lazily inside `_current_day_status` to avoid an import cycle.
    from app.absence.models import DayStatusEvent

from app.balance.models import BalanceLedgerEntry
from app.calendar.models import CALENDAR_KEY, CalendarDay, CalendarVersion
from app.enums import CalendarDayClass, SubmissionPurpose
from app.errors import CalendarUnavailable, PolicyEditionUnavailable
from app.identity.models import Employee, EmploymentContract, WorkScheduleVersion
from app.identity.service import effective_contracts_by_day, effective_schedules_by_day
from app.policy.editions import (
    OrdinaryTimeParameters,
    TravelParameters,
    UnknownPolicyEdition,
    resolve_edition,
)
from app.policy.engine import (
    BERLIN,
    BreakFact,
    CalendarClass,
    DayStatusFact,
    Finding,
    IntervalFact,
    LedgerFact,
    LedgerProposal,
    PeriodInputs,
    PeriodResult,
    TravelTopupFact,
    calculate_period,
)
from app.policy.models import PolicyVersion
from app.travel.context import (
    MonthIndependentTravelFacts,
    load_travel_calculation,
    month_independent_travel_facts,
)
from app.worktime.models import BreakInterval, WorkInterval
from app.worktime.queries import current_breaks, current_intervals, month_bounds
from app.worktime.walltime import local_date, now_berlin

POLICY_KEY = "ordinary-working-time"

#: ``TRAVEL-003``/``004``'s own policy key. A travel edition resolves from the
#: month exactly like the ordinary one — never "the newest" — so publishing a
#: corrected travel edition cannot restate an already-computed month
#: (``EINV-09``/``INV-11``). The row is seeded by ``0015_policy_edition_identity``
#: as ``phase-2-travel-v1``.
TRAVEL_POLICY_KEY = "travel-credit"

#: Which edition identity each frozen purpose projection asserts. A submission
#: package must point at a REAL edition, so the purpose (not a literal buried in
#: a payload) selects the policy key and the payload's own ``policy_version``
#: selects the version within it.
PURPOSE_POLICY_KEYS: dict[SubmissionPurpose, str] = {
    SubmissionPurpose.SHIFT_REPORT: "shift-work",
    SubmissionPurpose.OVERTIME_PAID: "overtime",
    SubmissionPurpose.OVERTIME_TIME_COMP: "overtime",
    SubmissionPurpose.OCD_COMPENSATION: "ocd-compensation",
}


def _period_end(month: date) -> date:
    return date(month.year + 1, 1, 1) if month.month == 12 else date(month.year, month.month + 1, 1)


def _requirements(
    session: Session,
    employee: Employee,
    start: date,
    end: date,
    *,
    schedules: Mapping[date, WorkScheduleVersion | None] | None = None,
) -> dict[date, int]:
    columns = (
        "mon_required_minutes",
        "tue_required_minutes",
        "wed_required_minutes",
        "thu_required_minutes",
        "fri_required_minutes",
        "sat_required_minutes",
        "sun_required_minutes",
    )
    result: dict[date, int] = {}
    # An effective schedule ALONE drives daily
    # requirements. Weekly thresholds already prefer the schedule
    # (WT-FLEX-003/004) with the contract as fallback; requiring BOTH made
    # a contract-less employee-maintained schedule yield nonzero weekly
    # but zero daily requirements (internally inconsistent). Missing
    # authoritative data is still represented as no requirement; contract-
    # dependent calculations (FTE, pay-group) keep their own explicit
    # blocked/unknown handling elsewhere.
    #
    # Resolved for the whole range at once: the per-day form cost one statement
    # per day, and the month view walks this range for every month it carries
    # forward. ``schedules`` lets one month resolve it once for both this function
    # and ``_weekly_required_minutes``; callers that pass nothing get the range
    # resolution here.
    if schedules is None:
        schedules = effective_schedules_by_day(session, employee.id, start, end)
    for day, schedule in schedules.items():
        result[day] = int(getattr(schedule, columns[day.weekday()])) if schedule is not None else 0
    return result


def _calendar_versions(
    session: Session, *, start: date | None, end: date
) -> Sequence[CalendarVersion]:
    """Calendar editions that can cover ``[start, end)``, newest first.

    ``start=None`` drops the lower bound, which is what the carry chain's
    once-per-view prefetch wants: the chain does not know its own anchor before it
    fetches, and an edition that cannot cover a day is excluded by the per-day walk
    below anyway, so a wider fetch is harmless.
    """
    query = select(CalendarVersion).where(
        CalendarVersion.calendar_key == CALENDAR_KEY,
        CalendarVersion.valid_from < end,
    )
    if start is not None:
        query = query.where(
            or_(CalendarVersion.valid_to.is_(None), CalendarVersion.valid_to >= start)
        )
    return session.scalars(query.order_by(CalendarVersion.valid_from.desc())).all()


def _calendar_days(session: Session, *, version_ids: set[UUID], end: date) -> Sequence[CalendarDay]:
    """Day classifications before ``end`` belonging to ``version_ids``.

    The edition filter is NOT optional: without it this would fetch every
    ``CalendarDay`` row of every calendar before ``end``, where the per-month query
    it replaces fetched only the rows of the editions covering that month. Rows are
    bounded by the editions the chain can actually select, so the prefetch is the
    union of what the per-month queries asked for — no wider.
    """
    return session.scalars(
        select(CalendarDay).where(
            CalendarDay.calendar_version_id.in_(version_ids),
            CalendarDay.day < end,
        )
    ).all()


def _calendar_classes(
    session: Session,
    start: date,
    end: date,
    *,
    versions: Sequence[CalendarVersion] | None = None,
    days: Sequence[CalendarDay] | None = None,
) -> tuple[dict[date, CalendarClass], tuple[UUID, ...]]:
    """Resolve the calendar for ``[start, end)``; fail closed when uncovered.

    Three cases, and the third is the fix for audit B-2's silent fallback:

    1. a covering edition exists and classifies the day -> that class;
    2. a covering edition exists and does not list the day -> ``WEEKDAY`` (an
       edition enumerates *exceptions*; an ordinary working day is the declared
       default **within** an edition);
    3. no covering edition exists for a day in the window -> ``CalendarUnavailable``
       -> 409 ``CALENDAR_BLOCKED`` naming the first uncovered local date. It is
       NOT treated as an ordinary working day: that made a holiday add required
       time and silently invalidated the balance.

    Resolution is "greatest ``valid_from`` covering the day wins", which
    ``uq_calendar_version_effective_from`` makes unique — so "which edition
    applies" is a data decision, not a ``version``-string tiebreak (EINV-04).
    A day row belonging to any other edition is ignored for that day: overlap is
    precedence, never composition.
    """
    if versions is None:
        versions = _calendar_versions(session, start=start, end=end)
    selected: dict[date, CalendarVersion] = {}
    first_uncovered: date | None = None
    day = start
    while day < end:
        version = next(
            (
                candidate
                for candidate in versions
                if candidate.valid_from <= day
                and (candidate.valid_to is None or candidate.valid_to >= day)
            ),
            None,
        )
        if version is not None:
            selected[day] = version
        elif first_uncovered is None:
            first_uncovered = day
        day += timedelta(days=1)
    if first_uncovered is not None:
        raise CalendarUnavailable(CALENDAR_KEY, first_uncovered)
    if days is None:
        rows = session.scalars(
            select(CalendarDay).where(
                CalendarDay.calendar_version_id.in_({version.id for version in selected.values()}),
                CalendarDay.day >= start,
                CalendarDay.day < end,
            )
        ).all()
    else:
        # Prefetched for the whole carry range; narrowed to this month's window,
        # which is exactly what the query above would have asked for.
        rows = [row for row in days if start <= row.day < end]
    mapping = {
        CalendarDayClass.PUBLIC_HOLIDAY: CalendarClass.PUBLIC_HOLIDAY,
        CalendarDayClass.EMPLOYER_FREE: CalendarClass.EMPLOYER_FREE,
    }
    return (
        {
            row.day: mapping[row.day_class]
            for row in rows
            if selected.get(row.day) is not None and row.calendar_version_id == selected[row.day].id
        },
        tuple(sorted({version.id for version in selected.values()}, key=str)),
    )


def policy_version_for(session: Session, month: date) -> PolicyVersion:
    """Public month-aligned resolver (cutover seed / reporting)."""
    return _policy_version(session, month)


def require_calendar_coverage(session: Session, start: date, end: date) -> None:
    """Fail closed (``CalendarUnavailable``) unless an edition covers every day.

    Exposes the SAME resolver the month view uses, so a caller that only needs to
    know "is this window covered?" (self-onboarding's prerequisite gate) cannot
    drift from what the calculation would actually accept. The resolved classes
    are discarded — the question here is coverage, not classification.
    """
    _calendar_classes(session, start, end)


def _policy_versions(session: Session) -> Sequence[PolicyVersion]:
    """Every ordinary-time edition, in the order the month resolver picks from.

    The carry chain resolves an edition for each month it walks; this lets it ask
    once. Ordering must match ``_policy_version``'s ``ORDER BY``, which is what
    makes the first covering row the same winner.
    """
    return session.scalars(
        select(PolicyVersion)
        .where(PolicyVersion.policy_key.in_((POLICY_KEY, TRAVEL_POLICY_KEY)))
        .order_by(PolicyVersion.effective_from.desc(), PolicyVersion.version.desc())
    ).all()


def _policy_version(
    session: Session, month: date, *, versions: Sequence[PolicyVersion] | None = None
) -> PolicyVersion:
    # PeriodInputs and snapshot lineage each carry one policy version;
    # policy editions are therefore month-aligned until a specified contract
    # introduces per-day policy-rule splitting (ARCH §8.3).
    candidates = _policy_versions(session) if versions is None else versions
    version = next(
        (
            candidate
            for candidate in candidates
            if candidate.policy_key == POLICY_KEY
            and candidate.effective_from <= month
            and (candidate.effective_to is None or candidate.effective_to >= month)
        ),
        None,
    )
    if version is None:
        # Fail closed, never fall back to the newest edition: that would
        # silently restate a historical month under today's rules (EINV-08).
        raise PolicyEditionUnavailable(POLICY_KEY, month)
    return version


def travel_edition_for(
    session: Session,
    month: date,
    *,
    versions: Sequence[PolicyVersion] | None = None,
) -> tuple[str, TravelParameters]:
    """The travel rule set and its frozen parameters for ``month`` (F8-9).

    Resolved from the MONTH, never from "the newest row", so an already-computed
    month keeps producing the numbers its own edition specifies. Fails closed
    (``EINV-08``) on three conditions, exactly as ``_month_inputs`` does for the
    ordinary edition: no covering row; a row whose rule set is not registered; or
    a registered rule set that carries no ``TravelParameters`` — the last one is
    what stops a mis-registered edition from silently executing on today's
    defaults instead of the parameters the row's identity claims.
    """
    if versions is None:
        row = session.scalar(
            select(PolicyVersion)
            .where(
                PolicyVersion.policy_key == TRAVEL_POLICY_KEY,
                PolicyVersion.effective_from <= month,
                or_(PolicyVersion.effective_to.is_(None), PolicyVersion.effective_to >= month),
            )
            .order_by(PolicyVersion.effective_from.desc(), PolicyVersion.version.desc())
            .limit(1)
        )
    else:
        row = next(
            (
                candidate
                for candidate in versions
                if candidate.policy_key == TRAVEL_POLICY_KEY
                and candidate.effective_from <= month
                and (candidate.effective_to is None or candidate.effective_to >= month)
            ),
            None,
        )
    if row is None:
        raise PolicyEditionUnavailable(TRAVEL_POLICY_KEY, month)
    try:
        edition = resolve_edition(row.engine_rule_set)
    except UnknownPolicyEdition:
        raise PolicyEditionUnavailable(TRAVEL_POLICY_KEY, month) from None
    if not isinstance(edition.parameters, TravelParameters):
        raise PolicyEditionUnavailable(TRAVEL_POLICY_KEY, month)
    return edition.rule_set, edition.parameters


def policy_version_for_purpose(
    session: Session,
    purpose: SubmissionPurpose,
    payload: Mapping[str, object],
    period_start: date,
) -> PolicyVersion:
    """Resolve the edition identity a frozen purpose projection asserts.

    ``submission_package.policy_version_id`` used to stay NULL, so a frozen
    package claimed an edition only through an unresolvable literal in its
    payload. The row is looked up by ``(policy_key, version)`` — unique by
    ``uq_policy_version_key`` — AND must be the edition effective at the
    package's own ``period_start``, exactly as ``backup.py`` verifies it
    afterwards: stamping a package with an edition that did not apply to its
    period would make the frozen artefact's own provenance false.

    The check is a POINT (``period_start``), not the whole period, and that is
    deliberate: a point matches at most one row of a non-overlapping range set,
    so the choice is deterministic (``EINV-04``/``EINV-18``). Testing the whole
    period would let two editions of one key both match a period that straddles
    their boundary, which is exactly the read-order ambiguity those invariants
    forbid. A missing row, an edition that did not apply, or an unresolvable
    rule set all fail closed rather than storing an unverifiable package.
    """
    policy_key = PURPOSE_POLICY_KEYS[purpose]
    row = session.scalar(
        select(PolicyVersion)
        .where(
            PolicyVersion.policy_key == policy_key,
            PolicyVersion.version == payload.get("policy_version"),
            PolicyVersion.effective_from <= period_start,
            or_(PolicyVersion.effective_to.is_(None), PolicyVersion.effective_to >= period_start),
        )
        .order_by(PolicyVersion.effective_from.desc(), PolicyVersion.version.desc())
        .limit(1)
    )
    if row is None:
        raise PolicyEditionUnavailable(policy_key, period_start)
    try:
        resolve_edition(row.engine_rule_set)
    except UnknownPolicyEdition:
        raise PolicyEditionUnavailable(policy_key, period_start) from None
    return row


def _weekly_required_minutes(
    session: Session,
    employee: Employee,
    start: date,
    end: date,
    *,
    schedules: Mapping[date, WorkScheduleVersion | None] | None = None,
    contracts: Mapping[date, EmploymentContract | None] | None = None,
) -> int:
    """Weight weekly thresholds by calendar days under each effective schedule.

    The employee-maintained schedule's weekly target is the calculation
    threshold (WT-FLEX-003/004); the contract value
    is the fallback when no effective schedule exists. Seeded master data has
    schedule weekly == contract weekly, so existing results are unchanged.
    The pure engine itself is untouched.
    """
    total = 0
    days_with_weekly = 0
    # Same range resolution as ``_requirements``, for the same reason — and when
    # the caller already resolved a range for this month, reuse it instead of
    # asking again.
    if schedules is None:
        schedules = effective_schedules_by_day(session, employee.id, start, end)
    if contracts is None:
        contracts = effective_contracts_by_day(session, employee.id, start, end)
    for day, schedule in schedules.items():
        contract = contracts[day]
        if schedule is not None:
            total += schedule.weekly_required_minutes
            days_with_weekly += 1
        elif contract is not None:
            total += contract.weekly_required_minutes
            days_with_weekly += 1
    return total // days_with_weekly if days_with_weekly else 0


def _travel_context(
    session: Session,
    employee: Employee,
    month: date,
    end: date,
    daily_required: dict[date, int],
    calendar_classes: dict[date, CalendarClass],
    *,
    travel_facts: MonthIndependentTravelFacts | None = None,
    policy_versions: Sequence[PolicyVersion] | None = None,
) -> tuple[tuple[TravelTopupFact, ...], tuple[LedgerProposal, ...], tuple[Finding, ...], str]:
    """Adapt the one shared travel loader to pure period-engine inputs.

    ``travel_facts`` is the carry chain's once-per-view prefetch of the two
    month-independent travel queries; a standalone month computation (or a test)
    leaves it out and the loader fetches them itself.
    """
    result = load_travel_calculation(
        session,
        employee,
        month,
        daily_required,
        calendar_classes,
        prefetch=travel_facts,
        policy_versions=policy_versions,
    ).result
    topups = tuple(
        TravelTopupFact(item.day, item.minutes) for item in result.topups if month <= item.day < end
    )
    credits = tuple(
        LedgerProposal(
            occurred_on=credit.occurred_on,
            minutes=credit.minutes,
            entry_type="TRAVEL_CREDIT",
            rule_id=credit.rule_id,
            source_entity_type=credit.source_entity_type,
            source_entity_id=credit.source_entity_id,
            calculation={
                "window_start": credit.window_start.isoformat(),
                "window_end": credit.window_end.isoformat(),
                "eligible_minutes": credit.eligible_minutes,
                "threshold_minutes": credit.threshold_minutes,
                "excess_minutes": credit.excess_minutes,
            },
        )
        for credit in result.credits
        if month <= credit.occurred_on < end
    )
    findings = tuple(
        finding
        for finding in result.findings
        if finding.date is None or month <= finding.date < end
    )
    return topups, credits, findings, result.input_fingerprint


def _current_day_status(session: Session, employee_id: UUID, start: date | None, end: date) -> list:
    """Current (unsuperseded) day-status facts for ``[start, end)``.

    ``start=None`` drops the lower bound, for the carry chain's once-per-view
    prefetch: the chain narrows each month in memory instead.

    The canonical current-version predicate (EINV-13): a row is current when no
    other row supersedes it. Only canonical facts are inputs; a superseded
    version must never influence the arithmetic.
    """
    from app.absence.models import DayStatusEvent

    child = aliased(DayStatusEvent)
    query = select(DayStatusEvent).where(
        DayStatusEvent.employee_id == employee_id,
        DayStatusEvent.day < end,
        ~exists(select(child.id).where(child.supersedes_id == DayStatusEvent.id)),
    )
    if start is not None:
        query = query.where(DayStatusEvent.day >= start)
    return list(session.scalars(query).all())


@dataclass(frozen=True)
class _CarryFacts:
    """The carry chain's once-per-view prefetch, narrowed per month in memory.

    Every field is the FULL history up to the requested month's end. Each month
    narrows it to its own window — exactly the window the per-month fetch used to
    ask the database for — so passing this is an optimization, never a behaviour
    change. ``test_p6_month_view_batching`` guards that equivalence, and the
    long-history month view went from ~3500 statements to a few dozen.

    It exists because the balance is derived, not stored: the view walks every
    month since the cutover, and resolving each month's edition, calendar,
    day-status facts and effective-dated state one query at a time is what made a
    36-month history slow. Nothing here is persisted.
    """

    intervals: Sequence[WorkInterval]
    breaks: Sequence[BreakInterval]
    day_status: Sequence[DayStatusEvent]
    calendar_versions: Sequence[CalendarVersion]
    calendar_days: Sequence[CalendarDay]
    policy_versions: Sequence[PolicyVersion]
    contracts: Sequence[EmploymentContract]
    schedules: Sequence[WorkScheduleVersion]
    #: The travel loader's two month-independent queries, fetched once here rather
    #: than once per chained month. Unlike every other field this one is NOT
    #: narrowed per month — it cannot be, because the query has no month predicate
    #: — so it is passed through untouched.
    travel: MonthIndependentTravelFacts


def _carry_facts(session: Session, employee: Employee, month: date) -> _CarryFacts:
    """Fetch once everything the carry chain and the requested month will read.

    Every field is bounded by the REQUESTED month's end and by the employee or the
    one institutional calendar, so this is a superset of what the per-month queries
    asked for, never a wider one. The lower bounds are deliberately absent: the
    chain does not know its anchor until it has read the ledger, and each month
    narrows in memory anyway.
    """
    end = _period_end(month)
    end_at = month_bounds(month)[1]
    versions = _calendar_versions(session, start=None, end=end)
    intervals = current_intervals(session, employee_id=employee.id, intersects_to=end_at)
    contracts = session.scalars(
        select(EmploymentContract)
        .where(
            EmploymentContract.employee_id == employee.id,
            EmploymentContract.valid_from < end,
        )
        .order_by(EmploymentContract.valid_from.desc(), EmploymentContract.version.desc())
    ).all()
    schedules = session.scalars(
        select(WorkScheduleVersion)
        .where(
            WorkScheduleVersion.employee_id == employee.id,
            WorkScheduleVersion.valid_from < end,
        )
        .order_by(WorkScheduleVersion.valid_from.desc(), WorkScheduleVersion.version.desc())
    ).all()
    return _CarryFacts(
        intervals=intervals,
        breaks=current_breaks(session, employee_id=employee.id, intersects_to=end_at),
        day_status=_current_day_status(session, employee.id, None, end),
        calendar_versions=versions,
        calendar_days=_calendar_days(
            session, version_ids={version.id for version in versions}, end=end
        ),
        policy_versions=_policy_versions(session),
        contracts=contracts,
        schedules=schedules,
        travel=replace(month_independent_travel_facts(session, employee.id), intervals=intervals),
    )


def _month_inputs(
    session: Session,
    employee: Employee,
    month: date,
    opening_entries: tuple[LedgerFact, ...],
    *,
    facts: _CarryFacts | None = None,
    as_of: date | None = None,
) -> PeriodInputs:
    """Build the pure engine inputs for one month with a given opening.

    ``facts`` is the carry chain's once-per-view prefetch. Every field is narrowed
    here to the window the direct fetch would have used, so a chained month sees
    exactly the rows it saw before — passing it is an optimization, never a
    behaviour change.

    ``as_of`` is the view's single business-date reading, threaded unchanged into
    every month of the chain so the carry and the requested month's findings can
    never disagree about what "today" was.
    """
    end = _period_end(month)
    _start_at, end_at = month_bounds(month)
    # Include all prior canonical intervals as rest context.  The pure result
    # allocates only values inside ``month`` and never mutates factual intervals.
    if facts is None:
        intervals = current_intervals(session, employee_id=employee.id, intersects_to=end_at)
    else:
        intervals = [row for row in facts.intervals if row.start_at < end_at]
    # Explicit breaks take the SAME window as intervals, because the statutory
    # rules evaluate a canonical work GROUP, not a month: a group that starts in
    # the previous month and ends in this one is ONE group, so its recorded break
    # must be visible here — otherwise the group is judged as unbroken work and
    # emits false WT-CORE-003 / WT-CORE-005 findings from a fact the employee
    # recorded. Only the month's own days are allocated below, and a break that
    # overlaps no day portion cannot move any day number.
    if facts is None:
        breaks = current_breaks(session, employee_id=employee.id, intersects_to=end_at)
    else:
        breaks = [row for row in facts.breaks if row.start_at < end_at]
    policy = _policy_version(
        session, month, versions=None if facts is None else facts.policy_versions
    )
    # The ONE place a recorded edition identity is validated against executable
    # code (F4 D1): a row whose rule set has no registry entry, or whose edition
    # carries no ordinary-time parameters, fails closed instead of silently
    # falling back to today's numbers.
    try:
        edition = resolve_edition(policy.engine_rule_set)
    except UnknownPolicyEdition:
        raise PolicyEditionUnavailable(policy.policy_key, month) from None
    if not isinstance(edition.parameters, OrdinaryTimeParameters):
        raise PolicyEditionUnavailable(policy.policy_key, month)
    calendar_classes, calendar_version_ids = _calendar_classes(
        session,
        month,
        end,
        versions=None if facts is None else facts.calendar_versions,
        days=None if facts is None else facts.calendar_days,
    )
    # One range resolution per month, shared by the daily and the weekly
    # thresholds: they walked the same days and asked the same question twice.
    schedules = effective_schedules_by_day(
        session,
        employee.id,
        month,
        end,
        versions=None if facts is None else facts.schedules,
    )
    contracts = effective_contracts_by_day(
        session,
        employee.id,
        month,
        end,
        versions=None if facts is None else facts.contracts,
    )
    daily_required = _requirements(session, employee, month, end, schedules=schedules)
    travel_topups, travel_credits, travel_findings, travel_fp = _travel_context(
        session,
        employee,
        month,
        end,
        daily_required,
        calendar_classes,
        travel_facts=None if facts is None else facts.travel,
        policy_versions=None if facts is None else facts.policy_versions,
    )
    return PeriodInputs(
        period_start=month,
        period_end=end,
        daily_required_minutes=daily_required,
        weekly_required_minutes=_weekly_required_minutes(
            session, employee, month, end, schedules=schedules, contracts=contracts
        ),
        calendar_classes=calendar_classes,
        calendar_version_ids=tuple(str(version_id) for version_id in calendar_version_ids),
        intervals=tuple(
            IntervalFact(str(row.id), row.start_at, row.end_at, row.work_type.value)
            for row in intervals
        ),
        travel_topups=travel_topups,
        travel_credits=travel_credits,
        travel_findings=travel_findings,
        travel_input_fingerprint=travel_fp,
        opening_ledger_entries=opening_entries,
        policy_version=policy.version,
        engine_build=edition.rule_set,
        parameters=edition.parameters,
        day_status=tuple(
            DayStatusFact(row.day, row.category.value, row.portion.value)
            for row in (
                _current_day_status(session, employee.id, month, end)
                if facts is None
                else [row for row in facts.day_status if month <= row.day < end]
            )
        ),
        breaks=tuple(
            BreakFact(str(row.id), row.start_at, row.end_at, row.kind.value) for row in breaks
        ),
        as_of=as_of,
    )


def _carried_opening(
    session: Session,
    employee: Employee,
    month: date,
    ledger_rows: Sequence[BalanceLedgerEntry],
    *,
    facts: _CarryFacts,
    as_of: date | None = None,
) -> tuple[LedgerFact, ...]:
    """Read-time month-by-month carry-forward (INV-08 / CUTOVER-T02).

    Derived daily deltas are never persisted as editable truth (docs/INVARIANTS.md),
    so every completed month between the cutover anchor (the month after the
    CUTOVER_SEED row, else the employee's earliest live interval) and
    ``month`` is re-run through the same deterministic engine, threading the
    closing balance forward.  Month M then opens with exactly month M-1's
    closing value — balances continue with no manual copy each month, while
    pre-cutover months stay in the Excel archive (only the validated seed
    crosses the boundary; no fake historical intervals or dates are created).

    A month that has not ELAPSED is not completed, so it is not re-run to its own
    end: an un-lived weekday is charged its full requirement against zero recorded
    work, and threading that projection forward would open the next month with
    debt the employee never incurred (INV-08). ``as_of`` is the view's single
    business-date reading — the chain re-runs each month only as far as that date,
    and a month that has not started yet contributes only its own recorded ledger
    rows. Explicit rows always apply in the month containing their ``occurred_on``,
    so the "every row applies exactly once" rule below holds unchanged.

    Consistency rule: an explicit ledger row belongs to
    the month containing its ``occurred_on`` — that month's closing (shown in
    its own direct view and in the carry into the next month) is the first to
    include it.  Every row therefore applies exactly once.

    ceiling: re-derives O(months-since-cutover) engine calls per view;
    materialize closing ledger rows if month views ever become hot.
    """
    rows = tuple(ledger_rows)
    seed = next(
        (
            row
            for row in sorted(rows, key=lambda r: r.occurred_on, reverse=True)
            if row.entry_type == "CUTOVER_SEED"
        ),
        None,
    )
    if seed is not None:
        seed_next = seed.occurred_on + timedelta(days=1)  # first day of cutover month
        anchor = date(seed_next.year, seed_next.month, 1)
    else:
        # Only current interval versions are canonical facts. A correction can
        # move an interval forward; anchoring to its superseded predecessor
        # would manufacture prior-month deficits in this read-time chain.
        # A travel-only employee (passive facts, zero work intervals)
        # anchors at the earliest current travel segment so its window credits
        # are not skipped out of existence by the chain start.
        # `_carry_facts` already loaded both canonical sources. Reuse those same
        # employee-scoped rows to find the anchor instead of issuing a second
        # interval read and another travel-segment read before the carry begins.
        # Intervals are bounded at the requested month end; anything starting
        # later cannot change whether this request has an earlier anchor.
        instants = [row.start_at for row in facts.intervals]
        instants.extend(
            segment.start_at
            for segments in facts.travel.segments_by_trip.values()
            for segment in segments
        )
        earliest = min(instants, default=None)
        anchor_local = earliest.astimezone(BERLIN) if earliest is not None else None
        anchor = month if anchor_local is None else date(anchor_local.year, anchor_local.month, 1)
    end = _period_end(month)

    def _facts(only: Callable[[BalanceLedgerEntry], bool]) -> tuple[LedgerFact, ...]:
        return tuple(LedgerFact(r.occurred_on, r.minutes, r.entry_type) for r in rows if only(r))

    if anchor >= month:
        # Nothing to chain: explicit rows (dated before this month's end,
        # which is the query window) apply to this month directly.
        return _facts(lambda r: r.occurred_on < end)
    carry = sum(r.minutes for r in rows if r.occurred_on < anchor)
    month_start = anchor
    while month_start < month:
        month_end = _period_end(month_start)
        # Carry-only window. A chained month's result is read for its CLOSING
        # BALANCE alone — its findings and input fingerprint are discarded — and
        # the closing balance is driven by the per-day allocation, which clips
        # every interval to its own day (engine `_portion_spans_for_day`). So only
        # the intervals and breaks that OVERLAP this month can move the carry, and
        # passing just those yields the identical value.
        #
        # It matters because the engine's cost is O(days x intervals): the tuple
        # grows with the history, so a 36-month chain paid ~530k interval scans to
        # produce 36 months of arithmetic (837 ms of the 1487 ms view). The
        # requested month itself is NOT narrowed — its findings are returned, so it
        # keeps the full prior-interval context the group and rest rules need.
        month_start_at = month_bounds(month_start)[0]
        in_month = [row for row in facts.intervals if row.end_at > month_start_at]
        in_month_breaks = [row for row in facts.breaks if row.end_at > month_start_at]
        # Explicit ledger events dated INSIDE a chain month apply exactly
        # once, in that month (authorized credits must not be double-counted).
        inner = _facts(lambda r, s=month_start, e=month_end: s <= r.occurred_on < e)
        carry_entry = LedgerFact(month_start - timedelta(days=1), carry, "FLEX_CARRY_FORWARD")
        # An un-lived month contributes NOTHING from its days. Re-running it to
        # its own end threads a PROJECTION forward as though it were ledger state,
        # so the next month opens with debt nobody has incurred (INV-08). A
        # partially elapsed month is therefore re-run only as far as the business
        # date, and for a clipped period the engine's closing balance IS the as-of
        # balance — so the carry keeps reading the one field it already read and
        # ``PeriodResult`` needs no new member.
        clip_end = None if as_of is None else min(month_end, as_of + timedelta(days=1))
        try:
            inputs = _month_inputs(
                session,
                employee,
                month_start,
                (carry_entry, *inner),
                facts=replace(facts, intervals=in_month, breaks=in_month_breaks),
                as_of=as_of,
            )
            if clip_end is None or clip_end > month_start:
                # ``None``: no clock, i.e. every direct caller and every test that
                # wants the period-end reading. ``clip_end > month_start`` covers
                # both the fully elapsed month (the ``min`` caps at ``month_end``,
                # so the inputs are byte-identical) and the partially elapsed one.
                period = inputs if clip_end is None else replace(inputs, period_end=clip_end)
                carry = calculate_period(period).closing_balance_minutes
            else:
                # The month has not started. ``calculate_period`` would refuse a
                # period whose end is not after its start, and no day of it has
                # been lived, so the opening passes through unchanged — but the
                # rows dated INSIDE it still apply here, because the engine is
                # never called for this month and no later month's window would
                # contain them. Dropping them would lose recorded minutes, which is
                # precisely what "every row applies exactly once" forbids.
                carry += sum(entry.minutes for entry in inner)
        except PolicyEditionUnavailable as exc:
            # Name the CHAIN month: the requested month may be perfectly covered
            # while an earlier month the balance chains through is not.
            raise PolicyEditionUnavailable(exc.policy_key, month, chain_month=month_start) from None
        except CalendarUnavailable as exc:
            # Same reasoning for the calendar: a chain month with no covering
            # edition gates every later month, so the refusal must name it.
            raise CalendarUnavailable(
                exc.calendar_key, exc.first_uncovered, chain_month=month_start
            ) from None
        month_start = month_end
    in_month = _facts(lambda r: month <= r.occurred_on < end)
    carry_entry = LedgerFact(month - timedelta(days=1), carry, "FLEX_CARRY_FORWARD")
    return (carry_entry, *in_month)


def calculate_employee_period(session: Session, employee: Employee, month: date) -> PeriodResult:
    """Resolve effective facts then call the pure engine; this performs no disclosure."""
    # Window: every row that can affect this month's own closing or any
    # earlier chain month (in-month rows belong to this month).
    ledger = session.scalars(
        select(BalanceLedgerEntry).where(
            BalanceLedgerEntry.employee_id == employee.id,
            BalanceLedgerEntry.occurred_on < _period_end(month),
        )
    ).all()
    # ONE fetch for the whole view: every chained month and the requested month
    # read the same facts, each narrowed to its own window by ``_month_inputs``.
    # Resolving them per month was pure duplication — 401 statements for a
    # 36-month history, over query plans that are sub-millisecond for the widest
    # shape.
    facts = _carry_facts(session, employee, month)
    # The view's ONE business-date reading (docs/ARCHITECTURE.md). Read here, not in the
    # engine — which is clock-free by contract — and not once per chain month,
    # which would let a midnight straddle make the carry and the requested month's
    # findings disagree about what "today" was inside a single view.
    as_of = local_date(now_berlin())
    opening = _carried_opening(session, employee, month, ledger, facts=facts, as_of=as_of)
    return calculate_period(
        _month_inputs(session, employee, month, opening, facts=facts, as_of=as_of)
    )
