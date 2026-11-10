"""Purpose-specific package persistence and recipient boundary tests."""

from datetime import datetime
from typing import Any

from app.audit.models import AuditEvent
from app.disclosure.models import SubmissionPackage, SubmissionPackageStatus
from app.identity.models import Employee
from app.integrations.shift_plan import ShiftAssignmentSeed
from app.ocd_overtime.service import seed_ocd_assignment
from app.shift.service import seed_shift_assignment
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from .conftest import (
    DEVELOPER_PRINCIPAL_HEADER,
    HR_HEADER,
    OTHER_EMPLOYEE_HEADER,
    RECIPIENT_HEADER,
)


def _employee(session: Session) -> Employee:
    employee = session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    return employee


def _employee_b(session: Session) -> Employee:
    employee = session.scalar(select(Employee).where(Employee.idp_subject == "employee-b"))
    assert employee is not None
    return employee


def _shift(session: Session):
    return seed_shift_assignment(
        session,
        _employee(session),
        ShiftAssignmentSeed(
            plan_key="submission-shift",
            plan_name="Submission test shift",
            planned_start_at=datetime.fromisoformat("2026-07-31T19:00:00+02:00"),
            planned_end_at=datetime.fromisoformat("2026-08-01T07:00:00+02:00"),
            shift_kind="NIGHT",
            source="TEST_SEED",
            source_ref="submission-package-test",
        ),
    )


def _create_shift_package(
    client: TestClient, session: Session, request_id: str = "c15a5e0d-6156-426e-beca-1d4b94566711"
) -> dict[str, Any]:
    assignment = _shift(session)
    preview = client.get(
        f"/api/v1/me/shifts/{assignment.id}/report-preview", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert preview.status_code == 200, preview.text
    created = client.post(
        "/api/v1/me/submissions/SHIFT_REPORT",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "scope": {"assignment_id": str(assignment.id)},
            "preview_fingerprint": preview.json()["preview_fingerprint"],
            "client_request_id": request_id,
            "recipients": ["recipient"],
        },
    )
    assert created.status_code == 200, created.text
    return created.json()


def _ocd_assignment(session: Session):
    return seed_ocd_assignment(
        session,
        _employee(session),
        start_at=datetime.fromisoformat("2026-07-31T18:00:00+02:00"),
        end_at=datetime.fromisoformat("2026-08-01T08:00:00+02:00"),
        scheduled_at=datetime.fromisoformat("2026-06-30T18:00:00+02:00"),
    )


def _create_ocd_package(
    client: TestClient, session: Session, request_id: str = "7b1d4c62-9f6a-4c0e-9f2b-0d5a1e6c3b74"
) -> dict[str, Any]:
    """Mirror of ``_create_shift_package`` for the OCD_COMPENSATION purpose."""
    assignment = _ocd_assignment(session)
    call = client.post(
        "/api/v1/me/ocd-calls",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "ocd_assignment_id": str(assignment.id),
            "call_start_local": "2026-07-31T23:30:00",
            "call_end_local": "2026-08-01T00:30:00",
            "mode": "ON_SITE",
            "travel_out_minutes": 15,
            "travel_back_minutes": 15,
        },
    )
    assert call.status_code == 200, call.text
    election = client.post(
        "/api/v1/me/compensation-elections",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"context_type": "OCD", "context_id": str(assignment.id), "mode": "PAY"},
    )
    assert election.status_code == 200, election.text
    preview = client.get(
        f"/api/v1/me/ocd-assignments/{assignment.id}/compensation-preview",
        headers=DEVELOPER_PRINCIPAL_HEADER,
    )
    assert preview.status_code == 200, preview.text
    created = client.post(
        "/api/v1/me/submissions/OCD_COMPENSATION",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "scope": {"assignment_id": str(assignment.id)},
            "preview_fingerprint": preview.json()["preview_fingerprint"],
            "client_request_id": request_id,
            "recipients": ["recipient"],
        },
    )
    assert created.status_code == 200, created.text
    return {"assignment": assignment, "call": call.json(), "package": created.json()}


