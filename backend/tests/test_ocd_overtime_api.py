"""Transaction/privacy integration coverage."""

import uuid
from datetime import datetime
from typing import Any

import pytest
from app.enums import WorkType
from app.identity.models import Employee
from app.ocd_overtime.models import OcdCall, OcdCallStatus
from app.ocd_overtime.service import link_overtime_interval, seed_ocd_assignment
from app.worktime.models import WorkInterval
from app.worktime.service import create_interval_instant
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from .conftest import (
    DEVELOPER_PRINCIPAL_HEADER,
    HR_HEADER,
    OTHER_EMPLOYEE_HEADER,
    RECIPIENT_HEADER,
    SUPERVISOR_HEADER,
    TECHNICAL_ADMIN_HEADER,
    decide_authorization,
)


def employee(session: Session) -> Employee:
    row = session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert row is not None
    return row


def assignment(session: Session):
    return seed_ocd_assignment(
        session,
        employee(session),
        start_at=datetime.fromisoformat("2026-07-31T18:00:00+02:00"),
        end_at=datetime.fromisoformat("2026-08-01T08:00:00+02:00"),
        scheduled_at=datetime.fromisoformat("2026-06-30T18:00:00+02:00"),
    )


def test_call_create_replay_correction_void_and_ownership(
    db_client: TestClient, db_session: Session
) -> None:
    seeded = assignment(db_session)
    body: dict[str, Any] = {
        "ocd_assignment_id": str(seeded.id),
        "call_start_local": "2026-07-31T23:30:00",
        "call_end_local": "2026-08-01T00:30:00",
        "mode": "ON_SITE",
        "travel_out_minutes": 15,
        "travel_back_minutes": 15,
        "client_request_id": "d3f4f39b-48c9-4ecb-9efe-a4443384a802",
    }
    created = db_client.post("/api/v1/me/ocd-calls", headers=DEVELOPER_PRINCIPAL_HEADER, json=body)
    assert created.status_code == 200, created.text
    replay = db_client.post("/api/v1/me/ocd-calls", headers=DEVELOPER_PRINCIPAL_HEADER, json=body)
    assert replay.json()["id"] == created.json()["id"]
    interval = db_session.scalar(
        select(WorkInterval).where(WorkInterval.linked_ocd_call_id == created.json()["id"])
    )
    assert interval is not None and interval.start_at == datetime.fromisoformat(
        "2026-07-31T21:15:00+00:00"
    )
    patched = db_client.patch(
        f"/api/v1/me/ocd-calls/{created.json()['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 1, "comment": "corrected"},
    )
    assert patched.status_code == 200
    assert (
        db_client.get(
            f"/api/v1/me/ocd-assignments/{seeded.id}/compensation-preview",
            headers=OTHER_EMPLOYEE_HEADER,
        ).status_code
        == 404
    )
    voided = db_client.post(
        f"/api/v1/me/ocd-calls/{patched.json()['id']}/void",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 2},
    )
    assert voided.status_code == 200 and voided.json()["status"] == "VOIDED"
    assert (
        db_session.scalar(select(OcdCall).where(OcdCall.status == OcdCallStatus.VOIDED)) is not None
    )
    assert (
        db_client.get("/api/v1/me/ocd?from=2026-07-01&to=2026-08-02", headers=HR_HEADER).status_code
        == 403
    )


def test_remote_travel_and_ambiguous_time_are_rejected(
    db_client: TestClient, db_session: Session
) -> None:
    seeded = assignment(db_session)
    for body in (
        {
            "ocd_assignment_id": str(seeded.id),
            "call_start_local": "2026-07-31T22:00:00",
            "call_end_local": "2026-07-31T23:00:00",
            "mode": "REMOTE",
            "travel_out_minutes": 1,
        },
        {
            "ocd_assignment_id": str(seeded.id),
            "call_start_local": "2026-10-25T02:30:00",
            "call_end_local": "2026-10-25T03:30:00",
            "mode": "REMOTE",
        },
    ):
        response = db_client.post(
            "/api/v1/me/ocd-calls", headers=DEVELOPER_PRINCIPAL_HEADER, json=body
        )
        assert response.status_code == 422


