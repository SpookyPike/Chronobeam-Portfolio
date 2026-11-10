"""Administration service: user lifecycle, roles, system status.

Small and explicit, mirroring the existing service patterns (cutover/service
duplicate handling, disclosure capability gates, audit allowlist). No generic
IAM abstractions; roles are code-defined (app/auth/roles.py).

Invariants (regression-tested):

- ADMIN sees identity/access state only; no interval/finding/submission/
  disclosure payload ever leaves these endpoints.
- APPLICATION_ADMIN gains no ledger capability: the ACCESS_ADMIN /
  SYSTEM_STATUS_READ capabilities are the ONLY ones roles can contribute that
  are not also held by ordinary employees.
- Identity binding is NOT an administration capability. The application API may
  set ``idp_subject`` only when creating a row (initial provisioning); changing
  an existing row's binding requires the operator CLI ``app.identity_admin``,
  because a rebind is a credential operation that would otherwise let an
  administrator resolve a subject it controls to another employee's ledger.
- Self-mutation is denied (deactivate self, grant/revoke own roles) so the
  acting administrator cannot escalate or lock out alone.
- Role revocation is soft (revoked_at); history never deleted.
"""

import uuid
from typing import Any

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.admin.schemas import (
    AdminCalendarEditionOut,
    AdminCalendarOut,
    AdminRoleAssignmentOut,
    AdminSystemOut,
    AdminUserCreate,
    AdminUserDetail,
    AdminUserPatch,
    AdminUserSummary,
)
from app.audit.models import record_audit
from app.auth.deps import effective_capabilities, employee_by_subject
from app.auth.models import RoleAssignment
from app.auth.principal import Principal
from app.auth.roles import Role
from app.auth.session_models import AuthSession
from app.calendar.models import CALENDAR_KEY, CalendarVersion
from app.config import get_settings
from app.errors import ErrorCode, api_error
from app.identity.models import Employee
from app.identity.service import (
    effective_contract,
    effective_schedule,
    employees_with_effective_contract,
    employees_with_effective_schedule,
)
from app.schema import expected_schema_revision
from app.version import APP_VERSION, build_sha
from app.worktime.walltime import now_berlin

APP_NAME = "chronobeam-backend"

ACTION_PROVISION = "ADMIN_USER_PROVISION"
ACTION_UPDATE = "ADMIN_USER_UPDATE"
ACTION_ROLE_ASSIGN = "ADMIN_ROLE_ASSIGN"
ACTION_ROLE_REVOKE = "ADMIN_ROLE_REVOKE"


def _not_found() -> Exception:
    """Byte-identical 404 for unknown targets (no enumeration)."""
    return api_error(404, ErrorCode.NOT_FOUND, "The requested resource does not exist.")


def _self_mutation_denied() -> Exception:
    return api_error(
        409,
        ErrorCode.ADMIN_SELF_MUTATION,
        "An administrator cannot deactivate or change roles on their own account.",
    )


def employee_or_404(session: Session, employee_id: uuid.UUID) -> Employee:
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise _not_found()
    return employee


def _actor_employee(session: Session, principal: Principal) -> Employee | None:
    # The actor's own row was already resolved to build the principal; reuse that
    # lookup rather than re-asking the database for it (``employee_by_subject``).
    return employee_by_subject(session, principal.subject)


# ---------------------------------------------------------------------------
# User lifecycle
# ---------------------------------------------------------------------------


#: The sortable columns, keyed by the API's own vocabulary (``UserSortKey``).
#: ``role`` is a correlated scalar subquery over the ACTIVE assignments, so it
#: orders by a role the person actually holds; role-less users have no role to
#: sort on and group together at one end, which is where an em dash reads.
_USER_SORT_COLUMNS: dict[str, Any] = {
    "name": Employee.legal_name,
    "employee_id": Employee.external_employee_id,
    "email": Employee.email,
    "status": Employee.active,
    "role": (
        select(func.min(RoleAssignment.role))
        .where(
            RoleAssignment.employee_id == Employee.id,
            RoleAssignment.revoked_at.is_(None),
        )
        .correlate(Employee)
        .scalar_subquery()
    ),
    "provisioned": Employee.created_at,
}


