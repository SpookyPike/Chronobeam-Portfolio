"""DB-backed worktime API tests (L3 integration; WT-BACKEND §7/§8.1).

DATA-005..010 P0 data-model cases, BE-CMD-001/002 semantics, overlap/order
validation, optimistic versioning, correction lineage, idempotency. Every
test runs inside its own rolled-back transaction (tests/conftest.py).
"""

import uuid
from datetime import datetime
from typing import Any

import pytest
from app.audit.models import AuditEvent
from app.worktime.models import Correction, WorkInterval, WorkPeriod
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.conftest import DEVELOPER_PRINCIPAL_HEADER as ME
from tests.conftest import HR_HEADER
from tests.conftest import OTHER_EMPLOYEE_HEADER as ME_B

MONTHS = "/api/v1/me/months"
INTERVALS = "/api/v1/me/work-intervals"


def make_payload(
    start_local: str,
    end_local: str,
    work_type: str = "ON_SITE",
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "start_local": start_local,
        "end_local": end_local,
        "work_type": work_type,
    }
    payload.update(extra)
    return payload


def create_interval(
    client: TestClient,
    start_local: str,
    end_local: str,
    headers: dict[str, str] = ME,
    **extra: Any,
) -> dict[str, Any]:
    response = client.post(
        INTERVALS, headers=headers, json=make_payload(start_local, end_local, **extra)
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_DATA_005_four_intervals_preserved(db_client: TestClient) -> None:
    """A July-like day with 4 intervals keeps every interval (no collapsing)."""
    day = "2026-07-06"
    for start, end in [
        ("08:00", "10:00"),
        ("10:15", "12:00"),
        ("12:30", "14:00"),
        ("14:15", "17:00"),
    ]:
        create_interval(db_client, f"{day}T{start}:00", f"{day}T{end}:00")

    summary = db_client.get(f"{MONTHS}/2026-07", headers=ME).json()
    assert summary["interval_count"] == 4
    day_summary = next(d for d in summary["days"] if d["date"] == day)
    assert day_summary["interval_count"] == 4

    detail = db_client.get(f"{MONTHS}/2026-07/days/{day}", headers=ME).json()
    assert len(detail["intervals"]) == 4
    assert [i["work_type"] for i in detail["intervals"]] == ["ON_SITE"] * 4


def test_DATA_006_mixed_work_types_same_day(db_client: TestClient) -> None:
    """ON_SITE morning + REMOTE afternoon: both preserved, no day overwrite."""
    day = "2026-07-07"
    create_interval(db_client, f"{day}T08:00:00", f"{day}T12:00:00", work_type="ON_SITE")
    create_interval(db_client, f"{day}T12:30:00", f"{day}T16:00:00", work_type="REMOTE")
    detail = db_client.get(f"{MONTHS}/2026-07/days/{day}", headers=ME).json()
    assert [i["work_type"] for i in detail["intervals"]] == ["ON_SITE", "REMOTE"]


def test_DATA_007_cross_midnight_interval(db_client: TestClient) -> None:
    """SHIFT 19:00 -> 02:30 next day: one canonical interval, both day views."""
    created = create_interval(
        db_client, "2026-07-31T19:00:00", "2026-08-01T02:30:00", work_type="SHIFT"
    )
    assert created["elapsed_seconds"] == 7 * 3600 + 30 * 60

    july_day = db_client.get(f"{MONTHS}/2026-07/days/2026-07-31", headers=ME).json()
    aug_day = db_client.get(f"{MONTHS}/2026-08/days/2026-08-01", headers=ME).json()
    assert [i["id"] for i in july_day["intervals"]] == [created["id"]]
    assert [i["id"] for i in aug_day["intervals"]] == [created["id"]]
    assert july_day["intervals"][0]["start_local"] == "2026-07-31T19:00:00"
    assert july_day["intervals"][0]["end_local"] == "2026-08-01T02:30:00"


def test_DATA_008_month_boundary_interval(db_client: TestClient, db_session: Session) -> None:
    """31 Aug 19:00 -> 1 Sep 02:30: stored once, work_period_id = August,
    visible in BOTH month views ."""
    created = create_interval(db_client, "2026-08-31T19:00:00", "2026-09-01T02:30:00")

    august = db_client.get(f"{MONTHS}/2026-08", headers=ME).json()
    september = db_client.get(f"{MONTHS}/2026-09", headers=ME).json()
    assert august["interval_count"] == 1
    assert september["interval_count"] == 1

    stored = db_session.scalars(select(WorkInterval)).all()
    assert len(stored) == 1
    period = db_session.get(WorkPeriod, stored[0].work_period_id)
    assert period is not None
    assert period.period_month.isoformat() == "2026-08-01"
    assert created["id"] == str(stored[0].id)

    # month views expose day buckets within their own month only
    assert [d["date"] for d in august["days"]] == ["2026-08-31"]
    assert [d["date"] for d in september["days"]] == ["2026-09-01"]


def test_DATA_009_dst_spring_forward_rejected(db_client: TestClient) -> None:
    """Nonexistent 02:30 on 2026-03-29 is rejected with the DST error."""
    response = db_client.post(
        INTERVALS,
        headers=ME,
        json=make_payload("2026-03-29T02:30:00", "2026-03-29T04:00:00"),
    )
    assert response.status_code == 422
    body = response.json()["detail"]
    assert body["code"] == "VALIDATION_DST_AMBIGUOUS"
    assert body["details"]["kind"] == "nonexistent"


def test_DATA_009_dst_gap_elapsed_is_instant_based(db_client: TestClient) -> None:
    """01:30 -> 03:30 across the gap is accepted and reports 1 elapsed hour."""
    created = create_interval(db_client, "2026-03-29T01:30:00", "2026-03-29T03:30:00")
    assert created["elapsed_seconds"] == 3600


def test_DATA_010_dst_fall_back_requires_fold(db_client: TestClient) -> None:
    """02:30 on 2026-10-25 is ambiguous: rejected without fold, resolved
    deterministically with fold=0/1 (two distinct instants)."""
    payload = make_payload("2026-10-25T02:30:00", "2026-10-25T04:00:00")
    response = db_client.post(INTERVALS, headers=ME, json=payload)
    assert response.status_code == 422
    body = response.json()["detail"]
    assert body["code"] == "VALIDATION_DST_AMBIGUOUS"
    assert body["details"]["kind"] == "ambiguous"
    assert len(body["details"]["candidates"]) == 2

    first = create_interval(db_client, "2026-10-25T02:30:00", "2026-10-25T04:00:00", fold=0)
    assert first["start_at"].endswith("+02:00")  # first occurrence (CEST)
    # The fold=0 interval ends 04:00 CET = 03:00 UTC; the fold=(1) variant
    # must start after that instant: 04:30 CET = 03:30 UTC (no overlap).
    second = create_interval(
        db_client,
        "2026-10-25T04:30:00",
        "2026-10-25T06:00:00",
        fold=1,
        client_request_id=str(uuid.uuid4()),
    )
    assert second["start_at"].endswith("+01:00")  # second occurrence (CET)


def test_cmd_001_order_rejected(db_client: TestClient) -> None:
    response = db_client.post(
        INTERVALS, headers=ME, json=make_payload("2026-07-01T12:00:00", "2026-07-01T08:00:00")
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VALIDATION_INTERVAL_ORDER"


def test_cmd_001_overlap_rejected_and_adjacent_ok(db_client: TestClient) -> None:
    create_interval(db_client, "2026-07-01T08:00:00", "2026-07-01T12:00:00")
    # touching end == start: OK
    create_interval(db_client, "2026-07-01T12:00:00", "2026-07-01T16:00:00")
    # overlapping: rejected
    response = db_client.post(
        INTERVALS, headers=ME, json=make_payload("2026-07-01T11:00:00", "2026-07-01T13:00:00")
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VALIDATION_INTERVAL_OVERLAP"


def test_cmd_001_overlap_is_period_agnostic(db_client: TestClient) -> None:
    """An August-starting interval colliding with a July one is rejected (M3)."""
    create_interval(db_client, "2026-07-31T20:00:00", "2026-07-31T23:00:00")
    response = db_client.post(
        INTERVALS,
        headers=ME,
        json=make_payload("2026-07-31T22:00:00", "2026-08-01T01:00:00"),
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VALIDATION_INTERVAL_OVERLAP"


def test_cmd_001_idempotent_replay_returns_existing(
    db_client: TestClient, db_session: Session
) -> None:
    request_id = uuid.uuid4()
    first = create_interval(
        db_client,
        "2026-07-02T08:00:00",
        "2026-07-02T10:00:00",
        client_request_id=str(request_id),
    )
    response = db_client.post(
        INTERVALS,
        headers=ME,
        json=make_payload(
            "2026-07-02T08:00:00", "2026-07-02T10:00:00", client_request_id=str(request_id)
        ),
    )
    assert response.status_code == 200
    assert response.json()["id"] == first["id"]
    assert len(db_session.scalars(select(WorkInterval)).all()) == 1


def test_cmd_001_idempotency_collision_across_employees(db_client: TestClient) -> None:
    """The same client_request_id used by another employee is a conflict
    (the unique index is global; a replay must never return someone else's
    interval)."""
    request_id = str(uuid.uuid4())
    create_interval(
        db_client, "2026-07-02T08:00:00", "2026-07-02T10:00:00", client_request_id=request_id
    )
    response = db_client.post(
        INTERVALS,
        headers=ME_B,
        json=make_payload(
            "2026-07-02T08:00:00", "2026-07-02T10:00:00", client_request_id=request_id
        ),
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VALIDATION_FAILED"


def test_cmd_002_correction_supersedes_and_keeps_lineage(
    db_client: TestClient, db_session: Session
) -> None:
    created = create_interval(db_client, "2026-07-03T08:00:00", "2026-07-03T12:00:00")
    response = db_client.patch(
        f"{INTERVALS}/{created['id']}",
        headers=ME,
        json={
            "version": 1,
            "start_local": "2026-07-03T08:15:00",
            "reason": "started later than planned",
        },
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["version"] == 2
    assert updated["supersedes_id"] == created["id"]
    assert updated["elapsed_seconds"] == 3 * 3600 + 45 * 60

    # the superseded row is preserved, the current view shows only v2
    rows = db_session.scalars(select(WorkInterval).order_by(WorkInterval.version)).all()
    assert [r.version for r in rows] == [1, 2]
    assert rows[1].supersedes_id == rows[0].id

    detail = db_client.get(f"{MONTHS}/2026-07/days/2026-07-03", headers=ME).json()
    assert len(detail["intervals"]) == 1
    assert detail["intervals"][0]["id"] == updated["id"]

    lineage = db_session.scalars(select(Correction)).all()
    assert len(lineage) == 1
    assert lineage[0].entity_type == "WORK_INTERVAL"
    assert lineage[0].old_version == 1
    assert lineage[0].new_version == 2
    assert lineage[0].actor_role == "EMPLOYEE"
    assert lineage[0].reason == "started later than planned"

    # editing an interval to still overlap its OWN predecessor is allowed (M3)
    response = db_client.patch(
        f"{INTERVALS}/{updated['id']}",
        headers=ME,
        json={"version": 2, "start_local": "2026-07-03T08:20:00"},
    )
    assert response.status_code == 200, response.text


def test_cmd_002_stale_version_conflict_409(db_client: TestClient) -> None:
    created = create_interval(db_client, "2026-07-04T08:00:00", "2026-07-04T12:00:00")
    db_client.patch(
        f"{INTERVALS}/{created['id']}",
        headers=ME,
        json={"version": 1, "end_local": "2026-07-04T13:00:00"},
    )
    # retry with the stale version -> 409
    response = db_client.patch(
        f"{INTERVALS}/{created['id']}",
        headers=ME,
        json={"version": 1, "end_local": "2026-07-04T14:00:00"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CONFLICT_VERSION"
    # ... and targeting the superseded id itself is also a conflict
    response = db_client.patch(
        f"{INTERVALS}/{created['id']}",
        headers=ME,
        json={"version": 2, "end_local": "2026-07-04T14:00:00"},
    )
    assert response.status_code == 409


def test_cmd_002_foreign_id_equals_nonexistent_404(
    db_client: TestClient, db_session: Session
) -> None:
    created = create_interval(db_client, "2026-07-05T08:00:00", "2026-07-05T12:00:00")
    # B tries to correct A's interval (ownership inside the lookup predicate,
    # M4): byte-identical 404 to a random UUID.
    foreign = db_client.patch(
        f"{INTERVALS}/{created['id']}",
        headers=ME_B,
        json={"version": 1, "end_local": "2026-07-05T13:00:00"},
    )
    missing = db_client.patch(
        f"{INTERVALS}/{uuid.uuid4()}",
        headers=ME_B,
        json={"version": 1, "end_local": "2026-07-05T13:00:00"},
    )
    assert foreign.status_code == 404
    assert missing.status_code == 404
    assert foreign.text == missing.text
    # Superseded rows stay hidden from the overlap check (M2): a correction of
    # A's interval that overlaps the OLD (superseded) v1 is still fine.
    first_correct = db_client.patch(
        f"{INTERVALS}/{created['id']}",
        headers=ME,
        json={"version": 1, "start_local": "2026-07-05T07:00:00"},
    )
    assert first_correct.status_code == 200


def test_delete_requiring_current_version(db_client: TestClient, db_session: Session) -> None:
    created = create_interval(db_client, "2026-07-09T08:00:00", "2026-07-09T12:00:00")
    # stale version -> 409
    stale = db_client.delete(f"{INTERVALS}/{created['id']}?version=2", headers=ME)
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "CONFLICT_VERSION"
    # foreign -> 404 identical to missing
    foreign = db_client.request(
        "DELETE",
        f"{INTERVALS}/{created['id']}?version=1",
        headers=ME_B,
        json={"reason": "x"},
    )
    missing = db_client.delete(f"{INTERVALS}/{uuid.uuid4()}?version=1", headers=ME_B)
    assert foreign.status_code == 404
    assert foreign.text == missing.text
    # correct delete: 204, row removed, correction lineage + audit written
    response = db_client.request(
        "DELETE",
        f"{INTERVALS}/{created['id']}?version=1",
        headers=ME,
        json={"reason": "entered by mistake"},
    )
    assert response.status_code == 204
    assert db_session.scalars(select(WorkInterval)).all() == []
    lineage = db_session.scalars(select(Correction)).all()
    assert len(lineage) == 1
    assert lineage[0].new_version is None
    assert lineage[0].old_version == 1
    assert lineage[0].reason == "entered by mistake"
    audits = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "WORK_INTERVAL_DELETE")
    ).all()
    assert len(audits) == 1
    assert audits[0].outcome == "SUCCESS"
    assert audits[0].object_id == uuid.UUID(created["id"])
    assert audits[0].actor_principal == "employee"
    # M12: audit metadata allowlist only — never the reason/comment free text
    assert audits[0].metadata_json == {"interval_version": 1}
    assert "mistake" not in str(audits[0].metadata_json)
    # the interval cannot reappear
    detail = db_client.get(f"{MONTHS}/2026-07/days/2026-07-09", headers=ME).json()
    assert detail["intervals"] == []


def test_delete_corrected_interval_removes_whole_chain(
    db_client: TestClient, db_session: Session
) -> None:
    """Reviewer F1 (HIGH): deleting the current version of a CORRECTED
    interval must not resurrect the superseded predecessor."""
    created = create_interval(db_client, "2026-07-08T08:00:00", "2026-07-08T12:00:00")
    corrected = db_client.patch(
        f"{INTERVALS}/{created['id']}",
        headers=ME,
        json={"version": 1, "start_local": "2026-07-08T08:15:00"},
    ).json()
    assert corrected["version"] == 2

    response = db_client.delete(f"{INTERVALS}/{corrected['id']}?version=2", headers=ME)
    assert response.status_code == 204

    # no version of the logical interval remains -> nothing can reappear
    assert db_session.scalars(select(WorkInterval)).all() == []
    detail = db_client.get(f"{MONTHS}/2026-07/days/2026-07-08", headers=ME).json()
    assert detail["intervals"] == []
    month = db_client.get(f"{MONTHS}/2026-07", headers=ME).json()
    assert month["interval_count"] == 0
    # lineage: the delete itself is recorded once (head version)
    lineage = db_session.scalars(select(Correction)).all()
    assert len(lineage) == 2  # one correction + one delete tombstone
    assert lineage[1].old_version == 2
    assert lineage[1].new_version is None


def test_replay_after_correction_returns_current_version(
    db_client: TestClient, db_session: Session
) -> None:
    """Reviewer F3 (LOW): a retried create after a correction must NOT return
    the superseded v1 row."""
    request_id = str(uuid.uuid4())
    created = create_interval(
        db_client, "2026-07-02T08:00:00", "2026-07-02T10:00:00", client_request_id=request_id
    )
    corrected = db_client.patch(
        f"{INTERVALS}/{created['id']}",
        headers=ME,
        json={"version": 1, "end_local": "2026-07-02T11:00:00"},
    ).json()
    replay = db_client.post(
        INTERVALS,
        headers=ME,
        json=make_payload(
            "2026-07-02T08:00:00", "2026-07-02T10:00:00", client_request_id=request_id
        ),
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == corrected["id"]
    assert replay.json()["version"] == 2


def test_interval_ending_exactly_at_midnight_day_bucket_agrees_with_day_detail(
    db_client: TestClient,
) -> None:
    """Reviewer F2 (LOW): an interval ending exactly at Berlin midnight spans
    no instant of the end date — month buckets and day detail must agree."""
    create_interval(db_client, "2026-07-15T23:00:00", "2026-07-16T00:00:00")
    summary = db_client.get(f"{MONTHS}/2026-07", headers=ME).json()
    assert [d["date"] for d in summary["days"]] == ["2026-07-15"]
    assert summary["days"][0]["interval_count"] == 1
    end_day = db_client.get(f"{MONTHS}/2026-07/days/2026-07-16", headers=ME).json()
    assert end_day["intervals"] == []
    start_day = db_client.get(f"{MONTHS}/2026-07/days/2026-07-15", headers=ME).json()
    assert len(start_day["intervals"]) == 1


def test_delete_stale_failure_writes_no_audit(db_client: TestClient, db_session: Session) -> None:
    """A rejected delete (stale version) is not an audited action."""
    created = create_interval(db_client, "2026-07-10T08:00:00", "2026-07-10T12:00:00")
    db_client.delete(f"{INTERVALS}/{created['id']}?version=99", headers=ME)
    audits = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "WORK_INTERVAL_DELETE")
    ).all()
    assert audits == []


def test_empty_month_is_200_not_404(db_client: TestClient) -> None:
    response = db_client.get(f"{MONTHS}/2026-07", headers=ME)
    assert response.status_code == 200
    body = response.json()
    assert body["interval_count"] == 0
    assert body["days"] == []
    assert body["status"] == "OPEN"


def test_month_and_day_validation(db_client: TestClient) -> None:
    bad_month = db_client.get(f"{MONTHS}/2026-13", headers=ME)
    assert bad_month.status_code == 422
    assert bad_month.json()["detail"]["code"] == "VALIDATION_INVALID_MONTH"

    bad_day = db_client.get(f"{MONTHS}/2026-07/days/2026-07-32", headers=ME)
    assert bad_day.status_code == 422
    assert bad_day.json()["detail"]["code"] == "VALIDATION_INVALID_DATE"

    wrong_month_day = db_client.get(f"{MONTHS}/2026-07/days/2026-08-01", headers=ME)
    assert wrong_month_day.status_code == 422
    assert wrong_month_day.json()["detail"]["code"] == "VALIDATION_INVALID_DATE"


def test_validation_envelope_strips_input(db_client: TestClient) -> None:
    """M6: Pydantic validation errors use the unified envelope; invalid
    values (potential free text) are never echoed back."""
    response = db_client.post(
        INTERVALS,
        headers=ME,
        json=make_payload(
            "2026-07-01T08:00:00", "2026-07-01T09:00:00", comment="garbage", work_type="NOT_A_TYPE"
        ),
    )
    assert response.status_code == 422
    body = response.json()["detail"]
    assert body["code"] == "VALIDATION_FAILED"
    assert "NOT_A_TYPE" not in response.text
    assert "input" not in response.text


def test_comment_length_capped(db_client: TestClient) -> None:
    response = db_client.post(
        INTERVALS,
        headers=ME,
        json=make_payload("2026-07-01T08:00:00", "2026-07-01T09:00:00", comment="x" * 501),
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VALIDATION_FAILED"


def test_own_data_only_scope(db_client: TestClient, db_session: Session) -> None:
    """Adjacent-employee isolation at the data level (IDOR-shaped): B's month
    never contains A's intervals, even with a valid own capability."""
    create_interval(db_client, "2026-07-11T08:00:00", "2026-07-11T12:00:00", headers=ME)
    b_summary = db_client.get(f"{MONTHS}/2026-07", headers=ME_B).json()
    assert b_summary["interval_count"] == 0
    b_day = db_client.get(f"{MONTHS}/2026-07/days/2026-07-11", headers=ME_B).json()
    assert b_day["intervals"] == []
    # A sees exactly one
    a_summary = db_client.get(f"{MONTHS}/2026-07", headers=ME).json()
    assert a_summary["interval_count"] == 1


def test_work_period_status_stays_open(db_client: TestClient) -> None:
    """Creating an interval leaves the employee-side period open."""
    create_interval(db_client, "2026-07-12T08:00:00", "2026-07-12T12:00:00")
    summary = db_client.get(f"{MONTHS}/2026-07", headers=ME).json()
    assert summary["status"] == "OPEN"


def test_phase_3_template_is_explicit_and_month_review_stays_private(
    db_client: TestClient,
) -> None:
    """FE-UX-001/002: preference application creates editable facts only after
    an explicit command; REVIEWED is not a disclosure action."""
    preferences = db_client.patch(
        "/api/v1/me/preferences",
        headers=ME,
        json={
            "normal_day_template": [
                {"start": "08:00", "end": "12:00", "work_type": "ON_SITE"},
                {"start": "12:30", "end": "16:18", "work_type": "REMOTE"},
            ]
        },
    )
    assert preferences.status_code == 200

    applied = db_client.post(
        f"{MONTHS}/2026-07/apply-template",
        headers=ME,
        json={"dates": ["2026-07-13", "2026-07-14"]},
    )
    assert applied.status_code == 200, applied.text
    assert len(applied.json()) == 4
    detail = db_client.get(f"{MONTHS}/2026-07/days/2026-07-13", headers=ME).json()
    assert [item["work_type"] for item in detail["intervals"]] == ["ON_SITE", "REMOTE"]

    # Without acknowledgement the server refuses the REVIEWED marker when
    # unresolved findings exist (review finding 4). July has findings
    # (worked vs required), so this must 422 — send an explicit body so the
    # request reaches review_month() itself (a bodyless request would 422
    # earlier in FastAPI validation and prove nothing).
    refused = db_client.post(f"{MONTHS}/2026-07/review", headers=ME, json={})
    assert refused.status_code == 422

    reviewed = db_client.post(f"{MONTHS}/2026-07/review", headers=ME, json={"acknowledged": True})
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "REVIEWED"
    assert {"daily_results", "closing_balance_minutes"} <= set(reviewed.json())


def test_phase_3_review_without_findings_needs_no_acknowledgement(
    db_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the period calculation has no findings, marking the month
    reviewed without acknowledgement must succeed (the gate only exists
    for unresolved findings)."""
    from types import SimpleNamespace

    import app.policy.service as policy_service

    monkeypatch.setattr(
        policy_service,
        "calculate_employee_period",
        lambda *args, **kwargs: SimpleNamespace(
            findings=[], daily_results=[], closing_balance_minutes=0
        ),
    )
    response = db_client.post(f"{MONTHS}/2026-08/review", headers=ME, json={})
    assert response.status_code == 200
    assert response.json()["status"] == "REVIEWED"


def test_phase_3_copy_and_split_are_atomic_employee_commands(db_client: TestClient) -> None:
    original = create_interval(db_client, "2026-07-15T08:00:00", "2026-07-15T12:00:00")
    split = db_client.post(
        f"{INTERVALS}/{original['id']}/split",
        headers=ME,
        json={"version": 1, "split_local": "2026-07-15T10:00:00"},
    )
    assert split.status_code == 200, split.text
    assert [item["elapsed_seconds"] for item in split.json()] == [7200, 7200]

    copied = db_client.post(f"{MONTHS}/2026-07/days/2026-07-16/copy-previous", headers=ME)
    assert copied.status_code == 200, copied.text
    copied_detail = db_client.get(f"{MONTHS}/2026-07/days/2026-07-16", headers=ME).json()
    assert len(copied_detail["intervals"]) == 2


def test_phase_3_copy_previous_replays_idempotently_with_command_key(
    db_client: TestClient,
) -> None:
    """Retrying copy-previous with the same command key must not duplicate
    factual intervals (review finding L2)."""
    seed = db_client.post(f"{MONTHS}/2026-07/days/2026-07-16/copy-previous", headers=ME, json={})
    assert seed.status_code == 200, seed.text

    key = str(uuid.uuid4())
    one = db_client.post(
        f"{MONTHS}/2026-07/days/2026-07-17/copy-previous",
        headers=ME,
        json={"client_request_id": key},
    )
    assert one.status_code == 200, one.text
    two = db_client.post(
        f"{MONTHS}/2026-07/days/2026-07-17/copy-previous",
        headers=ME,
        json={"client_request_id": key},
    )
    assert two.status_code == 200, two.text
    detail = db_client.get(f"{MONTHS}/2026-07/days/2026-07-17", headers=ME).json()
    assert len(detail["intervals"]) == len(one.json())  # replay added nothing


def test_phase_3_split_preserves_end_instant_through_dst_fold(db_client: TestClient) -> None:
    """Splitting an interval whose end falls in the ambiguous fall-back hour
    must keep the exact stored end instant (review finding L1)."""
    created = create_interval(
        db_client,
        "2026-10-25T01:30:00",
        "2026-10-25T02:30:00",
        fold=1,
    )
    split = db_client.post(
        f"{INTERVALS}/{created['id']}/split",
        headers=ME,
        json={"version": 1, "split_local": "2026-10-25T02:00:00", "fold": 1},
    )
    assert split.status_code == 200, split.text
    _first, second = split.json()
    # Same instant, possibly different tz serialization: compare as instants.
    assert datetime.fromisoformat(second["end_at"]) == datetime.fromisoformat(created["end_at"])


def test_phase_3_detailed_commands_are_denied_to_hr(db_client: TestClient) -> None:
    for path, payload in [
        (f"{MONTHS}/2026-07/apply-template", {"dates": ["2026-07-16"]}),
        (f"{MONTHS}/2026-07/review", {}),
        (f"{MONTHS}/2026-07/days/2026-07-16/copy-previous", {}),
    ]:
        response = db_client.post(path, headers=HR_HEADER, json=payload)
        assert response.status_code == 403
        assert "intervals" not in response.text


def test_phase_3_split_denied_to_hr_and_foreign_interval_is_404(db_client: TestClient) -> None:
    """Split is a detailed-fact endpoint: HR must get a uniform 403 and a
    foreign interval id must be byte-identical to a nonexistent one (M4)."""
    owned = create_interval(db_client, "2026-07-15T08:00:00", "2026-07-15T12:00:00")
    payload = {"version": 1, "split_local": "2026-07-15T10:00:00"}

    hr = db_client.post(f"{INTERVALS}/{owned['id']}/split", headers=HR_HEADER, json=payload)
    assert hr.status_code == 403
    assert "intervals" not in hr.text

    foreign = db_client.post(f"{INTERVALS}/{owned['id']}/split", headers=ME_B, json=payload)
    assert foreign.status_code == 404
    missing = db_client.post(f"{INTERVALS}/{uuid.uuid4()}/split", headers=ME, json=payload)
    assert missing.status_code == 404
    assert foreign.json() == missing.json()
