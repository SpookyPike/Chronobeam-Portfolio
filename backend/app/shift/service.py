"""Employee-private shift commands and minimized preview builder."""

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime

from fastapi import status
from sqlalchemy import Select, exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.audit.models import record_audit
from app.auth.principal import Principal
from app.config import get_settings
from app.db import constraint_name, lock_employee_facts
from app.disclosure.fingerprint import with_preview_fingerprint
from app.enums import (
    ACTIVE_ASSIGNMENT_STATUSES,
    EMPLOYEE_SOURCE,
    BreakKind,
    ShiftAssignmentStatus,
    ShiftDeviationType,
    WorkType,
    is_employee_writable_source,
)
from app.errors import ErrorCode, api_error
from app.identity.models import Employee
from app.integrations.shift_plan import ShiftAssignmentSeed
from app.policy.editions import SHIFT_RULE_SET_V1, resolve_edition
from app.shift.models import ShiftAssignment, ShiftDeviation, ShiftPlan, ShiftPlanRevision
from app.shift.policy import ShiftFinding, evaluate_shift
from app.shift.report import (
    ShiftReportInputs,
    build_shift_report,
    compensation_review_status,
)
from app.shift.schemas import (
    PaidBreakStatus,
    ShiftConfirmIn,
    ShiftDeviationIn,
    ShiftDeviationOut,
    ShiftFindingOut,
    ShiftOut,
    ShiftPlanIn,
    ShiftPlanPatch,
    ShiftReportPreviewOut,
    ShiftRevisionOut,
    ShiftWithdrawIn,
)
from app.worktime.models import BreakInterval, Correction, WorkInterval
from app.worktime.queries import current_breaks, current_intervals
from app.worktime.schemas import IntervalPatch
from app.worktime.service import (
    ACTOR_ROLE_EMPLOYEE,
    _command_key,
    _resolve,
    break_interval_out,
    correct_interval,
    create_interval_instant,
    delete_interval,
)
from app.worktime.walltime import BERLIN

#: The shift edition label the preview asserts, resolved from the registry so a
#: recorded edition identity has exactly one definition (F4.4). The value is
#: unchanged — it is already inside frozen payloads.
SHIFT_POLICY_VERSION = resolve_edition(SHIFT_RULE_SET_V1).version


def _current_deviation_query(employee_id: uuid.UUID) -> Select[tuple[ShiftDeviation]]:
    newer = aliased(ShiftDeviation)
    return select(ShiftDeviation).where(
        ShiftDeviation.employee_id == employee_id,
        ~exists(select(newer.id).where(newer.supersedes_id == ShiftDeviation.id)),
    )


def _assignment_owned(
    session: Session, employee_id: uuid.UUID, assignment_id: uuid.UUID
) -> ShiftAssignment:
    """Owner-scoped lookup, deliberately STATUS-AGNOSTIC.

    Returns any row the caller owns, whatever its status, and raises the
    nondisclosing 404 otherwise. It must NOT filter on status: ``confirm_shift``
    already turns a non-``PLANNED`` status into a 409 with its own message, and
    filtering here would silently change that endpoint's answer to a 404 for a row
    the employee can plainly see in their own list. Paths that require an ACTIVE
    plan apply ``_assert_active_plan`` themselves.
    """
    assignment = session.scalar(
        select(ShiftAssignment).where(
            ShiftAssignment.id == assignment_id,
            ShiftAssignment.employee_id == employee_id,
        )
    )
    if assignment is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Shift assignment not found.")
    return assignment


def _assert_active_plan(assignment: ShiftAssignment) -> None:
    """Refuse a non-active plan (``WITHDRAWN`` / ``CANCELLED`` / ``SUPERSEDED``).

    Only ``PLANNED`` is an active plan (``ACTIVE_ASSIGNMENT_STATUSES``). A
    withdrawn plan is retained lineage — visible to the employee as their own
    history — but must never be deviable or reportable. Called by the paths that
    require an active plan; ``confirm_shift`` keeps its own equivalent inline
    check so its long-standing 409 contract is untouched.
    """
    if assignment.status not in ACTIVE_ASSIGNMENT_STATUSES:
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_SHIFT_PLAN_CHANGED,
            "This shift plan is no longer active.",
        )


def _approved_revision(session: Session, assignment: ShiftAssignment) -> ShiftPlanRevision:
    if assignment.approved_revision_id is None:
        raise api_error(422, ErrorCode.POLICY_BLOCKED, "Shift assignment has no approved revision.")
    revision = session.scalar(
        select(ShiftPlanRevision).where(
            ShiftPlanRevision.id == assignment.approved_revision_id,
            ShiftPlanRevision.shift_assignment_id == assignment.id,
        )
    )
    if revision is None:  # database integrity guard for corrupted/incomplete imports
        raise api_error(422, ErrorCode.POLICY_BLOCKED, "Shift assignment has no usable revision.")
    return revision


