"""Identity/profile service — effective-dated queries and preferences.

There are no master-data write endpoints yet: contracts and
schedules are inserted only by the dev seed (source=ADMIN_SEED) until the
master-data integration phase.

Effective dating is **permanent shadowing**: the row with the greatest
``valid_from <= at`` owns that day, and its own ``valid_to`` — INCLUSIVE, with
boundary tests — decides whether ANYTHING governs it. A later-starting row
supersedes its predecessor permanently, so an expired successor leaves the day
with no version rather than resurrecting the arrangement it replaced. One
predicate (``_governs``) serves the point resolvers and the per-day resolver, so
they cannot drift apart.

Identity binding: the application API may set ``employee.idp_subject`` only
when creating a row. Changing an existing binding is an operator operation
(``app.identity_admin``); ``set_identity_binding`` holds its logic and
``lock_identity_administration`` is the fixed lock every such invocation takes
first.
"""

import uuid
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from app.audit.models import record_audit
from app.auth.session_models import AuthSession
from app.db import lock_employee_facts
from app.enums import MasterDataSource
from app.errors import ErrorCode, api_error
from app.identity.models import (
    Employee,
    EmployeePreference,
    EmploymentContract,
    IdentityBindingChange,
    WorkScheduleVersion,
)
from app.identity.schemas import (
    ContractOut,
    EmployeeOut,
    NormalDayTemplateInterval,
    PreferencesOut,
    ProfileOut,
    ScheduleOut,
    WorkingTimePut,
)
from app.worktime.service import open_reviewed_month_range
from app.worktime.walltime import now_berlin
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, object_session

IDENTITY_ADMIN_ACTOR = "IDENTITY_CLI"
ACTION_IDENTITY_REMAP = "ADMIN_IDENTITY_REMAP"

# Fixed advisory-lock key: EVERY identity-binding invocation takes this first,
# so the operator protocol is globally serialized. Collisions only
# over-serialize; remaps are rare enough that global serialization is simpler
# than a two-lock ordering protocol.
_IDENTITY_ADMIN_LOCK_KEY = "chronobeam_identity_admin"

# Fixed advisory-lock key for the bulk master-data import. Taken FIRST by every
# invocation, before any employee is resolved, then the per-employee fact lock —
# the same total order ``lock_identity_administration`` uses, so the two operator
# paths cannot interleave a resolve and a mutate.
_MASTER_DATA_ADMIN_LOCK_KEY = "chronobeam_master_data_admin"

WEEKDAY_MINUTE_FIELDS = (
    "mon_required_minutes",
    "tue_required_minutes",
    "wed_required_minutes",
    "thu_required_minutes",
    "fri_required_minutes",
    "sat_required_minutes",
    "sun_required_minutes",
)


def resolve_employee(session: Session, employee_id: uuid.UUID) -> Employee | None:
    return session.get(Employee, employee_id)


def _governs(row: Any | None, at: date) -> bool:
    """Does ``row`` actually govern ``at``?

    Effective dating here is **permanent shadowing**: the row with the greatest
    ``valid_from <= at`` owns that day (``version`` breaking a tie), and its own
    ``valid_to`` decides whether ANYTHING governs it. A later-starting row
    therefore supersedes its predecessor permanently — if it expires, the day has
    no version rather than falling back to an earlier one.

    This is the rule the rest of the codebase already assumes: the master-data
    import documents a governed range as ``[valid_from, min(valid_to,
    next_valid_from - 1)]`` and shadows earlier rows from the later row's start
    (``master_data_admin.py``), ``_effective_range_end`` reports the range that
    way, and ``_reopen_schedule_months`` reopens exactly that range. The
    alternative — a per-row ``valid_to >= at`` predicate, which lets a superseded
    row RESUME once its successor expires — made the arrangement chain contradict
    the schedule the ledger actually used (review R29): ``GET /me/profile?at``
    returned a row whose own reported ``valid_to`` said it had already ended.

    One predicate, three callers, so the point resolvers and the per-day resolver
    cannot drift apart.
    """
    if row is None:
        return False
    return row.valid_to is None or row.valid_to >= at


