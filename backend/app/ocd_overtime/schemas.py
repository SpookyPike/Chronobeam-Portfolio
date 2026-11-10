"""Private employee OCD/overtime API and minimized projection schemas."""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.enums import FindingStatus, WorkType
from app.ocd_overtime.models import (
    CompensationContextType,
    CompensationMode,
    OcdAssignmentStatus,
    OcdCallMode,
    OcdCallStatus,
    OvertimeAuthStatus,
)

#: The only statuses the EMPLOYEE write path may produce (ACCESS-017). An
#: institutional decision (APPROVED/REJECTED) is not expressible here at all.
EMPLOYEE_AUTH_STATUSES = frozenset({OvertimeAuthStatus.REQUESTED, OvertimeAuthStatus.CANCELLED})

#: The only statuses the INSTITUTIONAL writer may produce.
INSTITUTIONAL_AUTH_STATUSES = frozenset({OvertimeAuthStatus.APPROVED, OvertimeAuthStatus.REJECTED})


class FindingOut(BaseModel):
    rule_id: str
    status: FindingStatus
    message: str


class OcdCallIn(BaseModel):
    ocd_assignment_id: uuid.UUID
    call_start_local: datetime
    call_end_local: datetime
    timezone: Literal["Europe/Berlin"] = "Europe/Berlin"
    start_fold: Literal[0, 1] | None = None
    end_fold: Literal[0, 1] | None = None
    mode: OcdCallMode
    travel_out_minutes: int | None = Field(None, ge=0)
    travel_back_minutes: int | None = Field(None, ge=0)
    comment: str | None = Field(None, max_length=500)
    client_request_id: uuid.UUID | None = None

    @field_validator("call_start_local", "call_end_local")
    @classmethod
    def naive(cls, value: datetime) -> datetime:
        if value.tzinfo is not None:
            raise ValueError("wall times must be naive; the timezone field selects the zone")
        return value


