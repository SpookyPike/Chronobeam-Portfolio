"""F5 privacy / disclosure discrimination for day status (INV-05, ACCESS-003).

A day-status fact is a calculation input, so it changes the *arithmetic* — but
the category, source, `external_category`, comment and the ``WT-STATUS-*``
findings must never appear in a disclosed artefact. These tests pin that exact
boundary rather than an accident.
"""

import uuid
from datetime import datetime
from typing import Any

from app.enums import (
    DayStatusCategory,
    DayStatusDayPart,
    DayStatusPortion,
    WorkType,
)
from app.identity.models import Employee
from app.integrations.shift_plan import ShiftAssignmentSeed
from app.shift.service import seed_shift_assignment
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.conftest import (
    DEVELOPER_PRINCIPAL_HEADER as ME,
)
from tests.conftest import (
    HR_HEADER,
    RECIPIENT_HEADER,
)

DAY_STATUS = "/api/v1/me/day-status"
INTERVALS = "/api/v1/me/work-intervals"
MONTHS = "/api/v1/me/months"
HR_REQUESTS = "/api/v1/hr/access-requests"
ME_REQUESTS = "/api/v1/me/access-requests"

MONDAY = "2032-03-08"
MARCH = "2032-03"

#: The seeded shift's own span. The day-status fact used by the recipient test is
#: dated on its first day, so the package's period genuinely covers the fact.
SHIFT_DAY = "2026-07-31"
SHIFT_END_DAY = "2026-08-01"


def _employee(session: Session) -> Employee:
    employee = session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    return employee


FORBIDDEN_KEYS = {
    "category",
    "day_status",
    "day_part",
    "portion",
    "external_category",
    "source",
    "comment",
    "reason",
    "WT-STATUS-001",
    "WT-STATUS-002",
    "WT-STATUS-003",
    "WT-STATUS-004",
    "WT-STATUS-005",
}


def _disclose(db_client: TestClient) -> str:
    request = db_client.post(
        HR_REQUESTS,
        headers=HR_HEADER,
        json={
            "employee_external_id": "emp-0001",
            "purpose": "RANDOM_CHECK",
            "month": MARCH,
            "view_type": "work-time-record",
        },
    )
    assert request.status_code == 200, request.text
    granted = db_client.post(f"{ME_REQUESTS}/{request.json()['id']}/grant", headers=ME, json={})
    assert granted.status_code == 200, granted.text
    return granted.json()["id"]


def _flatten(value: object) -> str:
    return str(value)


def test_a_frozen_month_projection_contains_no_day_status_category_or_status_finding(
    db_client: TestClient,
) -> None:
    """The disclosed payload's per-day keys stay exactly the pre-F5 set."""
    created = db_client.post(
        DAY_STATUS,
        headers=ME,
        json={
            "day": MONDAY,
            "category": DayStatusCategory.VACATION.value,
            "portion": DayStatusPortion.HALF.value,
            "day_part": DayStatusDayPart.MORNING.value,
        },
    )
    assert created.status_code == 201, created.text
    grant_id = _disclose(db_client)
    view = db_client.get(f"/api/v1/disclosures/{grant_id}", headers=HR_HEADER)
    assert view.status_code == 200, view.text
    body = view.json()
    payload = body["snapshot"]
    for row in payload["daily"]:
        assert set(row) == {"date", "worked_minutes", "required_minutes", "delta_minutes"}
    serialized = _flatten(body)
    assert "VACATION" not in serialized
    assert "WT-STATUS" not in serialized
    assert "external_category" not in serialized


def test_the_disclosed_required_minutes_reflect_the_reduced_requirement(
    db_client: TestClient,
) -> None:
    """The arithmetic IS truthful — that is the boundary, not a bug."""
    created = db_client.post(
        DAY_STATUS,
        headers=ME,
        json={
            "day": MONDAY,
            "category": DayStatusCategory.VACATION.value,
            "portion": DayStatusPortion.HALF.value,
            "day_part": DayStatusDayPart.MORNING.value,
        },
    )
    assert created.status_code == 201, created.text
    grant_id = _disclose(db_client)
    view = db_client.get(f"/api/v1/disclosures/{grant_id}", headers=HR_HEADER).json()
    payload = view["snapshot"]
    row = next(item for item in payload["daily"] if item["date"] == MONDAY)
    assert row["required_minutes"] == 174


