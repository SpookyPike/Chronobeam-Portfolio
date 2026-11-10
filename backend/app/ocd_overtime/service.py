"""Employee-owned OCD/overtime commands.  All factual mutations are transactional."""

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from fastapi import status
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.audit.models import record_audit
from app.auth.principal import Principal
from app.config import get_settings
from app.db import constraint_name, lock_employee_facts
from app.disclosure.guards import assert_interval_not_disclosed
from app.enums import (
    OCD_ACTIVE_STATUSES,
    CalendarDayClass,
    IntervalSource,
    PayGroupBucket,
    WorkType,
    is_employee_writable_source,
)
from app.errors import ErrorCode, OvertimeAuthorizationDecisionRefused, api_error
from app.identity.models import Employee
from app.identity.service import effective_contract
from app.ocd_overtime.models import (
    EMPLOYEE_SOURCE,
    INSTITUTIONAL_SOURCE,
    CompensationContextType,
    CompensationElection,
    OcdAssignment,
    OcdAssignmentStatus,
    OcdCall,
    OcdCallMode,
    OcdCallStatus,
    OvertimeAuthorization,
    OvertimeAuthStatus,
    is_authoritative_approval,
    is_employee_writable,
)
from app.ocd_overtime.policy import (
    ClassifiedComponent,
    PolicyFinding,
    WorkSpan,
    classify_duty,
    classify_spans,
    monthly_assignment_findings,
    rest_findings,
)
from app.ocd_overtime.report import (
    OcdPreviewInputs,
    OvertimePreviewInputs,
    build_ocd_compensation_preview,
    build_overtime_preview,
)
from app.ocd_overtime.schemas import (
    ComponentOut,
    DutyOut,
    ElectionIn,
    ElectionOut,
    ElectionProjectionOut,
    FindingOut,
    OcdAssignmentCancelIn,
    OcdAssignmentIn,
    OcdAssignmentPatch,
    OcdCallIn,
    OcdCallOut,
    OcdCallPatch,
    OcdCallProjectionOut,
    OcdCompensationPreviewOut,
    OcdOut,
    OvertimeAuthorizationDecisionIn,
    OvertimeAuthorizationIn,
    OvertimeAuthorizationOut,
    OvertimeAuthorizationPatch,
    OvertimeAuthorizationProjectionOut,
    OvertimeIntervalOut,
    OvertimePreviewOut,
    VoidIn,
)
from app.shift.models import ShiftAssignment
from app.worktime.models import Correction, WorkInterval
from app.worktime.queries import current_intervals
from app.worktime.schemas import IntervalPatch
from app.worktime.service import (
    _check_overlap,
    _ensure_period,
    _resolve,
    correct_interval,
    open_reviewed_month_range,
    open_reviewed_months,
)
from app.worktime.walltime import BERLIN, DstResolutionError, resolve_wall_time

ACTOR_ROLE_EMPLOYEE = "EMPLOYEE"

#: The three distinct reasons the submission gate refuses an overtime interval
#: (``ACCESS-006``). One rule id, three messages — the tests discriminate on the
#: MESSAGE because the rule id alone cannot distinguish them.
ACCESS_006_NO_REFERENCE = "Overtime without an authorization reference cannot be submitted."
ACCESS_006_NOT_AUTHORITATIVE = (
    "The linked overtime authorization is not an institutional approval, "
    "so this overtime cannot be submitted."
)
ACCESS_006_OUTSIDE_WINDOW = (
    "The overtime interval is not covered by the linked authorization's validity window, "
    "so it cannot be submitted."
)

#: ``WT-CORE-009`` — the rule's own effective dating.
#:
#: POL-09 (the sample overtime-authorisation policy) applies from **1 June 2026**, and
#: its prior-authorisation clause is what this rule implements. A period the rule does
#: not govern must produce **no** finding, so the advisory below is period-scoped by
#: THIS date rather than applied globally to arbitrary historical periods.
#:
#: It is a **non-edition-bound rule-effective-date literal**: the rule's effective
#: date is a property of its SOURCE, not of an edition's accounting-start resolution
#: key (``ARCH-POL-001``), and it is recorded as such in ``docs/INVARIANTS.md``'s
#: ``EINV-09`` residual list.
WT_CORE_009_VALID_FROM = date(2026, 6, 1)

#: The single ``WT-CORE-009`` message. One rule id, one message: the finding says
#: one thing — the work was not covered by an approval already recorded when it
#: started — and the four-week retroactive bound is deliberately NOT claimed,
#: because its measurement origin is unresolved (``OPEN-20``).
WT_CORE_009_NOT_PRIOR_AUTHORIZED = (
    "This overtime interval was not covered by an approval that had "
    "already been recorded when the work started. Overtime has to be authorized in "
    "advance; retroactive authorization is permitted only in exceptional cases. This "
    "is a warning: submission is not blocked."
)

# The fields a correction may restate on an overtime authorization. ``source``
# is excluded: it records the provenance of the original fact and is immutable
# after creation (the corrected row copies it from the predecessor).
# ``authorized_by`` is excluded too: it names the institutional actor, so it is
# not the employee's to restate (``ACCESS-017``).
OVERTIME_AUTH_PATCHABLE_FIELDS = (
    "valid_from",
    "valid_to",
    "purpose",
    "status",
    "source_ref",
)


def _local_day(instant: datetime) -> date:
    """Berlin calendar date of an instant (period months are local months).

    A NAIVE value is read as a Berlin wall time (docs/ARCHITECTURE.md) instead of being
    left to ``astimezone``'s process-timezone fallback: the same request would
    otherwise attribute a different month on a UTC host than on a Berlin one,
    which made the review invalidation host-dependent.

    F8.1 anchored the write path (``create_authorization`` / ``patch_authorization``
    / ``decide_authorization`` resolve every window endpoint through the Berlin
    chokepoint before it is persisted), so the naive branch below is no longer
    reachable from any route or CLI. It is RETAINED as defence in depth: the
    fallback it prevents is host-timezone-dependent, and a future non-API caller
    that hands this helper a naive instant must not reintroduce it. It is pinned
    by ``tests/test_f8_walltime_anchoring.py``.
    """
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=BERLIN)
    return instant.astimezone(BERLIN).date()


def _valid_to_day(valid_to: datetime | None) -> date | None:
    """Local end date of an authorization window; ``None`` means open-ended.

    ``valid_to`` is the EXCLUSIVE end of the window: the travel calculation
    covers a work slice only when ``slice_end <= valid_to``, and slices are
    half-open, so no instant at or after ``valid_to`` is ever covered. The last
    affected local date is therefore the date of ``valid_to - 1µs`` — mapping
    the endpoint itself would reopen a month (e.g. April for
    ``2032-04-01T00:00``) that the window provably cannot reach.
    """
    if valid_to is None:
        return None
    return _local_day(valid_to - timedelta(microseconds=1))


def _anchor_seam_window(name: str, value: datetime) -> datetime:
    """Anchor an INSTITUTIONAL window endpoint to Berlin (F8.1).

    The seam (``app/overtime_admin.py``) resolves its own wall times, because it
    needs the fold to refuse a DST-ambiguous ``--from`` with the candidates named,
    and therefore hands ``decide_authorization`` an already AWARE instant. An
    aware value is already an instant and passes through untouched; a NAIVE one is
    a Berlin wall time and goes through ``resolve_wall_time`` — the SAME resolver
    the employee HTTP path uses via ``worktime.service._resolve``, so the product
    keeps ONE definition of "a naive wall time is Berlin".

    The refusal is the seam's own domain exception, never ``HTTPException``: this
    writer is called by non-HTTP callers that choose their own reporting.

    The minute-resolution rule is deliberately NOT applied here. It exists because
    factual work commands are minute-resolution (the day/month allocation is
    expressed in whole minutes); a window endpoint is a PERIOD boundary, and the
    operator CLI accepts any ISO-8601 instant, so refusing sub-minute precision
    would reject a legitimate operator input for no accounting gain.
    """
    if value.tzinfo is not None:
        return value
    try:
        return resolve_wall_time(value)
    except (DstResolutionError, ValueError) as exc:
        raise OvertimeAuthorizationDecisionRefused(f"{name}: {exc}") from None


def _authoritative_window(
    row: OvertimeAuthorization | None,
) -> tuple[datetime, datetime | None] | None:
    """The window a row contributes as a CALCULATION INPUT, or ``None``.

    A row is an input only when it is authoritative (``is_authoritative_approval``):
    the travel calculation consumes exactly the authoritative approvals' windows
    to suppress ``TRAVEL-SUNDAY-001``. A non-authoritative row — including a
    legacy employee-self-declared ``APPROVED`` one — contributes nothing.
    """
    if row is None or not is_authoritative_approval(row):
        return None
    return (row.valid_from, row.valid_to)


def _reopen_authorization_months(
    session: Session,
    employee_id: uuid.UUID,
    after: OvertimeAuthorization,
    before: OvertimeAuthorization | None = None,
) -> None:
    """EINV-15: reopen the months the AUTHORITATIVE input set covers (D2c).

    The reopen is derived from the before/after **authoritative-input windows**,
    never from a status literal:

        reopen = union(window(before), window(after))   # None contributes nothing

    with no reopen when the two windows are equal. Deriving it from the input
    state rather than from the new status is what makes the REMOVAL direction
    correct: withdrawing an already-authoritative approval removes a calculation
    input, so the months its old window covered must reopen. Keying on "the new
    status is REJECTED, therefore nothing changed" silently skipped exactly that
    case.

    A non-authoritative row is not an input at all, so an ordinary
    request/cancel or a metadata-only correction still reopens nothing — a
    default open-ended ``REQUESTED`` authorization would otherwise reopen every
    reviewed month from its start date onward.
    """
    old = _authoritative_window(before)
    new = _authoritative_window(after)
    if old == new:
        return
    for window in (old, new):
        if window is None:
            continue
        valid_from, valid_to = window
        open_reviewed_month_range(
            session, employee_id, _local_day(valid_from), _valid_to_day(valid_to)
        )