def list_users(
    session: Session,
    *,
    search: str | None,
    active: bool | None,
    sort: str = "provisioned",
    direction: str = "asc",
    limit: int,
    offset: int,
) -> list[AdminUserSummary]:
    query = select(Employee)
    if search:
        like = f"%{search.strip()}%"
        query = query.where(
            or_(
                Employee.legal_name.ilike(like),
                Employee.external_employee_id.ilike(like),
                Employee.idp_subject.ilike(like),
                Employee.email.ilike(like),
            )
        )
    if active is not None:
        query = query.where(Employee.active.is_(active))
    column = _USER_SORT_COLUMNS[sort]
    ordering = column.desc() if direction == "desc" else column.asc()
    # The secondary ``Employee.id`` makes the order TOTAL, so paging can neither
    # repeat nor skip a row when the sort key ties.
    query = query.order_by(ordering, Employee.id).offset(offset)
    if limit:
        query = query.limit(limit)
    rows = session.scalars(query).all()
    employee_ids = [row.id for row in rows]
    roles_by_employee = _active_roles_by_employee(session, employee_ids)
    # Readiness for the whole page in two statements, not one per user: the list is
    # bounded by its page size, so the statement count must not be.
    today = now_berlin().date()
    with_contract = employees_with_effective_contract(session, employee_ids, today)
    with_schedule = employees_with_effective_schedule(session, employee_ids, today)
    return [
        _summary(
            row,
            roles_by_employee.get(row.id, []),
            has_contract=row.id in with_contract,
            has_schedule=row.id in with_schedule,
        )
        for row in rows
    ]


def provision_user(
    session: Session, principal: Principal, payload: AdminUserCreate
) -> AdminUserDetail:
    payload = payload.normalized()
    employee = Employee(
        legal_name=payload.legal_name,
        external_employee_id=payload.external_employee_id,
        idp_subject=payload.idp_subject,
        email=payload.email,
        # Deliberate provisioning default: activation is an explicit step.
        # Mapping alone never grants access.
        active=False,
    )
    try:
        with session.begin_nested():
            session.add(employee)
            session.flush()
    except IntegrityError:
        raise api_error(
            409,
            ErrorCode.CONFLICT_DUPLICATE_IDENTITY,
            "An employee with this identity subject or external id already exists.",
        ) from None
    record_audit(
        session,
        actor_principal=principal.subject,
        action=ACTION_PROVISION,
        outcome="CREATED",
        object_type="EMPLOYEE",
        object_id=employee.id,
        employee_subject_id=employee.id,
    )
    return user_detail(session, employee)


def update_user(
    session: Session,
    principal: Principal,
    employee_id: uuid.UUID,
    payload: AdminUserPatch,
) -> AdminUserDetail:
    payload = payload.normalized()
    employee = employee_or_404(session, employee_id)
    actor = _actor_employee(session, principal)
    is_self = actor is not None and actor.id == employee.id
    changes: dict[str, object] = {}
    if payload.legal_name is not None:
        changes["legal_name"] = payload.legal_name
    if "email" in payload.model_fields_set:
        changes["email"] = payload.email
    if "external_employee_id" in payload.model_fields_set:
        changes["external_employee_id"] = payload.external_employee_id
    if payload.active is not None:
        if is_self and not payload.active:
            raise _self_mutation_denied()
        changes["active"] = payload.active
    if not changes:
        return user_detail(session, employee)
    for field, value in changes.items():
        setattr(employee, field, value)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        raise api_error(
            409,
            ErrorCode.CONFLICT_DUPLICATE_IDENTITY,
            "Another employee already uses this identity subject or external id.",
        ) from None
    if changes.get("active") is False:
        # A deactivation invalidates every opaque session belonging to this
        # employee.  The next access must require a fresh assertion, never
        # revive a browser cookie issued for old identity state.
        session.execute(delete(AuthSession).where(AuthSession.employee_id == employee.id))
    record_audit(
        session,
        actor_principal=principal.subject,
        action=ACTION_UPDATE,
        outcome="UPDATED",
        object_type="EMPLOYEE",
        object_id=employee.id,
        employee_subject_id=employee.id,
    )
    return user_detail(session, employee)


# ---------------------------------------------------------------------------
# Role assignments
# ---------------------------------------------------------------------------


