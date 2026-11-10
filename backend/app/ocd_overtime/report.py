"""Pure minimized OCD/overtime preview builders (Policy §13.2/§13.3)."""

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from app.disclosure.fingerprint import with_preview_fingerprint
from app.ocd_overtime.schemas import (
    ComponentOut,
    DutyOut,
    ElectionProjectionOut,
    FindingOut,
    OcdCallProjectionOut,
    OcdCompensationPreviewOut,
    OvertimeAuthorizationProjectionOut,
    OvertimeIntervalOut,
    OvertimePreviewOut,
)
from app.policy.editions import (
    OCD_RULE_SET_V1,
    OVERTIME_RULE_SET_V1,
    resolve_edition,
)

#: The edition label a preview asserts. Resolved from the registry rather than
#: re-typed, so a recorded edition identity has exactly one definition (F4.4).
OCD_POLICY_VERSION = resolve_edition(OCD_RULE_SET_V1).version
OVERTIME_POLICY_VERSION = resolve_edition(OVERTIME_RULE_SET_V1).version


@dataclass(frozen=True, slots=True)
class OcdPreviewInputs:
    employee_identifier: str
    assignment_id: uuid.UUID
    start_at: datetime
    end_at: datetime
    calls: list[OcdCallProjectionOut]
    components: list[ComponentOut]
    duty: DutyOut
    election: ElectionProjectionOut
    #: Provenance of the availability (D2) and the employee's REPORTED notice
    #: instant. The latter is disclosed as a reported fact and is never read by
    #: ``classify_duty``, so it cannot become authoritative.
    plan_source: str
    reported_notice_at: datetime | None
    findings: list[FindingOut]


def build_ocd_compensation_preview(inputs: OcdPreviewInputs) -> OcdCompensationPreviewOut:
    return with_preview_fingerprint(
        OcdCompensationPreviewOut(
            policy_version=OCD_POLICY_VERSION,
            policy_status="CONFIRMED",
            employee_identifier=inputs.employee_identifier,
            ocd_assignment_id=inputs.assignment_id,
            assignment_start_at=inputs.start_at,
            assignment_end_at=inputs.end_at,
            calls=inputs.calls,
            classified_components=inputs.components,
            duty=inputs.duty,
            election=inputs.election,
            plan_source=inputs.plan_source,
            reported_notice_at=inputs.reported_notice_at,
            findings=inputs.findings,
        )
    )


@dataclass(frozen=True, slots=True)
class OvertimePreviewInputs:
    employee_identifier: str
    period_start: date
    period_end: date
    authorizations: list[OvertimeAuthorizationProjectionOut]
    intervals: list[OvertimeIntervalOut]
    components: list[ComponentOut]
    election: ElectionProjectionOut
    findings: list[FindingOut]


def build_overtime_preview(inputs: OvertimePreviewInputs) -> OvertimePreviewOut:
    return with_preview_fingerprint(
        OvertimePreviewOut(
            policy_version=OVERTIME_POLICY_VERSION,
            policy_status="CONFIRMED",
            employee_identifier=inputs.employee_identifier,
            period_start=inputs.period_start,
            period_end=inputs.period_end,
            authorizations=inputs.authorizations,
            overtime_intervals=inputs.intervals,
            classified_components=inputs.components,
            election=inputs.election,
            findings=inputs.findings,
        )
    )