def _shift_package(db_client: TestClient, db_session: Session) -> dict[str, Any]:
    """A REAL ``SHIFT_REPORT`` package, built through preview -> submit.

    The real preview fingerprint is required, so the package is created by the
    same path a browser uses. Its period is the seeded shift's own span.
    """
    assignment = seed_shift_assignment(
        db_session,
        _employee(db_session),
        ShiftAssignmentSeed(
            plan_key="day-status-disclosure-shift",
            plan_name="Day-status disclosure test shift",
            planned_start_at=datetime.fromisoformat(f"{SHIFT_DAY}T19:00:00+02:00"),
            planned_end_at=datetime.fromisoformat(f"{SHIFT_END_DAY}T07:00:00+02:00"),
            shift_kind="NIGHT",
            source="TEST_SEED",
            source_ref="day-status-disclosure-test",
        ),
    )
    preview = db_client.get(f"/api/v1/me/shifts/{assignment.id}/report-preview", headers=ME)
    assert preview.status_code == 200, preview.text
    created = db_client.post(
        "/api/v1/me/submissions/SHIFT_REPORT",
        headers=ME,
        json={
            "scope": {"assignment_id": str(assignment.id)},
            "preview_fingerprint": preview.json()["preview_fingerprint"],
            "client_request_id": str(uuid.uuid4()),
            "recipients": ["recipient"],
        },
    )
    assert created.status_code == 200, created.text
    return created.json()


def test_a_recipient_response_never_contains_a_day_status_category(
    db_client: TestClient, db_session: Session
) -> None:
    """The recipient's own read of a real package names no category.

    The fact is dated inside the package's own period and the projection is
    asserted non-empty, so this cannot pass on an empty payload.
    """
    created = db_client.post(
        DAY_STATUS,
        headers=ME,
        json={
            "day": SHIFT_DAY,
            "category": DayStatusCategory.SICK.value,
            "portion": DayStatusPortion.FULL.value,
            "day_part": DayStatusDayPart.ALL.value,
        },
    )
    assert created.status_code == 201, created.text
    package = _shift_package(db_client, db_session)
    assert package["period_start"] == SHIFT_DAY
    view = db_client.get(f"/api/v1/recipient/submissions/{package['id']}", headers=RECIPIENT_HEADER)
    assert view.status_code == 200, view.text
    body = view.json()
    assert body["payload"], "the package must carry a projection or this is vacuous"
    serialized = str(body)
    for token in (
        "SICK",
        "VACATION",
        "day_status",
        "external_category",
        "portion",
        "day_part",
        "WT-STATUS",
    ):
        assert token not in serialized, token
    # The queue listing is the other recipient surface.
    queue = db_client.get("/api/v1/recipient/submissions", headers=RECIPIENT_HEADER)
    assert queue.status_code == 200, queue.text
    assert any(item["id"] == package["id"] for item in queue.json())
    assert "SICK" not in queue.text
    assert "external_category" not in queue.text


def test_the_day_status_response_exposes_no_external_category(db_client: TestClient) -> None:
    created = db_client.post(
        DAY_STATUS,
        headers=ME,
        json={
            "day": MONDAY,
            "category": DayStatusCategory.VACATION.value,
            "portion": DayStatusPortion.FULL.value,
            "day_part": DayStatusDayPart.ALL.value,
        },
    ).json()
    assert "external_category" not in created
    listed = db_client.get(f"{DAY_STATUS}?period={MARCH}", headers=ME).json()
    assert "external_category" not in str(listed)


def test_no_application_endpoint_exposes_another_employees_day_status(
    db_client: TestClient,
) -> None:
    """An OpenAPI-surface guard in the ``test_identity_surface.py`` style."""
    paths = db_client.get("/openapi.json").json()["paths"]
    day_status_paths = [path for path in paths if "day-status" in path]
    assert day_status_paths == [
        "/api/v1/me/day-status",
        "/api/v1/me/day-status/{entry_id}",
    ]
    for path in day_status_paths:
        for method in paths[path]:
            parameters = paths[path][method].get("parameters", [])
            names = {parameter["name"] for parameter in parameters}
            # No employee id may be named by a path or query parameter.
            assert "employee_id" not in names
            assert "employee" not in names


