"""F5 frozen-history regression for day status (EINV-07 / ACCESS-011).

The discriminating suite proving a day-status fact is NOT a new path around the
frozen-history contract. Each case runs against a month covered by both guard
kinds, and each fails on any implementation that invents its own approximation
of the rule instead of reusing ``disclosure/guards.py::assert_not_disclosed``.
"""

import uuid
from datetime import datetime

from app.enums import (
    DayStatusCategory,
    DayStatusDayPart,
    DayStatusPortion,
    WorkType,
)
from app.identity.models import Employee
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.conftest import DEVELOPER_PRINCIPAL_HEADER as ME
from tests.conftest import HR_HEADER

DAY_STATUS = "/api/v1/me/day-status"
INTERVALS = "/api/v1/me/work-intervals"
HR_REQUESTS = "/api/v1/hr/access-requests"
ME_REQUESTS = "/api/v1/me/access-requests"

MONTH = "2032-03"
MONDAY = "2032-03-08"


def _grant(db_client: TestClient, month: str = MONTH) -> str:
    request = db_client.post(
        HR_REQUESTS,
        headers=HR_HEADER,
        json={
            "employee_external_id": "emp-0001",
            "purpose": "RANDOM_CHECK",
            "month": month,
            "view_type": "work-time-record",
        },
    )
    assert request.status_code == 200, request.text
    granted = db_client.post(f"{ME_REQUESTS}/{request.json()['id']}/grant", headers=ME, json={})
    assert granted.status_code == 200, granted.text
    return granted.json()["id"]


def _create(db_client: TestClient, day: str = MONDAY, **overrides):
    payload = {
        "day": day,
        "category": DayStatusCategory.VACATION.value,
        "portion": DayStatusPortion.FULL.value,
        "day_part": DayStatusDayPart.ALL.value,
    }
    payload.update(overrides)
    return db_client.post(DAY_STATUS, headers=ME, json=payload)


def _submit_package(db_client: TestClient, db_session: Session) -> None:
    """Freeze a SHIFT_REPORT package covering the test month."""
    from app.integrations.shift_plan import ShiftAssignmentSeed
    from app.shift.service import seed_shift_assignment

    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    assignment = seed_shift_assignment(
        db_session,
        employee,
        ShiftAssignmentSeed(
            plan_key="day-status-frozen-history",
            plan_name="Day-status frozen-history shift",
            # The package's own period must cover the month's first day: the
            # disclosed-month guard is month-granular and compares against it.
            planned_start_at=datetime.fromisoformat(f"{MONTH}-01T08:00:00+01:00"),
            planned_end_at=datetime.fromisoformat(f"{MONTH}-01T16:00:00+01:00"),
            shift_kind="DAY",
            source="TEST_SEED",
            source_ref="day-status-frozen-history",
        ),
    )
    db_session.flush()
    preview = db_client.get(f"/api/v1/me/shifts/{assignment.id}/report-preview", headers=ME)
    assert preview.status_code == 200, preview.text
    created = db_client.post(
        "/api/v1/me/submissions/SHIFT_REPORT",
        headers=ME,
        json={
            "scope": {"assignment_id": str(assignment.id)},
            "preview_fingerprint": preview.json()["preview_fingerprint"],
            "recipients": ["recipient"],
            "client_request_id": str(uuid.uuid4()),
        },
    )
    assert created.status_code == 200, created.text


