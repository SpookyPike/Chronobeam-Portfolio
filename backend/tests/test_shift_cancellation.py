"""Shift cancellation vs mistaken-plan withdrawal, and the factual-report split.

Two acts that must never be conflated:

- **withdrawal** — the employee retracts a plan that was entered by mistake. No
  entitlement, no deviation, and the row stays as lineage.
- **cancellation** — an act of the COMPANY, recorded as a deviation. The plan stays
  PLANNED and survives as the compensation basis the sources make it.

The second half of this file covers the D19 split: a factual Shift Report for a
cancelled or aborted shift is submittable while SHIFT-009/010 are unresolved, and
the recipient gets one report-level review status — never an amount, a credit, a
compensation day or a payroll result, and never the employee's own findings.
"""

import uuid
from datetime import datetime
from typing import Any

import pytest
from app.enums import ShiftAssignmentStatus, ShiftDeviationType
from app.identity.models import Employee
from app.shift.models import ShiftAssignment, ShiftDeviation, ShiftPlanRevision
from app.shift.policy import ShiftFinding
from app.shift.schemas import ShiftFindingOut
from app.worktime.models import Correction, WorkInterval
from app.worktime.queries import current_intervals
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from .conftest import DEVELOPER_PRINCIPAL_HEADER, HR_HEADER, OTHER_EMPLOYEE_HEADER

SHIFTS = "/api/v1/me/shifts"

#: The D22 dropdown, verbatim from the source-backed decision roles.
DECISION_ROLES = (
    "Director",
    "Department Head",
    "Supervisor / Group Leader",
    "Person nominated by the Management Board",
    "Other / not stated",
)


def _employee(session: Session) -> Employee:
    employee = session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    return employee