def test_overtime_authorization_link_is_persisted(
    db_client: TestClient, db_session: Session
) -> None:
    created = db_client.post(
        "/api/v1/me/overtime-authorizations",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"valid_from": "2026-07-01T00:00", "purpose": "captured reference"},
    )
    assert created.status_code == 200
    interval, _ = create_interval_instant(
        db_session,
        employee(db_session),
        start_at=datetime.fromisoformat("2026-07-10T18:00:00+02:00"),
        end_at=datetime.fromisoformat("2026-07-10T19:00:00+02:00"),
        work_type=WorkType.OVERTIME_PAID,
        comment=None,
    )
    link_overtime_interval(db_session, employee(db_session), interval.id, created.json()["id"])
    preview = db_client.get(
        "/api/v1/me/overtime/preview?from=2026-07-01&to=2026-07-31",
        headers=DEVELOPER_PRINCIPAL_HEADER,
    )
    assert preview.status_code == 200
    assert (
        preview.json()["overtime_intervals"][0]["linked_authorization_id"] == created.json()["id"]
    )


def record_call(db_client: TestClient, db_session: Session, **overrides: Any) -> dict[str, Any]:
    """Full on-site call covering 23:30->00:30 with 15+15 travel; returns the JSON."""
    seeded = assignment(db_session)
    body: dict[str, Any] = {
        "ocd_assignment_id": str(seeded.id),
        "call_start_local": "2026-07-31T23:30:00",
        "call_end_local": "2026-08-01T00:30:00",
        "mode": "ON_SITE",
        "travel_out_minutes": 15,
        "travel_back_minutes": 15,
        **overrides,
    }
    response = db_client.post("/api/v1/me/ocd-calls", headers=DEVELOPER_PRINCIPAL_HEADER, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_OCD_T01_assignment_without_call_creates_no_work_fact(
    db_client: TestClient, db_session: Session
) -> None:
    seeded = assignment(db_session)
    listing = db_client.get(
        "/api/v1/me/ocd?from=2026-07-01&to=2026-08-02", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert listing.status_code == 200
    card = next(item for item in listing.json() if item["id"] == str(seeded.id))
    assert card["calls"] == []
    interval_rows = db_session.scalars(
        select(WorkInterval).where(WorkInterval.employee_id == employee(db_session).id)
    ).all()
    assert interval_rows == []


def test_ocd_call_response_carries_span_and_linked_interval(
    db_client: TestClient, db_session: Session
) -> None:
    created = record_call(db_client, db_session)
    assert created["linked_interval_id"] is not None
    # 23:30 call minus 15 min travel -> 23:15 Berlin; end 00:30 + 15 min -> 00:45.
    assert created["span_start_at"] == "2026-07-31T23:15:00+02:00"
    assert created["span_end_at"] == "2026-08-01T00:45:00+02:00"
    assert created["elapsed_seconds"] == 90 * 60


def test_OCD_006_travel_is_not_invented_when_none_is_declared(
    db_client: TestClient, db_session: Session
) -> None:
    """Negative control for OCD-006 / ``test_ocd_call_response_carries_span_and_linked_interval``.

    With declared travel the effective call span grows by it (23:30->00:30 becomes
    23:15->00:45); with none declared the span is the call itself. So the travel
    component is driven by the recorded fact, not by the ON_SITE work type.
    """
    created = record_call(db_client, db_session, travel_out_minutes=0, travel_back_minutes=0)
    assert created["span_start_at"] == "2026-07-31T23:30:00+02:00"
    assert created["span_end_at"] == "2026-08-01T00:30:00+02:00"
    assert created["elapsed_seconds"] == 60 * 60


def test_direct_interval_mutation_of_ocd_linked_fact_is_rejected(
    db_client: TestClient, db_session: Session
) -> None:
    created = record_call(db_client, db_session)
    interval_id = created["linked_interval_id"]
    assert interval_id is not None
    patch = db_client.patch(
        f"/api/v1/me/work-intervals/{interval_id}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 1, "comment": "not through the call command"},
    )
    assert patch.status_code == 422
    split = db_client.post(
        f"/api/v1/me/work-intervals/{interval_id}/split",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 1, "split_local": "2026-07-31T23:00:00"},
    )
    assert split.status_code == 422
    deleted = db_client.delete(
        f"/api/v1/me/work-intervals/{interval_id}?version=1",
        headers=DEVELOPER_PRINCIPAL_HEADER,
    )
    assert deleted.status_code == 422
    # The call and its fact are still intact afterwards.
    assert db_session.scalar(select(OcdCall).where(OcdCall.id == created["id"])) is not None


def test_void_call_cannot_delete_a_disclosed_linked_fact(
    db_client: TestClient, db_session: Session
) -> None:
    created = record_call(db_client, db_session)
    request = db_client.post(
        "/api/v1/hr/access-requests",
        headers=HR_HEADER,
        json={
            "employee_external_id": "emp-0001",
            "purpose": "RANDOM_CHECK",
            "month": "2026-08",
            "view_type": "work-time-record",
        },
    )
    assert request.status_code == 200, request.text
    granted = db_client.post(
        f"/api/v1/me/access-requests/{request.json()['id']}/grant",
        headers=DEVELOPER_PRINCIPAL_HEADER,
    )
    assert granted.status_code == 200, granted.text

    voided = db_client.post(
        f"/api/v1/me/ocd-calls/{created['id']}/void",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 1, "reason": "entered in error"},
    )
    assert voided.status_code == 409
    assert voided.json()["detail"]["code"] == "CONFLICT_DISCLOSED_FACT"
    assert db_session.get(WorkInterval, created["linked_interval_id"]) is not None