def assign_role(
    session: Session,
    principal: Principal,
    employee_id: uuid.UUID,
    role: Role,
) -> AdminRoleAssignmentOut:
    employee = employee_or_404(session, employee_id)
    actor = _actor_employee(session, principal)
    if actor is not None and actor.id == employee.id:
        raise _self_mutation_denied()
    existing = session.scalar(
        select(RoleAssignment).where(
            RoleAssignment.employee_id == employee.id,
            RoleAssignment.role == role.value,
            RoleAssignment.revoked_at.is_(None),
        )
    )
    if existing is not None:
        return _assignment_out(existing)  # idempotent: already assigned
    assignment = RoleAssignment(
        employee_id=employee.id,
        role=role.value,
        granted_by_principal=principal.subject,
    )
    try:
        with session.begin_nested():
            session.add(assignment)
            session.flush()
    except IntegrityError:
        # Concurrent duplicate assignment: re-read and return the winner.
        existing = session.scalar(
            select(RoleAssignment).where(
                RoleAssignment.employee_id == employee.id,
                RoleAssignment.role == role.value,
                RoleAssignment.revoked_at.is_(None),
            )
        )
        if existing is not None:
            return _assignment_out(existing)
        raise
    record_audit(
        session,
        actor_principal=principal.subject,
        action=ACTION_ROLE_ASSIGN,
        outcome="ASSIGNED",
        object_type="ROLE_ASSIGNMENT",
        object_id=assignment.id,
        employee_subject_id=employee.id,
    )
    return _assignment_out(assignment)


def revoke_role(
    session: Session,
    principal: Principal,
    employee_id: uuid.UUID,
    role: Role,
) -> None:
    employee = employee_or_404(session, employee_id)
    actor = _actor_employee(session, principal)
    if actor is not None and actor.id == employee.id:
        raise _self_mutation_denied()
    assignment = session.scalar(
        select(RoleAssignment).where(
            RoleAssignment.employee_id == employee.id,
            RoleAssignment.role == role.value,
            RoleAssignment.revoked_at.is_(None),
        )
    )
    if assignment is None:
        return  # already revoked / never assigned: no-op, nothing to audit
    assignment.revoked_at = now_berlin()
    session.flush()
    record_audit(
        session,
        actor_principal=principal.subject,
        action=ACTION_ROLE_REVOKE,
        outcome="REVOKED",
        object_type="ROLE_ASSIGNMENT",
        object_id=assignment.id,
        employee_subject_id=employee.id,
    )


# ---------------------------------------------------------------------------
# Inspect / system
# ---------------------------------------------------------------------------


def user_detail(session: Session, employee: Employee) -> AdminUserDetail:
    assignments = session.scalars(
        select(RoleAssignment)
        .where(RoleAssignment.employee_id == employee.id)
        .order_by(RoleAssignment.granted_at.asc(), RoleAssignment.id)
    ).all()
    today = now_berlin().date()
    return AdminUserDetail(
        id=employee.id,
        external_employee_id=employee.external_employee_id,
        legal_name=employee.legal_name,
        email=employee.email,
        active=employee.active,
        # Derived from the assignments already fetched above — no second query, and
        # the same "active means not revoked" rule the list uses.
        roles=sorted(row.role for row in assignments if row.revoked_at is None),
        created_at=employee.created_at,
        has_idp_subject=employee.idp_subject is not None,
        idp_subject=employee.idp_subject,
        role_assignments=[_assignment_out(row) for row in assignments],
        effective_capabilities=sorted(
            capability.value for capability in effective_capabilities(session, employee)
        ),
        has_effective_contract=effective_contract(session, employee.id, today) is not None,
        has_effective_schedule=effective_schedule(session, employee.id, today) is not None,
    )