def _plan(client: TestClient, start: str, end: str) -> dict[str, Any]:
    response = client.post(
        SHIFTS,
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "planned_start_local": start,
            "planned_end_local": end,
            "shift_kind": "NIGHT",
            "client_request_id": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _cancel(
    client: TestClient,
    assignment_id: str,
    *,
    noticed: str | None = "2026-07-09T10:00:00",
    role: str = "Director",
    reason: str | None = "Cancellation communicated by shift planning.",
    deviation_type: str = "CANCELLED",
    actual: tuple[str, str] | None = None,
) -> Any:
    payload: dict[str, Any] = {"deviation_type": deviation_type}
    if noticed is not None:
        payload["noticed_or_decided_local"] = noticed
    if role is not None:
        payload["decision_maker_type"] = role
    if reason is not None:
        payload["reason"] = reason
    # Actual times are REQUIRED for every deviation except CANCELLED — and that is
    # correct for an abortion: the entitlement is "the complete working time for
    # the shift", so the report must say what actually happened.
    if actual is not None:
        payload["actual_start_local"], payload["actual_end_local"] = actual
    return client.post(
        f"{SHIFTS}/{assignment_id}/deviation",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json=payload,
    )


def _submit(client: TestClient, assignment_id: str) -> Any:
    preview = client.get(
        f"{SHIFTS}/{assignment_id}/report-preview", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert preview.status_code == 200, preview.text
    return client.post(
        "/api/v1/me/submissions/SHIFT_REPORT",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "scope": {"assignment_id": assignment_id},
            "preview_fingerprint": preview.json()["preview_fingerprint"],
            "client_request_id": str(uuid.uuid4()),
            "recipients": ["recipient"],
        },
    )


def _revision(session: Session, assignment: ShiftAssignment) -> ShiftPlanRevision:
    revision = session.get(ShiftPlanRevision, assignment.approved_revision_id)
    assert revision is not None
    return revision


# ---------------------------------------------------------------------------
# The two acts
# ---------------------------------------------------------------------------


def test_a_cancellation_preserves_the_plan_and_records_the_context(
    entry_client: TestClient, db_session: Session
) -> None:
    """D23: the reported instant is STORED, and nothing is classified from it.

    No machine boundary is asserted anywhere: the sources say "within 14 days",
    and neither the inclusive endpoint, the unit (72 h vs 3 calendar days) nor the
    clock is settled.
    """
    body = _plan(entry_client, "2026-07-20T19:00:00", "2026-07-21T07:00:00")
    assignment = db_session.get(ShiftAssignment, uuid.UUID(body["id"]))
    assert assignment is not None
    before = _revision(db_session, assignment)
    before_times = (before.planned_start_at, before.planned_end_at, before.revision_no)

    response = _cancel(entry_client, body["id"], noticed="2026-07-01T10:00:00")
    assert response.status_code == 200, response.text

    refreshed = db_session.get(ShiftAssignment, uuid.UUID(body["id"]))
    assert refreshed is not None
    # The plan SURVIVES: it is the compensation basis (``POL-02``), and the
    # short-notice branch grants the complete working time for the shift.
    assert refreshed.status is ShiftAssignmentStatus.PLANNED
    after = _revision(db_session, refreshed)
    assert (after.planned_start_at, after.planned_end_at, after.revision_no) == before_times

    deviation = db_session.scalar(
        select(ShiftDeviation).where(ShiftDeviation.shift_assignment_id == refreshed.id)
    )
    assert deviation is not None
    assert deviation.deviation_type is ShiftDeviationType.CANCELLED
    assert deviation.noticed_or_decided_at is not None
    assert deviation.decision_maker_type == "Director"
    assert deviation.reason is not None

    # Nothing about an entitlement is asserted in the response.
    serialized = response.text.lower()
    for forbidden in ("compensation_day", "allowance_amount", "credit", "payroll"):
        assert forbidden not in serialized


def test_cancellation_within_fourteen_days_stores_the_instant_without_classifying_it(
    entry_client: TestClient, db_session: Session
) -> None:
    """The source wording's case: the instant is kept, the entitlement is not."""
    body = _plan(entry_client, "2026-07-20T19:00:00", "2026-07-21T07:00:00")
    response = _cancel(entry_client, body["id"], noticed="2026-07-10T10:00:00")
    assert response.status_code == 200, response.text

    findings = {f["rule_id"]: f for f in response.json()["findings"]}
    assert findings["SHIFT-009"]["status"] == "BLOCKED_POLICY"
    assert findings["SHIFT-010"]["status"] == "BLOCKED_POLICY"
    # The deviation carries the instant; no field carries a conclusion.
    assert response.json()["deviation"]["noticed_or_decided_at"] is not None


def test_cancellation_within_three_days_grants_no_compensation_day(
    entry_client: TestClient,
) -> None:
    """Gate off => SHIFT-010 BLOCKED_POLICY, and NO compensation day anywhere."""
    body = _plan(entry_client, "2026-07-20T19:00:00", "2026-07-21T07:00:00")
    response = _cancel(entry_client, body["id"], noticed="2026-07-19T10:00:00")
    assert response.status_code == 200, response.text
    findings = {f["rule_id"]: f for f in response.json()["findings"]}
    assert findings["SHIFT-010"]["status"] == "BLOCKED_POLICY"
    assert "compensation_day" not in response.text


def test_a_cancellation_can_be_recorded_for_each_source_backed_role_without_validation(
    entry_client: TestClient,
) -> None:
    """SHIFT-013: the role is reported data, never validated against a role list."""
    for index, role in enumerate(DECISION_ROLES):
        body = _plan(
            entry_client, f"2026-07-{20 + index:02d}T19:00:00", f"2026-07-{21 + index:02d}T07:00:00"
        )
        response = _cancel(entry_client, body["id"], role=role)
        assert response.status_code == 200, response.text
        assert response.json()["deviation"]["decision_maker_type"] == role

    # An arbitrary string is accepted too — the authority question is BLOCKED_POLICY.
    body = _plan(entry_client, "2026-07-28T19:00:00", "2026-07-29T07:00:00")
    response = _cancel(entry_client, body["id"], role="UNVALIDATED_REPORTED_ROLE")
    assert response.status_code == 200, response.text


def test_withdrawal_and_cancellation_are_distinguishable_and_mutually_exclusive(
    entry_client: TestClient, db_session: Session
) -> None:
    withdrawn_plan = _plan(entry_client, "2026-07-10T19:00:00", "2026-07-11T07:00:00")
    cancelled_plan = _plan(entry_client, "2026-07-12T19:00:00", "2026-07-13T07:00:00")

    # Withdraw: WITHDRAWN, and NO deviation — no shift was ever really planned.
    withdrawn = entry_client.post(
        f"{SHIFTS}/{withdrawn_plan['id']}/withdraw",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"approved_revision_id": withdrawn_plan["approved_revision"]["id"]},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["status"] == "WITHDRAWN"
    assert withdrawn.json()["deviation"] is None
    assert (
        db_session.scalar(
            select(ShiftDeviation).where(
                ShiftDeviation.shift_assignment_id == uuid.UUID(withdrawn_plan["id"])
            )
        )
        is None
    )

    # The withdrawal's lineage row records a STATUS write, not a version write: it
    # appends no revision, so `new_version` is NULL. Writing `N -> N` would claim a
    # correction that changed nothing, and it contradicts the repository's own shape
    # for a no-new-version write (the `void_call` interval tombstone, the absence
    # day-status removal).
    correction = db_session.scalar(
        select(Correction).where(
            Correction.entity_type == "SHIFT_ASSIGNMENT",
            Correction.entity_id == uuid.UUID(withdrawn_plan["id"]),
        )
    )
    assert correction is not None
    assert correction.old_version == 1
    assert correction.new_version is None, "a withdrawal appends no version"
    assert correction.reason == "Plan withdrawn by the employee."

    # Cancel: still PLANNED, WITH a deviation — the plan is the basis.
    assert _cancel(entry_client, cancelled_plan["id"]).status_code == 200

    # Once either act has happened the other is refused, because both are
    # plan-level mutations and the plan is frozen or non-active.
    assert _cancel(entry_client, withdrawn_plan["id"]).status_code == 409
    assert (
        entry_client.post(
            f"{SHIFTS}/{cancelled_plan['id']}/withdraw",
            headers=DEVELOPER_PRINCIPAL_HEADER,
            json={"approved_revision_id": cancelled_plan["approved_revision"]["id"]},
        ).status_code
        == 409
    )


# ---------------------------------------------------------------------------
# The D19 split: factual report vs authoritative entitlement
# ---------------------------------------------------------------------------


def test_the_entitlement_findings_do_not_block_a_factual_shift_report(
    entry_client: TestClient, db_session: Session
) -> None:
    """A cancelled shift is a real thing that happened; it must be reportable."""
    body = _plan(entry_client, "2026-07-20T19:00:00", "2026-07-21T07:00:00")
    assert _cancel(entry_client, body["id"], noticed="2026-07-19T10:00:00").status_code == 200

    created = _submit(entry_client, body["id"])
    assert created.status_code == 200, created.text
    payload = created.json()["payload"]

    # The complete factual record, and nothing more.
    assert payload["planned_start_at"] is not None
    assert payload["planned_end_at"] is not None
    assert payload["deviation_type"] == "CANCELLED"
    assert payload["noticed_or_decided_at"] is not None
    assert payload["decision_maker_type"] == "Director"
    assert payload["reason"] is not None
    assert payload["plan_source"] == "EMPLOYEE_RECORDED"
    assert payload["compensation_review_status"] == "MANUAL_REVIEW_REQUIRED"

    # The employee's private findings are NOT added to make this work.
    assert "findings" not in payload


def test_the_recipient_projection_carries_only_a_review_status(
    entry_client: TestClient,
) -> None:
    body = _plan(entry_client, "2026-07-20T19:00:00", "2026-07-21T07:00:00")
    assert _cancel(entry_client, body["id"]).status_code == 200
    created = _submit(entry_client, body["id"])
    assert created.status_code == 200, created.text

    serialized = str(created.json()["payload"]).lower()
    for forbidden in (
        "amount",
        "credit",
        "compensation_day",
        "allowance",
        "payroll",
        "entitlement",
        "short_notice",
        "finding",
    ):
        assert forbidden not in serialized, forbidden
    assert created.json()["payload"]["compensation_review_status"] == "MANUAL_REVIEW_REQUIRED"


def test_a_non_entitlement_blocked_finding_still_blocks_a_shift_report(
    entry_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exception is an ALLOWLIST, not a blanket exemption for BLOCKED_POLICY."""
    from app.shift import service as shift_service

    body = _plan(entry_client, "2026-07-20T19:00:00", "2026-07-21T07:00:00")
    assert _cancel(entry_client, body["id"]).status_code == 200

    real = shift_service.evaluate_shift

    def with_extra_blocked_finding(**kwargs: Any) -> list[ShiftFinding]:
        return [
            *real(**kwargs),
            ShiftFinding("SHIFT-999", "BLOCKED_POLICY", "A factual datum is missing."),
        ]

    monkeypatch.setattr(shift_service, "evaluate_shift", with_extra_blocked_finding)

    preview = entry_client.get(
        f"{SHIFTS}/{body['id']}/report-preview", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["compensation_review_status"] == "MANUAL_REVIEW_REQUIRED"

    refused = entry_client.post(
        "/api/v1/me/submissions/SHIFT_REPORT",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "scope": {"assignment_id": body["id"]},
            "preview_fingerprint": preview.json()["preview_fingerprint"],
            "client_request_id": str(uuid.uuid4()),
            "recipients": ["recipient"],
        },
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "SUBMISSION_MISSING_REQUIRED_DATA"


def test_the_exception_does_not_leak_to_the_other_submission_purposes(
    entry_client: TestClient, db_session: Session
) -> None:
    """The exempt set is EMPTY for every purpose except SHIFT_REPORT."""
    from app.disclosure.service import _contains_blocked_policy
    from app.enums import SubmissionPurpose

    class _Preview:
        def __init__(self) -> None:
            self.findings = [
                ShiftFindingOut(rule_id="SHIFT-009", status="BLOCKED_POLICY", message="x"),
                ShiftFindingOut(rule_id="SHIFT-010", status="BLOCKED_POLICY", message="y"),
            ]

    preview = _Preview()
    assert _contains_blocked_policy(preview, SubmissionPurpose.SHIFT_REPORT) is False
    for purpose in (
        SubmissionPurpose.OCD_COMPENSATION,
        SubmissionPurpose.OVERTIME_PAID,
        SubmissionPurpose.OVERTIME_TIME_COMP,
    ):
        assert _contains_blocked_policy(preview, purpose) is True


def test_a_company_abortion_after_shift_start_preserves_the_partial_actual_work(
    entry_client: TestClient, db_session: Session
) -> None:
    """ABORTED_BY_COMPANY is distinct from CANCELLED because it keeps the work.

    ``save_deviation`` DELETES the linked actual intervals for a CANCELLED
    deviation — the shift never happened. An abortion happened and was cut short,
    and that partial work is itself the entitlement input, so erasing it would
    destroy the very fact the entitlement is calculated from.
    """
    body = _plan(entry_client, "2026-07-20T19:00:00", "2026-07-21T07:00:00")
    confirmed = entry_client.post(
        f"{SHIFTS}/{body['id']}/confirm",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"approved_revision_id": body["approved_revision"]["id"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert (
        len(
            db_session.scalars(
                select(WorkInterval).where(
                    WorkInterval.linked_shift_assignment_id == uuid.UUID(body["id"])
                )
            ).all()
        )
        == 1
    )

    aborted = _cancel(
        entry_client,
        body["id"],
        deviation_type="ABORTED_BY_COMPANY",
        noticed="2026-07-20T21:00:00",
        reason="Beam lost; the shift was aborted by the Company.",
        # The shift started at 19:00 and was cut short at 21:00. Actual times are
        # REQUIRED for a non-CANCELLED deviation, and rightly so here: the
        # entitlement is "the complete working time for the shift", so the report
        # must say what actually happened.
        actual=("2026-07-20T19:00:00", "2026-07-20T21:00:00"),
    )
    assert aborted.status_code == 200, aborted.text
    findings = {f["rule_id"]: f for f in aborted.json()["findings"]}
    assert findings["SHIFT-010"]["status"] == "BLOCKED_POLICY"
    # SHIFT-009 asks about lead time before a shift "was to be performed", which an
    # abortion after the start does not have.
    assert "SHIFT-009" not in findings

    # The partial work SURVIVES. Assert the CURRENT version, not the row count: the
    # deviation is a version write, so the confirmed interval is superseded and
    # retained as lineage — two rows, one current fact (INV-11).
    current = current_intervals(
        db_session,
        employee_id=_employee(db_session).id,
        linked_shift_assignment_id=uuid.UUID(body["id"]),
    )
    assert len(current) == 1, "an abortion must preserve the partial actual work"
    assert current[0].start_at == datetime.fromisoformat("2026-07-20T19:00:00+02:00")
    assert current[0].end_at == datetime.fromisoformat("2026-07-20T21:00:00+02:00")

    assignment = db_session.get(ShiftAssignment, uuid.UUID(body["id"]))
    assert assignment is not None
    assert assignment.status is ShiftAssignmentStatus.PLANNED


def test_an_abortion_report_is_submittable_and_marks_the_entitlement_for_review(
    entry_client: TestClient,
) -> None:
    body = _plan(entry_client, "2026-07-20T19:00:00", "2026-07-21T07:00:00")
    assert (
        _cancel(
            entry_client,
            body["id"],
            deviation_type="ABORTED_BY_COMPANY",
            actual=("2026-07-20T19:00:00", "2026-07-20T21:00:00"),
        ).status_code
        == 200
    )
    created = _submit(entry_client, body["id"])
    assert created.status_code == 200, created.text
    assert created.json()["payload"]["deviation_type"] == "ABORTED_BY_COMPANY"
    assert created.json()["payload"]["compensation_review_status"] == "MANUAL_REVIEW_REQUIRED"


def test_a_non_cancellation_report_is_not_marked_for_review(entry_client: TestClient) -> None:
    """``NOT_APPLICABLE`` for a report that is not about an unresolved entitlement."""
    body = _plan(entry_client, "2026-07-20T19:00:00", "2026-07-21T07:00:00")
    preview = entry_client.get(
        f"{SHIFTS}/{body['id']}/report-preview", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["compensation_review_status"] == "NOT_APPLICABLE"
    assert preview.json()["plan_source"] == "EMPLOYEE_RECORDED"


# ---------------------------------------------------------------------------
# WITHDRAWN is never an active plan
# ---------------------------------------------------------------------------


def _withdraw(client: TestClient, body: dict[str, Any]) -> Any:
    return client.post(
        f"{SHIFTS}/{body['id']}/withdraw",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"approved_revision_id": body["approved_revision"]["id"]},
    )


def test_confirm_shift_refuses_a_withdrawn_assignment(
    entry_client: TestClient, db_session: Session
) -> None:
    body = _plan(entry_client, "2026-07-20T19:00:00", "2026-07-21T07:00:00")
    assert _withdraw(entry_client, body).status_code == 200

    response = entry_client.post(
        f"{SHIFTS}/{body['id']}/confirm",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"approved_revision_id": body["approved_revision"]["id"]},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "CONFLICT_SHIFT_PLAN_CHANGED"
    assert (
        db_session.scalars(
            select(WorkInterval).where(
                WorkInterval.linked_shift_assignment_id == uuid.UUID(body["id"])
            )
        ).all()
        == []
    )


def test_report_preview_refuses_a_withdrawn_assignment(entry_client: TestClient) -> None:
    """This path had NO status check before, so a withdrawn plan still previewed."""
    body = _plan(entry_client, "2026-07-20T19:00:00", "2026-07-21T07:00:00")
    assert _withdraw(entry_client, body).status_code == 200
    response = entry_client.get(
        f"{SHIFTS}/{body['id']}/report-preview", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert response.status_code == 409, response.text


def test_save_deviation_refuses_a_withdrawn_assignment(
    entry_client: TestClient, db_session: Session
) -> None:
    body = _plan(entry_client, "2026-07-20T19:00:00", "2026-07-21T07:00:00")
    assert _withdraw(entry_client, body).status_code == 200
    response = _cancel(entry_client, body["id"])
    assert response.status_code == 409, response.text
    assert (
        db_session.scalars(
            select(ShiftDeviation).where(
                ShiftDeviation.shift_assignment_id == uuid.UUID(body["id"])
            )
        ).all()
        == []
    )


def test_a_withdrawn_assignment_cannot_be_previewed_or_submitted(
    entry_client: TestClient, db_session: Session
) -> None:
    """Two DIFFERENT codes, deliberately.

    The preview resolves the row through the owner-scoped lookup, so "not found"
    would be dishonest for a row the employee can plainly see in their own list —
    it is 409. Submission resolves a PERIOD through the disclosure path, whose
    predicate makes the period unresolvable — it is the nondisclosing 404.
    """
    body = _plan(entry_client, "2026-07-20T19:00:00", "2026-07-21T07:00:00")
    assert _withdraw(entry_client, body).status_code == 200

    preview = entry_client.get(
        f"{SHIFTS}/{body['id']}/report-preview", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert preview.status_code == 409, preview.text

    submitted = entry_client.post(
        "/api/v1/me/submissions/SHIFT_REPORT",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "scope": {"assignment_id": body["id"]},
            # Well-formed, so the request reaches the disclosure path instead of
            # being refused by request validation before the lookup runs.
            "preview_fingerprint": "0" * 64,
            "client_request_id": str(uuid.uuid4()),
            "recipients": ["recipient"],
        },
    )
    assert submitted.status_code == 404, submitted.text


def test_a_break_cannot_be_linked_to_a_withdrawn_assignment(entry_client: TestClient) -> None:
    body = _plan(entry_client, "2026-07-20T19:00:00", "2026-07-21T07:00:00")
    assert _withdraw(entry_client, body).status_code == 200
    response = entry_client.post(
        "/api/v1/me/break-intervals",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": "2026-07-20T22:00:00",
            "end_local": "2026-07-20T22:30:00",
            "kind": "PAID_SHIFT_BREAK",
            "linked_shift_assignment_id": body["id"],
            "client_request_id": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 422, response.text


def test_a_withdrawn_assignment_is_not_an_active_plan(
    entry_client: TestClient, db_session: Session
) -> None:
    """Visible as the employee's OWN history, never as a usable plan."""
    body = _plan(entry_client, "2026-07-20T19:00:00", "2026-07-21T07:00:00")
    assert _withdraw(entry_client, body).status_code == 200

    listed = entry_client.get(
        f"{SHIFTS}?from=2026-07-01&to=2026-09-01", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert listed.status_code == 200
    rows = {row["id"]: row for row in listed.json()}
    assert rows[body["id"]]["status"] == "WITHDRAWN"

    # ...and a deviation against it is refused.
    assert _cancel(entry_client, body["id"]).status_code == 409


def test_assignment_owned_remains_status_agnostic(
    entry_client: TestClient, db_session: Session
) -> None:
    """The shared primitive must NOT filter by status.

    ``confirm_shift`` turns a non-PLANNED status into its own 409; if the helper
    filtered the row out first, that endpoint's answer would silently become a 404
    for a row the employee can see. This pins the invariant directly.
    """
    from app.shift.service import _assignment_owned

    body = _plan(entry_client, "2026-07-20T19:00:00", "2026-07-21T07:00:00")
    assert _withdraw(entry_client, body).status_code == 200

    employee = _employee(db_session)
    row = _assignment_owned(db_session, employee.id, uuid.UUID(body["id"]))
    assert row.status is ShiftAssignmentStatus.WITHDRAWN

    # A foreign or unknown id stays a nondisclosing 404.
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        _assignment_owned(db_session, employee.id, uuid.uuid4())
    assert excinfo.value.status_code == 404


def test_a_cancellation_does_not_open_or_widen_any_hr_visibility(
    entry_client: TestClient,
) -> None:
    body = _plan(entry_client, "2026-07-20T19:00:00", "2026-07-21T07:00:00")
    response = entry_client.post(
        f"{SHIFTS}/{body['id']}/deviation",
        headers=HR_HEADER,
        json={"deviation_type": "CANCELLED"},
    )
    assert response.status_code == 403, response.text

    # And another employee cannot reach the plan at all.
    foreign = entry_client.post(
        f"{SHIFTS}/{body['id']}/deviation",
        headers=OTHER_EMPLOYEE_HEADER,
        json={"deviation_type": "CANCELLED"},
    )
    assert foreign.status_code == 404, foreign.text


def test_the_shift_report_payload_states_the_plan_source(
    entry_client: TestClient,
) -> None:
    """D2: provenance reaches the frozen package, not just the preview."""
    body = _plan(entry_client, "2026-07-20T19:00:00", "2026-07-21T07:00:00")
    created = _submit(entry_client, body["id"])
    assert created.status_code == 200, created.text
    assert created.json()["payload"]["plan_source"] == "EMPLOYEE_RECORDED"

    preview = entry_client.get(
        f"{SHIFTS}/{body['id']}/report-preview", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert preview.json()["plan_source"] == "EMPLOYEE_RECORDED"


def test_the_shift_entry_switch_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gated-feature posture: OFF unless a deployment opts in."""
    from app.config import Settings

    monkeypatch.delenv("CHRONOBEAM_ENABLE_SHIFT_PLAN_ENTRY", raising=False)
    monkeypatch.delenv("CHRONOBEAM_ENABLE_OCD_PLAN_ENTRY", raising=False)
    settings = Settings()
    assert settings.enable_shift_plan_entry is False
    assert settings.enable_ocd_plan_entry is False


def test_the_decision_role_list_never_offers_a_non_decision_role() -> None:
    """D22: "Local Contact" and "Works Council" are NOT cancellation deciders.

    Neither appears in ``POL-02`` as a role that may cancel a shift, so offering
    them under a "who decided" label would imply an authority the sample policy
    does not grant.
    """
    lowered = [role.lower() for role in DECISION_ROLES]
    assert not any("local contact" in role for role in lowered)
    assert not any("works council" in role for role in lowered)


def test_a_cancelled_plan_still_consumes_a_report_preview(
    entry_client: TestClient, db_session: Session
) -> None:
    """The cancellation freezes the PLAN, not the reporting path."""
    body = _plan(entry_client, "2026-07-20T19:00:00", "2026-07-21T07:00:00")
    assert _cancel(entry_client, body["id"]).status_code == 200

    preview = entry_client.get(
        f"{SHIFTS}/{body['id']}/report-preview", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert preview.status_code == 200, preview.text

    # But the plan itself can no longer be moved.
    assert (
        entry_client.patch(
            f"{SHIFTS}/{body['id']}",
            headers=DEVELOPER_PRINCIPAL_HEADER,
            json={
                "approved_revision_id": body["approved_revision"]["id"],
                "planned_start_local": "2026-07-20T20:00:00",
                "planned_end_local": "2026-07-21T08:00:00",
            },
        ).status_code
        == 409
    )


def test_the_abortion_deviation_type_is_a_source_named_value() -> None:
    """D13: the value exists because the source names the event."""
    assert ShiftDeviationType.ABORTED_BY_COMPANY.value == "ABORTED_BY_COMPANY"
    assert ShiftAssignmentStatus.WITHDRAWN.value == "WITHDRAWN"
    # The two status enums are separate, so a mixed comparison is a type error.
    from app.enums import ACTIVE_ASSIGNMENT_STATUSES, OCD_ACTIVE_STATUSES

    assert frozenset({ShiftAssignmentStatus.PLANNED}) == ACTIVE_ASSIGNMENT_STATUSES
    assert "SCHEDULED" in {status.value for status in OCD_ACTIVE_STATUSES}


def test_the_wall_time_chokepoint_is_shared_not_re_implemented() -> None:
    """One wall-time resolver, so every command inherits the same DST contract."""
    from app.shift import service as shift_service
    from app.worktime import service as worktime_service

    assert shift_service._resolve is worktime_service._resolve