def effective_contract(
    session: Session, employee_id: uuid.UUID, at: date
) -> EmploymentContract | None:
    """The contract governing ``at`` under permanent shadowing (see ``_governs``)."""
    latest = session.scalar(
        select(EmploymentContract)
        .where(
            EmploymentContract.employee_id == employee_id,
            EmploymentContract.valid_from <= at,
        )
        .order_by(EmploymentContract.valid_from.desc(), EmploymentContract.version.desc())
        .limit(1)
    )
    return latest if _governs(latest, at) else None


def effective_schedule(
    session: Session, employee_id: uuid.UUID, at: date
) -> WorkScheduleVersion | None:
    """The schedule governing ``at`` under permanent shadowing (see ``_governs``)."""
    latest = session.scalar(
        select(WorkScheduleVersion)
        .where(
            WorkScheduleVersion.employee_id == employee_id,
            WorkScheduleVersion.valid_from <= at,
        )
        .order_by(WorkScheduleVersion.valid_from.desc(), WorkScheduleVersion.version.desc())
        .limit(1)
    )
    return latest if _governs(latest, at) else None


def _governing_by_employee(
    session: Session, model: type, employee_ids: Sequence[uuid.UUID], at: date
) -> dict[uuid.UUID, Any]:
    """The row governing ``at`` for each employee, in ONE statement.

    The same rule as the point resolvers — greatest ``valid_from <= at`` owns the
    day, ``version`` breaking the tie, and ``_governs`` deciding whether it
    actually covers it — resolved by SQL order rather than by one ``LIMIT 1`` per
    employee. Only rows that can win are fetched (``valid_from <= at``), and
    ``_governs`` deliberately still sees the expired latest row, because under
    permanent shadowing an expired successor leaves the day UNGOVERNED rather than
    resurrecting its predecessor.

    This exists because the admin users list asks the question for a whole page at
    once; asking per row would be one statement per user.
    """
    if not employee_ids:
        return {}
    rows = session.scalars(
        select(model)
        .where(model.employee_id.in_(employee_ids), model.valid_from <= at)
        .order_by(model.employee_id, model.valid_from.desc(), model.version.desc())
    ).all()
    governing: dict[uuid.UUID, Any] = {}
    for row in rows:
        governing.setdefault(row.employee_id, row)
    return governing


def employees_with_effective_contract(
    session: Session, employee_ids: Sequence[uuid.UUID], at: date
) -> set[uuid.UUID]:
    """``effective_contract(...) is not None`` for many employees, in one statement."""
    return {
        employee_id
        for employee_id, row in _governing_by_employee(
            session, EmploymentContract, employee_ids, at
        ).items()
        if _governs(row, at)
    }


def employees_with_effective_schedule(
    session: Session, employee_ids: Sequence[uuid.UUID], at: date
) -> set[uuid.UUID]:
    """``effective_schedule(...) is not None`` for many employees, in one statement."""
    return {
        employee_id
        for employee_id, row in _governing_by_employee(
            session, WorkScheduleVersion, employee_ids, at
        ).items()
        if _governs(row, at)
    }


def _resolve_by_day(rows: Sequence[Any], start: date, end: date) -> dict[date, Any]:
    """Pick, for every day of ``[start, end)``, the version covering that day.

    The rule is exactly ``effective_contract``/``effective_schedule``'s —
    permanent shadowing, applied to rows already fetched for the whole window:
    the greatest ``valid_from <= day`` owns the day (``version`` breaking a tie),
    and ``_governs`` decides whether it actually covers it. ``rows`` must be
    ordered by ``(valid_from DESC, version DESC)``, which is what makes
    ``next(...)`` the same winner the point resolvers' ``LIMIT 1`` returns.

    The row type is erased deliberately: contract and schedule versions share the
    effective-dating shape without a common base class, and a ``Protocol`` cannot
    be satisfied by SQLAlchemy's class-level ``Mapped[...]`` descriptors. The two
    public wrappers below keep their precise return types.
    """
    resolved: dict[date, Any] = {}
    day = start
    while day < end:
        latest = next((row for row in rows if row.valid_from <= day), None)
        resolved[day] = latest if _governs(latest, day) else None
        day += timedelta(days=1)
    return resolved