def _not_found() -> None:
    raise api_error(404, ErrorCode.NOT_FOUND, "Not found.")


def _current(model: Any, employee_id: uuid.UUID) -> Any:
    """The current versions of ``model`` belonging to ONE employee.

    The anti-join is employee-scoped too. ``supersedes_id`` is a plain self-FK, so
    the DATABASE does not forbid a restored or imported row belonging to another
    employee from superseding this one's. An unscoped anti-join would let that
    foreign row decide what is current for THIS employee — hiding their own
    authorization from the list, refusing the link to it, and dropping it from the
    calculations. A successor that is not the same employee's is not a version of
    this employee's fact, so it does not supersede it.
    """
    newer = aliased(model)
    return select(model).where(
        model.employee_id == employee_id,
        ~exists(
            select(newer.id).where(
                newer.supersedes_id == model.id,
                newer.employee_id == model.employee_id,
            )
        ),
    )  # type: ignore[attr-defined]


def _authorization_chain(
    session: Session, employee_id: uuid.UUID, auth_id: uuid.UUID
) -> list[OvertimeAuthorization]:
    """The linked row's WHOLE version chain, HEAD FIRST (D2).

    A link always points at the row the employee attached. An institutional
    decision supersedes that row, so resolving the link requires walking
    ``supersedes_id`` forward: the current-version predicate alone drops the
    linked id from the projection and the interval then produces NO finding at
    all — the stale-link hole, which becomes the normal path once approvals
    supersede the requests they decide.
    ``uq_overtime_authorization_supersedes_id`` makes the walk unambiguous.

    The whole chain matters, not only its head: the employee's COMPENSATION
    ELECTION is bound to the row they selected it on, which a later version then
    supersedes. Resolving only the head would lose that election and refuse an
    ordinary ``request -> election -> approval -> submit`` flow for a missing
    election.

    The walk therefore starts at the chain's ROOT, not at the linked row. One
    chain is one logical authorization (``patch_authorization`` versions it) and
    the employee may attach the link to ANY version, so an election recorded on
    an EARLIER version than the linked one must still be found. Walking forward
    from the linked row alone loses it and refuses an ordinary
    ``request -> election -> correct -> link -> approve -> submit`` flow.
    """
    row = session.scalar(
        select(OvertimeAuthorization).where(
            OvertimeAuthorization.id == auth_id,
            OvertimeAuthorization.employee_id == employee_id,
        )
    )
    if row is None:
        return []
    # Back to the root first. The employee check is fail-closed: a chain cannot
    # cross employees (FK + uq_supersedes_id), and refusing to follow an
    # unexpected predecessor is cheaper than trusting that it cannot happen.
    root_first = [row]
    while root_first[0].supersedes_id is not None:
        parent = session.get(OvertimeAuthorization, root_first[0].supersedes_id)
        if parent is None or parent.employee_id != employee_id:
            break
        root_first.insert(0, parent)
    # Then forward from the root to the head. The employee filter is the same
    # fail-closed guard as the walk back: ``supersedes_id`` is a plain self-FK, so
    # the DATABASE does not forbid a restored or imported row belonging to another
    # employee from superseding this one. Following such an edge would resolve a
    # foreign approval as this employee's head and expose it in their projection —
    # so a cross-employee successor is simply not followed, the linked row stays
    # the head, and the interval then fails the authority check.
    while True:
        child = session.scalar(
            select(OvertimeAuthorization).where(
                OvertimeAuthorization.supersedes_id == root_first[-1].id,
                OvertimeAuthorization.employee_id == employee_id,
            )
        )
        if child is None:
            return list(reversed(root_first))
        root_first.append(child)


def _chain_head(
    session: Session, employee_id: uuid.UUID, auth_id: uuid.UUID
) -> OvertimeAuthorization | None:
    """The CURRENT head of a linked authorization (the chain's first element)."""
    chain = _authorization_chain(session, employee_id, auth_id)
    return chain[0] if chain else None


def _covers(row: OvertimeAuthorization, start_at: datetime, end_at: datetime) -> bool:
    """``[valid_from, valid_to)`` CONTAINS ``[start_at, end_at)`` (E12).

    Containment, not overlap — matching the travel calculation's own semantics.
    A partially covered interval is a partial claim, and ``split_interval`` is
    the employee's remedy. ``valid_to = None`` is open-ended.
    """
    if start_at.astimezone(UTC) < row.valid_from.astimezone(UTC):
        return False
    return row.valid_to is None or end_at.astimezone(UTC) <= row.valid_to.astimezone(UTC)


def _as_of_authorization(
    chain: list[OvertimeAuthorization], instant: datetime
) -> OvertimeAuthorization | None:
    """The authorization version that existed AS OF ``instant`` (``WT-CORE-009``).

    ``chain`` is head-first (``_authorization_chain``). Prior-ness is the state at
    the **work's start**: the LATEST version whose VERSION RECORDED TIMESTAMP
    (``created_at``) is at or before ``instant`` — never a minimum over the chain's
    history. A minimum is wrong in BOTH directions:

    * a **post-work extension** — an approval recorded before the work, later
      extended to reach it — would SUPPRESS a warning that is due, because the
      earliest row is a valid approval recorded before the work;
    * an approval **withdrawn before** the work, followed by a later one, would
      launder the interval as prior-authorized.

    ``created_at`` is the version **recorded** timestamp, not a "decision instant":
    an employee-authored ``REQUESTED`` row carries it too, and that timestamp is not
    an institutional decision. For a version whose ``source`` is institutional and
    whose ``status`` is an institutional decision, the same value IS Chronobeam's
    persisted institutional-decision timestamp. It is the writing transaction's
    start, not the commit instant — immaterial at day granularity, but not to be
    overstated.
    """
    best: OvertimeAuthorization | None = None
    best_at: datetime | None = None
    for row in chain:
        recorded = row.created_at
        if recorded is None or recorded.astimezone(UTC) > instant.astimezone(UTC):
            continue
        if best_at is None or recorded > best_at:
            best = row
            best_at = recorded
    return best


def _assignment(session: Session, employee_id: uuid.UUID, value: uuid.UUID) -> OcdAssignment:
    row = session.scalar(
        select(OcdAssignment).where(
            OcdAssignment.id == value, OcdAssignment.employee_id == employee_id
        )
    )
    if row is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Not found.")
    return row


def _call(session: Session, employee_id: uuid.UUID, value: uuid.UUID) -> OcdCall:
    row = session.scalar(
        select(OcdCall).where(OcdCall.id == value, OcdCall.employee_id == employee_id)
    )
    if row is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Not found.")
    return row


def _current_assignment(
    session: Session, employee_id: uuid.UUID, assignment_id: uuid.UUID
) -> OcdAssignment:
    """The assignment ONLY if it is the chain HEAD; a stale id is a 404.

    A superseded version is not silently redirected to the current head. That is
    the repository's own contract for a user-supplied id (``_current_call``,
    ``patch_authorization``), and it is what keeps this path from becoming an
    existence oracle: a stale id is indistinguishable from one that never existed.

    ``_assignment`` itself stays the owner-scoped primitive — callers that need the
    head use this, and callers that need to accept ANY version of the chain (the
    election context) use ``_ocd_assignment_chain``.
    """
    row = _assignment(session, employee_id, assignment_id)
    newer = aliased(OcdAssignment)
    successor = session.scalar(
        select(newer.id).where(
            newer.supersedes_id == row.id,
            newer.employee_id == employee_id,
        )
    )
    if successor is not None:
        _not_found()
    return row


def _ocd_assignment_chain(
    session: Session, employee_id: uuid.UUID, assignment_id: uuid.UUID
) -> list[OcdAssignment]:
    """The referenced row's WHOLE version chain, HEAD FIRST.

    Same shape and same reasoning as ``_authorization_chain``: walk back to the
    ROOT first (an election may be bound to any earlier version), then forward to
    the head. Both directions are fail-closed on a missing or foreign predecessor
    or successor — ``supersedes_id`` is a plain self-FK, so the DATABASE does not
    forbid a restored or imported row of another employee from superseding this
    one, and following such an edge would resolve a foreign row as this employee's
    head. ``uq_ocd_assignment_supersedes_id`` makes the walk unambiguous.

    Used ONLY where a redirect is explicitly intended (election resolution). A
    user-supplied id goes through ``_current_assignment`` instead.
    """
    row = session.scalar(
        select(OcdAssignment).where(
            OcdAssignment.id == assignment_id,
            OcdAssignment.employee_id == employee_id,
        )
    )
    if row is None:
        return []
    chain = [row]
    while chain[0].supersedes_id is not None:
        parent = session.get(OcdAssignment, chain[0].supersedes_id)
        if parent is None or parent.employee_id != employee_id:
            break
        chain.insert(0, parent)
    while True:
        child = session.scalar(
            select(OcdAssignment).where(
                OcdAssignment.supersedes_id == chain[-1].id,
                OcdAssignment.employee_id == employee_id,
            )
        )
        if child is None:
            return list(reversed(chain))
        chain.append(child)


