"""Travel /me routes exercised before integration-owner router registration."""

import uuid
from typing import cast

from app.enums import WorkType
from app.travel.models import TravelSegment
from app.worktime.models import Correction, WorkInterval
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from .conftest import DEVELOPER_PRINCIPAL_HEADER, OTHER_EMPLOYEE_HEADER


def install_router(client: TestClient) -> None:
    # The travel router is registered app-wide in main.py.
    # This asserts the real registration instead of double-including it.
    paths = cast(FastAPI, client.app).openapi()["paths"]
    assert "/api/v1/me/travel-trips" in paths


def trip_body(**overrides: object) -> dict[str, object]:
    return {
        "start_local": "2026-05-01T06:00:00",
        "end_local": "2026-06-02T22:00:00",
        "start_origin": "HOME",
        **overrides,
    }


def make_trip(client: TestClient, **overrides: object) -> dict[str, object]:
    response = client.post(
        "/api/v1/me/travel-trips", headers=DEVELOPER_PRINCIPAL_HEADER, json=trip_body(**overrides)
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_trip_replay_source_lineage_and_envelope(
    db_client: TestClient, db_session: Session
) -> None:
    install_router(db_client)
    request_id = str(uuid.uuid4())
    trip = make_trip(db_client, client_request_id=request_id)
    replay = db_client.post(
        "/api/v1/me/travel-trips",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json=trip_body(client_request_id=request_id),
    )
    assert replay.status_code == 200 and replay.json()["id"] == trip["id"]
    assert trip["source"] == "EMPLOYEE_DECLARED"
    rejected = db_client.post(
        f"/api/v1/me/travel-trips/{trip['id']}/segments",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": "2026-04-30T23:00:00",
            "end_local": "2026-05-01T07:00:00",
            "classification": "PASSIVE_TRAVEL",
        },
    )
    assert rejected.status_code == 422
    patched = db_client.patch(
        f"/api/v1/me/travel-trips/{trip['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 1, "reason": "correct label", "reference_label": "Berlin visit"},
    )
    assert patched.status_code == 200 and patched.json()["version"] == 2
    assert db_session.scalar(select(Correction).where(Correction.entity_type == "BUSINESS_TRIP"))
    stale = db_client.patch(
        f"/api/v1/me/travel-trips/{patched.json()['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 1, "reason": "stale"},
    )
    assert stale.status_code == 409


def test_passive_overlap_and_active_pair_correction_delete(
    db_client: TestClient, db_session: Session
) -> None:
    install_router(db_client)
    trip = make_trip(db_client)
    passive = db_client.post(
        f"/api/v1/me/travel-trips/{trip['id']}/segments",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": "2026-05-02T08:00:00",
            "end_local": "2026-05-02T10:00:00",
            "classification": "PASSIVE_TRAVEL",
        },
    )
    assert passive.status_code == 201
    overlap = db_client.post(
        f"/api/v1/me/travel-trips/{trip['id']}/segments",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": "2026-05-02T09:00:00",
            "end_local": "2026-05-02T11:00:00",
            "classification": "PASSIVE_TRAVEL",
        },
    )
    assert overlap.status_code == 422
    assert overlap.json()["detail"]["code"] == "VALIDATION_TRAVEL_OVERLAP"
    active = db_client.post(
        f"/api/v1/me/travel-trips/{trip['id']}/segments",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": "2026-05-03T08:00:00",
            "end_local": "2026-05-03T10:00:00",
            "classification": "DRIVING",
        },
    )
    assert active.status_code == 201, active.text
    interval = db_session.get(WorkInterval, active.json()["work_interval_id"])
    assert interval is not None
    assert interval.work_type == WorkType.BUSINESS_TRIP_WORK
    assert interval.comment is None
    segment_row = db_session.get(TravelSegment, active.json()["id"])
    assert segment_row is not None
    assert interval.start_at == segment_row.start_at
    corrected = db_client.patch(
        f"/api/v1/me/travel-segments/{active.json()['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 1, "reason": "later arrival", "end_local": "2026-05-03T11:00:00"},
    )
    assert corrected.status_code == 200, corrected.text
    deleted = db_client.request(
        "DELETE",
        f"/api/v1/me/travel-segments/{corrected.json()['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 2, "reason": "entered in error"},
    )
    assert deleted.status_code == 204
    assert db_session.get(WorkInterval, corrected.json()["work_interval_id"]) is None