def _active_roles_by_employee(
    session: Session, employee_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    """ACTIVE role names per employee, in ONE statement.

    ``role_assignment`` is append-only history, so "active" is exactly
    ``revoked_at IS NULL``. Grouping in one query rather than asking per row keeps
    the list view's statement count independent of how many users it shows.
    """
    if not employee_ids:
        return {}
    rows = session.execute(
        select(RoleAssignment.employee_id, RoleAssignment.role)
        .where(
            RoleAssignment.employee_id.in_(employee_ids),
            RoleAssignment.revoked_at.is_(None),
        )
        .order_by(RoleAssignment.role)
    ).all()
    grouped: dict[uuid.UUID, list[str]] = {}
    for employee_id, role in rows:
        grouped.setdefault(employee_id, []).append(role)
    return grouped


def _summary(
    employee: Employee, roles: list[str], *, has_contract: bool, has_schedule: bool
) -> AdminUserSummary:
    return AdminUserSummary(
        id=employee.id,
        external_employee_id=employee.external_employee_id,
        legal_name=employee.legal_name,
        email=employee.email,
        active=employee.active,
        roles=roles,
        created_at=employee.created_at,
        has_idp_subject=employee.idp_subject is not None,
        has_effective_contract=has_contract,
        has_effective_schedule=has_schedule,
    )


def _assignment_out(assignment: RoleAssignment) -> AdminRoleAssignmentOut:
    return AdminRoleAssignmentOut(
        id=assignment.id,
        role=assignment.role,
        granted_by_principal=assignment.granted_by_principal,
        granted_at=assignment.granted_at,
        revoked_at=assignment.revoked_at,
    )


def system_status(session: Session) -> AdminSystemOut:
    from app.calendar.service import first_uncovered_month
    from app.cutover.self_service import month_key

    settings = get_settings()
    if settings.is_production:
        auth_mode = "PRODUCTION_OIDC" if settings.oidc_configured else "PRODUCTION_NOT_CONFIGURED"
    else:
        auth_mode = "DEVELOPMENT_HEADER"
    expected = expected_schema_revision()
    schema_revision: str | None = None
    try:
        with session.begin_nested():  # keep the outer transaction usable
            schema_revision = session.scalar(text("SELECT version_num FROM alembic_version"))
    except Exception:
        schema_revision = None
    editions = _calendar_editions(session)
    uncovered = first_uncovered_month(editions, now_berlin().date())
    return AdminSystemOut(
        application={"name": APP_NAME, "version": APP_VERSION, "build_sha": build_sha()},
        database={
            "reachable": True,  # this response required a DB-backed authz check
            "schema_revision": schema_revision,
            "expected_revision": expected,
            "schema_current": schema_revision == expected,
        },
        auth={"mode": auth_mode, "oidc_configured": settings.oidc_configured},
        integrations={
            # Code-defined integration state: the shift plan runs on the controlled
            # seed seam; the HR absence system and the external travel feed stay
            # blocked, and the employee declares the facts instead.
            "shift_plan": "SEED_SEAM",
            "hr_absence": "BLOCKED_INTEGRATION",
            "travel_feed": "BLOCKED_INTEGRATION",
            # Operator-visible codes, no secrets, no data, no new endpoint.
            # ``INCONSISTENT`` is exactly "the registry names a subject with no
            # currently-capable principal" — the state that used to produce a
            # package nobody can read.
            "recipient_registry": _recipient_registry_state(session),
            # ``NOT_LOADED`` means every month view answers 409 CALENDAR_BLOCKED:
            # the operator must publish an edition before the service is usable.
            # Same code and same meaning as before; it is now derived from the
            # editions the ``calendar`` field below already fetched, so the two can
            # never disagree.
            "calendar": "LOADED" if editions else "NOT_LOADED",
        },
        calendar=AdminCalendarOut(
            editions=[
                AdminCalendarEditionOut(
                    version=row.version,
                    valid_from=row.valid_from,
                    valid_to=row.valid_to,
                    source=row.source,
                )
                for row in editions
            ],
            # Derived, never stored: the operator question is "what is covered, and
            # when does it run out?" — the resolution rule and the fail-closed
            # coverage check stay in ``app.calendar`` where they are defined.
            first_uncovered_month=None if uncovered is None else month_key(uncovered),
        ),
    )


def _recipient_registry_state(session: Session) -> str:
    from app.disclosure.service import readable_recipient_subjects

    options = readable_recipient_subjects(session)
    if not options:
        return "EMPTY_FAIL_CLOSED"
    return "CONFIGURED" if all(option.readable for option in options) else "INCONSISTENT"


def _calendar_editions(session: Session) -> list[CalendarVersion]:
    """Every published edition of the one institutional key, oldest first.

    A timeline rather than the resolver's newest-first order: the operator reads
    this list to see what was published and where the coverage ends.
    """
    return list(
        session.scalars(
            select(CalendarVersion)
            .where(CalendarVersion.calendar_key == CALENDAR_KEY)
            .order_by(CalendarVersion.valid_from)
        ).all()
    )


def _calendar_loaded(session: Session) -> bool:
    from app.calendar.models import CALENDAR_KEY, CalendarVersion

    return (
        session.scalar(
            select(CalendarVersion.id).where(CalendarVersion.calendar_key == CALENDAR_KEY).limit(1)
        )
        is not None
    )
