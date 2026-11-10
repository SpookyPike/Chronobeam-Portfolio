"""Explicit operator bootstrap for the first application administrator.

Bootstrap path:

    empty installation
        -> operator runs this CLI once
        -> first APPLICATION_ADMIN exists
        -> that administrator provisions remaining users/assignments via the
           Administration UI/API

Properties (all regression-tested):

- CLI-ONLY: no HTTP endpoint, no startup hook, no environment-variable
  auto-provisioning, no local passwords.
- Serialized: a transaction-level PostgreSQL advisory lock means two
  concurrent bootstraps produce exactly ONE effective administrator.
- Fail-safe repeat: a second run (even for the same subject) is rejected with
  an audited ADMIN_BOOTSTRAP / REJECTED_ALREADY_BOOTSTRAPPED event and a
  nonzero exit; the rejection audit COMMITS before exiting.
- Recovery path: "effective administrator" means mapped (idp_subject NOT
  NULL) AND active AND holding an unrevoked APPLICATION_ADMIN assignment.
  If every administrator is later deactivated/unmapped/revoked, the operator
  may bootstrap again (documented recovery; requires CLI/DB operator access —
  not a backdoor).
- Honest actor: the audit actor is ``BOOTSTRAP_CLI``; the CLI never pretends
  the target subject authenticated itself.
- When promoting an existing employee, no contract/schedule rows are created
  (that is HR master-data territory, deliberately out of scope).
"""

import argparse
import sys

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.audit.models import record_audit
from app.auth.models import RoleAssignment
from app.auth.roles import Role
from app.config import get_settings
from app.db import get_session_factory
from app.identity.models import Employee

BOOTSTRAP_ACTOR = "BOOTSTRAP_CLI"
ACTION = "ADMIN_BOOTSTRAP"

# Fixed advisory-lock key for the empty-installation check (32-bit
# hashtext); collisions only over-serialize, never under-serialize.
_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtext('chronobeam_admin_bootstrap'))")


def effective_administrator(session: Session) -> Employee | None:
    """The first/only effective APPLICATION_ADMIN, if any.

    ``effective`` requires the employee to be mapped AND active AND to hold an
    unrevoked APPLICATION_ADMIN assignment — an inactive or unmapped former
    administrator does NOT block recovery bootstrap.
    """
    return session.scalar(
        select(Employee)
        .join(RoleAssignment, RoleAssignment.employee_id == Employee.id)
        .where(
            Employee.active.is_(True),
            Employee.idp_subject.is_not(None),
            RoleAssignment.role == Role.APPLICATION_ADMIN.value,
            RoleAssignment.revoked_at.is_(None),
        )
        .limit(1)
    )


def run_bootstrap(
    session: Session,
    *,
    subject: str,
    legal_name: str | None = None,
    external_employee_id: str | None = None,
    email: str | None = None,
) -> tuple[str, Employee]:
    """Execute one bootstrap attempt inside the caller's transaction.

    Returns (outcome, employee): outcome is "CREATED", "PROMOTED" or
    "REJECTED_ALREADY_BOOTSTRAPPED" (the existing administrator is returned
    for the rejected case, so the returned employee is never None). Callers
    must commit (rejections commit their audit row before exiting nonzero).
    """
    session.execute(_LOCK_SQL)
    existing = effective_administrator(session)
    if existing is not None:
        record_audit(
            session,
            actor_principal=BOOTSTRAP_ACTOR,
            action=ACTION,
            outcome="REJECTED_ALREADY_BOOTSTRAPPED",
            object_type="EMPLOYEE",
            object_id=existing.id,
            employee_subject_id=existing.id,
        )
        return "REJECTED_ALREADY_BOOTSTRAPPED", existing

    employee = session.scalar(select(Employee).where(Employee.idp_subject == subject))
    outcome = "PROMOTED"
    if employee is None:
        if not legal_name:
            raise ValueError(
                "--legal-name is required when the subject is not an existing employee"
            )
        employee = Employee(
            idp_subject=subject,
            external_employee_id=external_employee_id,
            legal_name=legal_name,
            email=email,
            active=True,
        )
        session.add(employee)
        session.flush()
        outcome = "CREATED"
    else:
        # An explicit operator bootstrap promotes a selected provisioned or
        # formerly deactivated employee to an *effective* administrator.
        # Merely adding a role to an inactive row would report success while
        # still leaving the installation without an administrator.
        employee.active = True

    assignment = session.scalar(
        select(RoleAssignment).where(
            RoleAssignment.employee_id == employee.id,
            RoleAssignment.role == Role.APPLICATION_ADMIN.value,
            RoleAssignment.revoked_at.is_(None),
        )
    )
    if assignment is None:
        session.add(
            RoleAssignment(
                employee_id=employee.id,
                role=Role.APPLICATION_ADMIN.value,
                granted_by_principal=BOOTSTRAP_ACTOR,
            )
        )
    record_audit(
        session,
        actor_principal=BOOTSTRAP_ACTOR,
        action=ACTION,
        outcome=outcome,
        object_type="EMPLOYEE",
        object_id=employee.id,
        employee_subject_id=employee.id,
    )
    return outcome, employee


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap the first Chronobeam application administrator (CLI only).",
    )
    parser.add_argument("--subject", required=True, help="Institutional identity subject to map")
    parser.add_argument("--legal-name", default=None, help="Required when the subject is new")
    parser.add_argument("--external-employee-id", default=None)
    parser.add_argument("--email", default=None)
    args = parser.parse_args(argv)
    subject = args.subject.strip()
    if not subject:
        parser.error("--subject must not be empty")

    settings = get_settings()
    with get_session_factory()() as session:
        try:
            outcome, employee = run_bootstrap(
                session,
                subject=subject,
                legal_name=args.legal_name,
                external_employee_id=args.external_employee_id,
                email=args.email,
            )
        except ValueError as exc:
            session.rollback()  # nothing written, nothing to audit
            print(f"bootstrap failed: {exc}", file=sys.stderr)
            return 2
        session.commit()
    print(
        f"bootstrap {outcome}: {employee.legal_name} (subject={subject}, "
        f"employee_id={employee.id}) is your APPLICATION_ADMIN (env={settings.env})"
    )
    return 0 if outcome in {"CREATED", "PROMOTED"} else 2


if __name__ == "__main__":
    sys.exit(main())
