"""FastAPI dependencies resolving the current principal and employee.

Two auth paths:

- dev/test: ``X-Chronobeam-Dev-Principal`` header selects a fixture principal
  (``auth/dev_principals.py``). A subject the fixture mapping does not carry is
  resolved from the DB instead, but ONLY while an employee row with that
  ``idp_subject`` exists AND is active — so a newly provisioned employee becomes
  addressable in the dev/demo seam by being activated in the product, with no CLI
  rebinding and no fixture edit. Everything else stays ``401
  AUTH_UNKNOWN_PRINCIPAL``.
- production: the opaque browser session cookie established by the OIDC
  authorization-code flow (``auth/routes.py``). There is deliberately NO
  bearer/API-token path: a token presented in an ``Authorization`` header
  authenticated without a nonce and bypassed the CSRF middleware by
  construction, and no machine consumer uses one. Mapped+active employees get
  OWN_LEDGER_READ/WRITE; unmapped subjects get NO capabilities (no JIT
  provisioning) and therefore the same byte-identical 403 on /me/* ledger
  routes.

Capability derivation is ONE shared function:
employee-row baseline caps (when mapped AND active) + caps of active role
assignments (``auth/roles.py``) + server-config allowlists (cutover
operator). Authentication may move to institutional SSO without ever
changing this derivation.

Every /me/* denial uses ONE byte-identical body (``errors.forbidden()``):
missing capability, unmapped subject, inactive employee — indistinguishable
(ARCH-AUTH-004, WT-BACKEND §11). Denials for ledger routes happen BEFORE
any data is materialized.
"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth.dev_principals import DEV_PRINCIPALS
from app.auth.principal import Capability, Principal
from app.auth.roles import role_capabilities
from app.auth.session_service import COOKIE_NAME, find_session
from app.config import get_settings
from app.db import get_db
from app.errors import ErrorCode, forbidden
from app.identity.models import Employee

DEV_PRINCIPAL_HEADER = "X-Chronobeam-Dev-Principal"

# Baseline capabilities of a mapped+active employee row.
EMPLOYEE_BASELINE_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.OWN_LEDGER_READ,
        Capability.OWN_LEDGER_WRITE,
        Capability.OWN_SUBMISSION_CREATE,
        Capability.OWN_DISCLOSURE_GRANT,
    }
)


def effective_capabilities(session: Session, employee: Employee | None) -> frozenset[Capability]:
    """One shared derivation: row baseline + active role assignments.

    Employee capabilities exist ONLY for a mapped AND active employee
    (hard invariant): an inactive or unmapped row contributes
    nothing — neither the baseline nor role capabilities — so deactivation
    cannot be bypassed through role assignments. Configuration-seam
    capabilities (cutover operator, pilot plan entry) are added here, AFTER the
    mapped-and-active guard, so deactivation cannot bypass them either.
    """
    if employee is None or not employee.active or employee.idp_subject is None:
        return frozenset()
    capabilities = EMPLOYEE_BASELINE_CAPABILITIES | role_capabilities(session, employee.id)
    settings = get_settings()
    # Each pilot capability comes from ITS OWN setting: the two domains switch
    # independently, so a domain whose plans an authoritative source now owns can
    # be closed while the other stays employee-entered. Never OR them together.
    if settings.enable_shift_plan_entry:
        capabilities |= {Capability.SHIFT_PLAN_ENTRY_WRITE}
    if settings.enable_ocd_plan_entry:
        capabilities |= {Capability.OCD_PLAN_ENTRY_WRITE}
    return frozenset(capabilities)


def _reject_unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": ErrorCode.AUTH_REQUIRED.value, "message": "Authentication required."},
    )


def _unknown_principal() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "code": ErrorCode.AUTH_UNKNOWN_PRINCIPAL.value,
            "message": "Unknown principal.",
        },
    )


def _resolve_provisioned_principal(session: Session, subject: str) -> Principal:
    """Dev/test only: resolve a header subject from its employee row.

    The fixture allowlist (``DEV_PRINCIPALS``) is code-controlled; this fallback
    is DATA-controlled, so it fails closed: the row must exist AND be active —
    exactly the condition ``effective_capabilities`` requires — otherwise the
    subject is as unknown as one with no row at all. A provisioned account is
    inactive by default, so a deactivated account must not be sign-in-capable
    through the dev header.
    """
    try:
        employee = employee_by_subject(session, subject)
    except SQLAlchemyError:
        # DB-free seam tests / unmigrated dev database: there is no fixture to
        # fall back to, so fail closed (production never reaches this branch).
        raise _unknown_principal() from None
    if employee is None or not employee.active:
        raise _unknown_principal()
    return Principal(
        subject=subject,
        display_name=employee.legal_name,
        capabilities=subject_capabilities(session, subject),
    )


def get_current_principal(
    request: Request,
    session: Annotated[Session, Depends(get_db)],
) -> Principal:
    """Resolve the authenticated principal (401 before any data access)."""
    settings = get_settings()
    if settings.is_production:
        browser_token = request.cookies.get(COOKIE_NAME)
        browser_session = find_session(session, browser_token, settings) if browser_token else None
        if browser_session is None:
            raise _reject_unauthorized()
        subject = browser_session.subject
        # Capabilities derive locally from the employee row. A non-
        # provisioned subject gets an empty set (no rows written, no JIT).
        # Mapped+active employees additionally get the row-derived
        # disclosure capabilities; non-employee subjects in production get
        # capabilities only from server-controlled config.
        # The cutover operator is a server-controlled
        # OIDC-subject allowlist (CHRONOBEAM_CUTOVER_OPERATOR_SUBJECTS); an
        # empty allowlist fails closed (nobody may seed).
        employee = employee_by_subject(session, subject)
        capabilities = set(effective_capabilities(session, employee))
        if subject in settings.cutover_operator_subjects:
            capabilities.add(Capability.CUTOVER_SEED_WRITE)
        return Principal(
            subject=subject, display_name=subject, capabilities=frozenset(capabilities)
        )

    # dev/test seam
    key = request.headers.get(DEV_PRINCIPAL_HEADER)
    if key is None:
        raise _reject_unauthorized()
    principal = DEV_PRINCIPALS.get(key)
    if principal is None:
        # Not a fixture subject: a provisioned, ACTIVE employee row makes its
        # own subject addressable in dev/demo (no CLI rebinding, no fixture
        # edit). Everything else — unmapped, inactive, or DB-less — is 401.
        return _resolve_provisioned_principal(session, key)
    # Dev principals keep their fixture capabilities, UNIONED with
    # DB-derived capabilities (baseline + active role assignments) when the
    # fixture subject maps to a DB employee row — so the dev seam exercises
    # the exact same derivation as production and role mechanics are testable
    # through the header. A mapped-but-INACTIVE employee yields NO application
    # capabilities (deactivation cannot be bypassed via fixture caps). Fixture
    # principals without a usable DB (or without an employee row) are returned
    # unchanged.
    try:
        return Principal(
            subject=principal.subject,
            display_name=principal.display_name,
            capabilities=subject_capabilities(session, principal.subject),
        )
    except SQLAlchemyError:
        # DB-free seam tests / unmigrated dev database: the fixture principal
        # stands alone (dev/test only; production has no such fallback).
        return principal


#: ``session.info`` key for ``employee_by_subject``'s per-session memo.
_EMPLOYEE_MEMO_KEY = "chronobeam_employee_by_subject"


def employee_by_subject(session: Session, subject: str) -> Employee | None:
    """The employee row for one identity subject, read at most ONCE per session.

    The request path resolves the same subject twice — once to derive the
    principal's capabilities (``subject_capabilities``) and once to map the
    principal to its row (``_resolve_employee``) — and each resolution was its own
    statement. The memo removes that second round-trip.

    Only a FOUND row is memoised, and for that case it is exactly equivalent to
    re-reading: SQLAlchemy's identity map already returns the same instance for a
    second ``select`` of the same primary key and does not refresh its loaded
    attributes. A MISSING row is deliberately not memoised, so a row provisioned
    later in the same session is still found on the next lookup — the
    provision→activate flow the dev seam supports depends on that, and caching an
    absence would make this session blind to it.

    ``session.info`` is SQLAlchemy's own per-session scratch dict, and a session is
    one request's transaction (``app.db``), so the memo cannot outlive the request.
    """
    memo: dict[str, Employee] = session.info.setdefault(_EMPLOYEE_MEMO_KEY, {})
    employee = memo.get(subject)
    if employee is None:
        employee = session.scalar(select(Employee).where(Employee.idp_subject == subject))
        if employee is not None:
            memo[subject] = employee
    return employee


def subject_capabilities(session: Session, subject: str) -> frozenset[Capability]:
    """Capabilities the request path derives for one subject.

    ONE derivation, shared by ``get_current_principal`` and by the recipient
    registry's liveness check — so a configured recipient cannot be "capable" on
    the request path and "unreadable" in the registry (or the reverse).

    Production: row-derived only (mapped AND active, else nothing).
    Dev/test: a fixture principal's capabilities when no employee row is mapped;
    nothing when the mapped row is INACTIVE (deactivation cannot be bypassed via
    fixture caps); otherwise fixture capabilities plus row-derived.
    """
    employee = employee_by_subject(session, subject)
    if get_settings().is_production:
        return effective_capabilities(session, employee)
    principal = DEV_PRINCIPALS.get(subject)
    if employee is None:
        return principal.capabilities if principal is not None else frozenset()
    if not employee.active:
        return frozenset()
    capabilities = set(effective_capabilities(session, employee))
    if principal is not None:
        capabilities |= set(principal.capabilities)
    return frozenset(capabilities)


def _resolve_employee(session: Session, principal: Principal, required: Capability) -> Employee:
    """Capability check + employee mapping with the single 403 body (M4)."""
    if not principal.has_capability(required):
        raise forbidden()
    employee = employee_by_subject(session, principal.subject)
    if employee is None or not employee.active:
        raise forbidden()
    return employee


def get_current_employee(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[Session, Depends(get_db)],
) -> Employee:
    """Employee required for ledger reads (OWN_LEDGER_READ)."""
    return _resolve_employee(session, principal, Capability.OWN_LEDGER_READ)


def get_current_employee_writer(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[Session, Depends(get_db)],
) -> Employee:
    """Employee required for ledger writes (OWN_LEDGER_WRITE)."""
    return _resolve_employee(session, principal, Capability.OWN_LEDGER_WRITE)


def get_current_submission_owner(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[Session, Depends(get_db)],
) -> Employee:
    """Employee required to create submission packages (OWN_SUBMISSION_CREATE)."""
    return _resolve_employee(session, principal, Capability.OWN_SUBMISSION_CREATE)


def get_current_grant_owner(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[Session, Depends(get_db)],
) -> Employee:
    """Employee required to grant disclosure requests (OWN_DISCLOSURE_GRANT)."""
    return _resolve_employee(session, principal, Capability.OWN_DISCLOSURE_GRANT)
