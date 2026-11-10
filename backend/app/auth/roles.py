"""Code-defined roles: convenience bundles over capabilities.

Roles NEVER replace server-side capability + object-scope checks: every route
continues to gate on ``Capability``, and the role vocabulary here only
determines which capability set a role assignment contributes. Roles are
statically defined in code (no runtime role editor, no generic IAM): adding a
role is a code change plus the role-column CHECK-constraint update.

Vocabulary:

- APPLICATION_ADMIN — application administration (users/access + system
  status). Deliberately NOT a platform/database authority and NOT business-
  data access: it grants no ledger/findings/submission/disclosure read.
- HR_REQUESTER — constrained HR random-check workflow (existing capabilities).
- SUBMISSION_RECIPIENT — reads only submission packages explicitly addressed
  to the principal (existing object-scoped capability).

No EMPLOYEE role (baseline employee capabilities are row-derived for mapped+
active employees, not an assignment) and no SUPERVISOR role (organizational
position grants nothing; ACCESS-012/013). CUTOVER_SEED_WRITE remains the
dedicated server-controlled operator allowlist, deliberately not folded into
any role.

Identity binding (``employee.idp_subject``) is deliberately NOT a capability
either: rebinding an existing row is a credential operation, so it lives
outside the application API entirely (``app/identity_admin``, host/DB operator
access) rather than as a grantable privilege. A grantable IDENTITY_ADMIN role
would not close the hole, because APPLICATION_ADMIN can assign any role to any
other account — including an account whose subject it controls.
"""

from enum import StrEnum
from typing import TYPE_CHECKING

from app.auth.principal import Capability

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checkers
    from sqlalchemy.orm import Session


class Role(StrEnum):
    APPLICATION_ADMIN = "APPLICATION_ADMIN"
    HR_REQUESTER = "HR_REQUESTER"
    SUBMISSION_RECIPIENT = "SUBMISSION_RECIPIENT"


ROLE_CAPABILITIES: dict[Role, frozenset[Capability]] = {
    Role.APPLICATION_ADMIN: frozenset({Capability.ACCESS_ADMIN, Capability.SYSTEM_STATUS_READ}),
    Role.HR_REQUESTER: frozenset(
        {Capability.ACCESS_REQUEST_CREATE, Capability.DISCLOSURE_READ_GRANTED}
    ),
    Role.SUBMISSION_RECIPIENT: frozenset({Capability.SUBMISSION_READ_ASSIGNED}),
}


def role_capabilities(session: "Session", employee_id: object) -> frozenset[Capability]:
    """Capabilities contributed by the employee's ACTIVE role assignments."""
    from sqlalchemy import select

    from app.auth.models import RoleAssignment

    roles = session.scalars(
        select(RoleAssignment.role).where(
            RoleAssignment.employee_id == employee_id,
            RoleAssignment.revoked_at.is_(None),
        )
    ).all()
    capabilities: set[Capability] = set()
    for role_value in roles:
        try:
            capabilities |= ROLE_CAPABILITIES[Role(role_value)]
        except ValueError:
            # Unknown stored role code: the DB CHECK forbids new writes, but
            # treat a stale row as inert rather than crashing the request.
            continue
    return frozenset(capabilities)