def _ocd_chain_head(
    session: Session, employee_id: uuid.UUID, assignment_id: uuid.UUID
) -> OcdAssignment | None:
    """Resolve ANY version of the chain to its head, or ``None`` if unknown."""
    chain = _ocd_assignment_chain(session, employee_id, assignment_id)
    return chain[0] if chain else None


def _ocd_chain_election(
    session: Session, employee: Employee, assignment_id: uuid.UUID
) -> CompensationElection | None:
    """The employee's election for this availability, resolved over the CHAIN.

    The election is bound to the exact version row the employee selected it on, and
    a correction supersedes that row — so resolving only the current head would
    lose the election and refuse an ordinary
    ``schedule -> elect -> correct -> preview`` flow for a missing election. Mirrors
    the overtime preview's own head-first chain loop.
    """
    for row in _ocd_assignment_chain(session, employee.id, assignment_id):
        found = _election(session, employee, CompensationContextType.OCD, row.id)
        if found is not None:
            return found
    return None


def _current_call(session: Session, employee_id: uuid.UUID, value: uuid.UUID) -> OcdCall:
    row = _call(session, employee_id, value)
    if session.scalar(select(OcdCall.id).where(OcdCall.supersedes_id == row.id)) is not None:
        _not_found()
    return row


def _travel(mode: OcdCallMode, out: int | None, back: int | None) -> tuple[int, int]:
    out, back = out or 0, back or 0
    if mode == OcdCallMode.REMOTE and (out or back):
        raise api_error(422, ErrorCode.VALIDATION_FAILED, "Remote OCD calls cannot include travel.")
    return out, back


def _span(call: OcdCall) -> tuple[datetime, datetime]:
    return call.call_start_at - timedelta(
        minutes=call.travel_out_minutes or 0
    ), call.call_end_at + timedelta(minutes=call.travel_back_minutes or 0)


def _interval(session: Session, employee_id: uuid.UUID, call_id: uuid.UUID) -> WorkInterval | None:
    child = aliased(WorkInterval)
    return session.scalar(
        select(WorkInterval).where(
            WorkInterval.employee_id == employee_id,
            WorkInterval.linked_ocd_call_id == call_id,
            ~exists(select(child.id).where(child.supersedes_id == WorkInterval.id)),
        )
    )


def _call_out(session: Session, employee_id: uuid.UUID, call: OcdCall) -> OcdCallOut:
    return _call_out_for(call, _interval(session, employee_id, call.id))


def _call_out_for(call: OcdCall, interval: WorkInterval | None) -> OcdCallOut:
    start, end = _span(call)
    return OcdCallOut(
        id=call.id,
        ocd_assignment_id=call.ocd_assignment_id,
        mode=call.mode,
        call_start_at=call.call_start_at,
        call_end_at=call.call_end_at,
        travel_out_minutes=call.travel_out_minutes,
        travel_back_minutes=call.travel_back_minutes,
        comment=call.comment,
        status=call.status,
        version=call.version,
        linked_interval_id=interval.id if interval else None,
        span_start_at=start,
        span_end_at=end,
        elapsed_seconds=int((end.astimezone(UTC) - start.astimezone(UTC)).total_seconds()),
    )


def _finding(items: list[PolicyFinding]) -> list[FindingOut]:
    return [FindingOut(rule_id=x.rule_id, status=x.status, message=x.message) for x in items]


def _components(items: list[ClassifiedComponent]) -> list[ComponentOut]:
    """Public projection of classified minutes.

    The ALLOW-002 supplement's time-equivalent (18 vs 9 min/h) is derived from
    the restricted pay-group bucket and is stripped here: the employee preview
    must equal the recipient payload exactly, and the bucket must not leave
    the employee's private domain.
    """
    return [
        ComponentOut(
            rule_id=x.rule_id,
            component=x.component,
            minutes=x.minutes,
            time_equivalent_minutes=(
                None if x.component == "OVERTIME_SUPPLEMENT" else x.time_equivalent_minutes
            ),
        )
        for x in items
    ]


def _calendar(session: Session, start: date, end: date) -> dict[date, CalendarDayClass]:
    from app.policy.service import _calendar_classes

    raw, _ = _calendar_classes(session, start, end)
    return {day: CalendarDayClass(value.value) for day, value in raw.items()}


def _election(
    session: Session, employee: Employee, context: CompensationContextType, context_id: uuid.UUID
) -> CompensationElection | None:
    return session.scalar(
        _current(CompensationElection, employee.id).where(
            CompensationElection.context_type == context,
            CompensationElection.context_id == context_id,
        )
    )


def _election_out(row: CompensationElection | None) -> ElectionProjectionOut:
    return ElectionProjectionOut(
        id=row.id if row else None,
        mode=row.mode if row else None,
        version=row.version if row else None,
    )


def seed_ocd_assignment(
    session: Session,
    employee: Employee,
    *,
    start_at: datetime,
    end_at: datetime,
    scheduled_at: datetime | None = None,
    source: str = "TEST_SEED",
    external_id: str | None = None,
) -> OcdAssignment:
    if (
        start_at.tzinfo is None
        or end_at.tzinfo is None
        or end_at.astimezone(UTC) <= start_at.astimezone(UTC)
    ):
        raise ValueError("seeded assignment timestamps must be valid aware instants")
    row = session.scalar(
        select(OcdAssignment).where(
            OcdAssignment.employee_id == employee.id,
            OcdAssignment.start_at == start_at,
            OcdAssignment.end_at == end_at,
            OcdAssignment.source == source,
            OcdAssignment.external_id == external_id,
        )
    )
    if row is not None:
        return row
    row = OcdAssignment(
        employee_id=employee.id,
        start_at=start_at,
        end_at=end_at,
        scheduled_at=scheduled_at,
        source=source,
        external_id=external_id,
        status=OcdAssignmentStatus.SCHEDULED,
    )
    session.add(row)
    session.flush()
    return row


OCD_IDEMPOTENCY_CONSTRAINT = "uq_ocd_call_client_request_id"

# --- employee plan entry (pilot slice) -------------------------------------

OCD_ASSIGNMENT_IDEMPOTENCY_CONSTRAINT = "uq_ocd_assignment_client_request_id"

OCD_PLAN_CREATE = "OCD_PLAN_CREATE"
OCD_PLAN_EDIT = "OCD_PLAN_EDIT"
OCD_PLAN_CANCEL = "OCD_PLAN_CANCEL"


def _require_ocd_plan_entry_enabled() -> None:
    """D24: the OCD switch is INDEPENDENT of the shift switch.

    Reading ``enable_shift_plan_entry`` here would couple the two domains, which
    D15/D24 forbid: OCD availability may stay employee-entered after shift planning
    has moved to a planner, and the reverse. 422 ``POLICY_BLOCKED`` rather than 403,
    for the same reason as the shift path — this is not an authorization denial.

    Called AFTER the replay pre-read, so an already-committed same-owner retry still
    succeeds with the switch off (D20).
    """
    if not get_settings().enable_ocd_plan_entry:
        raise api_error(
            422,
            ErrorCode.POLICY_BLOCKED,
            "An authoritative OCD availability source is configured; "
            "availability entry happens there.",
        )


def _replay_assignment_or_reject(
    session: Session, employee: Employee, client_request_id: uuid.UUID | None
) -> OcdAssignment | None:
    """The availability-create idempotency contract — used twice.

    GLOBAL lookup, never employee-scoped: ``client_request_id`` is unique table-wide,
    so a scoped lookup would miss a collision with another employee's key entirely.
    Same owner -> replay. Other owner -> the privacy-preserving 422. Absent -> None.
    """
    if client_request_id is None:
        return None
    existing = session.scalar(
        select(OcdAssignment).where(OcdAssignment.client_request_id == client_request_id)
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


def _assert_ocd_plan_mutable(
    session: Session, employee: Employee, assignment: OcdAssignment
) -> None:
    """D3, binding, for the OCD domain: mutable only while NOTHING depends on it.

    The dependency that freezes an availability is a current ``ocd_call``, and it
    freezes on **any** status including ``VOIDED``. That is deliberate: ``void_call``
    deletes the linked work interval and keeps the voided call as lineage, so letting
    the plan be cancelled afterwards would make an availability that once carried a
    disclosed call silently disappear.

    The practical consequence is that ``ocd_call.ocd_assignment_id`` can only ever
    reference a chain head, which is what makes the call link stable without a chain
    walk.

    409 ``CONFLICT_VERSION`` (not the shift path's ``CONFLICT_SHIFT_PLAN_CHANGED``):
    the two domains have two error vocabularies and must not be unified.
    """
    if assignment.status not in OCD_ACTIVE_STATUSES:
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_VERSION,
            "This availability is no longer scheduled, so it cannot be changed.",
        )
    if not is_employee_writable_source(assignment.source):
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_VERSION,
            "This availability was not recorded by you, so it cannot be changed here.",
        )
    if (
        session.scalar(
            _current(OcdCall, employee.id).where(OcdCall.ocd_assignment_id == assignment.id)
        )
        is not None
    ):
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_VERSION,
            "This availability already has a recorded on-call duty. "
            "Void the duty instead of changing the availability.",
        )