def _linked_intervals(
    session: Session, employee_id: uuid.UUID, assignment_id: uuid.UUID
) -> list[WorkInterval]:
    return list(
        current_intervals(
            session,
            employee_id=employee_id,
            linked_shift_assignment_id=assignment_id,
        )
    )


def _actual_bounds(intervals: list[WorkInterval]) -> tuple[datetime | None, datetime | None]:
    """Envelope of 0-N linked actual facts, compared as instants (INV-02)."""
    if not intervals:
        return None, None
    return (
        min(intervals, key=lambda interval: interval.start_at.astimezone(UTC)).start_at,
        max(intervals, key=lambda interval: interval.end_at.astimezone(UTC)).end_at,
    )


def _out_deviation(deviation: ShiftDeviation | None) -> ShiftDeviationOut | None:
    if deviation is None:
        return None
    return ShiftDeviationOut(
        id=deviation.id,
        deviation_type=deviation.deviation_type,
        actual_start_at=deviation.actual_start_at,
        actual_end_at=deviation.actual_end_at,
        noticed_or_decided_at=deviation.noticed_or_decided_at,
        decision_maker_type=deviation.decision_maker_type,
        reason=deviation.reason,
        source_work_interval_ids=[uuid.UUID(value) for value in deviation.source_work_interval_ids],
        version=deviation.version,
    )


def _finding_out(findings: list[ShiftFinding]) -> list[ShiftFindingOut]:
    return [
        ShiftFindingOut(rule_id=item.rule_id, status=item.status, message=item.message)
        for item in findings
    ]


def _linked_paid_breaks(
    session: Session, employee_id: uuid.UUID, assignment_id: uuid.UUID
) -> list[BreakInterval]:
    """Current PAID_SHIFT_BREAK facts linked to this one assignment.

    SHIFT-003's subject is the *paid* shift break, so the unpaid kind is
    deliberately not selected here: it is an ordinary factual break and makes no
    statement about compensated shift presence.
    """
    return [
        item
        for item in current_breaks(
            session,
            employee_id=employee_id,
            linked_shift_assignment_id=assignment_id,
        )
        if item.kind is BreakKind.PAID_SHIFT_BREAK
    ]


def _paid_break_status(paid_breaks: list[BreakInterval]) -> PaidBreakStatus:
    """SHIFT-003's state: has the employee recorded the required fact or not?"""
    return "RECORDED" if paid_breaks else "EXPLICIT_FACT_REQUIRED"


def _findings(
    revision: ShiftPlanRevision,
    deviation: ShiftDeviation | None,
    intervals: list[WorkInterval],
    paid_breaks: list[BreakInterval],
) -> list[ShiftFinding]:
    settings = get_settings()
    actual_start = deviation.actual_start_at if deviation else None
    actual_end = deviation.actual_end_at if deviation else None
    if actual_start is None:
        actual_start, actual_end = _actual_bounds(intervals)
    return evaluate_shift(
        deviation_type=deviation.deviation_type if deviation else None,
        actual_start_at=actual_start,
        actual_end_at=actual_end,
        communicated_at=revision.communicated_at,
        authoritative_compensation_enabled=settings.enable_shift_2026_authoritative_compensation,
        cancellation_3d_enabled=settings.enable_shift_cancellation_3d_auto_entitlement,
        paid_break_recorded=bool(paid_breaks),
    )


