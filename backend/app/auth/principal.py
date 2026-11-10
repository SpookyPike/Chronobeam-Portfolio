"""Authorization seam: principal + capabilities (WT-BACKEND §9).

Authorization is expressed as explicit capabilities, never role checks.
Capabilities are derived locally from the employee row (state), never taken
from IdP claims. The employee principal is linked to the ``employee`` row by
``subject == idp_subject``.
"""

from dataclasses import dataclass
from enum import StrEnum


class Capability(StrEnum):
    """Explicit capabilities (BE spec §9).

    Employees are row-derived OWN_SUBMISSION_CREATE + OWN_DISCLOSURE_GRANT;
    the HR requester carries ACCESS_REQUEST_CREATE + DISCLOSURE_READ_GRANTED;
    the disclosure recipient carries SUBMISSION_READ_ASSIGNED; supervisor and
    technical admin stay capability-less (ACCESS-012/013).
    CUTOVER_SEED_WRITE is the dedicated cutover-operator capability
    (server-controlled allowlist): a business-data continuity mutation,
    deliberately NOT folded into technical-admin.
    ACCESS_ADMIN (user lifecycle + role assignment surfaces) and
    SYSTEM_STATUS_READ (read-only system status card) form the administration
    family. Neither grants any business-data capability; APPLICATION_ADMIN
    holders gain no ledger/findings/submission/disclosure access (hard
    invariant, regression-tested).
    """

    ACCESS_ADMIN = "ACCESS_ADMIN"
    SYSTEM_STATUS_READ = "SYSTEM_STATUS_READ"
    CUTOVER_SEED_WRITE = "CUTOVER_SEED_WRITE"
    OWN_LEDGER_READ = "OWN_LEDGER_READ"
    OWN_LEDGER_WRITE = "OWN_LEDGER_WRITE"
    OWN_SUBMISSION_CREATE = "OWN_SUBMISSION_CREATE"
    OWN_DISCLOSURE_GRANT = "OWN_DISCLOSURE_GRANT"
    SUBMISSION_READ_ASSIGNED = "SUBMISSION_READ_ASSIGNED"
    ACCESS_REQUEST_CREATE = "ACCESS_REQUEST_CREATE"
    DISCLOSURE_READ_GRANTED = "DISCLOSURE_READ_GRANTED"
    #: Pilot plan-entry capabilities. Each is derived from ITS OWN deployment
    #: setting (``enable_shift_plan_entry`` / ``enable_ocd_plan_entry``) and gates
    #: ONLY its own domain's routes and controls. They are deliberately two
    #: members rather than one: when a shift-planning source becomes authoritative
    #: the operator turns the shift setting off while OCD stays employee-entered
    #: (or the reverse), and no route, service path or UI control may consult the
    #: other domain's capability. Both default OFF.
    SHIFT_PLAN_ENTRY_WRITE = "SHIFT_PLAN_ENTRY_WRITE"
    OCD_PLAN_ENTRY_WRITE = "OCD_PLAN_ENTRY_WRITE"


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    display_name: str
    capabilities: frozenset[Capability] = frozenset()

    def has_capability(self, capability: Capability) -> bool:
        return capability in self.capabilities