def effective_contracts_by_day(
    session: Session,
    employee_id: uuid.UUID,
    start: date,
    end: date,
    *,
    versions: Sequence[EmploymentContract] | None = None,
) -> dict[date, EmploymentContract | None]:
    """``effective_contract`` for every day of ``[start, end)``, in ONE statement.

    Every row that could win for some day satisfies ``valid_from <= day < end``,
    so the window predicate is ``valid_from < end`` — and deliberately NOTHING
    about ``valid_to``. Under permanent shadowing a row can be the LATEST for a
    day without covering it, and that is exactly what makes the day ungoverned:
    excluding an expired row would resurrect the arrangement it superseded. The
    old predicate added ``valid_to >= start`` on the reasoning that a winner must
    cover the day, which was true only of the per-row rule this replaced.

    This exists because the month view carries a balance forward month by month
    and asked per DAY for every month it walked: ~90 statements per month, ~3300
    for a 36-month history, resolving 1096 distinct days. The point resolver stays
    the right shape for a single date; a range wants this one.
    """
    rows = versions
    if rows is None:
        rows = session.scalars(
            select(EmploymentContract)
            .where(
                EmploymentContract.employee_id == employee_id,
                EmploymentContract.valid_from < end,
            )
            .order_by(EmploymentContract.valid_from.desc(), EmploymentContract.version.desc())
        ).all()
    return _resolve_by_day(rows, start, end)


def effective_schedules_by_day(
    session: Session,
    employee_id: uuid.UUID,
    start: date,
    end: date,
    *,
    versions: Sequence[WorkScheduleVersion] | None = None,
) -> dict[date, WorkScheduleVersion | None]:
    """``effective_schedule`` for every day of ``[start, end)``, in ONE statement.

    Same rule and same window predicate as ``effective_contracts_by_day``; see its
    docstring for why the range form exists.
    """
    rows = versions
    if rows is None:
        rows = session.scalars(
            select(WorkScheduleVersion)
            .where(
                WorkScheduleVersion.employee_id == employee_id,
                WorkScheduleVersion.valid_from < end,
            )
            .order_by(WorkScheduleVersion.valid_from.desc(), WorkScheduleVersion.version.desc())
        ).all()
    return _resolve_by_day(rows, start, end)


def _schedule_out(schedule: WorkScheduleVersion) -> ScheduleOut:
    """The API view of one effective-dated schedule row.

    ``valid_to`` is the LAST LOCAL DAY this row governs, not the raw column.
    Every row the application writes carries ``valid_to = NULL`` (EMPLOYEE rows
    always; MASTER imports usually), and ``effective_schedule`` lets it win from
    its ``valid_from`` until the next later-starting row begins — so the column
    alone reports "open-ended" for a superseded arrangement and the LATER
    arrangement is undiscoverable from the one in force. Reporting the effective
    end here makes the field mean what its name says, makes ranges contiguous and
    therefore enumerable through ``GET /me/profile?at=<each valid_from>``, and
    matches what the WRITE path already assumes (``_reopen_schedule_months``
    reopens exactly ``[valid_from, _schedule_range_end]``).

    A declared ``valid_to`` that ends EARLIER than the next row's start is kept:
    that row genuinely stops being effective there (``effective_schedule``'s
    ``valid_to >= at`` predicate), so the reported end is the narrower of the two.
    No column is written — this is a read-time projection only.

    The session comes from the instance (``object_session``) instead of a new
    parameter: every call site already holds a persistent row that was loaded or
    flushed in the session it is being projected for.
    """
    session = object_session(schedule)
    declared = schedule.valid_to
    derived = (
        _schedule_range_end(session, schedule.employee_id, schedule.valid_from)
        if session is not None
        else None
    )
    ends = [end for end in (declared, derived) if end is not None]
    return ScheduleOut(
        valid_from=schedule.valid_from,
        valid_to=min(ends) if ends else None,
        weekly_required_minutes=schedule.weekly_required_minutes,
        mon_required_minutes=schedule.mon_required_minutes,
        tue_required_minutes=schedule.tue_required_minutes,
        wed_required_minutes=schedule.wed_required_minutes,
        thu_required_minutes=schedule.thu_required_minutes,
        fri_required_minutes=schedule.fri_required_minutes,
        sat_required_minutes=schedule.sat_required_minutes,
        sun_required_minutes=schedule.sun_required_minutes,
        mon_typical_start=schedule.mon_typical_start,
        mon_typical_end=schedule.mon_typical_end,
        tue_typical_start=schedule.tue_typical_start,
        tue_typical_end=schedule.tue_typical_end,
        wed_typical_start=schedule.wed_typical_start,
        wed_typical_end=schedule.wed_typical_end,
        thu_typical_start=schedule.thu_typical_start,
        thu_typical_end=schedule.thu_typical_end,
        fri_typical_start=schedule.fri_typical_start,
        fri_typical_end=schedule.fri_typical_end,
        sat_typical_start=schedule.sat_typical_start,
        sat_typical_end=schedule.sat_typical_end,
        sun_typical_start=schedule.sun_typical_start,
        sun_typical_end=schedule.sun_typical_end,
        source=schedule.source,
        version=schedule.version,
    )