def list_shifts(
    session: Session, employee: Employee, start: datetime, end: datetime
) -> list[ShiftOut]:
    assignments = session.scalars(
        select(ShiftAssignment)
        .where(
            ShiftAssignment.employee_id == employee.id,
            ShiftAssignment.planned_start_at < end,
            ShiftAssignment.planned_end_at > start,
        )
        .order_by(ShiftAssignment.planned_start_at)
    ).all()
    if not assignments:
        return []
    assignment_ids = [row.id for row in assignments]
    # Ownership is established by the list query before materializing revisions.
    # The join also retains the same-assignment guard from _approved_revision.
    revisions = {
        row.shift_assignment_id: row
        for row in session.scalars(
            select(ShiftPlanRevision)
            .join(
                ShiftAssignment,
                (ShiftPlanRevision.id == ShiftAssignment.approved_revision_id)
                & (ShiftPlanRevision.shift_assignment_id == ShiftAssignment.id),
            )
            .where(
                ShiftAssignment.employee_id == employee.id,
                ShiftAssignment.id.in_(assignment_ids),
            )
        )
    }
    deviations = {
        row.shift_assignment_id: row
        for row in session.scalars(
            _current_deviation_query(employee.id).where(
                ShiftDeviation.shift_assignment_id.in_(assignment_ids)
            )
        )
    }
    interval_child = aliased(WorkInterval)
    intervals_by_assignment: dict[uuid.UUID, list[WorkInterval]] = defaultdict(list)
    for row in session.scalars(
        select(WorkInterval)
        .where(
            WorkInterval.employee_id == employee.id,
            WorkInterval.linked_shift_assignment_id.in_(assignment_ids),
            ~exists(
                select(interval_child.id).where(interval_child.supersedes_id == WorkInterval.id)
            ),
        )
        .order_by(WorkInterval.start_at)
    ):
        if row.linked_shift_assignment_id is not None:
            intervals_by_assignment[row.linked_shift_assignment_id].append(row)
    break_child = aliased(BreakInterval)
    breaks_by_assignment: dict[uuid.UUID, list[BreakInterval]] = defaultdict(list)
    for row in session.scalars(
        select(BreakInterval)
        .where(
            BreakInterval.employee_id == employee.id,
            BreakInterval.linked_shift_assignment_id.in_(assignment_ids),
            BreakInterval.kind == BreakKind.PAID_SHIFT_BREAK,
            ~exists(select(break_child.id).where(break_child.supersedes_id == BreakInterval.id)),
        )
        .order_by(BreakInterval.start_at)
    ):
        if row.linked_shift_assignment_id is not None:
            breaks_by_assignment[row.linked_shift_assignment_id].append(row)
    result: list[ShiftOut] = []
    for assignment in assignments:
        if assignment.approved_revision_id is None:
            raise api_error(
                422, ErrorCode.POLICY_BLOCKED, "Shift assignment has no approved revision."
            )
        revision = revisions.get(assignment.id)
        if revision is None:
            raise api_error(
                422, ErrorCode.POLICY_BLOCKED, "Shift assignment has no usable revision."
            )
        deviation = deviations.get(assignment.id)
        intervals = intervals_by_assignment[assignment.id]
        paid_breaks = breaks_by_assignment[assignment.id]
        actual_start = deviation.actual_start_at if deviation else None
        actual_end = deviation.actual_end_at if deviation else None
        if actual_start is None:
            actual_start, actual_end = _actual_bounds(intervals)
        result.append(
            ShiftOut(
                id=assignment.id,
                shift_kind=assignment.shift_kind,
                status=assignment.status,
                plan_source=revision.source,
                approved_revision=ShiftRevisionOut(
                    id=revision.id,
                    revision_no=revision.revision_no,
                    planned_start_at=revision.planned_start_at,
                    planned_end_at=revision.planned_end_at,
                    communicated_at=revision.communicated_at,
                ),
                actual_start_at=actual_start,
                actual_end_at=actual_end,
                deviation=_out_deviation(deviation),
                findings=_finding_out(_findings(revision, deviation, intervals, paid_breaks)),
                paid_break_status=_paid_break_status(paid_breaks),
                # The same list the status was resolved from — no second query,
                # and the two can never disagree about which breaks exist.
                paid_breaks=[break_interval_out(row) for row in paid_breaks],
            )
        )
    return result


def confirm_shift(
    session: Session, employee: Employee, assignment_id: uuid.UUID, payload: ShiftConfirmIn
) -> ShiftOut:
    """BE-CMD-004: one click creates a separate linked actual SHIFT fact."""
    lock_employee_facts(session, employee.id)
    assignment = _assignment_owned(session, employee.id, assignment_id)
    revision = _approved_revision(session, assignment)
    if (
        assignment.status != ShiftAssignmentStatus.PLANNED
        or revision.id != payload.approved_revision_id
    ):
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_SHIFT_PLAN_CHANGED,
            "The approved shift plan changed; refresh before confirming.",
        )
    intervals = _linked_intervals(session, employee.id, assignment.id)
    deviation = session.scalar(
        _current_deviation_query(employee.id).where(
            ShiftDeviation.shift_assignment_id == assignment.id
        )
    )
    actual_start, actual_end = _actual_bounds(intervals)
    if deviation is not None or (
        actual_start is not None
        and (
            actual_end is None
            or actual_start.astimezone(UTC) != revision.planned_start_at.astimezone(UTC)
            or actual_end.astimezone(UTC) != revision.planned_end_at.astimezone(UTC)
        )
    ):
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_SHIFT_PLAN_CHANGED,
            "Actual shift facts differ from the approved plan; "
            "record or update a deviation instead.",
        )
    if not intervals:
        create_interval_instant(
            session,
            employee,
            start_at=revision.planned_start_at,
            end_at=revision.planned_end_at,
            work_type=WorkType.SHIFT,
            comment=None,
            client_request_id=_command_key(
                payload.client_request_id,
                employee.id,
                f"shift-confirm:{assignment.id}",
            ),
            linked_shift_assignment_id=assignment.id,
        )
        session.flush()
    return next(
        shift
        for shift in list_shifts(
            session, employee, revision.planned_start_at, revision.planned_end_at
        )
        if shift.id == assignment.id
    )