def test_ocd_compensation_package_exposes_only_the_ocd_projection(
    db_client: TestClient, db_session: Session
) -> None:
    """ACCESS-005 / docs/SECURITY.md: ``Submit OCD Compensation`` -> OCD projection only.

    The employee has unrelated ordinary ledger material in the SAME month as the
    OCD assignment. The frozen recipient payload must carry the OCD projection
    and nothing else: no month, no unrelated ordinary work interval.
    """
    ordinary = db_client.post(
        "/api/v1/me/work-intervals",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": "2026-07-15T08:00:00",
            "end_local": "2026-07-15T12:00:00",
            "work_type": "ON_SITE",
        },
    )
    assert ordinary.status_code == 201, ordinary.text

    created = _create_ocd_package(db_client, db_session)
    assignment, package = created["assignment"], created["package"]
    assert package["purpose"] == "OCD_COMPENSATION"
    # The scope is the ASSIGNMENT, never a month window (ACCESS-005).
    assert package["period_start"] == "2026-07-31"
    assert package["period_end"] == "2026-08-01"

    detail = db_client.get(
        f"/api/v1/recipient/submissions/{package['id']}", headers=RECIPIENT_HEADER
    )
    assert detail.status_code == 200, detail.text
    payload = detail.json()["payload"]

    # The OCD projection is present in full.
    assert payload["report_type"] == "OCD_COMPENSATION"
    assert payload["ocd_assignment_id"] == str(assignment.id)
    assert [item["call_id"] for item in payload["calls"]] == [created["call"]["id"]]
    assert payload["election"]["mode"] == "PAY"
    assert payload["duty"]["duty_minutes"] is not None

    # ...and the payload shape cannot carry a ledger: any added field fails here.
    # ``plan_source`` and ``reported_notice_at`` were added deliberately by the
    # employee plan-entry slice (D2/D17): the recipient is told WHO wrote the
    # availability and the notice instant the employee REPORTED. Both are facts
    # about the assignment itself — neither is a ledger field, and the
    # ``reported_notice_at`` value is read by no classifier, so it cannot become an
    # authoritative short-notice determination.
    assert set(payload) == {
        "report_type",
        "policy_version",
        "policy_status",
        "employee_identifier",
        "ocd_assignment_id",
        "assignment_start_at",
        "assignment_end_at",
        "calls",
        "classified_components",
        "duty",
        "election",
        "plan_source",
        "reported_notice_at",
    }

    # Negative control: the ordinary interval DOES exist in the employee's own
    # private ledger — it is excluded from the package, not absent from the data.
    own_day = db_client.get(
        "/api/v1/me/months/2026-07/days/2026-07-15", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert own_day.status_code == 200, own_day.text
    assert ordinary.json()["id"] in own_day.text
    assert ordinary.json()["id"] not in detail.text
    assert "2026-07-15" not in detail.text
    assert "work_intervals" not in payload
    assert "overtime_intervals" not in payload
    assert "findings" not in detail.text


def test_shift_package_freezes_exact_preview_and_replays_idempotently(
    db_client: TestClient, db_session: Session
) -> None:
    created = _create_shift_package(db_client, db_session)
    assert created["purpose"] == "SHIFT_REPORT"
    assert created["period_start"] == "2026-07-31"
    assert created["period_end"] == "2026-08-01"
    assert created["version"] == 1
    assert "findings" not in created["payload"]
    assert "preview_fingerprint" not in created["payload"]
    assert created["recipients"] == [
        {"recipient_principal": "recipient", "recipient_type": "RECIPIENT", "read_at": None}
    ]

    replay = db_client.post(
        "/api/v1/me/submissions/SHIFT_REPORT",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "scope": {"assignment_id": created["payload"]["shift_assignment_id"]},
            "preview_fingerprint": "0" * 64,
            "client_request_id": "c15a5e0d-6156-426e-beca-1d4b94566711",
            "recipients": [],
        },
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == created["id"]
    audit = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "SUBMISSION_PACKAGE_CREATE")
    ).all()
    assert len(audit) == 1
    assert audit[0].metadata_json == {"package_purpose": "SHIFT_REPORT", "package_version": 1}


