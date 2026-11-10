"""Canonical enums (WT-BACKEND §4).

Stable machine identifiers, never display strings. The identity/ledger
slice defines the core vocabulary; later tables extend the enums as they
arrive.
"""

from enum import StrEnum
from typing import Literal

#: The shared finding-severity vocabulary (WT-BACKEND §4, BE-ENUM-005).
#: Defined ONCE here: the shift and OCD/overtime surfaces each carried their own
#: inline ``Literal[...]`` copy, which is exactly the drift a single definition
#: removes (audit finding ``n1``). ``FindingSeverity`` (``policy/engine.py``) is
#: the engine's subset of this vocabulary, and ``PASS`` has no representation
#: anywhere — BE-ENUM-005 is deliberately NOT a runtime enum, because nothing in
#: the application reads a rule status (F8-4's decision).
FindingStatus = Literal["INFO", "WARNING", "CRITICAL", "BLOCKED_POLICY"]


class WorkType(StrEnum):
    """BE-ENUM-001 — interval-level work type."""

    ON_SITE = "ON_SITE"
    REMOTE = "REMOTE"
    SHIFT = "SHIFT"
    OCD_CALL = "OCD_CALL"
    OVERTIME_PAID = "OVERTIME_PAID"
    OVERTIME_TIME_COMP = "OVERTIME_TIME_COMP"
    BUSINESS_TRIP_WORK = "BUSINESS_TRIP_WORK"


class PeriodStatus(StrEnum):
    """work_period.status — employee-side lifecycle only (BE spec §5.3/§12)."""

    OPEN = "OPEN"
    REVIEWED = "REVIEWED"
    CLOSED = "CLOSED"


class PayGroupBucket(StrEnum):
    """employment_contract.pay_group_bucket."""

    LE_9B = "LE_9B"
    GE_9C = "GE_9C"
    UNKNOWN = "UNKNOWN"


class MasterDataSource(StrEnum):
    """Source of effective-dated master data (contract/schedule).

    ``EMPLOYEE`` marks employee-maintained working-time schedule rows: the
    weekly target and weekday distribution are
    employee-maintained until an authoritative supported integration exists.
    It is only writable on ``work_schedule_version``; contract rows stay
    MASTER/ADMIN_SEED.
    """

    MASTER = "MASTER"
    ADMIN_SEED = "ADMIN_SEED"
    EMPLOYEE = "EMPLOYEE"


class IntervalSource(StrEnum):
    """work_interval.source."""

    EMPLOYEE = "EMPLOYEE"
    IMPORT = "IMPORT"


class SubmissionPurpose(StrEnum):
    """BE-ENUM-004 — purpose-scoped disclosure/submission objects.

    ``RANDOM_CHECK`` / ``WORK_TIME_RECORD`` are employee-opened month views
    via ``access_request``/``disclosure_grant``; the remaining purposes are
    immutable ``submission_package`` projections (ACCESS-004/005/006).
    """

    SHIFT_REPORT = "SHIFT_REPORT"
    OVERTIME_PAID = "OVERTIME_PAID"
    OVERTIME_TIME_COMP = "OVERTIME_TIME_COMP"
    OCD_COMPENSATION = "OCD_COMPENSATION"
    WORK_TIME_RECORD = "WORK_TIME_RECORD"
    RANDOM_CHECK = "RANDOM_CHECK"


class CalendarDayClass(StrEnum):
    """Versioned calendar classification used by ordinary-time calculation."""

    PUBLIC_HOLIDAY = "PUBLIC_HOLIDAY"
    EMPLOYER_FREE = "EMPLOYER_FREE"


class DayStatusCategory(StrEnum):
    """BE-ENUM-002 (narrowed) — the time-tracking day-status categories.

    Only the four categories with a source-backed working-time effect ship in
    F5. The rest of BE-ENUM-002's list (overtime compensation, beamtime
    compensation, unpaid vacation, educational/special exemption, …) is the
    deferred catalogue: each needs a source-backed effect before it may enter
    the calculation domain. ``REMOTE``/business trip/doctor attendance are
    explicitly NOT day statuses — they are work types or travel facts.
    """

    VACATION = "VACATION"
    SICK = "SICK"
    FLEX_COMPENSATION = "FLEX_COMPENSATION"
    SHIFT_COMPENSATION = "SHIFT_COMPENSATION"


class DayStatusPortion(StrEnum):
    """How much of the day the status covers."""

    FULL = "FULL"
    HALF = "HALF"


class DayStatusDayPart(StrEnum):
    """Which part of the day a HALF status covers.

    ``ALL`` exists so the column can be NOT NULL: an optional ``day_part`` in a
    Postgres unique key treats NULLs as distinct, so two full-day rows for one
    date would both be storable and "exactly one current version per logical
    fact" (EINV-04) would be unenforceable.
    """

    ALL = "ALL"
    MORNING = "MORNING"
    AFTERNOON = "AFTERNOON"


class PolicyVersionStatus(StrEnum):
    """Lifecycle of a code-backed policy rule set (not a runtime DSL)."""

    ACTIVE = "ACTIVE"
    PROVISIONAL = "PROVISIONAL"
    RETIRED = "RETIRED"
    BLOCKED = "BLOCKED"