def _ocd_row_context(
    session: Session, employee: Employee, start: datetime, end: datetime
) -> tuple[dict[date, CalendarDayClass], list[PolicyFinding], list[WorkSpan]]:
    """The window-scoped facts every rendered availability shares.

    Extracted so a create/correct/cancel response is rendered EXACTLY as the list
    response renders the same row: an employee must not see a different finding set
    depending on which endpoint returned it.
    """
    calendar = _calendar(
        session, start.astimezone(BERLIN).date(), end.astimezone(BERLIN).date() + timedelta(days=1)
    )
    month = start.astimezone(BERLIN).date().replace(day=1)
    # HEAD-ONLY, and mandatory: ``monthly_assignment_findings`` counts calendar days,
    # so a superseded predecessor that is still SCHEDULED would be counted alongside
    # its own successor and raise a FALSE OCD-003 CRITICAL.
    all_month = session.scalars(_current(OcdAssignment, employee.id)).all()
    limit = monthly_assignment_findings(
        [(x.start_at, x.end_at, x.status.value) for x in all_month], month
    )
    # CURRENT-version predicate only (EINV-13): the rest rule must consume the
    # canonical fact of each prior work period, never a corrected-away version.
    prior = [
        WorkSpan(x.start_at, x.end_at, x.work_type)
        for x in current_intervals(session, employee_id=employee.id)
    ]
    return calendar, limit, prior


def _ocd_out_for(
    session: Session,
    employee: Employee,
    assignment: OcdAssignment,
    context: tuple[dict[date, CalendarDayClass], list[PolicyFinding], list[WorkSpan]],
    *,
    calls: list[OcdCall] | None = None,
    intervals_by_call: dict[uuid.UUID, WorkInterval] | None = None,
) -> OcdOut:
    calendar, limit, prior = context
    if calls is None:
        calls = list(
            session.scalars(
                _current(OcdCall, employee.id).where(OcdCall.ocd_assignment_id == assignment.id)
            )
        )
    _duty, findings = classify_duty(
        assignment.start_at, assignment.end_at, assignment.scheduled_at, calendar
    )
    call_spans = [
        WorkSpan(*_span(x), WorkType.OCD_CALL) for x in calls if x.status == OcdCallStatus.RECORDED
    ]
    findings += limit + rest_findings(call_spans, prior)
    return OcdOut(
        id=assignment.id,
        start_at=assignment.start_at,
        end_at=assignment.end_at,
        status=assignment.status,
        scheduled_at=assignment.scheduled_at,
        reported_notice_at=assignment.reported_notice_at,
        source=assignment.source,
        version=assignment.version,
        calls=[
            _call_out_for(x, intervals_by_call.get(x.id))
            if intervals_by_call is not None
            else _call_out(session, employee.id, x)
            for x in calls
        ],
        findings=_finding(findings),
        compensation_preview_available=True,
    )


def _resolve_ocd_window(
    start_local: datetime, end_local: datetime, start_fold: int | None, end_fold: int | None
) -> tuple[datetime, datetime]:
    start = _resolve("start_local", start_local, start_fold)
    end = _resolve("end_local", end_local, end_fold)
    if end.astimezone(UTC) <= start.astimezone(UTC):
        raise api_error(
            422, ErrorCode.VALIDATION_INTERVAL_ORDER, "availability end must be after its start."
        )
    return start, end


def _resolve_reported_notice(value: datetime | None, fold: int | None) -> datetime | None:
    """Resolve an employee-reported notice instant, or ``None`` when cleared.

    The resolved instant lands in ``reported_notice_at``, NEVER in ``scheduled_at``:
    ``scheduled_at`` is the institutional notice instant and the sole input to the
    remuneration-relevant short-notice classification, so an employee value must not
    reach it (D17). ``reported_notice_at`` is read by no classifier.
    """
    if value is None:
        return None
    return _resolve("reported_notice_local", value, fold)


def create_ocd_assignment(
    session: Session, principal: Principal, employee: Employee, payload: OcdAssignmentIn
) -> tuple[OcdOut, bool]:
    """BE-CMD-001: the employee records an on-call availability for themselves.

    Returns ``(state, created)`` so the route answers 201 for a real create and 200
    for an idempotent replay, matching every other create-producing command here.

    Ordering (D20): authenticate -> detect an exact persisted replay -> only then
    enforce the feature switch, so flipping the switch off cannot turn an
    already-committed same-owner retry into an error.
    """
    lock_employee_facts(session, employee.id)

    replay = _replay_assignment_or_reject(session, employee, payload.client_request_id)
    if replay is not None:
        return _render_ocd(session, employee, replay), False

    _require_ocd_plan_entry_enabled()

    start, end = _resolve_ocd_window(
        payload.start_local, payload.end_local, payload.start_fold, payload.end_fold
    )
    reported = _resolve_reported_notice(payload.reported_notice_local, payload.reported_notice_fold)

    try:
        # SAVEPOINT around the whole create unit: the collision point is the flush
        # below and nothing dependent precedes it, so the loser rolls back only this
        # unit and the request transaction stays usable for the recovery re-read.
        with session.begin_nested():
            assignment = OcdAssignment(
                employee_id=employee.id,
                start_at=start,
                end_at=end,
                # D17: server-forced NULL. The request schema has no such field and
                # ``extra="forbid"`` makes smuggling one a loud failure.
                scheduled_at=None,
                reported_notice_at=reported,
                source=EMPLOYEE_SOURCE,
                external_id=None,
                status=OcdAssignmentStatus.SCHEDULED,
                version=1,
                supersedes_id=None,
                client_request_id=payload.client_request_id,
            )
            session.add(assignment)
            session.flush()
            record_audit(
                session,
                actor_principal=principal.subject,
                action=OCD_PLAN_CREATE,
                outcome="CREATED",
                object_type="OCD_ASSIGNMENT",
                object_id=assignment.id,
                employee_subject_id=employee.id,
            )
    except IntegrityError as exc:
        if constraint_name(exc) != OCD_ASSIGNMENT_IDEMPOTENCY_CONSTRAINT:
            raise  # never disguise an unrelated integrity failure
        # The loser blocked on the winner's uncommitted unique-index entry until it
        # committed, so this re-read is guaranteed to see it.
        winner = _replay_assignment_or_reject(session, employee, payload.client_request_id)
        if winner is None:
            raise  # winner vanished: do not guess
        return _render_ocd(session, employee, winner), False

    return _render_ocd(session, employee, assignment), True


def correct_ocd_assignment(
    session: Session,
    principal: Principal,
    employee: Employee,
    assignment_id: uuid.UUID,
    payload: OcdAssignmentPatch,
) -> OcdOut:
    """Correct an employee-recorded availability by APPENDING a version (INV-11).

    The predecessor row is untouched, so the previously recorded fact stays
    reproducible; the new row becomes the chain head and ``supersedes_id`` links the
    lineage. Correction after disclosure is permitted — only DELETION is refused
    (``disclosure/guards.py``) — so the already-submitted package stays byte-stable
    while the employee keeps the full correction path.
    """
    lock_employee_facts(session, employee.id)
    _require_ocd_plan_entry_enabled()
    # Head-only: a stale id is a nondisclosing 404, never a silent redirect.
    current = _current_assignment(session, employee.id, assignment_id)
    _assert_ocd_plan_mutable(session, employee, current)
    if current.version != payload.version:
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_VERSION,
            "This availability changed; refresh before correcting it.",
        )

    start, end = current.start_at, current.end_at
    if payload.start_local is not None and payload.end_local is not None:
        start, end = _resolve_ocd_window(
            payload.start_local, payload.end_local, payload.start_fold, payload.end_fold
        )
    # PRESENCE, not truthiness: an explicit null must CLEAR a notice the employee
    # entered by mistake, so an omitted field and a null one cannot be conflated.
    if "reported_notice_local" in payload.model_fields_set:
        reported = _resolve_reported_notice(
            payload.reported_notice_local, payload.reported_notice_fold
        )
    else:
        reported = current.reported_notice_at

    successor = OcdAssignment(
        employee_id=employee.id,
        start_at=start,
        end_at=end,
        # The corrected row is still employee-authored, so the institutional notice
        # instant stays NULL: provenance is a property of the WRITER, not of the row's
        # position in the chain.
        scheduled_at=None,
        reported_notice_at=reported,
        source=EMPLOYEE_SOURCE,
        external_id=None,
        status=OcdAssignmentStatus.SCHEDULED,
        version=current.version + 1,
        supersedes_id=current.id,
        client_request_id=None,
    )
    session.add(successor)
    session.flush()
    session.add(
        Correction(
            entity_type="OCD_ASSIGNMENT",
            entity_id=successor.id,
            old_version=current.version,
            new_version=successor.version,
            actor_employee_id=employee.id,
            actor_role=ACTOR_ROLE_EMPLOYEE,
            reason="Availability corrected by the employee.",
        )
    )
    record_audit(
        session,
        actor_principal=principal.subject,
        action=OCD_PLAN_EDIT,
        outcome="UPDATED",
        object_type="OCD_ASSIGNMENT",
        object_id=successor.id,
        employee_subject_id=employee.id,
    )
    return _render_ocd(session, employee, successor)