def save_deviation(
    session: Session,
    principal: Principal,
    employee: Employee,
    assignment_id: uuid.UUID,
    payload: ShiftDeviationIn,
) -> ShiftOut:
    """Create a structured deviation and a separately versioned actual fact."""
    lock_employee_facts(session, employee.id)
    assignment = _assignment_owned(session, employee.id, assignment_id)
    _assert_active_plan(assignment)
    revision = _approved_revision(session, assignment)
    current = session.scalar(
        _current_deviation_query(employee.id).where(
            ShiftDeviation.shift_assignment_id == assignment.id
        )
    )
    if current is not None and payload.version != current.version:
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_VERSION,
            "Shift deviation changed; refresh and retry.",
        )
    if current is None and payload.version is not None:
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_VERSION,
            "Shift deviation changed; refresh and retry.",
        )

    actual_start = (
        _resolve("actual_start_local", payload.actual_start_local, payload.start_fold)
        if payload.actual_start_local is not None
        else None
    )
    actual_end = (
        _resolve("actual_end_local", payload.actual_end_local, payload.end_fold)
        if payload.actual_end_local is not None
        else None
    )
    if (
        actual_start is not None
        and actual_end is not None
        and actual_end.astimezone(UTC) <= actual_start.astimezone(UTC)
    ):
        raise api_error(
            422, ErrorCode.VALIDATION_INTERVAL_ORDER, "actual end must be after actual start."
        )
    noticed = (
        _resolve(
            "noticed_or_decided_local",
            payload.noticed_or_decided_local,
            payload.noticed_or_decided_fold,
        )
        if payload.noticed_or_decided_local is not None
        else None
    )

    source_ids: list[str] = []
    existing = _linked_intervals(session, employee.id, assignment.id)
    if payload.deviation_type == ShiftDeviationType.CANCELLED and existing:
        # The prior exact-confirmation fact was unsubmitted and is now
        # corrected to cancellation.  Reuse the established correction/audit
        # command rather than allowing a cancelled shift to retain work.
        for interval in existing:
            delete_interval(
                session,
                principal,
                employee,
                interval.id,
                interval.version,
                "Shift cancellation recorded.",
            )
        existing = []
    if actual_start is not None and actual_end is not None:
        if len(existing) == 1:
            corrected = correct_interval(
                session,
                employee,
                existing[0].id,
                IntervalPatch(
                    version=existing[0].version,
                    start_local=actual_start.astimezone(BERLIN).replace(tzinfo=None),
                    end_local=actual_end.astimezone(BERLIN).replace(tzinfo=None),
                    start_fold=payload.start_fold,
                    end_fold=payload.end_fold,
                    reason=payload.reason,
                ),
            )
            source_ids = [str(corrected.id)]
        elif not existing:
            interval, _ = create_interval_instant(
                session,
                employee,
                start_at=actual_start,
                end_at=actual_end,
                work_type=WorkType.SHIFT,
                comment=None,
                linked_shift_assignment_id=assignment.id,
            )
            source_ids = [str(interval.id)]
        else:
            existing_start, existing_end = _actual_bounds(existing)
            if (
                existing_start is None
                or existing_end is None
                or actual_start.astimezone(UTC) != existing_start.astimezone(UTC)
                or actual_end.astimezone(UTC) != existing_end.astimezone(UTC)
            ):
                raise api_error(
                    422,
                    ErrorCode.VALIDATION_FAILED,
                    "Edit the individual shift intervals before changing their "
                    "aggregate actual span.",
                )
            source_ids = [str(interval.id) for interval in existing]
    if current is not None:
        deviation = ShiftDeviation(
            employee_id=employee.id,
            shift_assignment_id=assignment.id,
            actual_start_at=actual_start,
            actual_end_at=actual_end,
            deviation_type=payload.deviation_type,
            noticed_or_decided_at=noticed,
            decision_maker_type=payload.decision_maker_type,
            reason=payload.reason,
            source_work_interval_ids=source_ids or current.source_work_interval_ids,
            version=current.version + 1,
            supersedes_id=current.id,
        )
    else:
        deviation = ShiftDeviation(
            employee_id=employee.id,
            shift_assignment_id=assignment.id,
            actual_start_at=actual_start,
            actual_end_at=actual_end,
            deviation_type=payload.deviation_type,
            noticed_or_decided_at=noticed,
            decision_maker_type=payload.decision_maker_type,
            reason=payload.reason,
            source_work_interval_ids=source_ids,
            version=1,
        )
    session.add(deviation)
    session.flush()
    return next(
        shift
        for shift in list_shifts(
            session, employee, revision.planned_start_at, revision.planned_end_at
        )
        if shift.id == assignment.id
    )


