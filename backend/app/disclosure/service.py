"""Disclosure service — security core.

Employee-controlled visibility semantics that can never be UI decoration
(INV-05, ACCESS-001..016):

- ``create_access_request``: HR asks by external employee id; the request
  carries zero ledger content; unknown/inactive employees get the same
  byte-identical 404; the requester cannot request their own ledger.
- ``grant_access_request``: the employee reviews/corrects first (the ledger
  is unchanged by this call), then opens ONLY the requested one-month view.
  A NEW frozen append-only ``period_calculation_snapshot`` is written per
  grant (never refreshed); prior active grants for the same scope are
  atomically revoked and referenced through ``supersedes_id`` (D8).
  ``expires_at`` stays NULL (ACCESS-014 PROVISIONAL — no guessed retention).
- ``read_grant_projection``: the grantee reads ONLY the frozen snapshot
  projection, scope-checked per grant; reads are audited after authorization
  succeeds (D11).
- ``valid_recipient_subjects``: the server-side registry (D6) — dev fixture
  principals; production = server-controlled config, empty = fail closed.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import NoReturn

import sqlalchemy as sa
from fastapi import status
from sqlalchemy import exists, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased, load_only

from app.audit.models import record_audit
from app.auth.dev_principals import DEV_PRINCIPALS
from app.auth.principal import Capability, Principal
from app.config import get_settings
from app.db import constraint_name, lock_employee_facts
from app.disclosure.fingerprint import payload_fingerprint
from app.disclosure.models import (
    AccessRequest,
    AccessRequestStatus,
    DisclosureGrant,
    SubmissionPackage,
    SubmissionPackageStatus,
    SubmissionRecipient,
)
from app.enums import ACTIVE_ASSIGNMENT_STATUSES, OCD_ACTIVE_STATUSES, SubmissionPurpose
from app.errors import ErrorCode, api_error, forbidden
from app.identity.models import Employee
from app.policy.service import policy_version_for_purpose
from app.worktime.models import WorkPeriod
from app.worktime.service import elapsed_seconds
from app.worktime.walltime import BERLIN

MONTH_VIEW_TYPE = "work-time-record"
MONTH_GRANT_PURPOSES = frozenset(
    {SubmissionPurpose.RANDOM_CHECK, SubmissionPurpose.WORK_TIME_RECORD}
)


# ---------------------------------------------------------------------------
# Recipient registry (D6)
# ---------------------------------------------------------------------------


def valid_recipient_subjects() -> tuple[str, ...]:
    """Server-side registry of exact recipient/requester subjects (D6).

    Development/tests: the fixture principals are the registry. Production:
    ONLY ``CHRONOBEAM_RECIPIENT_SUBJECTS`` (comma-separated, server-controlled
    config) applies; an empty value means no recipients are configured and
    every submission stays blocked (fail closed).
    """
    settings = get_settings()
    configured = tuple(
        subject.strip() for subject in settings.recipient_subjects.split(",") if subject.strip()
    )
    if settings.is_production:
        return configured
    if configured:
        return configured
    return tuple(
        sorted(
            principal.subject
            for principal in DEV_PRINCIPALS.values()
            if principal.has_capability(Capability.SUBMISSION_READ_ASSIGNED)
        )
    )


@dataclass(frozen=True)
class RecipientOption:
    """One selectable recipient: the configured subject and whether it is readable."""

    subject: str
    readable: bool


def readable_recipient_subjects(session: Session) -> tuple[RecipientOption, ...]:
    """The server-controlled recipient registry, with a liveness check per entry.

    The capability is resolved through the ONE shared derivation
    (``app.auth.deps.subject_capabilities``, the same function the request path uses)
    rather than a hard-coded role
    string, so the registry and the capability model cannot drift: an employee
    whose ``SUBMISSION_RECIPIENT`` assignment was revoked, or who is inactive or
    unmapped, is reported unreadable rather than silently addressed. One query per
    registry entry; no ledger data, no display names, no employee ids.
    """
    from app.auth.deps import subject_capabilities

    return tuple(
        RecipientOption(
            subject=subject,
            readable=Capability.SUBMISSION_READ_ASSIGNED in subject_capabilities(session, subject),
        )
        for subject in valid_recipient_subjects()
    )


def _require_registered_recipients(session: Session, recipients: list[str]) -> None:
    """Reject any subject outside the registry, or inside it but unreadable (D6).

    The registry (``CHRONOBEAM_RECIPIENT_SUBJECTS``) and the capability
    (``Role.SUBMISSION_RECIPIENT -> SUBMISSION_READ_ASSIGNED``) are two separate
    controls — allowed identity vs capability — and they stay separate. What
    becomes detectable and blocking is only their *inconsistency*: a package
    addressed to a configured subject that no principal can read is worse than an
    explicit failure. The two failure modes keep distinct ``details`` keys because
    their fixes differ (configuration vs role assignment).
    """
    registry = {option.subject: option.readable for option in readable_recipient_subjects(session)}
    unknown = [subject for subject in recipients if subject not in registry]
    unreadable = [subject for subject in recipients if registry.get(subject) is False]
    if unknown or unreadable:
        raise api_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            ErrorCode.VALIDATION_FAILED,
            "Recipient subjects must come from the server-side registry and must hold the "
            "recipient capability.",
            details={"unknown_recipients": unknown, "unreadable_recipients": unreadable},
        )


# ---------------------------------------------------------------------------
# Access requests (ACCESS-007/008)
# ---------------------------------------------------------------------------


def _month_bounds(month: str) -> tuple[date, date]:
    """Parse yyyy-mm into the inclusive one-calendar-month range (D7)."""
    try:
        parsed = date.fromisoformat(f"{month}-01")
    except ValueError:
        raise api_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            ErrorCode.VALIDATION_INVALID_MONTH,
            "Month must be formatted as yyyy-mm.",
        ) from None
    if parsed.month == 12:
        end = date(parsed.year + 1, 1, 1)
    else:
        end = date(parsed.year, parsed.month + 1, 1)
    return parsed, end - timedelta(days=1)


def _nondisclosing_not_found() -> Exception:
    """Byte-identical 404 for unknown/inactive/foreign targets (D7)."""
    return api_error(
        status.HTTP_404_NOT_FOUND,
        ErrorCode.NOT_FOUND,
        "The requested resource does not exist.",
    )


def create_access_request(
    session: Session,
    principal: Principal,
    *,
    employee_external_id: str,
    purpose: SubmissionPurpose,
    month: str,
    view_type: str,
    message: str | None,
) -> AccessRequest:
    """Record an HR random-check request; the response reveals no ledger data."""
    # Purpose validation FIRST: it must not depend on whether the target
    # exists, or the status code becomes an existence oracle.
    if purpose not in MONTH_GRANT_PURPOSES:
        raise api_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            ErrorCode.VALIDATION_FAILED,
            "Access requests support only RANDOM_CHECK / WORK_TIME_RECORD purposes.",
        )
    period_start, period_end = _month_bounds(month)
    employee = session.scalar(
        select(Employee).where(Employee.external_employee_id == employee_external_id)
    )
    # Unknown AND inactive employees are byte-identical (no existence oracle,
    # D7). The request itself never carries work-time content (ACCESS-007).
    if employee is None or not employee.active:
        raise _nondisclosing_not_found()
    # Serialize on the REQUESTED employee's facts: the request targets that
    # employee's ledger scope, and the lock must be taken before any read of
    # the state the write depends on.
    lock_employee_facts(session, employee.id)
    if employee.idp_subject == principal.subject:
        raise forbidden()  # self-request rejected at create (D7)
    request = AccessRequest(
        employee_id=employee.id,
        requester_principal=principal.subject,
        purpose=purpose,
        period_start=period_start,
        period_end=period_end,
        view_type=view_type,
        message=message,
        status=AccessRequestStatus.PENDING,
    )
    session.add(request)
    session.flush()
    record_audit(
        session,
        actor_principal=principal.subject,
        action="ACCESS_REQUEST_CREATE",
        outcome="SUCCESS",
        object_type="access_request",
        object_id=request.id,
        employee_subject_id=employee.id,
        metadata={"request_purpose": purpose.value, "month": month, "view_type": view_type},
    )
    return request


def list_hr_access_requests(session: Session, principal: Principal) -> list[AccessRequest]:
    """The requester's own requests only; metadata only, no ledger content (D12).

    The requester is ALWAYS the grantee of any grant that follows, so the
    ACTIVE grant id is exposed as a pseudonymous metadata pulse (never
    ledger data) — this is how the HR UI opens the granted view without a
    lookup route (disclosure boundary preserved).
    """
    return list(
        session.scalars(
            select(AccessRequest)
            .where(AccessRequest.requester_principal == principal.subject)
            .order_by(AccessRequest.created_at.desc())
        )
    )


def list_employee_access_requests(session: Session, employee: Employee) -> list[AccessRequest]:
    """Requests awaiting (or already answered by) this employee."""
    return list(
        session.scalars(
            select(AccessRequest)
            .where(AccessRequest.employee_id == employee.id)
            .order_by(AccessRequest.created_at.desc())
        )
    )


def _owned_request(session: Session, employee: Employee, request_id: uuid.UUID) -> AccessRequest:
    """Ownership inside the lookup predicate (M4): foreign id == nonexistent id."""
    request = session.scalar(
        select(AccessRequest).where(
            AccessRequest.id == request_id, AccessRequest.employee_id == employee.id
        )
    )
    if request is None:
        raise _nondisclosing_not_found()
    return request


# ---------------------------------------------------------------------------
# Frozen snapshot projection (D8)
# ---------------------------------------------------------------------------


def _freeze_month_snapshot(
    session: Session,
    employee: Employee,
    month: date,
) -> tuple:
    """Write ONE new frozen append-only period_calculation_snapshot.

    The projection is the requested Work Time Record/month view (§13.4):
    per-day worked/required/delta minutes plus the current intervals. Private
    comments and findings are deliberately excluded (DP-DATA-002, ACCESS-003).
    """
    from app.policy.models import PeriodCalculationSnapshot
    from app.policy.service import _calendar_classes, _policy_version, calculate_employee_period
    from app.worktime.queries import current_intervals, month_bounds

    start, end = month_bounds(month)
    intervals = current_intervals(
        session, employee_id=employee.id, intersects_from=start, intersects_to=end
    )
    # A grant is scoped to ONE month, so the projection is clipped to it. An interval
    # that crosses the boundary is shown only for the part inside the granted period,
    # with the duration recomputed from the clipped ends and ``clipped`` marking it.
    # Without this a July grant would reveal the August portion of a 31 July to
    # 1 August interval (ACCESS-009).
    disclosed_intervals = []
    for interval in intervals:
        clipped_start = max(interval.start_at, start)
        clipped_end = min(interval.end_at, end)
        if clipped_start >= clipped_end:
            continue
        disclosed_intervals.append(
            {
                "start_at": clipped_start.astimezone(BERLIN).isoformat(),
                "end_at": clipped_end.astimezone(BERLIN).isoformat(),
                "work_type": interval.work_type.value,
                "elapsed_seconds": elapsed_seconds(clipped_start, clipped_end),
                "clipped": clipped_start != interval.start_at or clipped_end != interval.end_at,
            }
        )
    _, calendar_version_ids = _calendar_classes(session, start.date(), end.date())
    policy_version = _policy_version(session, month)
    calculation = calculate_employee_period(session, employee, month)
    result = {
        "employee_identifier": employee.external_employee_id or str(employee.id),
        "period_start": start.date().isoformat(),
        "period_end": end.date().isoformat(),
        # No wall-clock stamp: the frozen projection must be byte-reproducible
        # (INV-11). When the disclosure happened is already ``created_at``.
        "daily": [
            {
                "date": row.day.isoformat(),
                "worked_minutes": row.worked_minutes,
                "required_minutes": row.required_minutes,
                "delta_minutes": row.flex_delta_minutes,
            }
            for row in calculation.daily_results
        ],
        "intervals": disclosed_intervals,
    }
    period = session.scalar(
        select(WorkPeriod).where(
            WorkPeriod.employee_id == employee.id, WorkPeriod.period_month == month
        )
    )
    if period is None:  # empty month: create the period row (ON CONFLICT pattern)
        session.execute(
            pg_insert(WorkPeriod)
            .values(employee_id=employee.id, period_month=month)
            .on_conflict_do_nothing(index_elements=["employee_id", "period_month"])
        )
        period = session.scalar(
            select(WorkPeriod).where(
                WorkPeriod.employee_id == employee.id, WorkPeriod.period_month == month
            )
        )
        if period is None:  # pragma: no cover - ON CONFLICT guarantees presence
            raise RuntimeError("work period vanished after insert")
    snapshot_no = (
        session.scalar(
            select(sa.func.max(PeriodCalculationSnapshot.snapshot_no)).where(
                PeriodCalculationSnapshot.work_period_id == period.id
            )
        )
        or 0
    ) + 1
    snapshot = PeriodCalculationSnapshot(
        work_period_id=period.id,
        snapshot_no=snapshot_no,
        policy_version_id=policy_version.id,
        calendar_version_ids=[str(value) for value in calendar_version_ids],
        engine_build=policy_version.engine_rule_set,
        # The ENGINE INPUT fingerprint — the deterministic anchor that covers
        # every material calculation input (period bounds, requirements,
        # calendar classes + version ids, each interval's id/instants/work type,
        # opening ledger entries, the resolved policy edition and the travel
        # inputs). It used to hold ``payload_fingerprint(result)``, i.e. a hash
        # of the OUTPUT including a wall-clock stamp, so a stored result could
        # never be reproduced by a recomputation (F4.5).
        input_fingerprint=calculation.input_fingerprint,
        result_json=result,
        frozen=True,
    )
    session.add(snapshot)
    session.flush()
    return snapshot, result, policy_version


# ---------------------------------------------------------------------------
# Grants (ACCESS-008/009, BE-CMD-006)
# ---------------------------------------------------------------------------


def grant_access_request(
    session: Session,
    principal: Principal,
    employee: Employee,
    request_id: uuid.UUID,
) -> DisclosureGrant:
    """Employee opens the REQUESTED one-month view; no requester self-grant.

    Lock order, always: take the employee-fact lock BEFORE reading any
    request/package/fact state, then perform the conditional transition and
    every dependent write under the same lock and transaction.
    """
    lock_employee_facts(session, employee.id)
    request = _owned_request(session, employee, request_id)
    if request.requester_principal == employee.idp_subject:
        raise forbidden()  # self-grant rejected at grant (D7)
    if request.status != AccessRequestStatus.PENDING:
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_VERSION,
            "Only a pending access request can be granted.",
        )
    # The predicate IS the decision: a competing writer that bypasses the
    # advisory lock cannot slip a terminal state past this UPDATE. Doing it
    # first also means no snapshot work happens on a doomed grant.
    now = datetime.now(UTC)
    transitioned = session.execute(
        update(AccessRequest)
        .where(
            AccessRequest.id == request.id,
            AccessRequest.status == AccessRequestStatus.PENDING,
        )
        .values(status=AccessRequestStatus.GRANTED, closed_at=now)
        .returning(AccessRequest.id)
        .execution_options(synchronize_session=False)
    ).first()
    if transitioned is None:
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_VERSION,
            "Only a pending access request can be granted.",
        )
    # Required: the ORM instance is stale after a Core UPDATE, and the response
    # is built from its attributes.
    session.refresh(request)
    snapshot, _, _ = _freeze_month_snapshot(session, employee, request.period_start)
    # Atomic scope-lineage: every prior ACTIVE grant for the same
    # (employee, grantee, purpose, period, view) is revoked; the newest one is
    # referenced via supersedes_id (D8: re-grant atomically revokes prior).
    # The predicate MUST be aliased: comparing `DisclosureGrant.supersedes_id`
    # to the same table's `id` is a self-comparison that is always false, which
    # made `~superseded` inert and revoked superseded rows as if they were
    # current. `revoked_at` must mean "stopped being current by revocation";
    # supersession is expressed by the chain alone (same pattern as
    # `disclosure/guards.py`).
    newer_grant = aliased(DisclosureGrant)
    superseded = exists(
        select(newer_grant.id).where(newer_grant.supersedes_id == DisclosureGrant.id)
    )
    priors = session.scalars(
        select(DisclosureGrant)
        .where(
            DisclosureGrant.employee_id == employee.id,
            DisclosureGrant.grantee_principal == request.requester_principal,
            DisclosureGrant.purpose == request.purpose,
            DisclosureGrant.period_start == request.period_start,
            DisclosureGrant.period_end == request.period_end,
            DisclosureGrant.view_type == request.view_type,
            DisclosureGrant.revoked_at.is_(None),
            ~superseded,
        )
        .order_by(DisclosureGrant.granted_at.desc())
    ).all()
    for prior in priors:
        prior.revoked_at = now
    grant = DisclosureGrant(
        access_request_id=request.id,
        employee_id=employee.id,
        grantee_principal=request.requester_principal,
        purpose=request.purpose,
        period_start=request.period_start,
        period_end=request.period_end,
        view_type=request.view_type,
        snapshot_id=snapshot.id,
        supersedes_id=priors[0].id if priors else None,
        expires_at=None,  # ACCESS-014 PROVISIONAL — no guessed retention
    )
    session.add(grant)
    session.flush()  # the request status was already transitioned above
    record_audit(
        session,
        actor_principal=principal.subject,
        action="DISCLOSURE_GRANT_CREATE",
        outcome="SUCCESS",
        object_type="disclosure_grant",
        object_id=grant.id,
        employee_subject_id=employee.id,
        metadata={
            "grant_purpose": grant.purpose.value,
            "month": request.period_start.isoformat(),
            "view_type": grant.view_type,
        },
    )
    return grant


def decline_access_request(
    session: Session,
    principal: Principal,
    employee: Employee,
    request_id: uuid.UUID,
) -> AccessRequest:
    """Record the employee's answer without exposing any data (BE §8.1).

    Lock first, then read, then a conditional UPDATE: the predicate is the
    decision, so a competing writer that bypasses the advisory lock (as the F0
    reproduction's granter does) cannot leave the request DECLINED while an
    active grant for it exists.
    """
    lock_employee_facts(session, employee.id)
    request = _owned_request(session, employee, request_id)
    if request.status != AccessRequestStatus.PENDING:
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_VERSION,
            "Only a pending access request can be declined.",
        )
    transitioned = session.execute(
        update(AccessRequest)
        .where(
            AccessRequest.id == request.id,
            AccessRequest.status == AccessRequestStatus.PENDING,
        )
        .values(status=AccessRequestStatus.DECLINED, closed_at=datetime.now(UTC))
        .returning(AccessRequest.id)
        .execution_options(synchronize_session=False)
    ).first()
    if transitioned is None:
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_VERSION,
            "Only a pending access request can be declined.",
        )
    session.refresh(request)  # stale after the Core UPDATE; the response reads it
    session.flush()
    record_audit(
        session,
        actor_principal=principal.subject,
        action="ACCESS_REQUEST_DECLINE",
        outcome="SUCCESS",
        object_type="access_request",
        object_id=request.id,
        employee_subject_id=employee.id,
        metadata={
            "request_purpose": request.purpose.value,
            "month": request.period_start.isoformat(),
        },
    )
    return request


def read_grant_projection(
    session: Session,
    principal: Principal,
    grant_id: uuid.UUID,
) -> tuple[DisclosureGrant, dict]:
    """Grantee reads ONLY the frozen projection of their active grant.

    Object-scoped: the principal must be the exact grantee (D6). Reads are
    audited only AFTER the authorization succeeds (D11).
    """
    grant = session.scalar(select(DisclosureGrant).where(DisclosureGrant.id == grant_id))
    if grant is None or grant.grantee_principal != principal.subject:
        raise forbidden()
    if grant.revoked_at is not None:
        raise api_error(
            status.HTTP_410_GONE,
            ErrorCode.NOT_FOUND,
            "This disclosure grant is no longer active.",
        )
    superseded = session.scalar(
        select(DisclosureGrant.id).where(DisclosureGrant.supersedes_id == grant.id)
    )
    if superseded is not None:
        raise api_error(
            status.HTTP_410_GONE,
            ErrorCode.NOT_FOUND,
            "This disclosure grant is no longer active.",
        )
    if grant.expires_at is not None and grant.expires_at <= datetime.now(UTC):
        raise api_error(
            status.HTTP_410_GONE,
            ErrorCode.NOT_FOUND,
            "This disclosure grant is no longer active.",
        )
    record_audit(
        session,
        actor_principal=principal.subject,
        action="DISCLOSURE_READ",
        outcome="SUCCESS",
        object_type="disclosure_grant",
        object_id=grant.id,
        employee_subject_id=grant.employee_id,
        metadata={
            "grant_purpose": grant.purpose.value,
            "month": grant.period_start.isoformat(),
            "view_type": grant.view_type,
        },
    )
    from app.policy.models import PeriodCalculationSnapshot

    if grant.snapshot_id is None:  # pragma: no cover - grants are created with snapshots
        raise api_error(
            status.HTTP_410_GONE,
            ErrorCode.NOT_FOUND,
            "This disclosure grant is no longer active.",
        )
    snapshot = session.get(PeriodCalculationSnapshot, grant.snapshot_id)
    if snapshot is None:  # pragma: no cover - FK guarantees presence
        raise api_error(
            status.HTTP_410_GONE,
            ErrorCode.NOT_FOUND,
            "This disclosure grant is no longer active.",
        )
    return grant, dict(snapshot.result_json)


# ---------------------------------------------------------------------------
# Submission packages (ACCESS-004/005/006)
# ---------------------------------------------------------------------------

PACKAGE_PURPOSES = frozenset(
    {
        SubmissionPurpose.SHIFT_REPORT,
        SubmissionPurpose.OCD_COMPENSATION,
        SubmissionPurpose.OVERTIME_PAID,
        SubmissionPurpose.OVERTIME_TIME_COMP,
    }
)
ASSIGNMENT_PACKAGE_PURPOSES = frozenset(
    {SubmissionPurpose.SHIFT_REPORT, SubmissionPurpose.OCD_COMPENSATION}
)
OVERTIME_PACKAGE_PURPOSES = frozenset(
    {SubmissionPurpose.OVERTIME_PAID, SubmissionPurpose.OVERTIME_TIME_COMP}
)


def _submission_scope_error(message: str) -> NoReturn:
    raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, ErrorCode.VALIDATION_FAILED, message)


def _blocked_submission_data(message: str) -> NoReturn:
    raise api_error(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        ErrorCode.SUBMISSION_MISSING_REQUIRED_DATA,
        message,
    )


#: Findings that report an UNRESOLVED ENTITLEMENT rather than a missing FACT.
#: They block no factual package: the employee may share what happened, and the
#: recipient is told compensation needs review. EVERY other BLOCKED_POLICY finding
#: still blocks, so this is an allowlist, never a blanket exemption.
NON_BLOCKING_ENTITLEMENT_FINDINGS: frozenset[str] = frozenset({"SHIFT-009", "SHIFT-010"})


def _contains_blocked_policy(preview: object, purpose: SubmissionPurpose) -> bool:
    """Findings remain employee-private and block recipient package creation.

    One narrow exception: for a factual SHIFT_REPORT, the entitlement-class
    findings do not block. A cancelled or aborted shift is a real thing that
    happened, and the employee must be able to report it while SHIFT-009/010 are
    still unresolved — the alternative is that the fact cannot be shared at all
    until an institutional source settles the entitlement. The recipient does not
    get the findings; it gets one report-level ``compensation_review_status``, so
    nothing about the employee's private compliance picture is exposed to make this
    work.
    """
    exempt = (
        NON_BLOCKING_ENTITLEMENT_FINDINGS
        if purpose is SubmissionPurpose.SHIFT_REPORT
        else frozenset()
    )
    return any(
        getattr(finding, "status", None) == "BLOCKED_POLICY"
        and getattr(finding, "rule_id", None) not in exempt
        for finding in getattr(preview, "findings", [])
    )


def _assignment_period(
    session: Session, employee: Employee, purpose: SubmissionPurpose, assignment_id: uuid.UUID
) -> tuple[date, date]:
    """Berlin-local inclusive assignment dates; no fabricated cross-month dates."""
    if purpose == SubmissionPurpose.SHIFT_REPORT:
        from app.shift.models import ShiftAssignment, ShiftPlanRevision

        assignment = session.scalar(
            select(ShiftAssignment).where(
                ShiftAssignment.id == assignment_id,
                ShiftAssignment.employee_id == employee.id,
                # Only an ACTIVE plan has a disclosable period. A withdrawn plan is
                # retained lineage the employee can still see in their own list, but
                # it is not a plan anything may be submitted from.
                ShiftAssignment.status.in_(ACTIVE_ASSIGNMENT_STATUSES),
            )
        )
        if assignment is None:
            raise _nondisclosing_not_found()
        revision = session.scalar(
            select(ShiftPlanRevision).where(ShiftPlanRevision.id == assignment.approved_revision_id)
        )
        if revision is None:  # pragma: no cover - FK-backed assignment invariant
            raise _nondisclosing_not_found()
        return (
            revision.planned_start_at.astimezone(BERLIN).date(),
            revision.planned_end_at.astimezone(BERLIN).date(),
        )

    from app.ocd_overtime.models import OcdAssignment

    # Head-only, matching every other user-supplied OCD id: a superseded
    # availability is not a disclosable scope, and a stale id is not silently
    # redirected to the current head (it is indistinguishable from a nonexistent
    # one, which is what keeps this path from becoming an existence oracle).
    #
    # PLUS the OCD domain's OWN active predicate — head-only is not enough. A
    # CANCELLED row IS its own chain head, so without this a retracted availability
    # resolved a disclosable period and the recipient projection then computed a
    # duty classification from it, carrying hourly-wage units for a plan the
    # employee had cancelled and with no ``status`` field for the recipient to
    # notice. The SHIFT branch three blocks above applies its own set for exactly
    # the same reason; the two enums share no member, so each branch applies its
    # own (EINV-32) — never a shared ``ACTIVE_STATUSES``.
    #
    # The asymmetry with the employee's own preview is deliberate (§4.7.1): they
    # may still see what a cancelled availability would have been, while the
    # RECIPIENT path refuses with this path's existing nondisclosing 404.
    successor = aliased(OcdAssignment)
    assignment = session.scalar(
        select(OcdAssignment).where(
            OcdAssignment.id == assignment_id,
            OcdAssignment.employee_id == employee.id,
            OcdAssignment.status.in_(OCD_ACTIVE_STATUSES),
            ~exists(
                select(successor.id).where(
                    successor.supersedes_id == OcdAssignment.id,
                    successor.employee_id == employee.id,
                )
            ),
        )
    )
    if assignment is None:
        raise _nondisclosing_not_found()
    return (
        assignment.start_at.astimezone(BERLIN).date(),
        assignment.end_at.astimezone(BERLIN).date(),
    )


def _recompute_package_preview(
    session: Session,
    employee: Employee,
    purpose: SubmissionPurpose,
    scope: object,
) -> tuple[dict, date, date]:
    """Rebuild exactly the private preview whose fingerprint the employee saw."""
    if purpose not in PACKAGE_PURPOSES:
        _submission_scope_error("This purpose cannot be submitted as a package.")

    if purpose in ASSIGNMENT_PACKAGE_PURPOSES:
        from app.disclosure.schemas import AssignmentSubmissionScope

        if not isinstance(scope, AssignmentSubmissionScope):
            _submission_scope_error("Choose the assignment this package covers.")
        assignment_id = scope.assignment_id
        period_start, period_end = _assignment_period(session, employee, purpose, assignment_id)
        if purpose == SubmissionPurpose.SHIFT_REPORT:
            from app.shift.service import report_preview

            preview = report_preview(session, employee, assignment_id)
        else:
            from app.ocd_overtime.service import ocd_preview

            preview = ocd_preview(session, employee, assignment_id)
            if preview.election.mode is None:
                _blocked_submission_data("OCD compensation requires a compensation election.")
    else:
        from app.disclosure.schemas import MonthSubmissionScope

        if not isinstance(scope, MonthSubmissionScope):
            _submission_scope_error("Choose the month this overtime package covers.")
        month = scope.month
        period_start, period_end = _month_bounds(month)
        from app.ocd_overtime.service import overtime_preview

        # Existing overtime preview uses [from, to) Berlin-midnight bounds.
        preview = overtime_preview(session, employee, period_start, period_end + timedelta(days=1))
        election_mode = preview.election.mode
        if election_mode is None:
            _blocked_submission_data("Overtime submission requires a compensation election.")
        expected_mode = "PAY" if purpose == SubmissionPurpose.OVERTIME_PAID else "TIME"
        if election_mode.value != expected_mode:
            _blocked_submission_data(
                "The compensation election does not match this overtime purpose."
            )

    if _contains_blocked_policy(preview, purpose):
        _blocked_submission_data("Required submission data is blocked by policy.")
    from app.disclosure.fingerprint import payload_dict

    return payload_dict(preview), period_start, period_end


IDEMPOTENCY_CONSTRAINT = "uq_submission_package_client_request_id"


def _replay_or_reject(
    session: Session, employee: Employee, client_request_id: uuid.UUID
) -> SubmissionPackage | None:
    """The single submission-package idempotency contract — used twice.

    Same owner -> replay the existing package (``created=False`` semantics).
    Other owner -> the privacy-preserving 422: returning or revealing another
    employee's package would be a disclosure.
    Absent -> ``None`` (the caller proceeds to create).
    """
    existing = session.scalar(
        select(SubmissionPackage).where(SubmissionPackage.client_request_id == client_request_id)
    )
    if existing is None:
        return None
    if existing.employee_id == employee.id:
        return existing
    raise api_error(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        ErrorCode.VALIDATION_FAILED,
        "This change was already recorded — reload the page to see it.",
    )


def create_submission_package(
    session: Session,
    principal: Principal,
    employee: Employee,
    *,
    purpose: SubmissionPurpose,
    scope: object,
    preview_fingerprint: str,
    client_request_id: uuid.UUID,
    recipients: list[str],
) -> SubmissionPackage:
    """Freeze an employee-confirmed minimized projection for exact recipients.

    Three distinct uniqueness questions meet on this table and they are NOT
    equivalent, because ``client_request_id`` is unique GLOBALLY while the
    employee-fact lock is PER EMPLOYEE:

    - request idempotency (``client_request_id``) — a retry must replay; a
      DIFFERENT owner's key must not leak that owner's package. Two employees
      hold different locks, so this one needs its own handling below.
    - semantic scope/version and successor uniqueness — both contain the
      employee, so the lock makes them unreachable and a violation stays LOUD.
    """
    lock_employee_facts(session, employee.id)
    replay = _replay_or_reject(session, employee, client_request_id)
    if replay is not None:
        return replay
    if not recipients:
        _blocked_submission_data("At least one recipient is required.")
    _require_registered_recipients(session, recipients)
    payload, period_start, period_end = _recompute_package_preview(
        session, employee, purpose, scope
    )
    if payload_fingerprint(payload) != preview_fingerprint:
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.SUBMISSION_CHANGED,
            "Facts changed since preview; re-preview before submitting.",
            details={"re_preview": True},
        )

    latest = session.scalar(
        select(SubmissionPackage)
        .where(
            SubmissionPackage.employee_id == employee.id,
            SubmissionPackage.purpose == purpose,
            SubmissionPackage.period_start == period_start,
            SubmissionPackage.period_end == period_end,
        )
        .order_by(SubmissionPackage.version.desc(), SubmissionPackage.created_at.desc())
    )
    version = (latest.version + 1) if latest is not None else 1
    if latest is not None and latest.status == SubmissionPackageStatus.SUBMITTED:
        latest.status = SubmissionPackageStatus.SUPERSEDED
    package = SubmissionPackage(
        employee_id=employee.id,
        purpose=purpose,
        period_start=period_start,
        period_end=period_end,
        version=version,
        supersedes_id=latest.id if latest is not None else None,
        # A frozen package must reference a RESOLVABLE edition, not just carry
        # an unresolvable literal inside its payload (F4.4). ``snapshot_id``
        # stays NULL deliberately: this payload IS the frozen projection, and it
        # is not derived from a month calculation snapshot.
        policy_version_id=policy_version_for_purpose(session, purpose, payload, period_start).id,
        payload_json=payload,
        employee_confirmed_at=datetime.now(UTC),
        client_request_id=client_request_id,
        status=SubmissionPackageStatus.SUBMITTED,
    )
    try:
        # SAVEPOINT: a unique violation must neither poison the request
        # transaction nor discard the advisory lock. It also wraps the
        # ``latest.status = SUPERSEDED`` update, which SQLAlchemy expires on a
        # savepoint rollback.
        with session.begin_nested():
            session.add(package)
            session.flush()
            for recipient in dict.fromkeys(recipients):
                session.add(
                    SubmissionRecipient(
                        submission_package_id=package.id,
                        recipient_principal=recipient,
                        recipient_type="RECIPIENT",
                    )
                )
            session.flush()
    except IntegrityError as exc:
        if constraint_name(exc) != IDEMPOTENCY_CONSTRAINT:
            raise  # scope/version or successor -> loud, never disguised
        # In READ COMMITTED the losing INSERT blocked on the winner's
        # uncommitted unique-index entry until it committed, so the re-read is
        # guaranteed to see it; if the winner had rolled back there would be no
        # exception at all. No sleep, no retry loop.
        replay = _replay_or_reject(session, employee, client_request_id)
        if replay is None:
            raise  # winner vanished: do not guess
        return replay
    record_audit(
        session,
        actor_principal=principal.subject,
        action="SUBMISSION_PACKAGE_CREATE",
        outcome="SUCCESS",
        object_type="submission_package",
        object_id=package.id,
        employee_subject_id=employee.id,
        metadata={"package_purpose": purpose.value, "package_version": version},
    )
    return package


def list_submission_packages(session: Session, employee: Employee) -> list[SubmissionPackage]:
    return list(
        session.scalars(
            select(SubmissionPackage)
            .where(SubmissionPackage.employee_id == employee.id)
            .order_by(SubmissionPackage.created_at.desc())
        )
    )


def submission_recipients(session: Session, package_id: uuid.UUID) -> list[SubmissionRecipient]:
    return list(
        session.scalars(
            select(SubmissionRecipient)
            .where(SubmissionRecipient.submission_package_id == package_id)
            .order_by(SubmissionRecipient.recipient_principal)
        )
    )


def submission_recipients_for_packages(
    session: Session, employee: Employee, packages: list[SubmissionPackage]
) -> dict[uuid.UUID, list[SubmissionRecipient]]:
    """Batch recipients inside the employee's already authorized package scope."""
    if not packages:
        return {}
    recipients: dict[uuid.UUID, list[SubmissionRecipient]] = {}
    rows = session.scalars(
        select(SubmissionRecipient)
        .join(SubmissionPackage, SubmissionPackage.id == SubmissionRecipient.submission_package_id)
        .where(
            SubmissionPackage.employee_id == employee.id,
            SubmissionPackage.id.in_([package.id for package in packages]),
        )
        .order_by(SubmissionRecipient.recipient_principal)
    )
    for recipient in rows:
        recipients.setdefault(recipient.submission_package_id, []).append(recipient)
    return recipients


def withdraw_submission_package(
    session: Session, principal: Principal, employee: Employee, package_id: uuid.UUID
) -> SubmissionPackage:
    lock_employee_facts(session, employee.id)
    package = session.scalar(
        select(SubmissionPackage).where(
            SubmissionPackage.id == package_id, SubmissionPackage.employee_id == employee.id
        )
    )
    if package is None:
        raise _nondisclosing_not_found()
    if package.status != SubmissionPackageStatus.SUBMITTED:
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_VERSION,
            "Only a submitted package can be withdrawn.",
        )
    transitioned = session.execute(
        update(SubmissionPackage)
        .where(
            SubmissionPackage.id == package.id,
            SubmissionPackage.employee_id == employee.id,  # scope in the predicate too
            SubmissionPackage.status == SubmissionPackageStatus.SUBMITTED,
        )
        .values(status=SubmissionPackageStatus.WITHDRAWN)
        .returning(SubmissionPackage.id)
        .execution_options(synchronize_session=False)
    ).first()
    if transitioned is None:
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_VERSION,
            "Only a submitted package can be withdrawn.",
        )
    session.refresh(package)
    session.flush()
    record_audit(
        session,
        actor_principal=principal.subject,
        action="SUBMISSION_PACKAGE_WITHDRAW",
        outcome="SUCCESS",
        object_type="submission_package",
        object_id=package.id,
        employee_subject_id=employee.id,
        metadata={"package_purpose": package.purpose.value, "package_version": package.version},
    )
    return package