def test_segment_replay_returns_current_head_after_correction(
    db_client: TestClient, db_session: Session
) -> None:
    """A replayed segment create resolves the supersede chain to its head (F5)."""
    install_router(db_client)
    trip = make_trip(db_client)
    request_id = str(uuid.uuid4())
    created = db_client.post(
        f"/api/v1/me/travel-trips/{trip['id']}/segments",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": "2026-05-04T08:00:00",
            "end_local": "2026-05-04T10:00:00",
            "classification": "PASSIVE_TRAVEL",
            "client_request_id": request_id,
        },
    )
    assert created.status_code == 201
    corrected = db_client.patch(
        f"/api/v1/me/travel-segments/{created.json()['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 1, "reason": "later departure", "start_local": "2026-05-04T09:00:00"},
    )
    assert corrected.status_code == 200 and corrected.json()["version"] == 2
    replay = db_client.post(
        f"/api/v1/me/travel-trips/{trip['id']}/segments",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": "2026-05-04T08:00:00",
            "end_local": "2026-05-04T10:00:00",
            "classification": "PASSIVE_TRAVEL",
            "client_request_id": request_id,
        },
    )
    # The replay returns the corrected head (version 2, 09:00 start), never
    # the superseded version-1 row the original create returned.
    assert replay.status_code == 200
    assert replay.json()["id"] == corrected.json()["id"]
    assert replay.json()["version"] == 2
    assert replay.json()["start_local"] == "2026-05-04T09:00:00"


def test_idor_trip_delete_guard_and_summary_selection(
    db_client: TestClient, db_session: Session
) -> None:
    install_router(db_client)
    trip = make_trip(db_client, start_local="2026-05-31T00:00:00", end_local="2026-07-02T00:00:00")
    segment = db_client.post(
        f"/api/v1/me/travel-trips/{trip['id']}/segments",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": "2026-05-31T00:00:00",
            "end_local": "2026-05-31T16:00:00",
            "classification": "PASSIVE_TRAVEL",
        },
    )
    assert segment.status_code == 201
    for method, path, body in (
        ("patch", f"/api/v1/me/travel-trips/{trip['id']}", {"version": 1, "reason": "x"}),
        ("delete", f"/api/v1/me/travel-trips/{trip['id']}", {"version": 1}),
        (
            "post",
            f"/api/v1/me/travel-trips/{trip['id']}/segments",
            {
                "start_local": "2026-06-01T00:00:00",
                "end_local": "2026-06-01T01:00:00",
                "classification": "PASSIVE_TRAVEL",
            },
        ),
        (
            "patch",
            f"/api/v1/me/travel-segments/{segment.json()['id']}",
            {"version": 1, "reason": "x"},
        ),
        ("delete", f"/api/v1/me/travel-segments/{segment.json()['id']}", {"version": 1}),
    ):
        response = db_client.request(method.upper(), path, headers=OTHER_EMPLOYEE_HEADER, json=body)
        assert response.status_code in {403, 404}
    foreign_list = db_client.get("/api/v1/me/travel-trips", headers=OTHER_EMPLOYEE_HEADER)
    assert foreign_list.status_code == 200 and foreign_list.json() == {"trips": []}
    blocked = db_client.request(
        "DELETE",
        f"/api/v1/me/travel-trips/{trip['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 1},
    )
    assert blocked.status_code == 422
    june = db_client.get(
        "/api/v1/me/travel-summary?period=2026-07", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert june.status_code == 200, june.text
    assert june.json()["trips"][0]["window_end"] == "2026-06-30"
    assert june.json()["credits"][0]["occurred_on"] == "2026-07-01"


def test_a_trip_with_no_segments_is_still_listed_for_its_own_month(
    db_client: TestClient, db_session: Session
) -> None:
    """A declared trip is a fact the employee owns, and the UI lists it as
    "No segments yet — add the journey legs below." Deleting a trip also requires
    its segments to be deleted FIRST, so a segment-less trip is a reachable,
    product-mandated state. Dropping it from the engine input made it invisible in
    every month — and therefore undeletable from the UI, because the delete action
    lives on the trip card the list never rendered.
    """
    install_router(db_client)
    trip = make_trip(db_client, start_local="2026-11-10T07:00:00", end_local="2026-11-11T19:00:00")
    assert trip["segments"] == []

    november = db_client.get(
        "/api/v1/me/travel-summary?period=2026-11", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert november.status_code == 200, november.text
    listed = [item for item in november.json()["trips"] if item["trip_id"] == trip["id"]]
    assert len(listed) == 1, november.text
    assert listed[0]["eligible_minutes"] == 0
    assert listed[0]["credit_minutes"] == 0
    assert listed[0]["window_end"] == "2026-12-10"
    # A trip with no passive travel proposes no credit and raises no finding.
    assert november.json()["credits"] == []
    assert november.json()["findings"] == []

    # Membership stays fenced: a November trip is not listed for an unrelated month.
    july = db_client.get(
        "/api/v1/me/travel-summary?period=2026-07", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert july.status_code == 200, july.text
    assert all(item["trip_id"] != trip["id"] for item in july.json()["trips"])
