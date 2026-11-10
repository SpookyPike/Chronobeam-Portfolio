"""Private shift API and report-preview schemas."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.enums import FindingStatus, ShiftAssignmentStatus, ShiftDeviationType
from app.worktime.schemas import BreakIntervalOut

#: The Shift Report's paid-break state (SHIFT-003). ``EXPLICIT_FACT_REQUIRED``
#: means no paid-break fact is linked to this assignment, so the requirement is
#: outstanding; ``RECORDED`` means the employee recorded one and it is met.
#: Deliberately a status only — the break's own times are not added to a
#: disclosed projection.
PaidBreakStatus = Literal["EXPLICIT_FACT_REQUIRED", "RECORDED"]

#: Whether a recipient must review the compensation outcome by hand. Deliberately
#: report-LEVEL: the employee's findings stay private and are never added to a
#: recipient payload, so this single status is the narrowest honest signal that an
#: entitlement exists but is not authoritative yet. ``NOT_APPLICABLE`` means this
#: report is not about an unresolved entitlement at all.
CompensationReviewStatus = Literal["NOT_APPLICABLE", "MANUAL_REVIEW_REQUIRED"]


class ShiftFindingOut(BaseModel):
    rule_id: str
    status: FindingStatus
    message: str


class ShiftRevisionOut(BaseModel):
    id: uuid.UUID
    revision_no: int
    planned_start_at: datetime
    planned_end_at: datetime
    communicated_at: datetime | None


class ShiftDeviationOut(BaseModel):
    id: uuid.UUID
    deviation_type: ShiftDeviationType
    actual_start_at: datetime | None
    actual_end_at: datetime | None
    noticed_or_decided_at: datetime | None
    decision_maker_type: str | None
    reason: str | None
    source_work_interval_ids: list[uuid.UUID]
    version: int


class ShiftOut(BaseModel):
    id: uuid.UUID
    shift_kind: str | None
    status: ShiftAssignmentStatus
    #: Who wrote this plan. The employee's own record must never be presented as
    #: an institutionally approved plan, so the surface labels by provenance.
    plan_source: str
    approved_revision: ShiftRevisionOut
    actual_start_at: datetime | None
    actual_end_at: datetime | None
    deviation: ShiftDeviationOut | None
    findings: list[ShiftFindingOut]
    paid_break_status: PaidBreakStatus
    #: The paid-break FACTS the status above is derived from, so the employee can
    #: see what is already recorded before adding another one — a long shift can
    #: hold several. Resolved by ``linked_shift_assignment_id``, not by day: a
    #: break after midnight belongs to the next day's day-panel list, and the
    #: shift is the only thing that owns it. Correcting or removing one stays the
    #: day timeline's job (supersede/version, delete-with-reason), which is why
    #: this is a read-only projection and not a second break editor.
    paid_breaks: list[BreakIntervalOut]


class ShiftConfirmIn(BaseModel):
    """BE-CMD-004 optimistic confirmation of the loaded approved plan."""

    approved_revision_id: uuid.UUID
    client_request_id: uuid.UUID | None = None


class ShiftPlanIn(BaseModel):
    """Employee-recorded planned shift (``BE-CMD-001`` create).

    ``extra="forbid"`` is load-bearing: ``employee_id`` is NEVER a request field —
    the employee is resolved from the session — so a smuggled ``employee_id`` must
    fail loudly rather than be silently ignored.

    ``client_request_id`` is REQUIRED, not optional. A create must be retry-safe:
    without it a dropped response would leave the employee unable to tell whether
    the shift was recorded, and a retry would double-create it.
    """

    model_config = ConfigDict(extra="forbid")

    planned_start_local: datetime
    planned_end_local: datetime
    start_fold: Literal[0, 1] | None = None
    end_fold: Literal[0, 1] | None = None
    shift_kind: str | None = Field(default=None, max_length=120)
    client_request_id: uuid.UUID

    @field_validator("planned_start_local", "planned_end_local")
    @classmethod
    def _reject_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is not None:
            raise ValueError("wall times must be naive; the timezone is always Europe/Berlin")
        return value


class ShiftPlanPatch(BaseModel):
    """Correct an employee-recorded plan (optimistic on ``approved_revision_id``).

    A correction is NOT a create, so it carries no ``client_request_id``: it is
    governed by the expected token instead, and accepting both would give two
    answers to "is this the same command".

    The planned times are a PAIR (a shift window is one fact), so providing one
    without the other is refused rather than half-applied. ``shift_kind`` merges by
    PRESENCE, like every other nullable field on a versioned model here: omitted
    means unchanged, an explicit ``null`` clears it.
    """

    model_config = ConfigDict(extra="forbid")

    approved_revision_id: uuid.UUID
    planned_start_local: datetime | None = None
    planned_end_local: datetime | None = None
    start_fold: Literal[0, 1] | None = None
    end_fold: Literal[0, 1] | None = None
    shift_kind: str | None = Field(default=None, max_length=120)

    @field_validator("planned_start_local", "planned_end_local")
    @classmethod
    def _reject_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            raise ValueError("wall times must be naive; the timezone is always Europe/Berlin")
        return value

    @model_validator(mode="after")
    def _require_paired_times(self) -> "ShiftPlanPatch":
        if (self.planned_start_local is None) != (self.planned_end_local is None):
            raise ValueError("planned start and end must be provided together")
        return self


class ShiftWithdrawIn(BaseModel):
    """Retract an employee-recorded plan that was never the real plan.

    This is the MISTAKEN-ENTRY act, not a cancellation: a real operational
    cancellation is recorded through the deviation route, which leaves the plan
    ``PLANNED`` so the scheduled shift survives as the compensation basis.
    """

    model_config = ConfigDict(extra="forbid")

    approved_revision_id: uuid.UUID


class ShiftDeviationIn(BaseModel):
    """Actual wall-time facts plus structured plan-difference context.

    ``reason`` captures the supplied decision context; SHIFT-013 prevents
    validation of who was authorized to decide it.
    """

    deviation_type: ShiftDeviationType
    actual_start_local: datetime | None = None
    actual_end_local: datetime | None = None
    timezone: Literal["Europe/Berlin"] = "Europe/Berlin"
    start_fold: Literal[0, 1] | None = None
    end_fold: Literal[0, 1] | None = None
    noticed_or_decided_local: datetime | None = None
    noticed_or_decided_fold: Literal[0, 1] | None = None
    decision_maker_type: str | None = Field(default=None, max_length=120)
    reason: str | None = Field(default=None, max_length=500)
    version: int | None = Field(default=None, ge=1)

    @field_validator("actual_start_local", "actual_end_local", "noticed_or_decided_local")
    @classmethod
    def _reject_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            raise ValueError("wall times must be naive; the timezone field selects the zone")
        return value

    @model_validator(mode="after")
    def _require_paired_actual_times(self) -> "ShiftDeviationIn":
        if (self.actual_start_local is None) != (self.actual_end_local is None):
            raise ValueError("actual start and end must be provided together")
        if self.deviation_type != ShiftDeviationType.CANCELLED and self.actual_start_local is None:
            raise ValueError("actual times are required unless the shift was cancelled")
        return self


class ShiftReportPreviewOut(BaseModel):
    """Minimized, employee-private package shape; no recipient/disclosure exists."""

    report_type: Literal["SHIFT_REPORT"] = "SHIFT_REPORT"
    policy_version: str
    policy_status: Literal["PROVISIONAL", "BLOCKED"]
    employee_identifier: str
    shift_assignment_id: uuid.UUID
    planned_start_at: datetime
    planned_end_at: datetime
    actual_start_at: datetime | None
    actual_end_at: datetime | None
    deviation_type: ShiftDeviationType | None
    noticed_or_decided_at: datetime | None
    decision_maker_type: str | None
    reason: str | None
    findings: list[ShiftFindingOut]
    paid_break_status: PaidBreakStatus
    plan_source: str
    compensation_review_status: CompensationReviewStatus
    # Deterministic seal over the RECIPIENT payload (this model
    # minus findings). The submission command verifies it against a
    # recomputation — changed facts => different fingerprint => re-preview.
    preview_fingerprint: str | None = None