def list_recipient_submission_packages(
    session: Session, principal: Principal
) -> list[tuple[SubmissionPackage, SubmissionRecipient, Employee]]:
    rows = session.execute(
        select(SubmissionPackage, SubmissionRecipient, Employee)
        .options(
            load_only(
                SubmissionPackage.id,
                SubmissionPackage.purpose,
                SubmissionPackage.period_start,
                SubmissionPackage.period_end,
                SubmissionPackage.version,
                SubmissionPackage.status,
                SubmissionPackage.supersedes_id,
                SubmissionPackage.created_at,
                raiseload=True,
            ),
            load_only(SubmissionRecipient.read_at, raiseload=True),
            load_only(Employee.id, Employee.external_employee_id, raiseload=True),
        )
        .join(
            SubmissionRecipient,
            SubmissionRecipient.submission_package_id == SubmissionPackage.id,
        )
        .join(Employee, Employee.id == SubmissionPackage.employee_id)
        .where(
            SubmissionRecipient.recipient_principal == principal.subject,
            SubmissionPackage.status == SubmissionPackageStatus.SUBMITTED,
        )
        .order_by(SubmissionPackage.created_at.desc())
    ).all()
    return [(package, recipient, employee) for package, recipient, employee in rows]


def read_recipient_submission_package(
    session: Session, principal: Principal, package_id: uuid.UUID
) -> tuple[SubmissionPackage, SubmissionRecipient]:
    """Object-scoped recipient read; all foreign/inactive cases share one 404."""
    row = session.execute(
        select(SubmissionPackage, SubmissionRecipient)
        .join(
            SubmissionRecipient,
            SubmissionRecipient.submission_package_id == SubmissionPackage.id,
        )
        .where(
            SubmissionPackage.id == package_id,
            SubmissionRecipient.recipient_principal == principal.subject,
            SubmissionPackage.status == SubmissionPackageStatus.SUBMITTED,
        )
    ).one_or_none()
    if row is None:
        raise _nondisclosing_not_found()
    package, recipient = row
    if recipient.read_at is None:
        recipient.read_at = datetime.now(UTC)
    record_audit(
        session,
        actor_principal=principal.subject,
        action="SUBMISSION_READ",
        outcome="SUCCESS",
        object_type="submission_package",
        object_id=package.id,
        employee_subject_id=package.employee_id,
        metadata={"package_purpose": package.purpose.value, "package_version": package.version},
    )
    return package, recipient