def report_preview(
    session: Session, employee: Employee, assignment_id: uuid.UUID
) -> ShiftReportPreviewOut:
    """Build a private, minimized Shift Report shape from this shift only."""
    assignment = _assignment_owned(session, employee.id, assignment_id)
    _assert_active_plan(assignment)
    revision = _approved_revision(session, assignment)
    deviation = session.scalar(
        _current_deviation_query(employee.id).where(
            ShiftDeviation.shift_assignment_id == assignment.id
        )
    )
    intervals = _linked_intervals(session, employee.id, assignment.id)
    paid_breaks = _linked_paid_breaks(session, employee.id, assignment.id)
    actual_start = deviation.actual_start_at if deviation else None
    actual_end = deviation.actual_end_at if deviation else None
    if actual_start is None:
        actual_start, actual_end = _actual_bounds(intervals)
    # ONE finding list feeds both the report and its review status, so the two
    # cannot disagree about whether the entitlement is settled.
    findings = _finding_out(_findings(revision, deviation, intervals, paid_breaks))
    deviation_type = deviation.deviation_type if deviation else None
    return with_preview_fingerprint(
        build_shift_report(
            ShiftReportInputs(
                policy_version=SHIFT_POLICY_VERSION,
                policy_status="PROVISIONAL",
                employee_identifier=employee.external_employee_id or str(employee.id),
                shift_assignment_id=assignment.id,
                planned_start_at=revision.planned_start_at,
                planned_end_at=revision.planned_end_at,
                actual_start_at=actual_start,
                actual_end_at=actual_end,
                deviation_type=deviation_type,
                noticed_or_decided_at=deviation.noticed_or_decided_at if deviation else None,
                decision_maker_type=deviation.decision_maker_type if deviation else None,
                reason=deviation.reason if deviation else None,
                findings=findings,
                paid_break_status=_paid_break_status(paid_breaks),
                plan_source=revision.source,
                compensation_review_status=compensation_review_status(deviation_type, findings),
            )
        )
    )


def seed_shift_assignment(
    session: Session, employee: Employee, seed: ShiftAssignmentSeed
) -> ShiftAssignment:
    """Controlled OPEN-06 adapter seam used by synthetic tests and the demo seed.

    A real adapter may call this after the authoritative interface is supplied;
    this function performs no network activity and replay is idempotent.
    """
    if seed.planned_start_at.tzinfo is None or seed.planned_end_at.tzinfo is None:
        raise ValueError("seeded shift timestamps must be timezone-aware instants")
    # Minute resolution is the product contract (F3.8/EINV-17). Enforced at this
    # seam too, because an imported shift-plan feed arrives here rather than
    # through the wall-time command path.
    for label, value in (
        ("planned_start_at", seed.planned_start_at),
        ("planned_end_at", seed.planned_end_at),
    ):
        if value.second or value.microsecond:
            raise ValueError(f"seeded shift {label} must be at minute resolution")
    if seed.planned_end_at.astimezone(UTC) <= seed.planned_start_at.astimezone(UTC):
        raise ValueError("seeded shift end must be after start")
    plan = session.scalar(
        select(ShiftPlan).where(
            ShiftPlan.plan_key == seed.plan_key,
            ShiftPlan.source == seed.source,
            ShiftPlan.source_ref == seed.source_ref,
        )
    )
    if plan is None:
        plan = ShiftPlan(
            plan_key=seed.plan_key,
            name=seed.plan_name,
            valid_from=seed.planned_start_at.astimezone(BERLIN).date(),
            valid_to=None,
            source=seed.source,
            source_ref=seed.source_ref,
        )
        session.add(plan)
        session.flush()
    assignment = session.scalar(
        select(ShiftAssignment).where(
            ShiftAssignment.shift_plan_id == plan.id,
            ShiftAssignment.employee_id == employee.id,
            ShiftAssignment.planned_start_at == seed.planned_start_at,
            ShiftAssignment.planned_end_at == seed.planned_end_at,
            ShiftAssignment.shift_kind == seed.shift_kind,
        )
    )
    if assignment is not None:
        return assignment
    assignment = ShiftAssignment(
        shift_plan_id=plan.id,
        employee_id=employee.id,
        planned_start_at=seed.planned_start_at,
        planned_end_at=seed.planned_end_at,
        shift_kind=seed.shift_kind,
        status=ShiftAssignmentStatus.PLANNED,
    )
    session.add(assignment)
    session.flush()
    revision = ShiftPlanRevision(
        shift_assignment_id=assignment.id,
        revision_no=1,
        planned_start_at=seed.planned_start_at,
        planned_end_at=seed.planned_end_at,
        changed_at=datetime.now(UTC),
        communicated_at=None,
        decision_at=None,
        decision_maker_type=None,
        reason=None,
        source=seed.source,
    )
    session.add(revision)
    session.flush()
    assignment.approved_revision_id = revision.id
    session.flush()
    return assignment


# ---------------------------------------------------------------------------
# Employee plan entry (pilot slice)
# ---------------------------------------------------------------------------
#
# While no institutional shift-plan source exists, the employee records the shift
# planned for THEMSELVES. It is a normal plan for every downstream consumer
# (confirm / deviation / report-preview / paid-break resolution all read it
# through ``approved_revision_id``), so authority can later move to a supervisor,
# a planning module or an adapter without migrating the accounting model. What
# makes that handoff clean is provenance: only ``EMPLOYEE_RECORDED`` rows are
# writable here, so a row from any other writer is untouchable by construction.

#: The named unique constraint on ``shift_assignment.client_request_id``. Compared
#: against ``constraint_name(exc)`` so an UNRELATED integrity failure is re-raised
#: instead of being disguised as an idempotency collision. An auto-named
#: constraint would make this branch unreachable.
SHIFT_ASSIGNMENT_IDEMPOTENCY_CONSTRAINT = "uq_shift_assignment_client_request_id"