class OcdCallPatch(BaseModel):
    version: int = Field(ge=1)
    call_start_local: datetime | None = None
    call_end_local: datetime | None = None
    timezone: Literal["Europe/Berlin"] = "Europe/Berlin"
    start_fold: Literal[0, 1] | None = None
    end_fold: Literal[0, 1] | None = None
    mode: OcdCallMode | None = None
    travel_out_minutes: int | None = Field(None, ge=0)
    travel_back_minutes: int | None = Field(None, ge=0)
    comment: str | None = Field(None, max_length=500)

    @field_validator("call_start_local", "call_end_local")
    @classmethod
    def naive(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            raise ValueError("wall times must be naive; the timezone field selects the zone")
        return value


class VoidIn(BaseModel):
    version: int = Field(ge=1)
    reason: str | None = Field(None, max_length=500)


class OcdCallOut(BaseModel):
    id: uuid.UUID
    ocd_assignment_id: uuid.UUID | None
    mode: OcdCallMode
    call_start_at: datetime
    call_end_at: datetime
    travel_out_minutes: int | None
    travel_back_minutes: int | None
    comment: str | None
    status: OcdCallStatus
    version: int
    linked_interval_id: uuid.UUID | None = None
    span_start_at: datetime | None = None
    span_end_at: datetime | None = None
    elapsed_seconds: int | None = None


class OcdOut(BaseModel):
    id: uuid.UUID
    start_at: datetime
    end_at: datetime
    status: OcdAssignmentStatus
    scheduled_at: datetime | None
    #: The instant the EMPLOYEE reports they were told. A different fact from
    #: ``scheduled_at``: it is preserved so a short-notice case can be reviewed
    #: later, and no classifier reads it, so it never becomes authoritative.
    reported_notice_at: datetime | None
    source: str
    #: The optimistic-concurrency token. ``PATCH`` and ``POST …/cancel`` are
    #: version-guarded and answer 409 ``CONFLICT_VERSION`` on a stale token, so the
    #: client MUST be able to read the current one from the row it is looking at —
    #: without it the per-card Correct/Cancel controls cannot be offered at all.
    #: Same contract as ``OcdCallOut.version``.
    version: int
    calls: list[OcdCallOut]
    findings: list[FindingOut]
    compensation_preview_available: bool = True


def _naive_wall_time(value: datetime | None) -> datetime | None:
    """The wall-time INPUT contract (F8.1) shared by the employee window schemas.

    A wall time is a NAIVE ``Europe/Berlin`` local time; the service anchors it to
    Berlin at its boundary. Aware input is REFUSED rather than
    reinterpreted: accepting an offset here would create a second, silently
    different definition of the same window, and the window is a calculation
    input (``_covers``, the travel suppression) rather than a display value.

    Same contract as ``IntervalIn``/``OcdCallIn``/``TripIn`` — this family was the
    one that lacked it (``F8.6``), so a naive window was persisted through the
    database session timezone instead of Berlin.
    """

    if value is not None and value.tzinfo is not None:
        raise ValueError("wall times must be naive Berlin wall times, not aware instants")
    return value


class OvertimeAuthorizationIn(BaseModel):
    """An employee overtime APPLICATION — a request, not an authorization.

    ``status``, ``authorized_by`` and ``source`` are deliberately absent: the
    employee asks for overtime, an institutional writer decides it
    (``ACCESS-017``/``EINV-23``). ``extra="forbid"`` is what makes that
    testable — pydantic's default is ``ignore``, so without it a smuggled
    ``status="APPROVED"`` would be silently dropped rather than refused.
    """

    model_config = ConfigDict(extra="forbid")

    valid_from: datetime
    valid_to: datetime | None = None
    purpose: str | None = Field(default=None, max_length=500)
    source_ref: str | None = Field(default=None, max_length=200)

    @field_validator("valid_from", "valid_to")
    @classmethod
    def naive(cls, value: datetime | None) -> datetime | None:
        return _naive_wall_time(value)


class OvertimeAuthorizationPatch(BaseModel):
    """Correction of an overtime REQUEST — PRESENCE semantics.

    Deliberately NOT a subclass of ``OvertimeAuthorizationIn``: inheriting it
    would carry the create-time defaults into a correction, and a client that
    omits a field would then silently reset a terminal ``CANCELLED`` decision
    back to ``REQUESTED`` and NULL the omitted optional fields. Every sibling
    correction schema in this codebase merges by presence (``IntervalPatch``,
    ``OcdCallPatch``, ``TripPatch``, ``SegmentPatch``); this one does too.

    Absent = unchanged. Explicit ``null`` = cleared. ``source`` is immutable
    after creation: it records the provenance of the original fact.

    The employee may move their OWN request between ``REQUESTED`` and
    ``CANCELLED`` only — an institutional decision is refused here (422) *and*
    refused again by the service when the chain head is not employee-authored,
    so neither the body nor the target can smuggle an approval in.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    purpose: str | None = Field(default=None, max_length=500)
    status: OvertimeAuthStatus | None = None
    source_ref: str | None = Field(default=None, max_length=200)

    @field_validator("valid_from", "valid_to")
    @classmethod
    def naive(cls, value: datetime | None) -> datetime | None:
        return _naive_wall_time(value)

    @field_validator("status")
    @classmethod
    def employee_decision_only(cls, value: OvertimeAuthStatus | None) -> OvertimeAuthStatus | None:
        if value is not None and value not in EMPLOYEE_AUTH_STATUSES:
            raise ValueError(
                "an overtime authorization's approval is an institutional decision; "
                "the employee may only request or cancel"
            )
        return value


class OvertimeAuthorizationDecisionIn(BaseModel):
    """The INSTITUTIONAL shape — accepted only by ``decide_authorization``.

    No employee-reachable route or schema can produce these values
    (``ACCESS-017``). ``authorized_by`` is required: an approval must name the
    institutional actor that gave it, and the writer never copies the
    predecessor's ``source``.

    Window and ``purpose`` merge by PRESENCE: an omitted field keeps the
    predecessor's value, an explicit ``null`` clears it.

    The window deliberately carries **no** naive-only validator (unlike the two
    employee schemas above): the ONE existing caller is the operator CLI
    (``app/overtime_admin.py::_parse_instant``), which resolves its own wall times
    through ``resolve_wall_time`` precisely so that a DST-ambiguous ``--from`` is
    refused with the candidates named, and therefore hands this schema an ALREADY
    AWARE instant. Refusing aware input here would break that seam and would force
    the institutional side to re-do DST resolution it cannot do. The writer
    (``decide_authorization``) anchors a naive value to Berlin and passes an aware
    one through, so both forms meet ONE resolver.
    """

    model_config = ConfigDict(extra="forbid")

    status: OvertimeAuthStatus
    authorized_by: str = Field(min_length=1, max_length=200)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    purpose: str | None = Field(default=None, max_length=500)
    source_ref: str | None = Field(default=None, max_length=200)

    @field_validator("status")
    @classmethod
    def institutional_decision_only(cls, value: OvertimeAuthStatus) -> OvertimeAuthStatus:
        if value not in INSTITUTIONAL_AUTH_STATUSES:
            raise ValueError("an institutional decision is APPROVED or REJECTED")
        return value


class OvertimeAuthorizationOut(BaseModel):
    id: uuid.UUID
    valid_from: datetime
    valid_to: datetime | None
    purpose: str | None
    status: OvertimeAuthStatus
    authorized_by: str | None
    source_ref: str | None
    source: str
    version: int


class ElectionIn(BaseModel):
    context_type: CompensationContextType
    context_id: uuid.UUID
    mode: CompensationMode


class ElectionOut(BaseModel):
    id: uuid.UUID
    context_type: CompensationContextType
    context_id: uuid.UUID
    mode: CompensationMode
    version: int
    selected_at: datetime


class ComponentOut(BaseModel):
    """One classified minute component of a private preview.

    ``time_equivalent_minutes`` is carried for rule-constant equivalents
    (night 12, Saturday 12, Sunday 15, holiday 21 minutes per hour). The
    ALLOW-002 overtime supplement NEVER carries it: the 18/9-minute
    equivalent is pay-group-bucket-derived and must not leave the employee's
    private domain  — the preview must equal
    the recipient payload).
    """

    rule_id: str
    component: str
    minutes: int
    time_equivalent_minutes: int | None = None


class OcdCallProjectionOut(BaseModel):
    call_id: uuid.UUID
    mode: OcdCallMode
    call_start_at: datetime
    call_end_at: datetime
    travel_out_minutes: int | None
    travel_back_minutes: int | None
    span_start_at: datetime
    span_end_at: datetime
    elapsed_seconds: int


class DutyOut(BaseModel):
    duty_minutes: int
    duty_kind: Literal["DUTY_GE_12H", "DUTY_LT_12H"]
    units: int | None
    basis_points: int | None
    short_notice: bool | None


class ElectionProjectionOut(BaseModel):
    """Frozen election reference included in a recipient projection only."""

    id: uuid.UUID | None
    mode: CompensationMode | None
    version: int | None


class OcdCompensationPreviewOut(BaseModel):
    report_type: Literal["OCD_COMPENSATION"] = "OCD_COMPENSATION"
    policy_version: str
    policy_status: Literal["CONFIRMED", "PROVISIONAL", "BLOCKED"]
    employee_identifier: str
    ocd_assignment_id: uuid.UUID
    assignment_start_at: datetime
    assignment_end_at: datetime
    calls: list[OcdCallProjectionOut]
    classified_components: list[ComponentOut]
    duty: DutyOut
    election: ElectionProjectionOut
    #: Who wrote the availability this compensation is based on (D2).
    plan_source: str
    #: The employee-reported notice instant, disclosed AS a reported fact.
    reported_notice_at: datetime | None
    findings: list[FindingOut]
    # Seal over the RECIPIENT payload (this model minus
    # findings); submission verifies it against a recomputation.
    preview_fingerprint: str | None = None


class OvertimeIntervalOut(BaseModel):
    interval_id: uuid.UUID
    start_at: datetime
    end_at: datetime
    work_type: WorkType
    elapsed_seconds: int
    linked_authorization_id: uuid.UUID | None
    version: int


class OvertimeAuthorizationProjectionOut(BaseModel):
    id: uuid.UUID
    valid_from: datetime
    valid_to: datetime | None
    purpose: str | None
    status: OvertimeAuthStatus
    source_ref: str | None


class OvertimePreviewOut(BaseModel):
    report_type: Literal["OVERTIME"] = "OVERTIME"
    policy_version: str
    policy_status: Literal["CONFIRMED", "PROVISIONAL", "BLOCKED"]
    employee_identifier: str
    period_start: date
    period_end: date
    authorizations: list[OvertimeAuthorizationProjectionOut]
    overtime_intervals: list[OvertimeIntervalOut]
    classified_components: list[ComponentOut]
    election: ElectionProjectionOut
    findings: list[FindingOut]
    # Seal over the RECIPIENT payload (this model minus
    # findings); submission verifies it against a recomputation.
    preview_fingerprint: str | None = None


# ---------------------------------------------------------------------------
# Employee plan entry (pilot slice)
# ---------------------------------------------------------------------------


class OcdAssignmentIn(BaseModel):
    """Employee-recorded on-call availability (``BE-CMD-001`` create).

    ``extra="forbid"`` is load-bearing: ``employee_id`` is never a request field —
    the employee is resolved from the session — so a smuggled one must fail loudly.
    ``client_request_id`` is REQUIRED, because a create must be retry-safe.

    ``scheduled_at`` is deliberately ABSENT and cannot be smuggled: it is the
    INSTITUTIONAL notice instant and the sole input to the remuneration-relevant
    short-notice classification, so an employee may not assert it. What they can do
    is preserve their own information in ``reported_notice_local``, which no
    classifier reads.
    """

    model_config = ConfigDict(extra="forbid")

    start_local: datetime
    end_local: datetime
    start_fold: Literal[0, 1] | None = None
    end_fold: Literal[0, 1] | None = None
    reported_notice_local: datetime | None = None
    reported_notice_fold: Literal[0, 1] | None = None
    client_request_id: uuid.UUID

    @field_validator("start_local", "end_local", "reported_notice_local")
    @classmethod
    def _reject_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            raise ValueError("wall times must be naive; the timezone is always Europe/Berlin")
        return value


class OcdAssignmentPatch(BaseModel):
    """Correct an employee-recorded availability, optimistically on ``version``.

    ``reported_notice_local`` merges by PRESENCE, like every other nullable field
    on a versioned model here: omitted means unchanged, an explicit ``null``
    clears it. A truthiness test would make an explicit ``null`` indistinguishable
    from "leave it alone", so the employee could never clear a notice they entered
    by mistake.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    start_local: datetime | None = None
    end_local: datetime | None = None
    start_fold: Literal[0, 1] | None = None
    end_fold: Literal[0, 1] | None = None
    reported_notice_local: datetime | None = None
    reported_notice_fold: Literal[0, 1] | None = None

    @field_validator("start_local", "end_local", "reported_notice_local")
    @classmethod
    def _reject_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            raise ValueError("wall times must be naive; the timezone is always Europe/Berlin")
        return value

    @model_validator(mode="after")
    def _require_paired_times(self) -> "OcdAssignmentPatch":
        if (self.start_local is None) != (self.end_local is None):
            raise ValueError("start and end must be provided together")
        return self


class OcdAssignmentCancelIn(BaseModel):
    """Cancel an employee-recorded availability, optimistically on ``version``."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