def test_the_day_status_endpoint_denies_hr_supervisor_and_recipient_principals(
    db_client: TestClient,
) -> None:
    for headers in (HR_HEADER, RECIPIENT_HEADER):
        assert db_client.get(f"{DAY_STATUS}?period={MARCH}", headers=headers).status_code == 403


def test_the_frozen_snapshot_row_carries_no_day_status_columns(db_session: Session) -> None:
    from app.policy.models import PeriodCalculationSnapshot

    columns = {column.name for column in PeriodCalculationSnapshot.__table__.columns}
    assert not any("day_status" in name for name in columns)


def test_a_disclosed_payload_does_not_include_the_day_status_findings(
    db_client: TestClient, db_session: Session
) -> None:
    worked = db_client.post(
        INTERVALS,
        headers=ME,
        json={
            "start_local": f"{MONDAY}T08:00:00",
            "end_local": f"{MONDAY}T12:00:00",
            "work_type": WorkType.ON_SITE.value,
        },
    )
    assert worked.status_code == 201, worked.text
    created = db_client.post(
        DAY_STATUS,
        headers=ME,
        json={
            "day": MONDAY,
            "category": DayStatusCategory.SICK.value,
            "portion": DayStatusPortion.FULL.value,
            "day_part": DayStatusDayPart.ALL.value,
        },
    )
    assert created.status_code == 201, created.text
    from app.policy.models import PeriodCalculationSnapshot

    grant_id = _disclose(db_client)
    snapshot = db_session.scalars(
        select(PeriodCalculationSnapshot).order_by(PeriodCalculationSnapshot.snapshot_no.desc())
    ).first()
    assert snapshot is not None
    assert "WT-STATUS" not in str(snapshot.result_json)
    assert "SICK" not in str(snapshot.result_json)
    assert db_client.get(f"/api/v1/disclosures/{grant_id}", headers=HR_HEADER).status_code == 200


def test_a_day_status_category_is_named_only_by_the_employee_endpoint(
    db_client: TestClient,
) -> None:
    """The employee-facing ``GET /me/day-status`` is the only surface that names
    a category."""
    paths = db_client.get("/openapi.json").json()["paths"]
    naming = [
        path
        for path in paths
        if path.startswith("/api/v1/me/day-status")
        or path.startswith("/api/v1/admin")
        or path.startswith("/api/v1/hr")
        or path.startswith("/api/v1/recipient")
    ]
    assert all("day-status" not in path for path in naming if not path.startswith("/api/v1/me/"))


def test_the_hr_random_check_reveals_no_time_data_at_request_time(db_client: TestClient) -> None:
    """Unchanged by F5: the request itself exposes nothing, and it never
    auto-grants visibility of a day-status fact."""
    created = db_client.post(
        DAY_STATUS,
        headers=ME,
        json={
            "day": MONDAY,
            "category": DayStatusCategory.VACATION.value,
            "portion": DayStatusPortion.FULL.value,
            "day_part": DayStatusDayPart.ALL.value,
        },
    )
    assert created.status_code == 201, created.text
    request = db_client.post(
        HR_REQUESTS,
        headers=HR_HEADER,
        json={
            "employee_external_id": "emp-0001",
            "purpose": "RANDOM_CHECK",
            "month": MARCH,
            "view_type": "work-time-record",
        },
    )
    assert request.status_code == 200, request.text
    assert "VACATION" not in request.text
    assert "day_status" not in request.text


def test_the_disclosure_projection_shape_is_unchanged_by_f5(db_client: TestClient) -> None:
    grant_id = _disclose(db_client)
    view = db_client.get(f"/api/v1/disclosures/{grant_id}", headers=HR_HEADER).json()
    payload = view["snapshot"]
    assert set(payload) == {
        "employee_identifier",
        "period_start",
        "period_end",
        "daily",
        "intervals",
    }
    assert all(isinstance(row["date"], str) for row in payload["daily"])
    assert all(
        set(row) == {"date", "worked_minutes", "required_minutes", "delta_minutes"}
        for row in payload["daily"]
    )


def test_no_day_status_column_is_reachable_from_a_non_employee_route(
    db_client: TestClient,
) -> None:
    paths = db_client.get("/openapi.json").json()["paths"]
    for path, methods in paths.items():
        if path.startswith("/api/v1/me/"):
            continue
        for operation in methods.values():
            assert "day-status" not in operation.get("summary", "").lower()