class ShiftAssignmentStatus(StrEnum):
    """Lifecycle of a shift assignment (BE §5.4).

    ``PLANNED`` is the only ACTIVE state (``ACTIVE_ASSIGNMENT_STATUSES``).
    ``WITHDRAWN`` is the employee retracting their OWN plan record because it was
    entered in error or was never the real plan — it is **not** a cancellation and
    carries no entitlement (see ``ShiftDeviationType.CANCELLED`` for the real
    operational event, which leaves the plan ``PLANNED``). ``CANCELLED`` /
    ``SUPERSEDED`` remain import-feed lifecycle concepts and are not produced by
    any employee path.
    """

    PLANNED = "PLANNED"
    WITHDRAWN = "WITHDRAWN"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


class ShiftDeviationType(StrEnum):
    """BE-ENUM-003 — factual shift-plan deviation classification."""

    CANCELLED = "CANCELLED"
    RESCHEDULED = "RESCHEDULED"
    ADDITIONAL = "ADDITIONAL"
    SWAPPED = "SWAPPED"
    SHORTENED = "SHORTENED"
    OVERRUN = "OVERRUN"
    OVERRUN_GT_10H = "OVERRUN_GT_10H"
    MINOR_CHANGE = "MINOR_CHANGE"
    CANCELLED_SICKNESS = "CANCELLED_SICKNESS"
    #: The employer aborted a shift that had already STARTED (POL-02).
    #: Distinct from
    #: ``CANCELLED`` because the two must behave differently on the actual-work
    #: facts: ``save_deviation`` DELETES the linked intervals for a ``CANCELLED``
    #: deviation (the shift never happened), whereas an abortion must PRESERVE
    #: the partial work that really was performed (``INV-02``) —
    #: that partial work is itself the entitlement input. Both raise SHIFT-010
    #: ``BLOCKED_POLICY``.
    ABORTED_BY_COMPANY = "ABORTED_BY_COMPANY"


class OcdAssignmentStatus(StrEnum):
    """``ocd_assignment.status`` — the on-call AVAILABILITY lifecycle (``INV-10``).

    Distinct from ``ShiftAssignmentStatus``: the two enums share no member and no
    semantics, so they are never compared to one another and never share a
    predicate (``ACTIVE_ASSIGNMENT_STATUSES`` / ``OCD_ACTIVE_STATUSES``).
    """

    SCHEDULED = "SCHEDULED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


#: Shift: the ONLY status in which an assignment is an ACTIVE plan. Everything
#: else (``WITHDRAWN`` / ``CANCELLED`` / ``SUPERSEDED``) is retained lineage that
#: must never be confirmable, deviable, break-linkable, reportable or disclosable.
#: Element type is ``ShiftAssignmentStatus`` — applying this set to an
#: ``OcdAssignment`` is a type error, not a silent ``False``.
ACTIVE_ASSIGNMENT_STATUSES: frozenset[ShiftAssignmentStatus] = frozenset(
    {ShiftAssignmentStatus.PLANNED}
)

#: OCD: an assignment is a current, writable head iff its status is ``SCHEDULED``.
#: Reaching the head still requires the chain-head + writer-provenance + dependency
#: rules; this set is only the status dimension. Element type is
#: ``OcdAssignmentStatus``.
OCD_ACTIVE_STATUSES: frozenset[OcdAssignmentStatus] = frozenset({OcdAssignmentStatus.SCHEDULED})


class BreakKind(StrEnum):
    """``break_interval.kind`` — the explicit-break vocabulary (WT-BACKEND §5.3).

    An explicit break is for the cases a gap between intervals cannot express:
    a break *inside* one factual interval (typically a shift), which the span
    would otherwise count as worked time.

    Only ``UNPAID`` reduces worked time. ``PAID_SHIFT_BREAK`` is the compensated
    statutory shift break (SHIFT-003): it is recorded and distinguished but is
    NOT deducted, because it is remunerated presence rather than a gap. ``OTHER``
    is deliberately non-deducting too — the supplied sources state no unpaid
    semantics for it, and deducting it would be stricter than the regime
    (``INV-15``).
    """

    UNPAID = "UNPAID"
    PAID_SHIFT_BREAK = "PAID_SHIFT_BREAK"
    OTHER = "OTHER"


#: Provenance of a fact the EMPLOYEE authored themselves. Every employee write
#: path can only ever produce this value; it is server-forced, never accepted from
#: a request body (``ACCESS-017``).
EMPLOYEE_SOURCE = "EMPLOYEE_RECORDED"

#: Provenance of an INSTITUTIONAL decision (``ACCESS-017``/``EINV-23``).
INSTITUTIONAL_SOURCE = "INSTITUTIONAL_APPROVAL"

#: Provenance values that can make a row authoritative. DENY BY DEFAULT: an
#: unrecognised ``source`` is never authoritative, so a future adapter's
#: provenance value cannot silently become authoritative — and cannot silently
#: become employee-writable either (``is_employee_writable_source`` is the mirror
#: image and requires exactly ``EMPLOYEE_SOURCE``).
AUTHORITATIVE_SOURCES: frozenset[str] = frozenset({INSTITUTIONAL_SOURCE})


def is_employee_writable_source(source: str) -> bool:
    """May the employee write path version a row with this ``source``?

    ``EINV-23``/``ACCESS-017``: authority is a property of the WRITER, so the
    employee may only ever correct what the employee authored. Deny by default —
    an unknown provenance value is neither authoritative nor employee-writable,
    which is what makes a later institutional planner/import take over cleanly:
    once its rows carry a different ``source``, the employee path stops being able
    to touch them without any migration or precedence rule.
    """
    return source == EMPLOYEE_SOURCE