def test_a_day_status_delete_is_refused_on_a_month_with_a_live_grant(
    db_client: TestClient,
) -> None:
    created = _create(db_client).json()
    _grant(db_client)
    refused = db_client.delete(
        f"{DAY_STATUS}/{created['id']}?version={created['version']}", headers=ME
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "CONFLICT_DISCLOSED_FACT"
    assert refused.json()["detail"]["details"]["disclosure_kind"] == "grant"


def test_a_day_status_delete_is_refused_on_a_non_withdrawn_submission_package(
    db_client: TestClient, db_session: Session
) -> None:
    created = _create(db_client).json()
    _submit_package(db_client, db_session)
    refused = db_client.delete(
        f"{DAY_STATUS}/{created['id']}?version={created['version']}", headers=ME
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["details"]["disclosure_kind"] == "package"


def test_a_day_status_create_and_correction_are_permitted_on_a_disclosed_month(
    db_client: TestClient,
) -> None:
    """F5 must be exactly as permissive as the existing factual layer, not stricter."""
    _grant(db_client)
    created = _create(db_client)
    assert created.status_code == 201, created.text
    corrected = db_client.patch(
        f"{DAY_STATUS}/{created.json()['id']}",
        headers=ME,
        json={
            "version": created.json()["version"],
            "portion": DayStatusPortion.HALF.value,
            "day_part": DayStatusDayPart.MORNING.value,
        },
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["version"] == 2


def test_a_day_status_mutation_never_rewrites_a_frozen_snapshot_or_package_payload(
    db_client: TestClient, db_session: Session
) -> None:
    """The mutation changes the LIVE recomputation only."""
    from app.disclosure.models import SubmissionPackage
    from app.policy.models import PeriodCalculationSnapshot

    created = _create(db_client).json()
    _grant(db_client)
    _submit_package(db_client, db_session)
    snapshot = db_session.scalars(
        select(PeriodCalculationSnapshot).order_by(PeriodCalculationSnapshot.snapshot_no.desc())
    ).first()
    package = db_session.scalars(
        select(SubmissionPackage).order_by(SubmissionPackage.version.desc())
    ).first()
    assert snapshot is not None and package is not None
    frozen = (
        snapshot.result_json,
        snapshot.input_fingerprint,
        list(snapshot.calendar_version_ids),
        package.payload_json,
    )
    corrected = db_client.patch(
        f"{DAY_STATUS}/{created['id']}",
        headers=ME,
        json={
            "version": created["version"],
            "portion": DayStatusPortion.HALF.value,
            "day_part": DayStatusDayPart.MORNING.value,
        },
    )
    assert corrected.status_code == 200, corrected.text
    refused = db_client.delete(
        f"{DAY_STATUS}/{corrected.json()['id']}?version={corrected.json()['version']}",
        headers=ME,
    )
    assert refused.status_code == 409, refused.text
    db_session.expire_all()
    assert (
        snapshot.result_json,
        snapshot.input_fingerprint,
        list(snapshot.calendar_version_ids),
        package.payload_json,
    ) == frozen


def test_a_day_status_mutation_on_a_disclosed_month_does_not_change_what_the_grantee_reads(
    db_client: TestClient,
) -> None:
    created = _create(db_client).json()
    grant_id = _grant(db_client)
    before = db_client.get(f"/api/v1/disclosures/{grant_id}", headers=HR_HEADER).json()
    corrected = db_client.patch(
        f"{DAY_STATUS}/{created['id']}",
        headers=ME,
        json={
            "version": created["version"],
            "portion": DayStatusPortion.HALF.value,
            "day_part": DayStatusDayPart.MORNING.value,
        },
    )
    assert corrected.status_code == 200, corrected.text
    after = db_client.get(f"/api/v1/disclosures/{grant_id}", headers=HR_HEADER).json()
    assert after == before


def test_the_delete_refusal_matches_the_interval_delete_refusal(db_client: TestClient) -> None:
    """Asserted side by side, so the two paths cannot drift into different
    frozen-history semantics."""
    interval = db_client.post(
        INTERVALS,
        headers=ME,
        json={
            "start_local": f"{MONDAY}T08:00:00",
            "end_local": f"{MONDAY}T12:00:00",
            "work_type": WorkType.ON_SITE.value,
        },
    )
    assert interval.status_code == 201, interval.text
    created = _create(db_client).json()
    _grant(db_client)
    interval_refusal = db_client.delete(
        f"{INTERVALS}/{interval.json()['id']}?version={interval.json()['version']}", headers=ME
    )
    day_status_refusal = db_client.delete(
        f"{DAY_STATUS}/{created['id']}?version={created['version']}", headers=ME
    )
    assert interval_refusal.status_code == day_status_refusal.status_code == 409
    assert (
        interval_refusal.json()["detail"]["code"] == (day_status_refusal.json()["detail"]["code"])
    )
    assert set(interval_refusal.json()["detail"]["details"]) == set(
        day_status_refusal.json()["detail"]["details"]
    )


def test_a_withdrawn_package_does_not_block_the_delete(
    db_client: TestClient, db_session: Session
) -> None:
    """The guard's own semantics, reused rather than re-implemented."""
    created = _create(db_client).json()
    _submit_package(db_client, db_session)
    packages = db_client.get("/api/v1/me/submissions", headers=ME).json()
    package_id = packages[-1]["id"]
    withdrawn = db_client.post(
        f"/api/v1/me/submissions/{package_id}/withdraw",
        headers=ME,
        json={"version": packages[-1]["version"]},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    removed = db_client.delete(
        f"{DAY_STATUS}/{created['id']}?version={created['version']}", headers=ME
    )
    assert removed.status_code == 204, removed.text


def test_the_day_status_guard_uses_the_one_shared_function() -> None:
    """No independent approximation of ACCESS-011 exists."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "absence" / "service.py").read_text(
        encoding="utf-8"
    )
    assert "assert_not_disclosed(" in source
    assert "month_is_disclosed(" not in source