def test_submission_rejects_changed_preview_and_invalid_scope_or_recipient(
    db_client: TestClient, db_session: Session
) -> None:
    assignment = _shift(db_session)
    invalid_scope = db_client.post(
        "/api/v1/me/submissions/SHIFT_REPORT",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "scope": {"month": "2026-07"},
            "preview_fingerprint": "0" * 64,
            "client_request_id": "313c2690-127c-446c-bb0d-2b6eed45d72c",
            "recipients": ["recipient"],
        },
    )
    assert invalid_scope.status_code == 422
    assert invalid_scope.json()["detail"]["code"] == "VALIDATION_FAILED"

    preview = db_client.get(
        f"/api/v1/me/shifts/{assignment.id}/report-preview", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    changed = db_client.post(
        "/api/v1/me/submissions/SHIFT_REPORT",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "scope": {"assignment_id": str(assignment.id)},
            "preview_fingerprint": "0" * 64,
            "client_request_id": "862e4e09-9c12-4ce8-ab62-fc12460d92bc",
            "recipients": ["recipient"],
        },
    )
    assert preview.status_code == 200
    assert changed.status_code == 409
    assert changed.json()["detail"] == {
        "code": "SUBMISSION_CHANGED",
        "message": "Facts changed since preview; re-preview before submitting.",
        "details": {"re_preview": True},
    }
    missing_recipient = db_client.post(
        "/api/v1/me/submissions/SHIFT_REPORT",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "scope": {"assignment_id": str(assignment.id)},
            "preview_fingerprint": preview.json()["preview_fingerprint"],
            "client_request_id": "70b8ebfd-b63c-4538-b02f-d902ae2b2f42",
            "recipients": [],
        },
    )
    assert missing_recipient.status_code == 422
    assert missing_recipient.json()["detail"]["code"] == "SUBMISSION_MISSING_REQUIRED_DATA"


def test_submission_client_request_id_collision_is_rejected_without_package_leak(
    db_client: TestClient, db_session: Session
) -> None:
    request_id = "c15a5e0d-6156-426e-beca-1d4b94566711"
    first = _create_shift_package(db_client, db_session, request_id)
    assignment = seed_shift_assignment(
        db_session,
        _employee_b(db_session),
        ShiftAssignmentSeed(
            plan_key="submission-shift-employee-b",
            plan_name="Submission test shift B",
            planned_start_at=datetime.fromisoformat("2026-07-31T19:00:00+02:00"),
            planned_end_at=datetime.fromisoformat("2026-08-01T07:00:00+02:00"),
            shift_kind="NIGHT",
            source="TEST_SEED",
            source_ref="submission-package-test-b",
        ),
    )
    preview = db_client.get(
        f"/api/v1/me/shifts/{assignment.id}/report-preview", headers=OTHER_EMPLOYEE_HEADER
    )
    assert preview.status_code == 200, preview.text

    collision = db_client.post(
        "/api/v1/me/submissions/SHIFT_REPORT",
        headers=OTHER_EMPLOYEE_HEADER,
        json={
            "scope": {"assignment_id": str(assignment.id)},
            "preview_fingerprint": preview.json()["preview_fingerprint"],
            "client_request_id": request_id,
            "recipients": ["recipient"],
        },
    )
    assert collision.status_code == 422
    assert collision.json()["detail"] == {
        "code": "VALIDATION_FAILED",
        "message": "This change was already recorded — reload the page to see it.",
    }
    assert first["id"] not in collision.text