def test_elections_are_versioned_context_scoped_and_validated(
    db_client: TestClient, db_session: Session
) -> None:
    seeded = assignment(db_session)
    other = db_client.post(
        "/api/v1/me/overtime-authorizations",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"valid_from": "2026-07-01T00:00"},
    ).json()
    first = db_client.post(
        "/api/v1/me/compensation-elections",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"context_type": "OCD", "context_id": str(seeded.id), "mode": "PAY"},
    )
    assert first.status_code == 200 and first.json()["version"] == 1
    second = db_client.post(
        "/api/v1/me/compensation-elections",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"context_type": "OCD", "context_id": str(seeded.id), "mode": "TIME"},
    )
    assert second.status_code == 200 and second.json()["version"] == 2
    current = db_client.get(
        f"/api/v1/me/compensation-elections?context_type=OCD&context_id={seeded.id}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
    )
    assert current.json()["mode"] == "TIME" and current.json()["version"] == 2
    # Independent contexts keep independent elections.
    overtime = db_client.post(
        "/api/v1/me/compensation-elections",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"context_type": "OVERTIME", "context_id": other["id"], "mode": "PAY"},
    )
    assert overtime.status_code == 200 and overtime.json()["version"] == 1
    # Unknown context ids and type mismatches are rejected. A fresh UUID is
    # guaranteed distinct (replacing a hex character only works when the
    # random uuid happens to contain that letter — flaky otherwise).
    unknown = db_client.post(
        "/api/v1/me/compensation-elections",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "context_type": "OCD",
            "context_id": str(uuid.uuid4()),
            "mode": "PAY",
        },
    )
    assert unknown.status_code == 422
    mismatch = db_client.post(
        "/api/v1/me/compensation-elections",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"context_type": "OVERTIME", "context_id": str(seeded.id), "mode": "PAY"},
    )
    assert mismatch.status_code == 422