def cancel_ocd_assignment(
    session: Session,
    principal: Principal,
    employee: Employee,
    assignment_id: uuid.UUID,
    payload: OcdAssignmentCancelIn,
) -> OcdOut:
    """Cancel an availability the employee can no longer provide.

    A version write, not a delete: the predecessor stays as lineage and the new head
    carries ``CANCELLED``. Nothing is erased, so the previously recorded plan remains
    reproducible and the correction path is unchanged.

    Note this is the AVAILABILITY lifecycle, and it is not the shift's "withdrawn"
    act: an availability is not a scheduled shift, so cancelling it carries no
    entitlement question and no compensation basis to protect.
    """
    lock_employee_facts(session, employee.id)
    _require_ocd_plan_entry_enabled()
    current = _current_assignment(session, employee.id, assignment_id)
    _assert_ocd_plan_mutable(session, employee, current)
    if current.version != payload.version:
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_VERSION,
            "This availability changed; refresh before cancelling it.",
        )

    successor = OcdAssignment(
        employee_id=employee.id,
        start_at=current.start_at,
        end_at=current.end_at,
        scheduled_at=None,
        # The reported notice survives the cancellation: it is a fact the employee
        # reported about a real notification, and cancelling the availability does
        # not un-tell them.
        reported_notice_at=current.reported_notice_at,
        source=EMPLOYEE_SOURCE,
        external_id=None,
        status=OcdAssignmentStatus.CANCELLED,
        version=current.version + 1,
        supersedes_id=current.id,
        client_request_id=None,
    )
    session.add(successor)
    session.flush()
    session.add(
        Correction(
            entity_type="OCD_ASSIGNMENT",
            entity_id=successor.id,
            old_version=current.version,
            new_version=successor.version,
            actor_employee_id=employee.id,
            actor_role=ACTOR_ROLE_EMPLOYEE,
            reason="Availability cancelled by the employee.",
        )
    )
    record_audit(
        session,
        actor_principal=principal.subject,
        action=OCD_PLAN_CANCEL,
        outcome="UPDATED",
        object_type="OCD_ASSIGNMENT",
        object_id=successor.id,
        employee_subject_id=employee.id,
    )
    return _render_ocd(session, employee, successor)


def _render_ocd(session: Session, employee: Employee, assignment: OcdAssignment) -> OcdOut:
    """Render ONE availability with its own window context."""
    return _ocd_out_for(
        session,
        employee,
        assignment,
        _ocd_row_context(session, employee, assignment.start_at, assignment.end_at),
    )


def _replay_call_or_reject(
    session: Session, employee: Employee, client_request_id: uuid.UUID | None
) -> OcdCall | None:
    """The OCD-call idempotency contract — used twice (pre-read and recovery).

    Same owner -> replay the existing call. Other owner -> the
    privacy-preserving 422 (the key is globally unique, so the row belongs to
    somebody else and returning it would be a disclosure). Absent -> ``None``.
    """
    if client_request_id is None:
        return None
    existing = session.scalar(select(OcdCall).where(OcdCall.client_request_id == client_request_id))
    if existing is None:
        return None
    if existing.employee_id == employee.id:
        return existing
    raise api_error(
        422,
        ErrorCode.VALIDATION_FAILED,
        "This change was already recorded — reload the page to see it.",
    )


def create_call(
    session: Session, employee: Employee, payload: OcdCallIn
) -> tuple[OcdCallOut, bool]:
    lock_employee_facts(session, employee.id)
    if payload.client_request_id:
        # GLOBAL re-read, not employee-scoped: ``client_request_id`` is unique
        # across the whole table, so an employee-scoped lookup would miss a
        # collision with another employee's key entirely.
        existing = _replay_call_or_reject(session, employee, payload.client_request_id)
        if existing is not None:
            return _call_out(session, employee.id, existing), False
    # A call links only a chain HEAD: a superseded availability is not a live
    # availability, so a stale id is a nondisclosing 404 rather than a silent
    # redirect to the current version.
    assignment = _current_assignment(session, employee.id, payload.ocd_assignment_id)
    if assignment.status != OcdAssignmentStatus.SCHEDULED:
        raise api_error(
            422, ErrorCode.VALIDATION_FAILED, "OCD assignment is not available for call entry."
        )
    start, end = (
        _resolve("call_start_local", payload.call_start_local, payload.start_fold),
        _resolve("call_end_local", payload.call_end_local, payload.end_fold),
    )
    if end.astimezone(UTC) <= start.astimezone(UTC):
        raise api_error(
            422, ErrorCode.VALIDATION_INTERVAL_ORDER, "call end must be after call start."
        )
    travel_out, travel_back = _travel(
        payload.mode, payload.travel_out_minutes, payload.travel_back_minutes
    )
    call = OcdCall(
        ocd_assignment_id=assignment.id,
        employee_id=employee.id,
        call_start_at=start,
        call_end_at=end,
        mode=payload.mode,
        travel_out_minutes=travel_out or None,
        travel_back_minutes=travel_back or None,
        comment=payload.comment,
        status=OcdCallStatus.RECORDED,
        version=1,
        client_request_id=payload.client_request_id,
    )
    try:
        # SAVEPOINT around the whole attempted create unit. Live inspection
        # confirms the collision point is the flush below and that NO dependent
        # write precedes it: the assignment/status guard and the pure travel
        # resolution are reads, and ``_ensure_period`` plus the linked
        # WorkInterval both come AFTER. On a collision the loser therefore
        # raises before either dependent write exists, so there is nothing to
        # unwind — and the savepoint keeps the request transaction usable.
        with session.begin_nested():
            session.add(call)
            session.flush()
            span_start, span_end = _span(call)
            _check_overlap(session, employee.id, span_start, span_end)
            period = _ensure_period(session, employee.id, span_start)
            session.add(
                WorkInterval(
                    employee_id=employee.id,
                    work_period_id=period.id,
                    start_at=span_start,
                    end_at=span_end,
                    work_type=WorkType.OCD_CALL,
                    comment=payload.comment,
                    linked_ocd_call_id=call.id,
                    source=IntervalSource.EMPLOYEE,
                    version=1,
                    created_by=employee.id,
                )
            )
            session.flush()
            open_reviewed_months(session, employee.id, [(span_start, span_end)])
    except IntegrityError as exc:
        if constraint_name(exc) != OCD_IDEMPOTENCY_CONSTRAINT:
            raise  # never disguise an unrelated integrity failure
        # The loser blocked on the winner's uncommitted unique-index entry
        # until it committed, so this re-read is guaranteed to see it.
        replay = _replay_call_or_reject(session, employee, payload.client_request_id)
        if replay is None:
            raise  # winner vanished: do not guess
        return _call_out(session, employee.id, replay), False
    return _call_out(session, employee.id, call), True


def patch_call(
    session: Session, employee: Employee, call_id: uuid.UUID, payload: OcdCallPatch
) -> OcdCallOut:
    lock_employee_facts(session, employee.id)
    current = _current_call(session, employee.id, call_id)
    # EINV-12: VOIDED is terminal for ordinary mutation. Editing a voided call
    # used to regenerate its linked work interval as a fresh root — effective
    # work reappearing from a terminal fact. Restoration, if it is ever
    # required, must be an explicit audited transition, never a PATCH side
    # effect.
    if current.status is not OcdCallStatus.RECORDED:
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_VERSION,
            "A voided OCD call cannot be edited.",
        )
    if current.version != payload.version:
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_VERSION,
            "OCD call changed; refresh and retry.",
        )
    start = (
        _resolve("call_start_local", payload.call_start_local, payload.start_fold)
        if payload.call_start_local
        else current.call_start_at
    )
    end = (
        _resolve("call_end_local", payload.call_end_local, payload.end_fold)
        if payload.call_end_local
        else current.call_end_at
    )
    if end.astimezone(UTC) <= start.astimezone(UTC):
        raise api_error(
            422, ErrorCode.VALIDATION_INTERVAL_ORDER, "call end must be after call start."
        )
    mode = payload.mode or current.mode
    out = (
        payload.travel_out_minutes
        if "travel_out_minutes" in payload.model_fields_set
        else current.travel_out_minutes
    )
    back = (
        payload.travel_back_minutes
        if "travel_back_minutes" in payload.model_fields_set
        else current.travel_back_minutes
    )
    out, back = _travel(mode, out, back)
    replacement = OcdCall(
        ocd_assignment_id=current.ocd_assignment_id,
        employee_id=employee.id,
        call_start_at=start,
        call_end_at=end,
        mode=mode,
        travel_out_minutes=out or None,
        travel_back_minutes=back or None,
        comment=payload.comment if "comment" in payload.model_fields_set else current.comment,
        status=current.status,
        version=current.version + 1,
        supersedes_id=current.id,
    )
    session.add(replacement)
    session.flush()
    old = _interval(session, employee.id, current.id)
    if old is None:
        # Fail closed. A RECORDED head with no current linked interval is an
        # inconsistent canonical state; manufacturing a new root here (the old
        # `else` branch) is what made a voided call's work reappear, and it
        # also skipped the overlap check. Repair belongs to an explicit
        # reconciliation path, never to metadata editing.
        raise RuntimeError("RECORDED OCD call has no current linked work interval")
    span_start, span_end = _span(replacement)
    # Unconditional: every corrected call fact is overlap-checked, whichever
    # branch produced it (EINV-05).
    _check_overlap(session, employee.id, span_start, span_end, exclude_id=old.id)
    # EINV-14: the owning period is re-derived from the new start month, never
    # copied from the predecessor (travel's patch_segment is the reference).
    period = _ensure_period(session, employee.id, span_start)
    new_interval = WorkInterval(
        employee_id=employee.id,
        work_period_id=period.id,
        start_at=span_start,
        end_at=span_end,
        work_type=WorkType.OCD_CALL,
        comment=replacement.comment,
        linked_ocd_call_id=replacement.id,
        source=IntervalSource.EMPLOYEE,
        version=old.version + 1,
        supersedes_id=old.id,
        created_by=employee.id,
    )
    session.add(new_interval)
    session.add(
        Correction(
            entity_type="OCD_CALL",
            entity_id=replacement.id,
            old_version=current.version,
            new_version=replacement.version,
            actor_employee_id=employee.id,
            actor_role=ACTOR_ROLE_EMPLOYEE,
            reason=None,
        )
    )
    session.add(
        Correction(
            entity_type="WORK_INTERVAL",
            entity_id=new_interval.id,
            old_version=old.version,
            new_version=new_interval.version,
            actor_employee_id=employee.id,
            actor_role=ACTOR_ROLE_EMPLOYEE,
            reason=None,
        )
    )
    open_reviewed_months(
        session,
        employee.id,
        [(old.start_at, old.end_at), (new_interval.start_at, new_interval.end_at)],
    )
    session.flush()
    return _call_out(session, employee.id, replacement)


