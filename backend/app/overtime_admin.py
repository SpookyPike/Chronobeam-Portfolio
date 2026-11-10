"""Operator CLI for recording an INSTITUTIONAL overtime decision (ACCESS-017).

Approving or rejecting an employee's overtime request is an institutional
decision, not an employee one: the ``/api/v1/me/*`` write path can only produce a
``REQUESTED``/``CANCELLED`` request, and there is deliberately no HTTP route for
the decision itself (an approval queue plus an approver-routing model would be
the "generic workflow engine" this service deliberately does not have, and no source defines the
approver, the routing or an SLA).

This CLI is therefore the concrete trusted writer — the same answer the
repository already gives for the calendar (``app.calendar_admin``), master data
(``app.master_data_admin``) and identity bindings (``app.identity_admin``).

It is a thin argument parser over the ONE writer
(``app.ocd_overtime.service.decide_authorization``): it takes no advisory lock of
its own and implements no validation of its own, so the authority predicate and
the ``EINV-15`` reopen have exactly one implementation. It owns its transaction
(there is no FastAPI dependency teardown to hide behind), so it commits **before**
printing success, and ``--dry-run`` never commits.

The service function's signature **is** the external-HR seam: when a machine
interface exists, an adapter calls the same function rather than growing a second
authority path.

Usage::

    python -m app.overtime_admin decide --employee-id <uuid> \
        --authorization <uuid> --status APPROVED \
        --authorized-by "WoCo (A. Beispiel)" \
        --from 2026-11-01T00:00:00+01:00 --to 2026-12-01T00:00:00+01:00 \
        --source-ref CHG-123
    python -m app.overtime_admin decide --employee-id <uuid> \
        --authorization <uuid> --status REJECTED \
        --authorized-by "WoCo (A. Beispiel)" --dry-run

``--from``/``--to`` accept an explicit UTC offset. A naive value is read as a
Berlin wall time and is REFUSED if that wall time does not exist or occurs twice
(a DST transition), so the window is never guessed.
"""

import argparse
import sys
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session_factory
from app.errors import OvertimeAuthorizationDecisionRefused
from app.identity.models import Employee
from app.ocd_overtime.models import OvertimeAuthStatus
from app.ocd_overtime.schemas import OvertimeAuthorizationDecisionIn
from app.ocd_overtime.service import decide_authorization
from app.worktime.walltime import DstResolutionError, resolve_wall_time

ACTOR_PRINCIPAL = "OVERTIME_CLI"


def _parse_instant(
    parser: argparse.ArgumentParser, flag: str, value: str | None
) -> datetime | None:
    """An ISO-8601 instant; a naive value is a Berlin wall time (docs/ARCHITECTURE.md).

    A naive value goes through ``resolve_wall_time``, so a nonexistent or
    ambiguous Berlin wall time is REFUSED rather than guessed: the operator
    disambiguates with an explicit UTC offset. Stamping ``BERLIN`` on directly
    would accept ``2032-03-28T02:30`` — an hour that does not exist — and would
    silently pick one of the two occurrences of ``2032-10-31T02:30``. A wrong
    window is not cosmetic: ``_covers`` and the travel suppression both read it.
    """
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parser.error(f"{flag}: not an ISO-8601 instant: {value!r}")
    if parsed.tzinfo is not None:
        return parsed
    try:
        return resolve_wall_time(parsed)
    except DstResolutionError as exc:
        parser.error(f"{flag}: {exc}; pass an explicit UTC offset, e.g. +01:00 or +02:00")


def decide(
    session: Session,
    *,
    employee_id: uuid.UUID,
    authorization_id: uuid.UUID,
    decision: OvertimeAuthorizationDecisionIn,
    dry_run: bool,
) -> int:
    employee = session.get(Employee, employee_id)
    if employee is None:
        print("decide refused: no employee matches that --employee-id", file=sys.stderr)
        return 2
    legal_name = employee.legal_name
    if dry_run:
        # Run the ONE writer and roll back: a dry run that validated nothing
        # would be worse than no dry run at all.
        decide_authorization(session, employee_id, authorization_id, decision, ACTOR_PRINCIPAL)
        session.rollback()
        print(
            f"dry-run: {decision.status.value} authorization {authorization_id} for "
            f"{legal_name} (employee_id={employee_id}), "
            f"authorized_by={decision.authorized_by!r} — nothing written"
        )
        return 0
    result = decide_authorization(session, employee_id, authorization_id, decision, ACTOR_PRINCIPAL)
    # The decision is durable BEFORE success is reported (EINV-11): a scripted
    # approval must never report success on an uncommitted row.
    session.commit()
    print(
        f"decide OK: {result.status.value} v{result.version} for {legal_name} "
        f"(employee_id={employee_id}, authorization_id={result.id}), "
        f"authorized_by={result.authorized_by!r} (env={get_settings().env})"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record an institutional overtime decision (operator CLI only).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("decide", help="Approve or reject one overtime request")
    command.add_argument("--employee-id", required=True, help="Target employee uuid")
    command.add_argument("--authorization", required=True, help="Overtime authorization uuid")
    command.add_argument(
        "--status",
        required=True,
        choices=[OvertimeAuthStatus.APPROVED.value, OvertimeAuthStatus.REJECTED.value],
    )
    command.add_argument(
        "--authorized-by", required=True, help="The institutional actor (recorded)"
    )
    command.add_argument("--from", dest="valid_from", default=None, help="ISO-8601 window start")
    command.add_argument(
        "--to", dest="valid_to", default=None, help="ISO-8601 exclusive window end"
    )
    command.add_argument("--purpose", default=None, help="Occasion the decision covers")
    command.add_argument("--source-ref", dest="source_ref", default=None, help="External reference")
    command.add_argument(
        "--dry-run", action="store_true", help="Validate through the writer, write nothing"
    )
    args = parser.parse_args(argv)

    authorized_by = args.authorized_by.strip()
    if not authorized_by:
        parser.error("--authorized-by must not be blank: a decision must name its actor")
    try:
        employee_id = uuid.UUID(args.employee_id)
        authorization_id = uuid.UUID(args.authorization)
    except ValueError:
        parser.error("--employee-id and --authorization must be uuids")
    valid_from = _parse_instant(parser, "--from", args.valid_from)
    valid_to = _parse_instant(parser, "--to", args.valid_to)

    values: dict[str, Any] = {
        "status": OvertimeAuthStatus(args.status),
        "authorized_by": authorized_by,
    }
    # PRESENCE semantics: an omitted field keeps the request's own value.
    if valid_from is not None:
        values["valid_from"] = valid_from
    if valid_to is not None:
        values["valid_to"] = valid_to
    if args.purpose is not None:
        values["purpose"] = args.purpose
    if args.source_ref is not None:
        values["source_ref"] = args.source_ref
    decision = OvertimeAuthorizationDecisionIn(**values)

    with get_session_factory()() as session:
        try:
            return decide(
                session,
                employee_id=employee_id,
                authorization_id=authorization_id,
                decision=decision,
                dry_run=args.dry_run,
            )
        except OvertimeAuthorizationDecisionRefused as exc:
            session.rollback()
            print(f"decide refused: {exc.reason}", file=sys.stderr)
            return 2


if __name__ == "__main__":
    sys.exit(main())
