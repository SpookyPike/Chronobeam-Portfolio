"""Development/test principals.

These identities exist only for local development and tests (dev/test env,
header seam). The subject IS the ``employee.idp_subject``:
``just seed-dev`` creates the corresponding synthetic employee rows, so the
dev seam exercises the real principal->employee mapping used in production.

Employee A owns ledger fixtures; employee B is the same-capability non-owner
peer whose access to A's data must be denied (ACCESS-T02, IDOR-shaped).
HR requester and disclosure recipient carry only the disclosure capability
family: ACCESS_REQUEST_CREATE + DISCLOSURE_READ_GRANTED /
SUBMISSION_READ_ASSIGNED. Supervisor and technical admin carry NO
capabilities — absence of OWN_LEDGER_* is the denial point
(ACCESS-001/012/013), and no business-read capability is granted to them.

Fixture principal caps are UNIONED with DB-derived capabilities
(active role assignments) when the subject maps to a DB employee row, so the
role mechanics are exercised through the same dev header; a mapped-but-
inactive employee yields no application capabilities at all. There is no
hardcoded admin fixture: the bootstrap CLI assigns APPLICATION_ADMIN to an
existing fixture subject (e.g. ``employee``) when an administrator is needed.

This mapping is the CODE-controlled allowlist, and it is no longer the only way
the dev seam can resolve a subject. A subject absent here is resolved from the
DB by ``deps.py::_resolve_provisioned_principal`` when — and only when — an
employee row with that ``idp_subject`` exists AND is active, so an employee
provisioned through the product (``POST /api/v1/admin/users``, inactive by
default) becomes addressable by activating it: provision -> activate -> open
with that subject -> onboard, with no CLI rebinding. The fallback is
data-controlled, hence the active check: a deactivated account must not be
sign-in-capable through the dev header. Anything unmapped-and-inactive (or
DB-less) still answers 401 AUTH_UNKNOWN_PRINCIPAL.
"""

from app.auth.principal import Capability, Principal

EMPLOYEE_A = Principal(
    subject="employee",
    display_name="Employee A (synthetic)",
    capabilities=frozenset(
        {
            Capability.OWN_LEDGER_READ,
            Capability.OWN_LEDGER_WRITE,
            Capability.OWN_SUBMISSION_CREATE,
            Capability.OWN_DISCLOSURE_GRANT,
        }
    ),
)

EMPLOYEE_B = Principal(
    subject="employee-b",
    display_name="Employee B (synthetic)",
    capabilities=frozenset(
        {
            Capability.OWN_LEDGER_READ,
            Capability.OWN_LEDGER_WRITE,
            Capability.OWN_SUBMISSION_CREATE,
            Capability.OWN_DISCLOSURE_GRANT,
        }
    ),
)

HR_REQUESTER = Principal(
    subject="hr-requester",
    display_name="HR requester",
    capabilities=frozenset({Capability.ACCESS_REQUEST_CREATE, Capability.DISCLOSURE_READ_GRANTED}),
)

SUPERVISOR = Principal(
    subject="supervisor",
    display_name="Supervisor",
)

RECIPIENT = Principal(
    subject="recipient",
    display_name="Disclosure recipient",
    capabilities=frozenset({Capability.SUBMISSION_READ_ASSIGNED}),
)

TECHNICAL_ADMIN = Principal(
    subject="technical-admin",
    display_name="Technical admin",
)

# Dedicated cutover operator carrying ONLY the
# opening-balance seed capability. Deliberately separate from technical-admin.
CUTOVER_OPERATOR = Principal(
    subject="cutover-operator",
    display_name="Cutover operator (synthetic)",
    capabilities=frozenset({Capability.CUTOVER_SEED_WRITE}),
)

# Demo personas for the recorded product demo (dev/test only,
# `python -m app.demo_seed`). Fictional professional identities, no real PII.
#
# Only the employee personas carry *fixture* capabilities. The admin/HR/recipient
# personas carry none (except the recipient's registry capability), so their role
# capabilities come EXCLUSIVELY from DB role assignments and the demo shows the
# real role model at work. Note what that does NOT mean: every persona the seed
# provisions is a mapped+active employee row, so ``effective_capabilities`` grants
# each one the row-derived employee baseline (own ledger read/write, own submission
# and own disclosure). A role only *adds* reach; it never removes a person's own
# time tracking. Every subject the seed provisions must exist in this mapping: a
# subject outside it is resolved from the DB only when its employee row is
# mapped AND active (``deps.py::_resolve_provisioned_principal``), so a persona
# the seed leaves INACTIVE would be unusable in the demo.