def active_grants_for_requests(
    session: Session, requests: list[AccessRequest]
) -> dict[uuid.UUID, DisclosureGrant]:
    """Resolve latest grants and successor status in one authorized-scope query.

    Choose the latest grant BEFORE checking expiry/revocation/supersession:
    filtering inactive rows first would resurrect an older authorization.
    Request ids come from the employee/requester-scoped list, never a global
    grant scan. A successor can belong to another request, so its EXISTS check
    deliberately covers the whole grant lineage.
    """
    if not requests:
        return {}
    latest = (
        select(
            DisclosureGrant.id,
            sa.func.row_number()
            .over(
                partition_by=DisclosureGrant.access_request_id,
                order_by=DisclosureGrant.granted_at.desc(),
            )
            .label("position"),
        )
        .where(DisclosureGrant.access_request_id.in_([request.id for request in requests]))
        .subquery()
    )
    successor = aliased(DisclosureGrant)
    rows = session.execute(
        select(
            DisclosureGrant,
            exists().where(successor.supersedes_id == DisclosureGrant.id).label("superseded"),
        )
        .join(latest, latest.c.id == DisclosureGrant.id)
        .where(latest.c.position == 1)
    )
    now = datetime.now(UTC)
    return {
        grant.access_request_id: grant
        for grant, superseded in rows
        if grant.access_request_id is not None
        and grant.revoked_at is None
        and not superseded
        and (grant.expires_at is None or grant.expires_at > now)
    }


def active_grant_for_request(session: Session, request_id: uuid.UUID) -> DisclosureGrant | None:
    """Current active grant metadata for a single already-authorized request."""
    # Single mutation responses already hold the authorized request id. Keep
    # their one-object lookup without introducing another access-request read.
    grant = session.scalar(
        select(DisclosureGrant)
        .where(DisclosureGrant.access_request_id == request_id)
        .order_by(DisclosureGrant.granted_at.desc())
        .limit(1)
    )
    if grant is None:
        return None
    superseded = session.scalar(
        select(DisclosureGrant.id).where(DisclosureGrant.supersedes_id == grant.id)
    )
    if (
        grant.revoked_at is not None
        or superseded is not None
        or (grant.expires_at is not None and grant.expires_at <= datetime.now(UTC))
    ):
        return None
    return grant