#: Audit actions for the three plan-entry commands.
SHIFT_PLAN_CREATE = "SHIFT_PLAN_CREATE"
SHIFT_PLAN_EDIT = "SHIFT_PLAN_EDIT"
SHIFT_PLAN_WITHDRAW = "SHIFT_PLAN_WITHDRAW"


def _require_plan_entry_enabled() -> None:
    """Enforce THIS domain's switch, and only its own (D24).

    422 ``POLICY_BLOCKED``, not 403: ``auth/deps.py`` documents one byte-identical
    body for every ``/me/*`` DENIAL, and this is not an authorization denial — the
    deployment has simply moved plan authority elsewhere.

    Called from inside the create command, AFTER its replay pre-read, so an
    already-committed same-owner retry still succeeds with the switch off (D20).
    """
    if not get_settings().enable_shift_plan_entry:
        raise api_error(
            422,
            ErrorCode.POLICY_BLOCKED,
            "An authoritative shift plan source is configured; shift plan entry happens there.",
        )


def _employee_plan_key(employee: Employee) -> str:
    """Server-generated container key. Never accepted from the client."""
    return f"employee:{employee.id}"


def _employee_plan(session: Session, employee: Employee, coverage: date) -> ShiftPlan:
    """Resolve-or-create the ONE employee-entry plan container.

    Exactly one container per employee for this source. ``ShiftPlan`` partitions by
    SOURCE, and the employee is one source, so a future adapter's plans live in
    their OWN container (resolved on its own ``plan_key``/``source``) rather than
    sharing this one.

    ``valid_from`` is container-COVERAGE metadata (D21): the monotonic minimum of
    the contained plans' Berlin start dates. It is deliberately not frozen to the
    first entry's date — a later entry for an EARLIER date must extend the coverage
    backwards, or the container would claim validity beginning after its own
    contents. It is not shrunk by a withdrawal either: the container did cover that
    date, and a withdrawn plan is retained lineage.
    """
    plan = session.scalar(
        select(ShiftPlan).where(
            ShiftPlan.plan_key == _employee_plan_key(employee),
            ShiftPlan.source == EMPLOYEE_SOURCE,
            ShiftPlan.source_ref.is_(None),
        )
    )
    if plan is None:
        plan = ShiftPlan(
            plan_key=_employee_plan_key(employee),
            name=None,
            valid_from=coverage,
            valid_to=None,
            source=EMPLOYEE_SOURCE,
            source_ref=None,
        )
        session.add(plan)
        session.flush()
        return plan
    if coverage < plan.valid_from:
        plan.valid_from = coverage
        session.flush()
    return plan


def _replay_plan_or_reject(
    session: Session, employee: Employee, client_request_id: uuid.UUID | None
) -> ShiftAssignment | None:
    """The shift-create idempotency contract — used twice (pre-read and recovery).

    GLOBAL lookup, never employee-scoped: ``client_request_id`` is unique across
    the whole table, so a scoped lookup would miss a collision with another
    employee's key entirely. Same owner -> replay. Other owner -> the
    privacy-preserving 422 (returning somebody else's row would be a disclosure).
    Absent -> ``None``.
    """
    if client_request_id is None:
        return None
    existing = session.scalar(
        select(ShiftAssignment).where(ShiftAssignment.client_request_id == client_request_id)
    )
    if existing is None:
        return None
    if existing.employee_id == employee.id:
        return existing
    raise api_error(
        422,
        ErrorCode.VALIDATION_FAILED,
        "This change was already recorded — reload the page to see it.",
    )


def _assert_plan_mutable(session: Session, employee: Employee, assignment: ShiftAssignment) -> None:
    """D3, binding: a plan is mutable only while NOTHING depends on it.

    Once any of these exists the plan is frozen, and the employee's route to change
    the outcome is the deviation path (or voiding the call) — not rewriting the plan
    the fact was recorded against. Each refusal states the reason AND the correct
    action, because the employee reads the message verbatim and a bare refusal
    leaves them with no next step. There is deliberately NO containment exception
    for a paid break: the break is linked, not span-filtered, so a break that would
    still fit inside a moved window is not a reason to move it.
    """
    _assert_active_plan(assignment)
    revision = _approved_revision(session, assignment)
    if not is_employee_writable_source(revision.source):
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_SHIFT_PLAN_CHANGED,
            "This shift plan was not recorded by you, so it cannot be changed here.",
        )
    if _linked_intervals(session, employee.id, assignment.id):
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_SHIFT_PLAN_CHANGED,
            "This shift already has recorded actual time. Record a deviation instead.",
        )
    if (
        session.scalar(
            _current_deviation_query(employee.id).where(
                ShiftDeviation.shift_assignment_id == assignment.id
            )
        )
        is not None
    ):
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_SHIFT_PLAN_CHANGED,
            "This shift already has a recorded deviation. Update the deviation instead.",
        )
    if _linked_paid_breaks(session, employee.id, assignment.id):
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_SHIFT_PLAN_CHANGED,
            "This shift already has a recorded paid break, so it cannot be moved.",
        )