def void_call(
    session: Session, principal: Principal, employee: Employee, call_id: uuid.UUID, payload: VoidIn
) -> OcdCallOut:
    lock_employee_facts(session, employee.id)
    current = _current_call(session, employee.id, call_id)
    # A second terminal write is refused rather than appended (EINV-06's
    # established pattern for decline/withdraw).
    if current.status is OcdCallStatus.VOIDED:
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_VERSION,
            "This OCD call is already voided.",
        )
    if current.version != payload.version:
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_VERSION,
            "OCD call changed; refresh and retry.",
        )
    voided = OcdCall(
        ocd_assignment_id=current.ocd_assignment_id,
        employee_id=employee.id,
        call_start_at=current.call_start_at,
        call_end_at=current.call_end_at,
        mode=current.mode,
        travel_out_minutes=current.travel_out_minutes,
        travel_back_minutes=current.travel_back_minutes,
        comment=current.comment,
        status=OcdCallStatus.VOIDED,
        version=current.version + 1,
        supersedes_id=current.id,
    )
    session.add(voided)
    session.flush()
    interval = _interval(session, employee.id, current.id)
    if interval:
        assert_interval_not_disclosed(session, employee.id, interval)
        open_reviewed_months(session, employee.id, [(interval.start_at, interval.end_at)])
        chain = [interval]
        cursor = interval
        while cursor.supersedes_id is not None:
            cursor = session.get(WorkInterval, cursor.supersedes_id)
            if cursor is None:  # pragma: no cover - self FK protects this chain
                raise RuntimeError("OCD interval correction chain is incomplete")
            chain.append(cursor)
        for row in chain:
            session.delete(row)
            session.flush()
        session.add(
            Correction(
                entity_type="WORK_INTERVAL",
                entity_id=interval.id,
                old_version=interval.version,
                new_version=None,
                actor_employee_id=employee.id,
                actor_role=ACTOR_ROLE_EMPLOYEE,
                reason=payload.reason,
            )
        )
        record_audit(
            session,
            actor_principal=principal.subject,
            action="WORK_INTERVAL_DELETE",
            outcome="SUCCESS",
            object_type="WORK_INTERVAL",
            object_id=interval.id,
            employee_subject_id=employee.id,
            metadata={"interval_version": interval.version},
        )
    session.add(
        Correction(
            entity_type="OCD_CALL",
            entity_id=voided.id,
            old_version=current.version,
            new_version=voided.version,
            actor_employee_id=employee.id,
            actor_role=ACTOR_ROLE_EMPLOYEE,
            reason=payload.reason,
        )
    )
    session.flush()
    return _call_out(session, employee.id, voided)


def list_ocd(session: Session, employee: Employee, start: datetime, end: datetime) -> list[OcdOut]:
    assignments = session.scalars(
        _current(OcdAssignment, employee.id)
        .where(
            OcdAssignment.start_at < end,
            OcdAssignment.end_at > start,
        )
        .order_by(OcdAssignment.start_at)
    ).all()
    calendar, limit, prior = _ocd_row_context(session, employee, start, end)
    context = (calendar, limit, prior)
    if not assignments:
        return []
    assignment_ids = [row.id for row in assignments]
    calls = list(
        session.scalars(
            _current(OcdCall, employee.id).where(OcdCall.ocd_assignment_id.in_(assignment_ids))
        )
    )
    calls_by_assignment: dict[uuid.UUID, list[OcdCall]] = defaultdict(list)
    for call in calls:
        if call.ocd_assignment_id is not None:
            calls_by_assignment[call.ocd_assignment_id].append(call)
    intervals_by_call: dict[uuid.UUID, WorkInterval] = {}
    if calls:
        child = aliased(WorkInterval)
        for interval in session.scalars(
            select(WorkInterval).where(
                WorkInterval.employee_id == employee.id,
                WorkInterval.linked_ocd_call_id.in_([call.id for call in calls]),
                ~exists(select(child.id).where(child.supersedes_id == WorkInterval.id)),
            )
        ):
            # Match the existing scalar lookup's first current fact if legacy
            # data contains more than one interval for the same call.
            if interval.linked_ocd_call_id is not None:
                intervals_by_call.setdefault(interval.linked_ocd_call_id, interval)
    return [
        _ocd_out_for(
            session,
            employee,
            assignment,
            context,
            calls=calls_by_assignment[assignment.id],
            intervals_by_call=intervals_by_call,
        )
        for assignment in assignments
    ]