def test_overtime_link_via_patch_validates_ownership_and_type(
    db_client: TestClient, db_session: Session
) -> None:
    authorization = db_client.post(
        "/api/v1/me/overtime-authorizations",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"valid_from": "2026-07-01T00:00"},
    ).json()
    interval, _ = create_interval_instant(
        db_session,
        employee(db_session),
        start_at=datetime.fromisoformat("2026-07-10T18:00:00+02:00"),
        end_at=datetime.fromisoformat("2026-07-10T19:00:00+02:00"),
        work_type=WorkType.OVERTIME_PAID,
        comment=None,
    )
    linked = db_client.patch(
        f"/api/v1/me/work-intervals/{interval.id}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 1, "overtime_authorization_id": authorization["id"]},
    )
    assert linked.status_code == 200 and linked.json()["version"] == 2
    # Corrections return the NEW head id; the next write must target it.
    head_id = linked.json()["id"]
    # Cross-employee authorization reference is rejected (no existence leak).
    other_auth = db_client.post(
        "/api/v1/me/overtime-authorizations",
        headers=OTHER_EMPLOYEE_HEADER,
        json={"valid_from": "2026-07-01T00:00"},
    ).json()
    forged = db_client.patch(
        f"/api/v1/me/work-intervals/{head_id}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 2, "overtime_authorization_id": other_auth["id"]},
    )
    assert forged.status_code == 422
    # A link on a non-overtime interval is rejected.
    ordinary, _ = create_interval_instant(
        db_session,
        employee(db_session),
        start_at=datetime.fromisoformat("2026-07-11T08:00:00+02:00"),
        end_at=datetime.fromisoformat("2026-07-11T09:00:00+02:00"),
        work_type=WorkType.ON_SITE,
        comment=None,
    )
    wrong_type = db_client.patch(
        f"/api/v1/me/work-intervals/{ordinary.id}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 1, "overtime_authorization_id": authorization["id"]},
    )
    assert wrong_type.status_code == 422
    # Clearing the link is an explicit versioned correction (targets the head).
    cleared = db_client.patch(
        f"/api/v1/me/work-intervals/{head_id}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 2, "overtime_authorization_id": None},
    )
    assert cleared.status_code == 200 and cleared.json()["version"] == 3


def test_previews_are_minimized_and_never_leak_pay_group_data(
    db_client: TestClient, db_session: Session
) -> None:
    record_call(db_client, db_session)
    authorization = db_client.post(
        "/api/v1/me/overtime-authorizations",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"valid_from": "2026-07-01T00:00"},
    ).json()
    # The approval is an INSTITUTIONAL decision (ACCESS-017): the employee route
    # cannot produce one, so the test reaches the authoritative state through the
    # same writer the operator CLI uses. The decision supersedes the request, so
    # the link must name the CURRENT chain head.
    approved = decide_authorization(
        db_session, employee(db_session), uuid.UUID(authorization["id"]), authorized_by="woco"
    )
    interval, _ = create_interval_instant(
        db_session,
        employee(db_session),
        start_at=datetime.fromisoformat("2026-07-13T22:00:00+02:00"),
        end_at=datetime.fromisoformat("2026-07-13T23:00:00+02:00"),
        work_type=WorkType.OVERTIME_PAID,
        comment=None,
    )
    linked = db_client.patch(
        f"/api/v1/me/work-intervals/{interval.id}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 1, "overtime_authorization_id": str(approved.id)},
    )
    assert linked.status_code == 200
    linked_head_id = linked.json()["id"]

    ocd_preview = db_client.get(
        f"/api/v1/me/ocd-assignments/{assignment(db_session).id}/compensation-preview",
        headers=DEVELOPER_PRINCIPAL_HEADER,
    )
    assert ocd_preview.status_code == 200
    ocd_text = ocd_preview.text
    for forbidden in ("LE_9B", "GE_9C", "pay_group", "UNKNOWN"):
        assert forbidden not in ocd_text
    # The call's classified span includes the required travel (OCD-006).
    ocd_call = ocd_preview.json()["calls"][0]
    assert ocd_call["travel_out_minutes"] == 15 and ocd_call["travel_back_minutes"] == 15

    overtime_preview = db_client.get(
        "/api/v1/me/overtime/preview?from=2026-07-01&to=2026-07-31",
        headers=DEVELOPER_PRINCIPAL_HEADER,
    )
    assert overtime_preview.status_code == 200
    overtime_text = overtime_preview.text
    supplement = next(
        item
        for item in overtime_preview.json()["classified_components"]
        if item["component"] == "OVERTIME_SUPPLEMENT"
    )
    # The bucket-derived time equivalent NEVER leaves the private domain.
    assert supplement["time_equivalent_minutes"] is None
    assert "LE_9B" not in overtime_text and "GE_9C" not in overtime_text
    assert "pay_group" not in overtime_text
    # Ordinary intervals are not part of the overtime projection.
    assert len(overtime_preview.json()["overtime_intervals"]) == 1
    projection = overtime_preview.json()["overtime_intervals"][0]
    assert projection["interval_id"] == linked_head_id
    assert projection["version"] == 2  # versioned link (current head)
    # Package projections freeze only the election identity/version/mode;
    # selection timestamps and private blocking hints are not recipient data.
    assert set(ocd_preview.json()["election"]) == {"id", "mode", "version"}
    assert set(overtime_preview.json()["election"]) == {"id", "mode", "version"}


