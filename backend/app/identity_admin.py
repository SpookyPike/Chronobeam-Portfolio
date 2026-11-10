"""Operator CLI for changing an employee's identity binding.

Changing which institutional identity resolves to an existing employee row is a
CREDENTIAL operation: whoever can do it can become that employee and read their
private ledger. It is therefore deliberately NOT available through the
application API — the API may set a binding only when creating a row, and
``PATCH /admin/users/{id}`` rejects the field. This CLI is the only way to
change an existing binding, and it needs host/DB operator access, exactly like
``app.admin_bootstrap`` (which establishes the first administrator).

Properties (regression-tested):

- CLI-ONLY: no HTTP endpoint, no startup hook, no environment auto-provisioning.
- Serialized: a FIXED advisory lock is taken first by every invocation, before
  the target is resolved, then the per-employee fact lock. See the protocol
  below.
- Honest actor: the audit actor is ``IDENTITY_CLI``; the CLI never pretends the
  target subject authenticated itself.
- Reason required: ``--reason`` must be non-blank (argparse, the service and a
  DB CHECK each enforce it).
- Provenance: one ``identity_binding_change`` row per change, carrying the
  previous and new subject, the reason and the actor.
- Session invalidation: every opaque session of the employee is deleted, so the
  displaced subject cannot keep using a live cookie.

Lock order (fixed for every invocation — a total order with one global first
lock cannot deadlock):

    fixed identity-admin lock
      -> resolve the target (by --employee-id, or by --current-subject)
      -> lock_employee_facts(employee_id)
      -> re-read / re-validate the selector
      -> mutate binding + provenance + session invalidation + audit
      -> commit

Usage::

    python -m app.identity_admin set-binding --employee-id <uuid> \
        --new-subject <subject> --reason "ticket CHG-123"
    python -m app.identity_admin set-binding --current-subject <subject> \
        --clear --reason "employee left"
"""

import argparse
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db import get_session_factory
from app.identity.models import Employee
from app.identity.service import (
    IDENTITY_ADMIN_ACTOR,
    lock_identity_administration,
    set_identity_binding,
)


def resolve_target(
    session, *, employee_id: uuid.UUID | None, current_subject: str | None
) -> Employee:
    """Resolve the employee to rebind; raises ``ValueError`` when ambiguous."""
    if (employee_id is None) == (current_subject is None):
        raise ValueError("exactly one of --employee-id or --current-subject is required")
    if employee_id is not None:
        employee = session.get(Employee, employee_id)
    else:
        employee = session.scalar(select(Employee).where(Employee.idp_subject == current_subject))
    if employee is None:
        raise ValueError("no employee matches that selector")
    return employee


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Change an employee's identity binding (operator CLI only).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    set_binding = sub.add_parser("set-binding", help="Set or clear one employee's binding")
    selector = set_binding.add_mutually_exclusive_group(required=True)
    selector.add_argument("--employee-id", help="Target employee uuid")
    selector.add_argument("--current-subject", help="Target the employee mapped to this subject")
    binding = set_binding.add_mutually_exclusive_group(required=True)
    binding.add_argument("--new-subject", help="Subject to bind")
    binding.add_argument("--clear", action="store_true", help="Remove the binding entirely")
    set_binding.add_argument("--reason", required=True, help="Why (recorded as provenance)")
    set_binding.add_argument(
        "--dry-run", action="store_true", help="Resolve and print, write nothing"
    )
    args = parser.parse_args(argv)

    reason = args.reason.strip()
    if not reason:
        parser.error("--reason must not be blank")
    new_subject = None if args.clear else args.new_subject
    if new_subject is not None:
        new_subject = new_subject.strip()
        if not new_subject:
            parser.error("--new-subject must not be blank (use --clear to unmap)")
    employee_id: uuid.UUID | None = None
    if args.employee_id is not None:
        try:
            employee_id = uuid.UUID(args.employee_id)
        except ValueError:
            parser.error("--employee-id must be a uuid")

    settings = get_settings()
    previous: str | None = None
    resolved_id: uuid.UUID | None = None
    legal_name = ""
    with get_session_factory()() as session:
        try:
            # Protocol step 1: the fixed lock, before ANY target resolution.
            lock_identity_administration(session)
            employee = resolve_target(
                session, employee_id=employee_id, current_subject=args.current_subject
            )
            # Read what is needed now: commit/rollback expires the instance.
            resolved_id = employee.id
            legal_name = employee.legal_name
            current = employee.idp_subject
            if args.dry_run:
                print(
                    f"dry-run: {legal_name} (employee_id={resolved_id}) "
                    f"binding {current!r} -> {new_subject!r} (env={settings.env})"
                )
                return 0
            previous = set_identity_binding(
                session,
                actor_principal=IDENTITY_ADMIN_ACTOR,
                employee_id=resolved_id,
                new_subject=new_subject,
                reason=reason,
                matched_subject=args.current_subject,
            )
            session.commit()
        except ValueError as exc:
            session.rollback()
            print(f"set-binding failed: {exc}", file=sys.stderr)
            return 2
        except IntegrityError:
            session.rollback()
            print(
                "set-binding failed: that subject is already bound to another employee",
                file=sys.stderr,
            )
            return 2
    if previous == new_subject:
        print(f"unchanged: {legal_name} is already bound to {new_subject!r}")
        return 0
    print(
        f"set-binding OK: {legal_name} (employee_id={resolved_id}) "
        f"{previous!r} -> {new_subject!r}; sessions invalidated (env={settings.env})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