def ocd_preview(
    session: Session, employee: Employee, assignment_id: uuid.UUID
) -> OcdCompensationPreviewOut:
    assignment = _current_assignment(session, employee.id, assignment_id)
    calls = session.scalars(
        _current(OcdCall, employee.id).where(
            OcdCall.ocd_assignment_id == assignment.id, OcdCall.status == OcdCallStatus.RECORDED
        )
    ).all()
    calendar = _calendar(
        session,
        assignment.start_at.astimezone(BERLIN).date(),
        assignment.end_at.astimezone(BERLIN).date() + timedelta(days=1),
    )
    contract = effective_contract(
        session, employee.id, assignment.start_at.astimezone(BERLIN).date()
    )
    spans = [WorkSpan(*_span(call), WorkType.OCD_CALL) for call in calls]
    components, findings = classify_spans(
        spans,
        calendar,
        contract.pay_group_bucket if contract else PayGroupBucket.UNKNOWN,
        float(contract.fte) if contract and contract.fte is not None else None,
        contract.weekly_required_minutes if contract else None,
    )
    # Call work carries its factual 100% component separately from overtime classification.
    components += [
        ClassifiedComponent(
            "OCD-006",
            "OCD_CALL_BASE",
            int((end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() // 60),
            int((end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() // 60),
        )
        for start, end in (_span(call) for call in calls)
    ]
    duty, duty_findings = classify_duty(
        assignment.start_at, assignment.end_at, assignment.scheduled_at, calendar
    )
    findings += duty_findings
    election = _ocd_chain_election(session, employee, assignment.id)
    return build_ocd_compensation_preview(
        OcdPreviewInputs(
            employee.external_employee_id or str(employee.id),
            assignment.id,
            assignment.start_at,
            assignment.end_at,
            [
                OcdCallProjectionOut(
                    call_id=x.id,
                    mode=x.mode,
                    call_start_at=x.call_start_at,
                    call_end_at=x.call_end_at,
                    travel_out_minutes=x.travel_out_minutes,
                    travel_back_minutes=x.travel_back_minutes,
                    span_start_at=_span(x)[0],
                    span_end_at=_span(x)[1],
                    elapsed_seconds=int(
                        (_span(x)[1].astimezone(UTC) - _span(x)[0].astimezone(UTC)).total_seconds()
                    ),
                )
                for x in calls
            ],
            _components(components),
            DutyOut(
                duty_minutes=duty.duty_minutes,
                duty_kind=duty.duty_kind,
                units=duty.units,
                basis_points=duty.basis_points,
                short_notice=duty.short_notice,
            ),
            _election_out(election),
            assignment.source,
            assignment.reported_notice_at,
            _finding(findings),
        )
    )


def _auth_out(row: OvertimeAuthorization) -> OvertimeAuthorizationOut:
    return OvertimeAuthorizationOut(
        id=row.id,
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        purpose=row.purpose,
        status=row.status,
        authorized_by=row.authorized_by,
        source_ref=row.source_ref,
        source=row.source,
        version=row.version,
    )


def create_authorization(
    session: Session, employee: Employee, payload: OvertimeAuthorizationIn
) -> OvertimeAuthorizationOut:
    """Record an employee overtime APPLICATION (ACCESS-017).

    The employee asks; an institutional writer decides. ``status``,
    ``authorized_by`` and ``source`` are therefore SERVER-FORCED here and are
    not even expressible in ``OvertimeAuthorizationIn``.
    """
    lock_employee_facts(session, employee.id)
    # F8.1 — the window endpoints are BERLIN wall times and are anchored at this
    # boundary. Persisting the naive value let PostgreSQL interpret it in the DB
    # session timezone (UTC in the container), so a stored endpoint sat 1-2 hours
    # off the wall time the employee typed and the window moved with the server's
    # timezone.
    valid_from = _resolve("valid_from", payload.valid_from, None)
    valid_to = None if payload.valid_to is None else _resolve("valid_to", payload.valid_to, None)
    if valid_to is not None and valid_to.astimezone(UTC) <= valid_from.astimezone(UTC):
        raise api_error(
            422,
            ErrorCode.VALIDATION_FAILED,
            "The end of the authorised period must be after its start.",
        )
    row = OvertimeAuthorization(
        employee_id=employee.id,
        valid_from=valid_from,
        valid_to=valid_to,
        purpose=payload.purpose,
        source_ref=payload.source_ref,
        status=OvertimeAuthStatus.REQUESTED,
        authorized_by=None,
        source=EMPLOYEE_SOURCE,
    )
    session.add(row)
    session.flush()
    # EINV-15/D2c: a REQUESTED employee row is not a calculation input, so this
    # reopens nothing (and must not: a default open-ended request would
    # otherwise reopen every reviewed month from its start date onward).
    _reopen_authorization_months(session, employee.id, row)
    session.flush()
    return _auth_out(row)


def patch_authorization(
    session: Session, employee: Employee, auth_id: uuid.UUID, payload: OvertimeAuthorizationPatch
) -> OvertimeAuthorizationOut:
    lock_employee_facts(session, employee.id)
    current = session.scalar(
        _current(OvertimeAuthorization, employee.id).where(OvertimeAuthorization.id == auth_id)
    )
    if current is None:
        raise api_error(404, ErrorCode.NOT_FOUND, "Not found.")
    if current.version != payload.version:
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_VERSION,
            "Overtime authorization changed; refresh and retry.",
        )
    # ACCESS-017/EINV-23 — the authority predicate, DENY BY DEFAULT. The employee
    # may version only a chain head they authored: an institutional decision is
    # a POLICY DECISION owned by another actor, and letting the employee cancel
    # it would also silently remove the TRAVEL-SUNDAY-001 suppression it carries.
    # Refused as 422 VALIDATION_FAILED (the module convention), never 403:
    # auth/deps.py documents one byte-identical body for every /me/* denial and a
    # second 403 body would break that property.
    if not is_employee_writable(current):
        raise api_error(
            422,
            ErrorCode.VALIDATION_FAILED,
            "This overtime authorization was decided institutionally and cannot be changed here.",
        )
    # Required persisted fields cannot be cleared by a correction: an explicit
    # null would otherwise reach a comparison or a NOT NULL column as a 500.
    # Named in the EMPLOYEE's words, never the API field name: the message is
    # displayed verbatim (BACKEND_SPEC §11), and a refusal is a fixed sentence —
    # an indirection through a dict is what let a field name in here before.
    if "valid_from" in payload.model_fields_set and payload.valid_from is None:
        raise api_error(
            422,
            ErrorCode.VALIDATION_FAILED,
            "An authorization needs a start; it cannot be cleared.",
        )
    if "status" in payload.model_fields_set and payload.status is None:
        raise api_error(
            422,
            ErrorCode.VALIDATION_FAILED,
            "An authorization needs a status; it cannot be cleared.",
        )
    values = {
        field: getattr(payload, field)
        for field in payload.model_fields_set
        if field not in {"version", "source"}
    }
    # F8.1 — a payload window endpoint is a Berlin wall time and is anchored
    # BEFORE it is merged with the stored (already aware) predecessor values, so
    # the comparison below is between two instants and the persisted window is the
    # Berlin instant the employee typed. An explicit `null` means "cleared" and is
    # left alone.
    for field in ("valid_from", "valid_to"):
        if field in values and values[field] is not None:
            values[field] = _resolve(field, values[field], None)
    merged = {field: getattr(current, field) for field in OVERTIME_AUTH_PATCHABLE_FIELDS}
    merged.update(values)
    valid_from = merged["valid_from"]
    valid_to = merged["valid_to"]
    if valid_to and valid_to.astimezone(UTC) <= valid_from.astimezone(UTC):
        raise api_error(
            422,
            ErrorCode.VALIDATION_FAILED,
            "The end of the authorised period must be after its start.",
        )
    row = OvertimeAuthorization(
        employee_id=employee.id,
        version=current.version + 1,
        supersedes_id=current.id,
        source=current.source,  # provenance of the original fact is immutable
        authorized_by=current.authorized_by,  # not the employee's field to restate
        **merged,
    )
    session.add(row)
    session.flush()
    # EINV-15/D2c: reopen the months of the union of the before/after
    # authoritative-input windows. For an employee-authored chain both windows
    # are ``None`` (never authoritative), so an ordinary request/cancel or a
    # metadata-only correction reopens nothing.
    _reopen_authorization_months(session, employee.id, row, before=current)
    session.flush()
    return _auth_out(row)


def decide_authorization(
    session: Session,
    employee_id: uuid.UUID,
    auth_id: uuid.UUID,
    decision: OvertimeAuthorizationDecisionIn,
    actor_principal: str,
) -> OvertimeAuthorizationOut:
    """The ONE authoritative writer (ACCESS-017) — the external HR seam.

    Called by the operator CLI today and by a future adapter without change: it
    raises a plain domain exception rather than ``HTTPException``, so non-HTTP
    callers choose their own reporting. There is deliberately no HTTP route for
    an authority decision (an approval queue and an
    approver-routing model are a "generic workflow engine" and are not defined by
    any source).

    The decision is a NEW version: the prior row stays intact (correction
    lineage, never a destructive overwrite), ``source`` becomes the institutional
    provenance — it is NOT copied from the predecessor the way the employee
    correction copies it — and the actor that gave the decision is recorded.
    """
    lock_employee_facts(session, employee_id)
    current = session.scalar(
        _current(OvertimeAuthorization, employee_id).where(OvertimeAuthorization.id == auth_id)
    )
    if current is None:
        # Either the row does not exist for this employee, or it is no longer the
        # chain head (a concurrent decision or employee correction already
        # superseded it). Refusing is what makes the fork impossible: only ONE
        # successor per head can exist (`uq_overtime_authorization_supersedes_id`).
        raise OvertimeAuthorizationDecisionRefused(
            "no current overtime authorization matches that id for this employee"
        )
    values: dict[str, Any] = {}
    for field in ("valid_from", "valid_to", "purpose", "source_ref"):
        values[field] = (
            getattr(decision, field)
            if field in decision.model_fields_set
            else getattr(current, field)
        )
    valid_from = values["valid_from"]
    valid_to = values["valid_to"]
    if valid_from is None:
        raise OvertimeAuthorizationDecisionRefused("valid_from is required")
    # F8.1 — a NAIVE endpoint is a Berlin wall time (the CLI resolves its own, so
    # it normally arrives aware); anchor whichever form arrives before comparing.
    # The anchored values go back INTO ``values``: that mapping is what builds the
    # row below, so anchoring a local copy would leave the persisted window naive.
    valid_from = values["valid_from"] = _anchor_seam_window("valid_from", valid_from)
    valid_to = values["valid_to"] = (
        None if valid_to is None else _anchor_seam_window("valid_to", valid_to)
    )
    if valid_to and valid_to.astimezone(UTC) <= valid_from.astimezone(UTC):
        raise OvertimeAuthorizationDecisionRefused("valid_to must be after valid_from")
    row = OvertimeAuthorization(
        employee_id=employee_id,
        version=current.version + 1,
        supersedes_id=current.id,
        source=INSTITUTIONAL_SOURCE,
        status=decision.status,
        authorized_by=decision.authorized_by,
        **values,
    )
    session.add(row)
    session.flush()
    # D2c: `before=current` is REQUIRED. An authoritative approval that is
    # withdrawn removes a calculation input, and only the before/after window
    # union can detect that direction.
    _reopen_authorization_months(session, employee_id, row, before=current)
    # An institutional decision on an employee's overtime is exactly the kind of
    # domain-accountability event audit exists for, and every sibling operator
    # writer records one (CALENDAR_VERSION_PUBLISH, CUTOVER_SEED, ADMIN_*,
    # SUBMISSION_*). Written in the SAME transaction as the decision, so a
    # committed decision always has its record.
    record_audit(
        session,
        actor_principal=actor_principal,
        action="OVERTIME_AUTHORIZATION_DECIDE",
        outcome="SUCCESS",
        object_type="OVERTIME_AUTHORIZATION",
        object_id=row.id,
        employee_subject_id=employee_id,
    )
    session.flush()
    return _auth_out(row)


def list_authorizations(
    session: Session, employee: Employee, start: datetime, end: datetime
) -> list[OvertimeAuthorizationOut]:
    rows = session.scalars(
        _current(OvertimeAuthorization, employee.id)
        .where(
            OvertimeAuthorization.valid_from < end,
            (OvertimeAuthorization.valid_to.is_(None) | (OvertimeAuthorization.valid_to > start)),
        )
        .order_by(OvertimeAuthorization.valid_from)
    ).all()
    return [_auth_out(x) for x in rows]


def link_overtime_interval(
    session: Session,
    employee: Employee,
    interval_id: uuid.UUID,
    authorization_id: uuid.UUID,
) -> None:
    """Attach an overtime authorization reference through the ORDINARY versioned
    correction command (Corrections preserve or explicitly
    change the link, always versioned and never a raw UPDATE).

    The generic ``PATCH /api/v1/me/work-intervals/{id}`` command (with
    ``overtime_authorization_id``) is the primary path; this helper keeps
    the same semantics for service-level callers.
    """
    interval = session.scalar(
        select(WorkInterval).where(
            WorkInterval.id == interval_id,
            WorkInterval.employee_id == employee.id,
            WorkInterval.work_type.in_([WorkType.OVERTIME_PAID, WorkType.OVERTIME_TIME_COMP]),
        )
    )
    if interval is None:
        raise api_error(422, ErrorCode.VALIDATION_FAILED, "Overtime authorization link is invalid.")
    authorization = session.scalar(
        _current(OvertimeAuthorization, employee.id).where(
            OvertimeAuthorization.id == authorization_id
        )
    )
    if authorization is None:
        raise api_error(422, ErrorCode.VALIDATION_FAILED, "Overtime authorization link is invalid.")
    correct_interval(
        session,
        employee,
        interval.id,
        IntervalPatch(
            version=interval.version,
            overtime_authorization_id=authorization.id,
        ),
    )


def _validate_context(
    session: Session,
    employee: Employee,
    context_type: CompensationContextType,
    context_id: uuid.UUID,
) -> None:
    model = {
        CompensationContextType.OVERTIME: OvertimeAuthorization,
        CompensationContextType.OCD: OcdAssignment,
        CompensationContextType.SHIFT: ShiftAssignment,
    }[context_type]
    if (
        session.scalar(
            select(model.id).where(model.id == context_id, model.employee_id == employee.id)
        )
        is None
    ):
        raise api_error(422, ErrorCode.VALIDATION_FAILED, "Compensation context is not available.")


def get_election(
    session: Session,
    employee: Employee,
    context_type: CompensationContextType,
    context_id: uuid.UUID,
) -> ElectionOut | None:
    # An election recorded on a predecessor is legitimate and must be found, so
    # this resolves over the chain rather than requiring the head.
    row = (
        _ocd_chain_election(session, employee, context_id)
        if context_type is CompensationContextType.OCD
        else _election(session, employee, context_type, context_id)
    )
    return (
        ElectionOut(
            id=row.id,
            context_type=row.context_type,
            context_id=row.context_id,
            mode=row.mode,
            version=row.version,
            selected_at=row.selected_at,
        )
        if row
        else None
    )


def create_election(session: Session, employee: Employee, payload: ElectionIn) -> ElectionOut:
    lock_employee_facts(session, employee.id)
    _validate_context(session, employee, payload.context_type, payload.context_id)
    current = _election(session, employee, payload.context_type, payload.context_id)
    row = CompensationElection(
        employee_id=employee.id,
        context_type=payload.context_type,
        context_id=payload.context_id,
        mode=payload.mode,
        version=(current.version + 1 if current else 1),
        supersedes_id=current.id if current else None,
    )
    session.add(row)
    session.flush()
    return ElectionOut(
        id=row.id,
        context_type=row.context_type,
        context_id=row.context_id,
        mode=row.mode,
        version=row.version,
        selected_at=row.selected_at,
    )


def overtime_preview(
    session: Session, employee: Employee, start_day: date, end_day: date
) -> OvertimePreviewOut:
    start = datetime.combine(start_day, time.min, tzinfo=BERLIN)
    end = datetime.combine(end_day, time.min, tzinfo=BERLIN)
    # CURRENT-version predicate only: a corrected OVERTIME_* fact must never
    # appear twice (the ordinary read paths share this predicate, M2/M3).
    intervals = [
        row
        for row in current_intervals(
            session, employee_id=employee.id, intersects_from=start, intersects_to=end
        )
        if row.work_type in {WorkType.OVERTIME_PAID, WorkType.OVERTIME_TIME_COMP}
    ]
    auth_by_interval = {row.id: row.overtime_authorization_id for row in intervals}
    linked_ids = {value for value in auth_by_interval.values() if value}
    # Resolve every link to its CURRENT chain head. A linked row that a later
    # institutional decision superseded must still be found, or the interval
    # would carry a `linked_authorization_id` that resolves to nothing and the
    # preview would raise no finding at all (the stale-link hole, D2).
    chains = {
        auth_id: _authorization_chain(session, employee.id, auth_id) for auth_id in linked_ids
    }
    resolved = {
        interval_id: (chains[linked][0] if linked and chains.get(linked) else None)
        for interval_id, linked in auth_by_interval.items()
    }
    contract = effective_contract(session, employee.id, start_day)
    calendar = _calendar(session, start_day, end_day)
    components, findings = classify_spans(
        [WorkSpan(x.start_at, x.end_at, x.work_type) for x in intervals],
        calendar,
        contract.pay_group_bucket if contract else PayGroupBucket.UNKNOWN,
        float(contract.fte) if contract and contract.fte is not None else None,
        contract.weekly_required_minutes if contract else None,
    )
    # The three ACCESS-006 modes, each with its OWN message: the rule id alone
    # cannot discriminate them, so the tests key on the message. One finding per
    # offending interval, de-duplicated like `classify_spans` — the same refusal
    # on twenty intervals is one finding, not twenty.
    for interval in intervals:
        head = resolved[interval.id]
        if auth_by_interval[interval.id] is None:
            message = ACCESS_006_NO_REFERENCE
        elif head is None or not is_authoritative_approval(head):
            message = ACCESS_006_NOT_AUTHORITATIVE
        elif not _covers(head, interval.start_at, interval.end_at):
            message = ACCESS_006_OUTSIDE_WINDOW
        else:
            continue
        findings.append(PolicyFinding("ACCESS-006", "BLOCKED_POLICY", message))
    # ``WT-CORE-009`` (POL-09) — prior-ness, ADVISORY ONLY.
    #
    # "Overtime has to be authorised in advance and can only in exceptional cases be
    # authorised retroactively within four weeks." Only the prior-ness half is live:
    # the numeric four-week bound is BLOCKED, because the sample policy fixes the act
    # and an outer bound but not the measured quantity, its units or the actor.
    # Hence WARNING, never BLOCKED_POLICY — a hard gate would forbid a case the policy
    # explicitly permits and would be silently stricter than the source (INV-15), and
    # it could not express a window legitimately covering both prior and retroactive
    # days.
    #
    # The rule is scoped to the periods its OWN policy governs — POL-09 applies from
    # 2026-06-01 — by comparing the interval's Berlin day against
    # ``WT_CORE_009_VALID_FROM``.
    #
    # The alternative (resolve the period's ordinary edition and compare its
    # ``effective_from``) was rejected on purpose: per ``ARCH-POL-001`` an edition's
    # ``effective_from`` is a RESOLUTION KEY derived from the accounting start, not a
    # restatement of the source's legal effective date. Deriving a RULE's
    # applicability from it would let a future edition's accounting boundary silently
    # move the rule, and it could only ever be false for an edition whose accounting
    # start precedes 2026-06-01 — an edition that must never exist. The rule's
    # effective date is a property of its source, so it stays a named constant
    # (recorded in the ``EINV-09`` residual literal list) and no policy edition is
    # resolved on this read path.
    for interval in intervals:
        if interval.start_at.astimezone(BERLIN).date() < WT_CORE_009_VALID_FROM:
            continue
        # An interval with NO reference is not prior-authorized either — it is not
        # authorized AT ALL — so the advisory is emitted for it too. `ACCESS-006`
        # mode (i) already refuses the submission, and the two findings say
        # different things: the refusal is about the seal, the advisory is about
        # the rule. The advisory never becomes the blocking finding.
        linked = auth_by_interval[interval.id]
        chain = chains.get(linked) if linked is not None else None
        as_of = _as_of_authorization(chain or [], interval.start_at)
        if (
            as_of is not None
            and is_authoritative_approval(as_of)
            and _covers(as_of, interval.start_at, interval.end_at)
        ):
            continue
        findings.append(PolicyFinding("WT-CORE-009", "WARNING", WT_CORE_009_NOT_PRIOR_AUTHORIZED))
    unique = {(x.rule_id, x.status, x.message): x for x in findings}
    findings = list(unique.values())
    auths = sorted(
        {head.id: head for head in resolved.values() if head is not None}.values(),
        key=lambda row: row.valid_from,
    )
    # The id the payload names: the resolved head when there is one, otherwise
    # the link the employee attached (which then fails mode (ii)).
    linked_projection = {
        interval_id: (head.id if head is not None else auth_by_interval[interval_id])
        for interval_id, head in resolved.items()
    }
    # The election is bound to the row the employee selected it on, and a
    # decision supersedes that row — so resolve it over the CHAIN, head first.
    elections: list[CompensationElection] = []
    for chain in chains.values():
        for row in chain:
            found = _election(session, employee, CompensationContextType.OVERTIME, row.id)
            if found is not None:
                elections.append(found)
                break
    election = next((x for x in elections if x is not None), None)
    return build_overtime_preview(
        OvertimePreviewInputs(
            employee.external_employee_id or str(employee.id),
            start_day,
            end_day,
            [
                OvertimeAuthorizationProjectionOut(
                    id=x.id,
                    valid_from=x.valid_from,
                    valid_to=x.valid_to,
                    purpose=x.purpose,
                    status=x.status,
                    source_ref=x.source_ref,
                )
                for x in auths
            ],
            [
                OvertimeIntervalOut(
                    interval_id=x.id,
                    start_at=x.start_at,
                    end_at=x.end_at,
                    work_type=x.work_type,
                    elapsed_seconds=int(
                        (x.end_at.astimezone(UTC) - x.start_at.astimezone(UTC)).total_seconds()
                    ),
                    linked_authorization_id=linked_projection[x.id],
                    version=x.version,
                )
                for x in intervals
            ],
            _components(components),
            _election_out(election),
            _finding(findings),
        )
    )