def _effective_range_end(
    session: Session, model: type, employee_id: uuid.UUID, valid_from: date
) -> date | None:
    """Last local day a row of ``model`` starting at ``valid_from`` is effective.

    Under permanent shadowing (see ``_governs``) the row governs from its
    ``valid_from`` until the EARLIER of:

    * its own declared ``valid_to`` — a finite end stops it outright; and
    * the day before the next later-starting row, which supersedes it.

    ``None`` means the range is genuinely open-ended, which requires BOTH to be
    absent. Reading only the successor was a real defect: a row imported as
    effective for October alone, with no successor, reported ``None`` and the
    reopen functions then cleared the review state of every month from October
    onward — months the row does not govern at all (review R30).

    ``(employee_id, valid_from)`` is unique, so the declared bound is a single
    row. Both bounds are resolved HERE rather than at the call sites, so the
    projection, the reopen range and the master-data import cannot disagree.
    """
    declared = session.scalar(
        select(model.valid_to).where(
            model.employee_id == employee_id,
            model.valid_from == valid_from,
        )
    )
    next_start = session.scalar(
        select(func.min(model.valid_from)).where(
            model.employee_id == employee_id,
            model.valid_from > valid_from,
        )
    )
    successor_end = next_start - timedelta(days=1) if next_start is not None else None
    bounds = [bound for bound in (declared, successor_end) if bound is not None]
    return min(bounds) if bounds else None


def _schedule_range_end(session: Session, employee_id: uuid.UUID, valid_from: date) -> date | None:
    return _effective_range_end(session, WorkScheduleVersion, employee_id, valid_from)


def _contract_range_end(session: Session, employee_id: uuid.UUID, valid_from: date) -> date | None:
    return _effective_range_end(session, EmploymentContract, employee_id, valid_from)