def _shift_out_for(session: Session, employee: Employee, assignment: ShiftAssignment) -> ShiftOut:
    """The server's state for one assignment, in the same shape the reads return.

    Every write returns the freshly-read row rather than a hand-built response, so
    a write and the next read can never disagree.
    """
    return next(
        shift
        for shift in list_shifts(
            session, employee, assignment.planned_start_at, assignment.planned_end_at
        )
        if shift.id == assignment.id
    )


def _resolve_plan_window(
    start_local: datetime,
    end_local: datetime,
    start_fold: int | None,
    end_fold: int | None,
) -> tuple[datetime, datetime]:
    """Resolve both wall times, then check the ORDER on the resolved instants.

    The order check is on elapsed instants, not on the naive wall-clock values: a
    window that crosses the DST change can legitimately look "backwards" as text
    while still being a forward interval (and vice versa).
    """
    start = _resolve("planned_start_local", start_local, start_fold)
    end = _resolve("planned_end_local", end_local, end_fold)
    if end <= start:
        raise api_error(
            422,
            ErrorCode.VALIDATION_FAILED,
            "The end of the shift must be after its start.",
        )
    return start, end


def create_plan_shift(
    session: Session, principal: Principal, employee: Employee, payload: ShiftPlanIn
) -> tuple[ShiftOut, bool]:
    """BE-CMD-001: the employee records the shift planned for themselves.

    Returns ``(state, created)`` so the route can answer 201 for a real create and
    200 for an idempotent replay — the same shape every other create-producing
    command in this codebase uses, and the only way a client can tell the two
    apart.

    Ordering matters (D20): authenticate -> detect an exact persisted replay ->
    only then enforce the feature switch. An already-committed same-owner retry is
    not a new mutation, so flipping the switch off must not turn a successful retry
    into an error.
    """
    lock_employee_facts(session, employee.id)

    replay = _replay_plan_or_reject(session, employee, payload.client_request_id)
    if replay is not None:
        return _shift_out_for(session, employee, replay), False

    _require_plan_entry_enabled()

    start, end = _resolve_plan_window(
        payload.planned_start_local, payload.planned_end_local, payload.start_fold, payload.end_fold
    )
    plan = _employee_plan(session, employee, start.astimezone(BERLIN).date())

    try:
        # SAVEPOINT around the whole create unit. The collision point is the flush
        # below, and nothing dependent precedes it. On a collision the loser rolls
        # back only this unit, so the request transaction stays usable for the
        # recovery re-read and the response.
        with session.begin_nested():
            assignment = ShiftAssignment(
                shift_plan_id=plan.id,
                employee_id=employee.id,
                planned_start_at=start,
                planned_end_at=end,
                shift_kind=payload.shift_kind,
                status=ShiftAssignmentStatus.PLANNED,
                client_request_id=payload.client_request_id,
            )
            session.add(assignment)
            session.flush()
            revision = ShiftPlanRevision(
                shift_assignment_id=assignment.id,
                revision_no=1,
                planned_start_at=start,
                planned_end_at=end,
                changed_at=datetime.now(UTC),
                # D9: an employee-recorded plan never asserts an institutional
                # communication time. SHIFT-009 measures cancellation timing
                # against the planned start, so inventing one here would feed a
                # PROVISIONAL rule a fabricated input.
                communicated_at=None,
                decision_at=None,
                decision_maker_type=None,
                reason=None,
                source=EMPLOYEE_SOURCE,
            )
            session.add(revision)
            session.flush()
            # The head cache and the head pointer move in ONE place (EINV-14-class
            # invariant): list_shifts filters on the assignment columns while
            # reporting the revision's, so a stale cache would make the shift
            # invisible in its own window — and disclosure reads the same rows.
            assignment.approved_revision_id = revision.id
            session.flush()
            record_audit(
                session,
                actor_principal=principal.subject,
                action=SHIFT_PLAN_CREATE,
                outcome="CREATED",
                object_type="SHIFT_ASSIGNMENT",
                object_id=assignment.id,
                employee_subject_id=employee.id,
            )
    except IntegrityError as exc:
        if constraint_name(exc) != SHIFT_ASSIGNMENT_IDEMPOTENCY_CONSTRAINT:
            raise  # never disguise an unrelated integrity failure
        # The loser blocked on the winner's uncommitted unique-index entry until it
        # committed, so this re-read is guaranteed to see it.
        winner = _replay_plan_or_reject(session, employee, payload.client_request_id)
        if winner is None:
            raise  # winner vanished: do not guess
        return _shift_out_for(session, employee, winner), False

    return _shift_out_for(session, employee, assignment), True


