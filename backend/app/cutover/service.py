"""Controlled one-time CUTOVER_SEED opening-balance writes.

Clean month-boundary cutover (INV-13): the validated legacy closing balance
becomes exactly ONE ``balance_ledger_entry`` row per employee, dated the last
day of month M-1 so the first Chronobeam month M opens with exactly those
minutes (``calculate_employee_period`` sums rows with ``occurred_on < M``).
The row is a ledger entry, not a fake historical interval or date (INV-02,
CUTOVER-T01). Duplicate protection is two-layered: a service
pre-check for the deterministic 409 plus a partial unique index that is
authoritative under races; the failure audit survives because rejections
return a result (no exception) and the savepoint isolates the failed insert.

The audit never records the balance amount (DP-LOG-001); provenance lives on
the ledger row itself (BE §5.8).
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.models import record_audit
from app.auth.principal import Principal
from app.balance.models import BalanceLedgerEntry
from app.errors import ErrorCode, PolicyEditionUnavailable, api_error
from app.identity.models import Employee
from app.policy.service import policy_version_for
from app.worktime.models import WorkInterval
from app.worktime.queries import month_bounds
from app.worktime.service import open_reviewed_month_range

ENTRY_TYPE_CUTOVER_SEED = "CUTOVER_SEED"
PREVIOUS_SYSTEM_EXCEL = "EXCEL"


@dataclass(frozen=True)
class CutoverSeedCreated:
    entry: BalanceLedgerEntry
    previous_period: str


@dataclass(frozen=True)
class CutoverSeedRejected:
    reason: str  # DUPLICATE_SEED | PRE_CUTOVER_FACTS
    message: str


CutoverSeedResult = CutoverSeedCreated | CutoverSeedRejected


def _nondisclosing_not_found() -> Exception:
    """Byte-identical 404 for unknown/inactive targets (no enumeration)."""
    return api_error(
        404,
        ErrorCode.NOT_FOUND,
        "The requested resource does not exist.",
    )


def _rejected(
    session: Session,
    principal: Principal,
    employee: Employee,
    month_key: str,
    outcome: str,
    reason: str,
    message: str,
) -> CutoverSeedRejected:
    record_audit(
        session,
        actor_principal=principal.subject,
        action="CUTOVER_SEED",
        outcome=outcome,
        object_type="BALANCE_LEDGER_ENTRY",
        employee_subject_id=employee.id,
        metadata={"month": month_key, "error_code": ErrorCode.CONFLICT_CUTOVER_SEED.value},
    )
    return CutoverSeedRejected(reason=reason, message=message)


def _write_opening_entry(
    session: Session,
    employee: Employee,
    *,
    entry_type: str,
    minutes: int,
    occurred_on: date,
    previous_period: str,
    policy_version_id: uuid.UUID,
    confirmed_by: str,
    source_entity_id: uuid.UUID | None = None,
    extra_metadata: Mapping[str, object] | None = None,
) -> BalanceLedgerEntry:
    """Add and flush exactly ONE opening-state ledger row.

    The CALLER owns the transaction boundary, deliberately: ``seed_opening_balance``
    wraps this in its own SAVEPOINT so a concurrent duplicate loses cleanly, while
    self-onboarding wraps it together with the schedule write so a failure cannot
    leave a half-onboarded employee (one savepoint, two writes).

    ``source_entity_type`` is always the seed's own type. For the seed row that is
    self-referential — exactly what the shipped row carries — and for a correction
    it is the link back to the seed it corrects (the append-only lineage; the
    original row is never touched).
    """
    metadata: dict[str, object] = {
        "previous_system": PREVIOUS_SYSTEM_EXCEL,
        "previous_period": previous_period,
        "confirmed_by": confirmed_by,
    }
    if extra_metadata is not None:
        metadata.update(extra_metadata)
    entry = BalanceLedgerEntry(
        employee_id=employee.id,
        work_period_id=None,
        occurred_on=occurred_on,
        entry_type=entry_type,
        minutes=minutes,
        source_entity_type=ENTRY_TYPE_CUTOVER_SEED,
        source_entity_id=source_entity_id,
        policy_version_id=policy_version_id,
        metadata_json=metadata,
    )
    session.add(entry)
    session.flush()
    return entry


def seed_opening_balance(
    session: Session,
    principal: Principal,
    *,
    employee_external_id: str,
    cutover_month: date,
    minutes: int,
) -> CutoverSeedResult:
    """Create the one authorized opening-balance seed, or reject atomically.

    ``cutover_month`` is the FIRST day of the first Chronobeam month M.
    Rejections never mutate canonical data; they do record an audit event.
    """
    employee = session.scalar(
        select(Employee).where(Employee.external_employee_id == employee_external_id)
    )
    if employee is None or not employee.active:
        raise _nondisclosing_not_found()

    month_key = f"{cutover_month.year:04d}-{cutover_month.month:02d}"

    duplicate = session.scalar(
        select(BalanceLedgerEntry.id).where(
            BalanceLedgerEntry.employee_id == employee.id,
            BalanceLedgerEntry.entry_type == ENTRY_TYPE_CUTOVER_SEED,
        )
    )
    if duplicate is not None:
        return _rejected(
            session,
            principal,
            employee,
            month_key,
            "REJECTED_DUPLICATE",
            "DUPLICATE_SEED",
            "An opening-balance cutover seed already exists for this employee.",
        )

    # Clean-cutover guard: any pre-M ledger row or pre-M interval fact (in any
    # version, including cross-midnight starts) makes an additive seed wrong.
    pre_ledger = session.scalar(
        select(func.count())
        .select_from(BalanceLedgerEntry)
        .where(
            BalanceLedgerEntry.employee_id == employee.id,
            BalanceLedgerEntry.occurred_on < cutover_month,
        )
    )
    month_start_at, _ = month_bounds(cutover_month)
    pre_intervals = session.scalar(
        select(func.count())
        .select_from(WorkInterval)
        .where(WorkInterval.employee_id == employee.id, WorkInterval.start_at < month_start_at)
    )
    if pre_ledger or pre_intervals:
        return _rejected(
            session,
            principal,
            employee,
            month_key,
            "REJECTED_PRE_CUTOVER_FACTS",
            "PRE_CUTOVER_FACTS",
            (
                "The employee already has pre-cutover ledger or interval facts; an opening "
                "seed would double-count or violate clean cutover."
            ),
        )

    previous_end = cutover_month - timedelta(days=1)  # last day of M-1
    previous_period = f"{previous_end.year:04d}-{previous_end.month:02d}"
    # Stamp month M's receiving policy version: the seed
    # minutes are a validated fact, not a re-derivation of Excel-era rules.
    try:
        policy = policy_version_for(session, cutover_month)
    except PolicyEditionUnavailable:
        # An out-of-range month is an input rejection, not a server fault;
        # nothing has been written or audited yet.
        raise api_error(
            422,
            ErrorCode.VALIDATION_INVALID_MONTH,
            "No installed working-time policy version covers this cutover month.",
        ) from None
    try:
        # Savepoint: a concurrent duplicate loses cleanly and only this
        # insert is rolled back; the outer transaction (with its audit)
        # still commits.
        with session.begin_nested():
            entry = _write_opening_entry(
                session,
                employee,
                entry_type=ENTRY_TYPE_CUTOVER_SEED,
                minutes=minutes,
                occurred_on=previous_end,
                previous_period=previous_period,
                policy_version_id=policy.id,
                confirmed_by=principal.subject,
            )
    except IntegrityError:
        # The SAVEPOINT rollback already expunges the never-committed
        # instance (proven by the live concurrent-seed race test); nothing
        # can re-flush it into the outer transaction.
        return _rejected(
            session,
            principal,
            employee,
            month_key,
            "REJECTED_DUPLICATE",
            "DUPLICATE_SEED",
            "An opening-balance cutover seed already exists for this employee.",
        )
    # EINV-15: the opening seed changes the balance carried into the cutover
    # month, so any review of that month is stale. The seed is dated on the last
    # day of M-1, and ``_carried_opening`` includes ledger rows dated before the
    # requested month's end — so M-1's own closing balance changes too and its
    # review must reopen with it.
    #
    # NOTE (owner decision, not a code gap): whether the seed SHOULD appear in
    # the archived pre-cutover month's closing at all is a cutover-semantics
    # question (docs/KNOWN_LIMITATIONS.md / INV-13: the seed is the opening state for M, and
    # historical months stay in the Excel archive). F3 does not change any
    # calculation here — it only keeps the review assertion honest.
    open_reviewed_month_range(session, employee.id, previous_end, cutover_month)
    session.flush()

    record_audit(
        session,
        actor_principal=principal.subject,
        action="CUTOVER_SEED",
        outcome="CREATED",
        object_type="BALANCE_LEDGER_ENTRY",
        object_id=entry.id,
        employee_subject_id=employee.id,
        metadata={"month": month_key},
    )
    return CutoverSeedCreated(entry=entry, previous_period=previous_period)
