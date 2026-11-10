"""Employee self-onboarding: the one-time cutover state.

An employee establishes their starting Chronobeam state **once**, at the start of
their first Chronobeam month M:

1. when Chronobeam starts for them (M);
2. their normal weekly working time, distributed across weekdays;
3. the balance carried from the previous Excel system.

It is deliberately a small extension of the EXISTING cutover model, not a new
accounting mechanism:

- the opening balance is the same ``CUTOVER_SEED`` ledger row the operator path
  writes (same entry type, same ``occurred_on = M-1`` convention, same
  provenance shape) — only the actor differs;
- the schedule is written through the existing effective-dated employee
  working-time writer (``identity.service.set_working_time``);
- the balance stays ledger-derived: nothing is materialized, and the month-to-
  month carry-forward is untouched.

Two concepts that must never be conflated:

- the **rollout boundary** is the institutional/deployment boundary for when
  Chronobeam is usable at all. It is configured, not chosen here, and onboarding
  never publishes or alters a policy edition.
- the **employee cutover month M** is that employee's first Chronobeam month.
  The employee selects it; that selection never touches the rollout boundary.

Scope boundaries worth stating because they are easy to get wrong:

- no external HR / master-data synchronisation: the employee's own working-time
  entry is the only source (there is no contract prerequisite);
- no historical Excel import, and no workbook-upload route anywhere;
- no ``version``/``supersedes_id``/current-head on the ledger: the opening state
  is append-only, and a correction is a bounded compensating DELTA row linked to
  the seed it corrects.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.absence.models import DayStatusEvent
from app.audit.models import record_audit
from app.balance.models import BalanceLedgerEntry
from app.calendar.models import CalendarVersion
from app.config import get_settings
from app.cutover.schemas import (
    MONTH_PATTERN,
    OnboardingBlockedReason,
    OnboardingCorrectionIn,
    OnboardingIn,
    OnboardingState,
    OnboardingStateOut,
    OpeningInUseReason,
    OpeningStateOut,
)
from app.cutover.service import ENTRY_TYPE_CUTOVER_SEED, _write_opening_entry
from app.db import lock_employee_facts
from app.disclosure.models import DisclosureGrant, SubmissionPackage
from app.errors import CalendarUnavailable, ErrorCode, PolicyEditionUnavailable, api_error
from app.identity.models import Employee
from app.identity.schemas import WorkingTimePut
from app.identity.service import _schedule_out, effective_schedule, set_working_time
from app.policy.models import PeriodCalculationSnapshot, PolicyVersion
from app.policy.service import POLICY_KEY, policy_version_for, require_calendar_coverage
from app.worktime.models import WorkInterval, WorkPeriod
from app.worktime.queries import month_bounds
from app.worktime.service import open_reviewed_month_range
from app.worktime.walltime import now_berlin

ENTRY_TYPE_CUTOVER_CORRECTION = "CUTOVER_SEED_CORRECTION"


def month_key(month: date) -> str:
    return f"{month.year:04d}-{month.month:02d}"


def parse_month(value: str) -> date:
    year, month = (int(part) for part in value.split("-"))
    return date(year, month, 1)


def next_month(month: date) -> date:
    return date(month.year + 1, 1, 1) if month.month == 12 else date(month.year, month.month + 1, 1)


def current_berlin_month() -> date:
    return now_berlin().date().replace(day=1)


def rollout_boundary() -> date | None:
    """The configured institutional rollout boundary, or ``None`` when unset.

    Unset is the honest default while the production rollout month is still
    undecided: the effective lower bound is then just the installed
    policy/calendar coverage. Nothing is invented here (docs/INVARIANTS.md).
    """
    raw = get_settings().rollout_boundary_month.strip()
    return parse_month(raw) if raw else None


def _invalid_month(message: str) -> Exception:
    return api_error(422, ErrorCode.VALIDATION_INVALID_MONTH, message)


def requested_month(value: str) -> date:
    """The month the employee ASKED about, validated at the API boundary.

    ``start_month`` arrives as a raw query string, so its shape is checked
    before parsing: ``parse_month`` alone raises ``ValueError`` on ``nope`` or
    ``2032-13`` and would surface as a 500 instead of the
    ``VALIDATION_INVALID_MONTH`` envelope every other month parameter returns.
    ``parse_month`` stays the trusting parser for stored rows.
    """
    if not re.fullmatch(MONTH_PATTERN, value):
        raise _invalid_month("That month is not a calendar month.")
    return parse_month(value)


@dataclass(frozen=True)
class OnboardingRejected:
    """A refused onboarding write, returned rather than raised.

    Returned so the rejection AUDIT survives: the write pair sits in a SAVEPOINT
    that a failure rolls back, and ``get_db`` commits whenever a route returns —
    so an exception would take the audit with it. Mirrors the operator path's
    ``CutoverSeedRejected``.
    """

    status_code: int
    code: ErrorCode
    message: str
    reason: str


def _reject(
    session: Session,
    employee: Employee,
    month: date,
    *,
    status_code: int,
    code: ErrorCode,
    reason: str,
    message: str,
) -> OnboardingRejected:
    record_audit(
        session,
        actor_principal=str(employee.idp_subject or ""),
        action="CUTOVER_SEED",
        outcome="REJECTED",
        object_type="BALANCE_LEDGER_ENTRY",
        employee_subject_id=employee.id,
        metadata={"month": month_key(month), "error_code": code.value},
    )
    return OnboardingRejected(status_code=status_code, code=code, message=message, reason=reason)


# --------------------------------------------------------------------------
# Coverage — bounded, never a month-by-month walk
# --------------------------------------------------------------------------


def _covered(session: Session, month: date) -> bool:
    """True when BOTH an ordinary-time edition and a calendar cover ``month``."""
    try:
        policy_version_for(session, month)
    except PolicyEditionUnavailable:
        return False
    try:
        require_calendar_coverage(session, month, next_month(month))
    except CalendarUnavailable:
        return False
    return True


def _edition_start_months(session: Session) -> list[date]:
    """Every month in which a policy OR calendar edition begins, ascending."""
    starts = set(
        session.scalars(
            select(PolicyVersion.effective_from).where(PolicyVersion.policy_key == POLICY_KEY)
        ).all()
    )
    starts.update(session.scalars(select(CalendarVersion.valid_from)).all())
    return sorted(month.replace(day=1) for month in starts)


def default_start_month(session: Session, current: date) -> str | None:
    """Earliest month at/after ``current`` covered by BOTH policy and calendar.

    BOUNDED by construction — no month-by-month search. The candidate set is
    ``current`` plus every edition start month at/after it, and each candidate is
    tested with the real resolvers.

    Completeness: let ``m`` be the earliest covered month >= ``current``. If
    ``m > current``, take the policy edition and the calendar edition that cover
    ``m``, starting at ``p`` and ``c`` respectively. Both are <= ``m``, and each
    edition covers a CONTIGUOUS month range, so if both ``p < m`` and ``c < m``
    then ``m-1`` (which is still >= ``current``) would also be covered — against
    minimality. So at least one of them starts at ``m``: every earliest covered
    month is either ``current`` or an edition start month.

    ``None`` means no covered month exists yet; the caller then shows the blocked
    state and preselects nothing.
    """
    candidates = {current, *(start for start in _edition_start_months(session) if start >= current)}
    for candidate in sorted(candidates):
        if _covered(session, candidate):
            return month_key(candidate)
    return None


# --------------------------------------------------------------------------
# The opening state — read from the append-only facts
# --------------------------------------------------------------------------


def _opening_rows(
    session: Session, employee_id: uuid.UUID
) -> tuple[BalanceLedgerEntry | None, list[BalanceLedgerEntry]]:
    """The single seed row and every correction delta, in creation order."""
    seed = session.scalar(
        select(BalanceLedgerEntry).where(
            BalanceLedgerEntry.employee_id == employee_id,
            BalanceLedgerEntry.entry_type == ENTRY_TYPE_CUTOVER_SEED,
        )
    )
    if seed is None:
        return None, []
    corrections = list(
        session.scalars(
            select(BalanceLedgerEntry)
            .where(
                BalanceLedgerEntry.employee_id == employee_id,
                BalanceLedgerEntry.entry_type == ENTRY_TYPE_CUTOVER_CORRECTION,
            )
            .order_by(BalanceLedgerEntry.created_at, BalanceLedgerEntry.id)
        ).all()
    )
    return seed, corrections


def _effective_total(seed: BalanceLedgerEntry, corrections: list[BalanceLedgerEntry]) -> int:
    """Seed + every delta. The ledger sum stays the accounting mechanism."""
    return seed.minutes + sum(row.minutes for row in corrections)


def opening_revision(corrections: list[BalanceLedgerEntry]) -> int:
    """Optimistic token DERIVED from the facts: seed revision 1, plus corrections.

    No fake entity version is stored: ``balance_ledger_entry`` is not a versioned
    entity, and the append-only correction rows are the real lineage.
    """
    return 1 + len(corrections)


def opening_state_out(
    seed: BalanceLedgerEntry | None, corrections: list[BalanceLedgerEntry]
) -> OpeningStateOut | None:
    if seed is None:
        return None
    metadata = seed.metadata_json or {}
    return OpeningStateOut(
        cutover_month=month_key(seed.occurred_on + timedelta(days=1)),
        minutes=_effective_total(seed, corrections),
        seed_minutes=seed.minutes,
        occurred_on=seed.occurred_on,
        previous_period=str(metadata.get("previous_period", "")),
        confirmed_by=str(metadata.get("confirmed_by", "")),
        revision=opening_revision(corrections),
        corrected_at=corrections[-1].created_at if corrections else None,
    )


# --------------------------------------------------------------------------
# The in-use window (D6) — self-correction closes at the first real use
# --------------------------------------------------------------------------


def _has_facts_in_month(session: Session, employee_id: uuid.UUID, month: date) -> bool:
    """Facts already recorded INSIDE ``month`` (surface warning only, D10)."""
    start_at, end_at = month_bounds(month)
    if session.scalar(
        select(func.count())
        .select_from(WorkInterval)
        .where(
            WorkInterval.employee_id == employee_id,
            WorkInterval.start_at < end_at,
            WorkInterval.end_at > start_at,
        )
    ):
        return True
    if session.scalar(
        select(func.count())
        .select_from(DayStatusEvent)
        .where(
            DayStatusEvent.employee_id == employee_id,
            DayStatusEvent.day >= month,
            DayStatusEvent.day < next_month(month),
        )
    ):
        return True
    return bool(
        session.scalar(
            select(func.count())
            .select_from(BalanceLedgerEntry)
            .where(
                BalanceLedgerEntry.employee_id == employee_id,
                BalanceLedgerEntry.occurred_on >= month,
                BalanceLedgerEntry.occurred_on < next_month(month),
            )
        )
    )


def in_use_by_review(session: Session, employee_id: uuid.UUID, cutover_month: date) -> bool:
    """A review/close of M or later, or a frozen calculation for such a period.

    ``period_calculation_snapshot`` carries its own ``frozen`` flag, but ANY
    snapshot on a period >= M is treated as in use rather than only
    ``frozen = True``. That is a deliberate superset: the only writer
    (``disclosure.service``) sets ``frozen = True``, so the extra strictness is
    unreachable in practice, and a frozen result means the month's outcome was
    already recorded.
    """
    any_period = select(WorkPeriod.id).where(
        WorkPeriod.employee_id == employee_id,
        WorkPeriod.period_month >= cutover_month,
    )
    if session.scalar(
        select(func.count())
        .select_from(WorkPeriod)
        .where(
            WorkPeriod.employee_id == employee_id,
            WorkPeriod.period_month >= cutover_month,
            WorkPeriod.status != "OPEN",
        )
    ):
        return True
    return bool(
        session.scalar(
            select(func.count())
            .select_from(PeriodCalculationSnapshot)
            .where(PeriodCalculationSnapshot.work_period_id.in_(any_period))
        )
    )


def in_use_by_disclosure(session: Session, employee_id: uuid.UUID, cutover_month: date) -> bool:
    """A grant or a submitted package whose window reaches M or later.

    The overlap is conservative in the safe direction: ANY package or grant
    reaching M or beyond closes the window, so a granted month cannot be missed
    by an off-by-one in the stored inclusive/exclusive convention.
    """
    if session.scalar(
        select(func.count())
        .select_from(DisclosureGrant)
        .where(
            DisclosureGrant.employee_id == employee_id,
            DisclosureGrant.period_end >= cutover_month,
        )
    ):
        return True
    return bool(
        session.scalar(
            select(func.count())
            .select_from(SubmissionPackage)
            .where(
                SubmissionPackage.employee_id == employee_id,
                SubmissionPackage.period_end >= cutover_month,
            )
        )
    )


def opening_state_in_use(
    session: Session, employee_id: uuid.UUID, cutover_month: date
) -> OpeningInUseReason | None:
    """The first reason the opening state is already in use, or ``None``.

    The window closes permanently at the first real use, so the order of these
    checks is the order the UI explains them in. Everything is scoped to ``>= M``:
    the opening state governs M and every month after it, and nothing before it
    depends on the value.

    Deliberately NOT blocking: a pending ``access_request``. It reveals no data —
    the GRANT is the access event (docs/SECURITY.md) — so an unanswered request must not
    close the employee's own correction window.
    """
    # Anything from M onward depends on the opening state. Intervals are matched
    # by OVERLAP, not by start: a night shift that starts on M-1's last day and
    # ends inside M already depends on the carried value, and a start-based
    # predicate would let it escape the window (the same shape as
    # ``_has_facts_in_month``).
    month_start_at, _ = month_bounds(cutover_month)
    if session.scalar(
        select(func.count())
        .select_from(WorkInterval)
        .where(WorkInterval.employee_id == employee_id, WorkInterval.end_at > month_start_at)
    ):
        return OpeningInUseReason.IN_USE_BY_FACTS
    if session.scalar(
        select(func.count())
        .select_from(DayStatusEvent)
        .where(DayStatusEvent.employee_id == employee_id, DayStatusEvent.day >= cutover_month)
    ):
        return OpeningInUseReason.IN_USE_BY_FACTS
    if session.scalar(
        select(func.count())
        .select_from(BalanceLedgerEntry)
        .where(
            BalanceLedgerEntry.employee_id == employee_id,
            BalanceLedgerEntry.occurred_on >= cutover_month,
        )
    ):
        return OpeningInUseReason.IN_USE_BY_FACTS

    # Disclosure is checked BEFORE review: the grant flow freezes a snapshot for
    # the granted month, so a shared month would otherwise always be explained as
    # "reviewed" — the right window, the wrong reason.
    if in_use_by_disclosure(session, employee_id, cutover_month):
        return OpeningInUseReason.IN_USE_BY_DISCLOSURE
    if in_use_by_review(session, employee_id, cutover_month):
        return OpeningInUseReason.IN_USE_BY_REVIEW
    return None


# --------------------------------------------------------------------------
# The read
# --------------------------------------------------------------------------


def _blocked_reason(session: Session, month: date) -> OnboardingBlockedReason | None:
    """Why ``month`` cannot host a cutover, in the order the employee hits them.

    The current-month rule and the configured rollout boundary share
    ``ROLLOUT_BOUNDARY``: both mean "Chronobeam is not usable that early", and the
    response names the month either way. Policy and calendar coverage are
    separate because they are separate operator fixes.
    """
    lower_bound = current_berlin_month()
    boundary = rollout_boundary()
    if boundary is not None:
        lower_bound = max(lower_bound, boundary)
    if month < lower_bound:
        return OnboardingBlockedReason.ROLLOUT_BOUNDARY
    try:
        policy_version_for(session, month)
    except PolicyEditionUnavailable:
        return OnboardingBlockedReason.POLICY_EDITION_MISSING
    try:
        require_calendar_coverage(session, month, next_month(month))
    except CalendarUnavailable:
        return OnboardingBlockedReason.CALENDAR_MISSING
    return None


def onboarding_state(
    session: Session, employee: Employee, *, start_month: str | None = None
) -> OnboardingStateOut:
    """Month-aware onboarding state. ``state`` always describes the named month.

    There is deliberately no "unselected month" verdict: when ``start_month`` is
    omitted the read describes the current Berlin month, and the response always
    says which month that was. ``POST`` remains the authoritative validator — this
    verdict is advisory and is never trusted as a precondition (no TOCTOU trust).
    """
    requested = requested_month(start_month) if start_month else current_berlin_month()
    seed, corrections = _opening_rows(session, employee.id)
    opening = opening_state_out(seed, corrections)
    in_use = (
        opening_state_in_use(session, employee.id, parse_month(opening.cutover_month))
        if opening is not None
        else None
    )
    if opening is not None:
        state = OnboardingState.COMPLETED
        blocked = None
    else:
        blocked = _blocked_reason(session, requested)
        state = OnboardingState.BLOCKED if blocked is not None else OnboardingState.READY

    # The schedule this response reports is resolved at the month the ANSWER is
    # about: the requested month before onboarding (that is the prefill for the
    # form), and the employee's ACTUAL cutover month once it exists. Resolving it
    # at `requested` after onboarding would report whatever arrangement happens
    # to be effective today — so an employee who started in a FUTURE month would
    # reopen Profile and be shown an older arrangement, or "no days set", as
    # their own onboarding result.
    schedule_month = parse_month(opening.cutover_month) if opening is not None else requested
    schedule = effective_schedule(session, employee.id, schedule_month)
    return OnboardingStateOut(
        state=state,
        month=month_key(requested),
        default_start_month=default_start_month(session, current_berlin_month()),
        opening=opening,
        blocked_reason=blocked,
        blocked_month=month_key(requested) if blocked is not None else None,
        has_facts_in_month=_has_facts_in_month(session, employee.id, requested),
        correctable=opening is not None and in_use is None,
        in_use_reason=in_use,
        schedule=_schedule_out(schedule) if schedule is not None else None,
    )


# --------------------------------------------------------------------------
# Establish — one transaction, two writers, one savepoint
# --------------------------------------------------------------------------


def establish_opening_state(
    session: Session, employee: Employee, payload: OnboardingIn
) -> OnboardingStateOut | OnboardingRejected:
    """Create the one-time opening state, or refuse atomically.

    Order is load-bearing. ``lock_employee_facts`` is taken FIRST so every read
    below happens under the lock, then every guard runs BEFORE any write, and only
    then are the two writes made inside ONE savepoint.

    The savepoint is not decoration. ``set_working_time`` flushes internally and
    ``get_db`` commits whenever a route RETURNS — including a refusal — so a route
    that flushed the schedule and then returned a rejection would commit a
    half-onboarded employee. One savepoint means any failure (including the
    duplicate-race ``IntegrityError``) rolls back BOTH writes, while the rejection
    audit, written outside it, still commits.
    """
    lock_employee_facts(session, employee.id)

    month = parse_month(payload.start_month)

    # --- guards, all BEFORE any write -------------------------------------
    lower_bound = current_berlin_month()
    boundary = rollout_boundary()
    if boundary is not None:
        lower_bound = max(lower_bound, boundary)
    if month < lower_bound:
        if boundary is None:
            raise _invalid_month(
                "A Chronobeam start month cannot be earlier than the current month."
            )
        raise _invalid_month(
            "A Chronobeam start month cannot be earlier than the current month "
            "or the configured rollout boundary."
        )

    try:
        policy = policy_version_for(session, month)
    except PolicyEditionUnavailable:
        raise _invalid_month(
            "No installed working-time policy version covers this start month."
        ) from None
    try:
        require_calendar_coverage(session, month, next_month(month))
    except CalendarUnavailable:
        raise api_error(
            409,
            ErrorCode.CALENDAR_BLOCKED,
            "No institutional calendar edition covers this start month.",
        ) from None

    seed, _corrections = _opening_rows(session, employee.id)
    if seed is not None:
        return _reject(
            session,
            employee,
            month,
            status_code=409,
            code=ErrorCode.CONFLICT_CUTOVER_SEED,
            reason="DUPLICATE_SEED",
            message="An opening-balance cutover seed already exists for this employee.",
        )

    # Clean cutover: a pre-M ledger row or pre-M interval fact (in any version,
    # including cross-midnight starts) makes an additive opening balance wrong.
    # Mirrors the operator guard exactly.
    month_start_at, _ = month_bounds(month)
    if session.scalar(
        select(func.count())
        .select_from(BalanceLedgerEntry)
        .where(
            BalanceLedgerEntry.employee_id == employee.id,
            BalanceLedgerEntry.occurred_on < month,
        )
    ) or session.scalar(
        select(func.count())
        .select_from(WorkInterval)
        .where(WorkInterval.employee_id == employee.id, WorkInterval.start_at < month_start_at)
    ):
        return _reject(
            session,
            employee,
            month,
            status_code=409,
            code=ErrorCode.CONFLICT_CUTOVER_SEED,
            reason="PRE_CUTOVER_FACTS",
            message=(
                "The employee already has pre-cutover ledger or interval facts; an opening "
                "balance would double-count or violate clean cutover."
            ),
        )

    # A month already reviewed or disclosed cannot host a first cutover: its
    # result would change under facts that were already frozen. Facts merely
    # RECORDED inside M do NOT block (D10) — they are surfaced before confirmation
    # and the opening state recalculates them.
    if in_use_by_disclosure(session, employee.id, month):
        return _reject(
            session,
            employee,
            month,
            status_code=409,
            code=ErrorCode.CONFLICT_CUTOVER_SEED,
            reason=OpeningInUseReason.IN_USE_BY_DISCLOSURE.value,
            message="This month has already been shared.",
        )
    if in_use_by_review(session, employee.id, month):
        return _reject(
            session,
            employee,
            month,
            status_code=409,
            code=ErrorCode.CONFLICT_CUTOVER_SEED,
            reason=OpeningInUseReason.IN_USE_BY_REVIEW.value,
            message="This month has already been reviewed or closed.",
        )

    # --- the two writes, ONE savepoint ------------------------------------
    previous_end = month - timedelta(days=1)  # last day of M-1
    previous_period = month_key(previous_end)
    try:
        with session.begin_nested():
            # The EXISTING effective-dated employee schedule writer: no second
            # schedule mechanism, and no contract prerequisite (D1).
            set_working_time(
                session,
                employee,
                WorkingTimePut(
                    valid_from=month,
                    weekly_required_minutes=payload.weekly_required_minutes,
                    mon_required_minutes=payload.mon_required_minutes,
                    tue_required_minutes=payload.tue_required_minutes,
                    wed_required_minutes=payload.wed_required_minutes,
                    thu_required_minutes=payload.thu_required_minutes,
                    fri_required_minutes=payload.fri_required_minutes,
                    sat_required_minutes=payload.sat_required_minutes,
                    sun_required_minutes=payload.sun_required_minutes,
                ),
            )
            entry = _write_opening_entry(
                session,
                employee,
                entry_type=ENTRY_TYPE_CUTOVER_SEED,
                minutes=payload.opening_balance_minutes,
                occurred_on=previous_end,
                previous_period=previous_period,
                policy_version_id=policy.id,
                confirmed_by=str(employee.idp_subject or ""),
            )
    except IntegrityError:
        # The authoritative duplicate guard is the existing partial unique index;
        # the SAVEPOINT rollback already undid the schedule write too.
        return _reject(
            session,
            employee,
            month,
            status_code=409,
            code=ErrorCode.CONFLICT_CUTOVER_SEED,
            reason="DUPLICATE_SEED",
            message="An opening-balance cutover seed already exists for this employee.",
        )

    # EINV-15: the opening state changes the balance carried into M, so any review
    # of the months it governs is stale. Mirrors the operator seed exactly.
    open_reviewed_month_range(session, employee.id, previous_end, month)
    record_audit(
        session,
        actor_principal=str(employee.idp_subject or ""),
        action="CUTOVER_SEED",
        outcome="CREATED",
        object_type="BALANCE_LEDGER_ENTRY",
        object_id=entry.id,
        employee_subject_id=employee.id,
        metadata={"month": month_key(month)},
    )
    session.flush()
    return onboarding_state(session, employee, start_month=month_key(month))


# --------------------------------------------------------------------------
# Correct — append-only, bounded to the pre-use window
# --------------------------------------------------------------------------


def correct_opening_state(
    session: Session, employee: Employee, payload: OnboardingCorrectionIn
) -> OnboardingStateOut | OnboardingRejected:
    """Correct the carried balance while the opening state is still unused.

    The row written is a DELTA, not the corrected total:

        delta = corrected_total - effective_current_total

    ``_carried_opening`` sums every ledger row dated before the requested month
    and the engine sums every opening entry, so a second row carrying the TOTAL
    would double-count. A delta nets correctly with ZERO read-path change, and the
    corrected total is still self-describing for an auditor because it is recorded
    in the row's own metadata. The original seed is never mutated or deleted.

    Concurrency is why the order below is load-bearing. Without the lock two
    requests both read revision 1, both compute a delta from the same total, and
    both append — the second delta is wrong and the effective total is corrupted.
    Taking ``lock_employee_facts`` BEFORE the reread makes the second request
    block; when it acquires the lock its reread sees revision 2, so its
    ``expected_revision`` no longer matches and it is refused. Exactly one wins.

    A correction that restates the same total is accepted: it appends a zero
    delta, which is honest (the employee did submit a correction) and harmless to
    the arithmetic. Refusing it would be an invented rule.
    """
    lock_employee_facts(session, employee.id)

    # Reread ONLY under the lock: seed, every delta, the effective total, the
    # revision and the in-use state.
    seed, corrections = _opening_rows(session, employee.id)
    if seed is None:
        return _reject(
            session,
            employee,
            current_berlin_month(),
            status_code=409,
            code=ErrorCode.CONFLICT_CUTOVER_SEED,
            reason="NO_OPENING_STATE",
            message="There is no opening state to correct yet.",
        )

    cutover_month = parse_month(month_key(seed.occurred_on + timedelta(days=1)))

    # A closed window is more fundamental than a stale revision: no revision is
    # meaningful once the opening state is in use.
    in_use = opening_state_in_use(session, employee.id, cutover_month)
    if in_use is not None:
        return _reject(
            session,
            employee,
            cutover_month,
            status_code=409,
            code=ErrorCode.CONFLICT_CUTOVER_SEED,
            reason=in_use.value,
            message="The opening state is already in use; self-service correction is closed.",
        )

    revision = opening_revision(corrections)
    if payload.expected_revision != revision:
        return _reject(
            session,
            employee,
            cutover_month,
            status_code=409,
            code=ErrorCode.CONFLICT_VERSION,
            reason="STALE_REVISION",
            message="The carried balance changed since you loaded it; reload and try again.",
        )

    delta = payload.opening_balance_minutes - _effective_total(seed, corrections)
    metadata = seed.metadata_json or {}
    entry = _write_opening_entry(
        session,
        employee,
        entry_type=ENTRY_TYPE_CUTOVER_CORRECTION,
        minutes=delta,
        # Same date as the seed it corrects: the correction belongs to the opening
        # state, so it must NOT land inside M and trip the in-use predicate.
        occurred_on=seed.occurred_on,
        previous_period=str(metadata.get("previous_period", month_key(seed.occurred_on))),
        policy_version_id=seed.policy_version_id,
        confirmed_by=str(employee.idp_subject or ""),
        # The append-only lineage: the correction points at the seed it corrects.
        source_entity_id=seed.id,
        extra_metadata={
            "corrected_total_minutes": payload.opening_balance_minutes,
            "reason": payload.reason,
        },
    )
    # EINV-15: the corrected total changes the balance carried into M and every
    # month after it, so their reviews are stale.
    open_reviewed_month_range(session, employee.id, seed.occurred_on, cutover_month)
    record_audit(
        session,
        actor_principal=str(employee.idp_subject or ""),
        action="CUTOVER_SEED",
        outcome="CORRECTED",
        object_type="BALANCE_LEDGER_ENTRY",
        object_id=entry.id,
        employee_subject_id=employee.id,
        metadata={"month": month_key(cutover_month)},
    )
    session.flush()
    return onboarding_state(session, employee, start_month=month_key(cutover_month))


__all__ = [
    "ENTRY_TYPE_CUTOVER_CORRECTION",
    "OnboardingRejected",
    "correct_opening_state",
    "current_berlin_month",
    "default_start_month",
    "establish_opening_state",
    "in_use_by_disclosure",
    "in_use_by_review",
    "month_key",
    "next_month",
    "onboarding_state",
    "opening_revision",
    "opening_state_in_use",
    "opening_state_out",
    "parse_month",
    "rollout_boundary",
]