def set_working_time(session: Session, employee: Employee, payload: WorkingTimePut) -> ScheduleOut:
    """Employee-maintained working time.

    Creates a NEW effective-dated ``work_schedule_version`` row with
    ``source=EMPLOYEE``, ``valid_to=NULL`` and the next version number;
    historical dates keep resolving to the previously effective schedule.
    An existing EMPLOYEE row on the same ``valid_from`` is corrected in place
    (version bumped): the employee's own unsubmitted configuration.
    Authoritative (MASTER/ADMIN_SEED) rows on that date are protected.
    """
    # The schedule is effective-dated employee master data: two concurrent
    # writes on the same valid_from would otherwise both read "no existing row"
    # (or the same max version) and one would be lost or collide.
    lock_employee_facts(session, employee.id)
    allocated = sum(getattr(payload, field) for field in WEEKDAY_MINUTE_FIELDS)
    if allocated != payload.weekly_required_minutes:
        raise api_error(
            422,
            ErrorCode.VALIDATION_FAILED,
            "Weekday allocation must sum exactly to the weekly target.",
            details={
                "weekly_required_minutes": payload.weekly_required_minutes,
                "allocated_minutes": allocated,
            },
        )

    existing = session.scalar(
        select(WorkScheduleVersion).where(
            WorkScheduleVersion.employee_id == employee.id,
            WorkScheduleVersion.valid_from == payload.valid_from,
        )
    )
    if existing is not None and existing.source != MasterDataSource.EMPLOYEE:
        raise api_error(
            422,
            ErrorCode.VALIDATION_FAILED,
            "A working-time schedule from an authoritative source already starts "
            "on that date; choose another effective date.",
        )

    if existing is None:
        max_version = session.scalar(
            select(func.max(WorkScheduleVersion.version)).where(
                WorkScheduleVersion.employee_id == employee.id
            )
        )
        row = WorkScheduleVersion(
            employee_id=employee.id,
            valid_from=payload.valid_from,
            valid_to=None,
            weekly_required_minutes=payload.weekly_required_minutes,
            mon_required_minutes=payload.mon_required_minutes,
            tue_required_minutes=payload.tue_required_minutes,
            wed_required_minutes=payload.wed_required_minutes,
            thu_required_minutes=payload.thu_required_minutes,
            fri_required_minutes=payload.fri_required_minutes,
            sat_required_minutes=payload.sat_required_minutes,
            sun_required_minutes=payload.sun_required_minutes,
            source=MasterDataSource.EMPLOYEE,
            version=(max_version or 0) + 1,
        )
        session.add(row)
        session.flush()
        _reopen_schedule_months(session, employee, payload.valid_from)
        return _schedule_out(row)

    existing.weekly_required_minutes = payload.weekly_required_minutes
    for field in WEEKDAY_MINUTE_FIELDS:
        setattr(existing, field, getattr(payload, field))
    existing.version += 1
    session.flush()
    _reopen_schedule_months(session, employee, payload.valid_from)
    return _schedule_out(existing)


def _reopen_schedule_months(session: Session, employee: Employee, valid_from: date) -> None:
    """EINV-15: a schedule change reopens the review of its effective months.

    The daily and weekly required minutes are engine inputs, so changing them
    changes the reviewed month's daily results, balance and findings. The
    reopened set is exactly the row's effective range — months before
    ``valid_from`` keep resolving to the previously effective row and are
    deliberately left alone (no transitive cascade).
    """
    open_reviewed_month_range(
        session,
        employee.id,
        valid_from,
        _schedule_range_end(session, employee.id, valid_from),
    )


def _reopen_contract_months(session: Session, employee: Employee, valid_from: date) -> None:
    """EINV-15: a contract change reopens the review of its effective months.

    Mirrors ``_reopen_schedule_months`` exactly: the contract's weekly minutes and
    FTE/pay-group are engine inputs (the weekly threshold fallback and the
    contract-dependent calculations), so a corrected row must reopen the months it
    governs — and only those.
    """
    open_reviewed_month_range(
        session,
        employee.id,
        valid_from,
        _contract_range_end(session, employee.id, valid_from),
    )


def profile(session: Session, employee: Employee, at: date | None = None) -> ProfileOut:
    effective_at = at if at is not None else now_berlin().date()
    contract = effective_contract(session, employee.id, effective_at)
    schedule = effective_schedule(session, employee.id, effective_at)
    preference = session.scalar(
        select(EmployeePreference).where(EmployeePreference.employee_id == employee.id)
    )
    preferences = _preferences_out(preference)
    return ProfileOut(
        effective_at=effective_at,
        employee=EmployeeOut(
            id=employee.id,
            external_employee_id=employee.external_employee_id,
            legal_name=employee.legal_name,
            email=employee.email,
            active=employee.active,
        ),
        contract=(
            ContractOut(
                valid_from=contract.valid_from,
                valid_to=contract.valid_to,
                fte=float(contract.fte) if contract.fte is not None else None,
                weekly_required_minutes=contract.weekly_required_minutes,
                pay_group_bucket=contract.pay_group_bucket,
                source=contract.source,
                version=contract.version,
            )
            if contract is not None
            else None
        ),
        schedule=(_schedule_out(schedule) if schedule is not None else None),
        preferences=preferences,
    )


