"""Pure minimized Shift Report package-shape builder (ACCESS-004)."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.enums import ShiftDeviationType
from app.shift.schemas import (
    CompensationReviewStatus,
    PaidBreakStatus,
    ShiftFindingOut,
    ShiftReportPreviewOut,
)


@dataclass(frozen=True, slots=True)
class ShiftReportInputs:
    """Only fields required by the Shift Report baseline (Policy §13.1).

    There is intentionally no ordinary-interval collection here.  The caller
    must supply the actual facts already scoped to this one assignment.
    """

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
    # SHIFT-003's state for THIS assignment. The caller resolves it from the
    # linked paid-break facts; the builder never infers it from the plan.
    paid_break_status: PaidBreakStatus
    #: Provenance of the plan this report is about (D2).
    plan_source: str
    #: Computed by the caller from the SAME finding list it passes in, so the
    #: status and the findings cannot disagree.
    compensation_review_status: CompensationReviewStatus


def build_shift_report(inputs: ShiftReportInputs) -> ShiftReportPreviewOut:
    """Build a preview/package shape only; it neither persists nor discloses."""
    return ShiftReportPreviewOut(
        policy_version=inputs.policy_version,
        policy_status=inputs.policy_status,
        employee_identifier=inputs.employee_identifier,
        shift_assignment_id=inputs.shift_assignment_id,
        planned_start_at=inputs.planned_start_at,
        planned_end_at=inputs.planned_end_at,
        actual_start_at=inputs.actual_start_at,
        actual_end_at=inputs.actual_end_at,
        deviation_type=inputs.deviation_type,
        noticed_or_decided_at=inputs.noticed_or_decided_at,
        decision_maker_type=inputs.decision_maker_type,
        reason=inputs.reason,
        findings=inputs.findings,
        paid_break_status=inputs.paid_break_status,
        plan_source=inputs.plan_source,
        compensation_review_status=inputs.compensation_review_status,
    )


#: The entitlement-class findings. A report carrying ANY of them is reporting an
#: entitlement that is not authoritative yet, whatever the finding's severity: the
#: rules stay PROVISIONAL / BLOCKED_POLICY, so even a WARNING is not a grant.
ENTITLEMENT_FINDING_IDS: frozenset[str] = frozenset({"SHIFT-009", "SHIFT-010"})

#: Deviation types whose compensation the sources make an entitlement question
#: (a cancellation within the notice window, or a Company abortion).
ENTITLEMENT_DEVIATION_TYPES: frozenset[ShiftDeviationType] = frozenset(
    {ShiftDeviationType.CANCELLED, ShiftDeviationType.ABORTED_BY_COMPANY}
)


def compensation_review_status(
    deviation_type: ShiftDeviationType | None, findings: list[ShiftFindingOut]
) -> CompensationReviewStatus:
    """Whether a recipient must review the compensation outcome by hand.

    Fail-closed: it reports that review is needed whenever the deviation is one
    whose compensation the sources make an entitlement question AND any
    entitlement-class finding is present. It never asserts an amount, a
    compensation day, a credit or a payroll result — that is the whole point of the
    split between a factual report and an authoritative determination.
    """
    if deviation_type not in ENTITLEMENT_DEVIATION_TYPES:
        return "NOT_APPLICABLE"
    if any(finding.rule_id in ENTITLEMENT_FINDING_IDS for finding in findings):
        return "MANUAL_REVIEW_REQUIRED"
    return "NOT_APPLICABLE"