def _demo_employee(subject: str, display_name: str) -> Principal:
    """A demo persona that is an ordinary employee and nothing else.

    The baseline capability set is the same one EMPLOYEE_A/B carry, deliberately
    not parameterised per persona: a persona's *position* (supervisor, admin, HR)
    must never be expressible as a fixture capability, or the demo would show a
    privilege the real role model does not grant.
    """
    return Principal(
        subject=subject,
        display_name=display_name,
        capabilities=frozenset(
            {
                Capability.OWN_LEDGER_READ,
                Capability.OWN_LEDGER_WRITE,
                Capability.OWN_SUBMISSION_CREATE,
                Capability.OWN_DISCLOSURE_GRANT,
            }
        ),
    )


DEMO_EMPLOYEE = _demo_employee("demo-employee", "Mara Klein")
DEMO_PARTTIME = _demo_employee("demo-parttime", "Svenja Ohlsen")
DEMO_OVERTIME = _demo_employee("demo-overtime", "Jonas Peters")
DEMO_OCD = _demo_employee("demo-ocd", "Aylin Demir")
DEMO_TRAVEL = _demo_employee("demo-travel", "Marek Nowak")
DEMO_FLEX = _demo_employee("demo-flex", "Ines Wagner")
DEMO_FINDINGS = _demo_employee("demo-findings", "Tomas Brandt")
DEMO_CLEAN = _demo_employee("demo-clean", "Nora Sandberg")
DEMO_SHIFT = _demo_employee("demo-shift", "Pia Lindqvist")

# The onboarding persona: the only demo employee seeded with NO facts, so the
# start-state form is actually available to demonstrate. Every other persona
# holds July facts and the clean-cutover guard refuses to combine those with an
# opening balance (see the note above ``DEMO_PERSONAS`` in ``app/demo_seed.py``).
DEMO_ONBOARDING = _demo_employee("demo-onboarding", "Julia Rehm")

# The deny-boundary persona. It deliberately has NO fixture capabilities and NO
# role assignment, but note what that does and does not mean: because the seed
# gives it a mapped+active employee row, ``subject_capabilities`` still grants the
# row-derived employee baseline, so the person records and reads their OWN time
# like anyone else. What the missing role denies is any access to *other*
# employees' data — an organizational position grants nothing (ACCESS-012/013),
# and there is deliberately no SUPERVISOR *role* (`app/auth/roles.py`): adding one
# is a separately designed authorization slice, not a demo convenience
# (a development-only identity seam).
DEMO_SUPERVISOR = Principal(subject="demo-supervisor", display_name="Katrin Voss")

DEMO_ADMIN = Principal(subject="demo-admin", display_name="Nils Brandt")

DEMO_HR_REQUESTER = Principal(subject="demo-hr", display_name="Lena Hoffmann")

DEMO_RECIPIENT = Principal(
    subject="demo-recipient",
    display_name="Tobias Fischer",
    # Same role as the RECIPIENT fixture: membership in the dev/test D6
    # recipient registry (valid_recipient_subjects reads these caps).
    capabilities=frozenset({Capability.SUBMISSION_READ_ASSIGNED}),
)

#: The demo employee personas, in the order the seed reports them.
DEMO_EMPLOYEE_PRINCIPALS = (
    DEMO_EMPLOYEE,
    DEMO_PARTTIME,
    DEMO_OVERTIME,
    DEMO_OCD,
    DEMO_TRAVEL,
    DEMO_FLEX,
    DEMO_FINDINGS,
    DEMO_CLEAN,
    DEMO_SHIFT,
    DEMO_ONBOARDING,
    DEMO_SUPERVISOR,
)

DEV_PRINCIPALS: dict[str, Principal] = {
    principal.subject: principal
    for principal in (
        EMPLOYEE_A,
        EMPLOYEE_B,
        HR_REQUESTER,
        SUPERVISOR,
        RECIPIENT,
        TECHNICAL_ADMIN,
        CUTOVER_OPERATOR,
        *DEMO_EMPLOYEE_PRINCIPALS,
        DEMO_ADMIN,
        DEMO_HR_REQUESTER,
        DEMO_RECIPIENT,
    )
}

# Synthetic seeded employee ids (dev/test only, matches just seed-dev).
SEED_IDP_SUBJECTS = ("employee", "employee-b")