def lock_identity_administration(session: Session) -> None:
    """First statement of every identity-binding invocation (fixed, global).

    Taken BEFORE the target is resolved, so two invocations — however they
    select their target (``--employee-id`` or ``--current-subject``) — cannot
    interleave a resolve and a mutate. The per-employee fact lock is taken
    second, inside ``set_identity_binding``, so the operator path also
    serializes against ordinary application mutations of that employee.

    A total order with a single global first lock cannot deadlock.
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": _IDENTITY_ADMIN_LOCK_KEY},
    )


def lock_master_data_administration(session: Session) -> None:
    """First statement of every bulk master-data import (fixed, global).

    Order: this lock, then ``lock_employee_facts`` per row. The same shape as
    ``lock_identity_administration``, so a single global first lock makes the
    operator paths deadlock-free.
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": _MASTER_DATA_ADMIN_LOCK_KEY},
    )


def set_identity_binding(
    session: Session,
    *,
    actor_principal: str,
    employee_id: uuid.UUID,
    new_subject: str | None,
    reason: str,
    matched_subject: str | None = None,
) -> str | None:
    """Set or clear one employee's identity binding; returns the PREVIOUS value.

    Caller contract: ``lock_identity_administration`` has already been taken and
    the target resolved. This function then takes the per-employee fact lock and
    re-reads the row, so the decision is made under both locks.

    ``matched_subject`` re-validates a ``--current-subject`` selector under the
    locks: the row's binding must still equal the subject that was matched, else
    the caller is working from a stale lookup.

    Performs NO capability check — the CLI is the gate — which is why the
    mutation surface is regression-guarded structurally (no route may reach
    this function).

    Raises ``ValueError`` for a blank reason or a stale selector, and lets the
    ``UNIQUE(idp_subject)`` ``IntegrityError`` propagate to the caller, which
    rolls back and reports it.
    """
    reason = reason.strip()
    if not reason:
        raise ValueError("--reason must not be blank")
    lock_employee_facts(session, employee_id)
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise ValueError(f"unknown employee_id {employee_id}")
    # Genuine re-read under both locks: the identity map would otherwise hand
    # back the instance the caller loaded BEFORE the locks were taken.
    session.refresh(employee)
    if matched_subject is not None and employee.idp_subject != matched_subject:
        raise ValueError(
            "the target's binding changed since it was matched; re-run with the current value"
        )
    previous = employee.idp_subject
    if previous == new_subject:
        return previous  # idempotent no-op: no provenance row, no session churn
    employee.idp_subject = new_subject
    session.add(
        IdentityBindingChange(
            employee_id=employee.id,
            previous_subject=previous,
            new_subject=new_subject,
            reason=reason,
            actor_principal=actor_principal,
        )
    )
    # The displaced subject must not keep a live browser session: the next
    # access requires a fresh assertion (same statement the deactivation path
    # uses).
    session.execute(delete(AuthSession).where(AuthSession.employee_id == employee.id))
    record_audit(
        session,
        actor_principal=actor_principal,
        action=ACTION_IDENTITY_REMAP,
        outcome="UPDATED",
        object_type="EMPLOYEE",
        object_id=employee.id,
        employee_subject_id=employee.id,
    )
    session.flush()
    return previous


def upsert_preferences(
    session: Session,
    employee: Employee,
    changes: dict[str, object],
) -> PreferencesOut:
    """Upsert the employee's preference row (PK = employee_id).

    ``changes`` contains only keys explicitly sent by the client (PATCH
    semantics based on the schema's fields_set). An empty dict writes nothing.
    """
    if changes:
        stmt = (
            pg_insert(EmployeePreference)
            .values(employee_id=employee.id, **changes)
            .on_conflict_do_update(index_elements=["employee_id"], set_=dict(changes))
        )
        session.execute(stmt)
    row = session.scalar(
        select(EmployeePreference).where(EmployeePreference.employee_id == employee.id)
    )
    return _preferences_out(row)


def _preferences_out(row: EmployeePreference | None) -> PreferencesOut:
    template = row.normal_day_template_json if row is not None else None
    return PreferencesOut(
        usual_work_type=row.usual_work_type if row is not None else None,
        preferred_calendar_view=row.preferred_calendar_view if row is not None else None,
        normal_day_template=(
            [NormalDayTemplateInterval.model_validate(item) for item in template]
            if isinstance(template, list)
            else None
        ),
    )