def test_resubmission_supersedes_submitted_package_and_withdrawal_closes_read_path(
    db_client: TestClient, db_session: Session
) -> None:
    first = _create_shift_package(db_client, db_session)
    assignment_id = first["payload"]["shift_assignment_id"]
    preview = db_client.get(
        f"/api/v1/me/shifts/{assignment_id}/report-preview", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    second = db_client.post(
        "/api/v1/me/submissions/SHIFT_REPORT",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "scope": {"assignment_id": assignment_id},
            "preview_fingerprint": preview.json()["preview_fingerprint"],
            "client_request_id": "5c190b83-11ea-43a7-a3f5-0a04056104b1",
            "recipients": ["recipient"],
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["version"] == 2
    assert second.json()["supersedes_id"] == first["id"]

    listed = db_client.get("/api/v1/me/submissions", headers=DEVELOPER_PRINCIPAL_HEADER)
    assert listed.status_code == 200
    assert {item["status"] for item in listed.json()} == {"SUPERSEDED", "SUBMITTED"}
    withdrawn = db_client.post(
        f"/api/v1/me/submissions/{second.json()['id']}/withdraw",
        headers=DEVELOPER_PRINCIPAL_HEADER,
    )
    assert withdrawn.status_code == 200
    assert withdrawn.json()["status"] == "WITHDRAWN"
    assert (
        db_client.get(
            f"/api/v1/recipient/submissions/{second.json()['id']}", headers=RECIPIENT_HEADER
        ).status_code
        == 404
    )
    row = db_session.scalar(
        select(AuditEvent).where(AuditEvent.action == "SUBMISSION_PACKAGE_WITHDRAW")
    )
    assert row is not None
    assert row.metadata_json == {"package_purpose": "SHIFT_REPORT", "package_version": 2}


def test_recipient_queue_is_assigned_metadata_only_and_detail_audits_read(
    db_client: TestClient, db_session: Session
) -> None:
    created = _create_shift_package(db_client, db_session)
    queue = db_client.get("/api/v1/recipient/submissions", headers=RECIPIENT_HEADER)
    assert queue.status_code == 200, queue.text
    assert queue.json()[0]["id"] == created["id"]
    assert "payload" not in queue.json()[0]
    assert "legal_name" not in queue.text
    detail = db_client.get(
        f"/api/v1/recipient/submissions/{created['id']}", headers=RECIPIENT_HEADER
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["payload"] == created["payload"]
    assert "recipients" not in detail.json()
    assert "findings" not in detail.text
    read = db_session.scalar(select(AuditEvent).where(AuditEvent.action == "SUBMISSION_READ"))
    assert read is not None
    assert read.metadata_json is not None
    assert read.metadata_json["package_version"] == 1

    foreign = db_client.get(f"/api/v1/recipient/submissions/{created['id']}", headers=HR_HEADER)
    unknown = db_client.get(
        "/api/v1/recipient/submissions/00000000-0000-0000-0000-000000000000",
        headers=RECIPIENT_HEADER,
    )
    assert foreign.status_code == 403
    assert unknown.status_code == 404
    other_queue = db_client.get("/api/v1/recipient/submissions", headers=OTHER_EMPLOYEE_HEADER)
    assert other_queue.status_code == 403


def test_withdraw_foreign_and_unknown_are_byte_identical(
    db_client: TestClient, db_session: Session
) -> None:
    created = _create_shift_package(db_client, db_session)
    foreign = db_client.post(
        f"/api/v1/me/submissions/{created['id']}/withdraw", headers=OTHER_EMPLOYEE_HEADER
    )
    unknown = db_client.post(
        "/api/v1/me/submissions/00000000-0000-0000-0000-000000000000/withdraw",
        headers=DEVELOPER_PRINCIPAL_HEADER,
    )
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.content == unknown.content
    package = db_session.get(SubmissionPackage, created["id"])
    assert package is not None
    assert package.status == SubmissionPackageStatus.SUBMITTED