def test_unlinked_overtime_is_blocked_for_submission(
    db_client: TestClient, db_session: Session
) -> None:
    create_interval_instant(
        db_session,
        employee(db_session),
        start_at=datetime.fromisoformat("2026-07-13T22:00:00+02:00"),
        end_at=datetime.fromisoformat("2026-07-13T23:00:00+02:00"),
        work_type=WorkType.OVERTIME_TIME_COMP,
        comment=None,
    )
    preview = db_client.get(
        "/api/v1/me/overtime/preview?from=2026-07-01&to=2026-07-31",
        headers=DEVELOPER_PRINCIPAL_HEADER,
    )
    findings = preview.json()["findings"]
    assert any(
        item["rule_id"] == "ACCESS-006" and item["status"] == "BLOCKED_POLICY" for item in findings
    )
    assert preview.json()["election"]["mode"] is None


def test_nonexistent_dst_wall_time_is_rejected_for_calls(
    db_client: TestClient, db_session: Session
) -> None:
    # 2026-03-29 02:30 does not exist in Europe/Berlin (spring forward).
    response = db_client.post(
        "/api/v1/me/ocd-calls",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "ocd_assignment_id": str(assignment(db_session).id),
            "call_start_local": "2026-03-29T02:30:00",
            "call_end_local": "2026-03-29T03:30:00",
            "mode": "REMOTE",
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VALIDATION_DST_AMBIGUOUS"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (HR_HEADER, 403),
        (SUPERVISOR_HEADER, 403),
        (RECIPIENT_HEADER, 403),
        (TECHNICAL_ADMIN_HEADER, 403),
    ],
)
def test_special_work_routes_deny_non_employee_principals(
    db_client: TestClient, db_session: Session, header: dict[str, str], expected: int
) -> None:
    for path in (
        "/api/v1/me/ocd?from=2026-07-01&to=2026-08-01",
        "/api/v1/me/overtime/preview?from=2026-07-01&to=2026-07-31",
    ):
        response = db_client.get(path, headers=header)
        assert response.status_code == expected
        if expected == 403:
            assert response.json()["detail"] == {
                "code": "AUTH_FORBIDDEN",
                "message": "Access to this resource is restricted.",
            }


def test_foreign_assignment_and_call_ids_are_byte_identical_404(
    db_client: TestClient, db_session: Session
) -> None:
    seeded = assignment(db_session)
    owner_view = db_client.get(
        f"/api/v1/me/ocd-assignments/{seeded.id}/compensation-preview",
        headers=OTHER_EMPLOYEE_HEADER,
    )
    random_uuid = "00000000-0000-0000-0000-000000000000"
    unknown_view = db_client.get(
        f"/api/v1/me/ocd-assignments/{random_uuid}/compensation-preview",
        headers=DEVELOPER_PRINCIPAL_HEADER,
    )
    assert owner_view.status_code == 404 and unknown_view.status_code == 404
    assert owner_view.text == unknown_view.text