def edit_plan_shift(
    session: Session,
    principal: Principal,
    employee: Employee,
    assignment_id: uuid.UUID,
    payload: ShiftPlanPatch,
) -> ShiftOut:
    """Correct an employee-recorded plan, optimistically on ``approved_revision_id``.

    A correction is a version write, not a create: the token is the concurrency
    control and there is no ``client_request_id``. The terminal-state and freeze
    guards run BEFORE the token comparison, as in ``patch_call``.
    """
    lock_employee_facts(session, employee.id)
    assignment = _assignment_owned(session, employee.id, assignment_id)
    _require_plan_entry_enabled()
    _assert_plan_mutable(session, employee, assignment)

    current = _approved_revision(session, assignment)
    if current.id != payload.approved_revision_id:
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_SHIFT_PLAN_CHANGED,
            "The approved shift plan changed; refresh before correcting it.",
        )

    if payload.planned_start_local is not None and payload.planned_end_local is not None:
        start, end = _resolve_plan_window(
            payload.planned_start_local,
            payload.planned_end_local,
            payload.start_fold,
            payload.end_fold,
        )
    else:
        start, end = current.planned_start_at, current.planned_end_at

    # ``shift_kind`` merges by PRESENCE: omitted means unchanged, an explicit null
    # clears it. A truthiness test would make "clear it" indistinguishable from
    # "leave it alone".
    shift_kind = (
        payload.shift_kind if "shift_kind" in payload.model_fields_set else assignment.shift_kind
    )

    revision_no = (
        session.scalar(
            select(ShiftPlanRevision.revision_no)
            .where(ShiftPlanRevision.shift_assignment_id == assignment.id)
            .order_by(ShiftPlanRevision.revision_no.desc())
            .limit(1)
        )
        or 0
    ) + 1
    revision = ShiftPlanRevision(
        shift_assignment_id=assignment.id,
        revision_no=revision_no,
        planned_start_at=start,
        planned_end_at=end,
        changed_at=datetime.now(UTC),
        communicated_at=None,
        decision_at=current.decision_at,
        decision_maker_type=current.decision_maker_type,
        reason=current.reason,
        source=current.source,
    )
    session.add(revision)
    session.flush()
    assignment.approved_revision_id = revision.id
    assignment.planned_start_at = start
    assignment.planned_end_at = end
    assignment.shift_kind = shift_kind
    session.flush()
    # A later entry for an EARLIER date extends the container's coverage (D21).
    _employee_plan(session, employee, start.astimezone(BERLIN).date())
    session.add(
        Correction(
            entity_type="SHIFT_ASSIGNMENT",
            entity_id=assignment.id,
            old_version=current.revision_no,
            new_version=revision.revision_no,
            actor_employee_id=employee.id,
            actor_role=ACTOR_ROLE_EMPLOYEE,
            reason=None,
        )
    )
    record_audit(
        session,
        actor_principal=principal.subject,
        action=SHIFT_PLAN_EDIT,
        outcome="UPDATED",
        object_type="SHIFT_ASSIGNMENT",
        object_id=assignment.id,
        employee_subject_id=employee.id,
    )
    return _shift_out_for(session, employee, assignment)


def withdraw_plan_shift(
    session: Session,
    principal: Principal,
    employee: Employee,
    assignment_id: uuid.UUID,
    payload: ShiftWithdrawIn,
) -> ShiftOut:
    """Retract a plan that was entered by mistake or was never the real plan.

    NOT a cancellation. A real operational cancellation is an act of the Company
    recorded through the deviation route, which leaves the plan ``PLANNED`` and
    preserves the scheduled shift as the compensation basis (POL-02 §4.4). This
    act carries no entitlement, which is exactly why the two must never be
    conflated — and why the UI steers the employee to the cancellation path when
    one actually happened.

    The row, its revision chain, its ``Correction`` row and its audit event are all
    retained: this is lineage, not a delete.
    """
    lock_employee_facts(session, employee.id)
    assignment = _assignment_owned(session, employee.id, assignment_id)
    _require_plan_entry_enabled()
    _assert_plan_mutable(session, employee, assignment)

    current = _approved_revision(session, assignment)
    if current.id != payload.approved_revision_id:
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_SHIFT_PLAN_CHANGED,
            "The approved shift plan changed; refresh before removing it.",
        )

    assignment.status = ShiftAssignmentStatus.WITHDRAWN
    session.flush()
    session.add(
        Correction(
            entity_type="SHIFT_ASSIGNMENT",
            entity_id=assignment.id,
            old_version=current.revision_no,
            # ``None``, NOT ``current.revision_no``. A withdrawal is a STATUS write
            # that appends no revision, so there is no version transition to record;
            # ``N -> N`` would claim a correction that changed nothing. This is the
            # repository's established shape for a no-new-version write — the
            # ``void_call`` interval tombstone and the absence day-status removal both
            # write ``old_version=<n>, new_version=None``.
            new_version=None,
            actor_employee_id=employee.id,
            actor_role=ACTOR_ROLE_EMPLOYEE,
            reason="Plan withdrawn by the employee.",
        )
    )
    record_audit(
        session,
        actor_principal=principal.subject,
        action=SHIFT_PLAN_WITHDRAW,
        outcome="UPDATED",
        object_type="SHIFT_ASSIGNMENT",
        object_id=assignment.id,
        employee_subject_id=employee.id,
    )
    return _shift_out_for(session, employee, assignment)
