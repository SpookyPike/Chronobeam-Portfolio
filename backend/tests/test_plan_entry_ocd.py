"""Employee-entered on-call availability: create / correct / cancel (pilot slice).

Companion to ``test_plan_entry.py`` (the shift half). The two domains share the
provenance vocabulary but have SEPARATE capabilities, separate active predicates and
separate error vocabularies — several tests here exist only to prove they stay
separate.
"""

import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
from app.enums import ACTIVE_ASSIGNMENT_STATUSES, OCD_ACTIVE_STATUSES
from app.identity.models import Employee
from app.ocd_overtime.models import OcdAssignment, OcdAssignmentStatus
from app.ocd_overtime.service import seed_ocd_assignment
from app.worktime.models import Correction
from app.worktime.walltime import BERLIN
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from .conftest import (
    DEVELOPER_PRINCIPAL_HEADER,
    HR_HEADER,
    OTHER_EMPLOYEE_HEADER,
)

PREFIX = "/api/v1/me/ocd-assignments"


def _employee(session: Session) -> Employee:
    row = session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert row is not None
    return row


def _create_body(
    request_id: str | None = None,
    *,
    start: str = "2026-08-10T18:00:00",
    end: str = "2026-08-11T08:00:00",
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "start_local": start,
        "end_local": end,
        "client_request_id": request_id or str(uuid.uuid4()),
    }
    body.update(extra)
    return body


def _create(client: TestClient, **kwargs: Any) -> dict[str, Any]:
    response = client.post(PREFIX, headers=DEVELOPER_PRINCIPAL_HEADER, json=_create_body(**kwargs))
    assert response.status_code == 201, response.text
    return response.json()


def _chain(session: Session, assignment_id: uuid.UUID) -> list[OcdAssignment]:
    """The whole version chain of ``assignment_id``, head first."""
    rows: list[OcdAssignment] = []
    cursor: OcdAssignment | None = session.get(OcdAssignment, assignment_id)
    while cursor is not None:
        rows.append(cursor)
        cursor = (
            session.scalar(select(OcdAssignment).where(OcdAssignment.supersedes_id == cursor.id))
            if cursor.supersedes_id is None
            else session.get(OcdAssignment, cursor.supersedes_id)
        )
        if cursor is not None and cursor.id in {row.id for row in rows}:
            break
    return rows


def _elect(client: TestClient, assignment_id: str, mode: str = "PAY") -> None:
    """OCD_COMPENSATION requires an election, so a submittable state needs one."""
    response = client.post(
        "/api/v1/me/compensation-elections",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"context_type": "OCD", "context_id": assignment_id, "mode": mode},
    )
    assert response.status_code == 200, response.text


def _findings(body: dict[str, Any], rule_id: str) -> list[dict[str, Any]]:
    return [item for item in body["findings"] if item["rule_id"] == rule_id]


# --- 39-41: create ---------------------------------------------------------


def test_employee_creates_own_ocd_assignment(entry_client: TestClient, db_session: Session) -> None:
    body = _create(entry_client)
    row = db_session.get(OcdAssignment, uuid.UUID(body["id"]))
    assert row is not None
    assert row.source == "EMPLOYEE_RECORDED"
    assert row.status is OcdAssignmentStatus.SCHEDULED
    assert row.version == 1
    assert row.supersedes_id is None
    # D17: the institutional notice instant is server-forced NULL on every
    # employee-authored row, because it is the sole input to the
    # remuneration-relevant short-notice classification.
    assert row.scheduled_at is None
    assert row.reported_notice_at is None
    # The version-guarded PATCH/cancel read their token from THIS response, so a
    # payload without it would make the per-card controls unusable.
    assert body["version"] == 1

    listed = entry_client.get(
        "/api/v1/me/ocd?from=2026-08-01&to=2026-09-01", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [body["id"]]


def test_employee_cannot_create_an_ocd_assignment_for_another_employee(
    entry_client: TestClient,
) -> None:
    """``employee_id`` is not a request field at all, and cannot be smuggled."""
    body = _create_body()
    body["employee_id"] = str(uuid.uuid4())
    response = entry_client.post(PREFIX, headers=DEVELOPER_PRINCIPAL_HEADER, json=body)
    assert response.status_code == 422, response.text


def test_an_employee_reported_notice_is_preserved_but_never_authoritative(
    entry_client: TestClient, db_session: Session
) -> None:
    """D17: the employee keeps the fact; policy keeps the authority."""
    body = _create(entry_client, reported_notice_local="2026-08-01T09:00:00")
    row = db_session.get(OcdAssignment, uuid.UUID(body["id"]))
    assert row is not None
    assert row.reported_notice_at is not None
    assert row.reported_notice_at.astimezone(BERLIN).isoformat().startswith("2026-08-01T09:00")
    assert row.scheduled_at is None

    # A smuggled institutional instant is refused, not silently dropped.
    smuggled = _create_body()
    smuggled["scheduled_at"] = "2026-08-01T09:00:00"
    refused = entry_client.post(PREFIX, headers=DEVELOPER_PRINCIPAL_HEADER, json=smuggled)
    assert refused.status_code == 422, refused.text

    preview = entry_client.get(
        f"{PREFIX}/{body['id']}/compensation-preview", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert preview.status_code == 200, preview.text
    payload = preview.json()
    # The reported fact is disclosed AS a reported fact...
    assert payload["reported_notice_at"] is not None
    assert payload["plan_source"] == "EMPLOYEE_RECORDED"
    # ...and stays out of the classification, which still sees no scheduled time.
    assert payload["duty"]["short_notice"] is None
    assert _findings(payload, "OCD-007"), "the 'scheduled time is absent' INFO must still fire"
    assert not _findings(payload, "OCD-002"), "no notice classification may be invented"


# --- 42-43: correct / cancel ----------------------------------------------


def test_ocd_assignment_edit_creates_a_version_and_keeps_the_chain(
    entry_client: TestClient, db_session: Session
) -> None:
    root = _create(entry_client)
    edited = entry_client.patch(
        f"{PREFIX}/{root['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "version": 1,
            "start_local": "2026-08-10T20:00:00",
            "end_local": "2026-08-11T06:00:00",
        },
    )
    assert edited.status_code == 200, edited.text
    head = db_session.get(OcdAssignment, uuid.UUID(edited.json()["id"]))
    assert head is not None
    assert head.version == 2
    assert head.supersedes_id == uuid.UUID(root["id"])
    assert edited.json()["version"] == 2, "the response carries the NEW token"
    # Append-only lineage: the predecessor is untouched, not mutated in place.
    predecessor = db_session.get(OcdAssignment, uuid.UUID(root["id"]))
    assert predecessor is not None and predecessor.version == 1
    assert predecessor.status is OcdAssignmentStatus.SCHEDULED
    assert head.start_at > predecessor.start_at, "the correction moved the window later"

    correction = db_session.scalar(select(Correction).where(Correction.entity_id == head.id))
    assert correction is not None
    assert correction.entity_type == "OCD_ASSIGNMENT"
    assert correction.old_version == 1 and correction.new_version == 2

    listed = entry_client.get(
        "/api/v1/me/ocd?from=2026-08-01&to=2026-09-01", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert [item["id"] for item in listed.json()] == [edited.json()["id"]]


def test_ocd_assignment_cancel_creates_a_version_with_cancelled_status(
    entry_client: TestClient, db_session: Session
) -> None:
    root = _create(entry_client)
    cancelled = entry_client.post(
        f"{PREFIX}/{root['id']}/cancel",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 1},
    )
    assert cancelled.status_code == 200, cancelled.text
    head = db_session.get(OcdAssignment, uuid.UUID(cancelled.json()["id"]))
    assert head is not None
    assert head.status is OcdAssignmentStatus.CANCELLED
    assert head.version == 2
    assert head.supersedes_id == uuid.UUID(root["id"])
    predecessor = db_session.get(OcdAssignment, uuid.UUID(root["id"]))
    assert predecessor is not None and predecessor.status is OcdAssignmentStatus.SCHEDULED


# --- 44-45: the monthly limit ---------------------------------------------


def _day_assignment(session: Session, day: int) -> OcdAssignment:
    return seed_ocd_assignment(
        session,
        _employee(session),
        start_at=datetime.fromisoformat(f"2026-07-{day:02d}T18:00:00+02:00"),
        end_at=datetime.fromisoformat(f"2026-07-{day:02d}T20:00:00+02:00"),
    )


def test_EINV_13_ocd_monthly_limit_counts_only_current_versions(
    db_client: TestClient, db_session: Session
) -> None:
    """A superseded predecessor still SCHEDULED must not double-count its days.

    The predecessor's window is WIDER than the head's: it covers 16-17 July, the head
    only 16 July. With the head predicate the month has 16 OCD days (1..16) and
    OCD-003 stays silent. Without it the predecessor's extra day is counted alongside
    its successor's, the month has 17, and a FALSE OCD-003 fires.

    MUTATION PROOF: dropping the ``_current(...)`` predicate from ``list_ocd``'s
    ``all_month`` query (i.e. counting every version) turns this red.
    """
    for day in range(1, 16):
        _day_assignment(db_session, day)
    root = seed_ocd_assignment(
        db_session,
        _employee(db_session),
        start_at=datetime.fromisoformat("2026-07-16T18:00:00+02:00"),
        end_at=datetime.fromisoformat("2026-07-17T20:00:00+02:00"),
    )
    head = OcdAssignment(
        employee_id=root.employee_id,
        start_at=datetime.fromisoformat("2026-07-16T18:00:00+02:00"),
        end_at=datetime.fromisoformat("2026-07-16T20:00:00+02:00"),
        scheduled_at=None,
        reported_notice_at=None,
        source="TEST_SEED",
        external_id=None,
        status=OcdAssignmentStatus.SCHEDULED,
        version=2,
        supersedes_id=root.id,
    )
    db_session.add(head)
    db_session.flush()
    assert root.status is OcdAssignmentStatus.SCHEDULED, "the predecessor keeps its own status"

    listed = db_client.get(
        "/api/v1/me/ocd?from=2026-07-01&to=2026-08-01", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert listed.status_code == 200, listed.text
    bodies = listed.json()
    assert len(bodies) == 16, "exactly one row per CURRENT assignment, not per version"
    for body in bodies:
        assert not _findings(body, "OCD-003"), (
            "a superseded predecessor must not be counted alongside its successor"
        )


def test_ocd_003_a_cancelled_assignment_does_not_count_toward_the_monthly_limit(
    entry_client: TestClient, db_session: Session
) -> None:
    """Pins CURRENT behaviour while ``OPEN-23`` is open.

    ``monthly_assignment_findings`` skips every row whose status is not
    ``SCHEDULED``, and OCD-003's text says nothing about cancelled assignments. This
    test does not decide the question; it stops the behaviour drifting silently
    before the employer answers it.
    """
    for day in range(1, 17):
        _day_assignment(db_session, day)
    cancelled = _create(entry_client, start="2026-07-17T18:00:00", end="2026-07-17T20:00:00")
    cancelled_head = entry_client.post(
        f"{PREFIX}/{cancelled['id']}/cancel",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 1},
    )
    assert cancelled_head.status_code == 200, cancelled_head.text

    listed = entry_client.get(
        "/api/v1/me/ocd?from=2026-07-01&to=2026-08-01", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert listed.status_code == 200, listed.text
    bodies = listed.json()
    assert len(bodies) == 17
    assert not any(_findings(body, "OCD-003") for body in bodies), (
        "16 SCHEDULED days + 1 CANCELLED day must not raise OCD-003 while OPEN-23 is open"
    )

    # The control: a 17th SCHEDULED day DOES raise it, so the assertion above is
    # about the cancellation and not about the limit being unreachable.
    _day_assignment(db_session, 18)
    over = entry_client.get(
        "/api/v1/me/ocd?from=2026-07-01&to=2026-08-01", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert any(_findings(body, "OCD-003") for body in over.json())


# --- 46-49: chain identity, freeze, and the two refusal shapes -------------


def test_ocd_call_links_only_a_chain_head(entry_client: TestClient, db_session: Session) -> None:
    """The §4.5 freeze is what makes the call link stable without a chain walk."""
    root = _create(entry_client)
    edited = entry_client.patch(
        f"{PREFIX}/{root['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "version": 1,
            "start_local": "2026-08-10T19:00:00",
            "end_local": "2026-08-11T07:00:00",
        },
    )
    assert edited.status_code == 200, edited.text
    head_id = edited.json()["id"]

    called = entry_client.post(
        "/api/v1/me/ocd-calls",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "ocd_assignment_id": head_id,
            "call_start_local": "2026-08-10T23:00:00",
            "call_end_local": "2026-08-10T23:30:00",
            "mode": "REMOTE",
        },
    )
    assert called.status_code == 200, called.text
    from app.ocd_overtime.models import OcdCall

    call = db_session.get(OcdCall, uuid.UUID(called.json()["id"]))
    assert call is not None
    assert call.ocd_assignment_id == uuid.UUID(head_id)

    # The freeze: once the call exists the plan is immutable, so the link above can
    # never be invalidated by a later correction.
    frozen = entry_client.patch(
        f"{PREFIX}/{head_id}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "version": 2,
            "start_local": "2026-08-10T21:00:00",
            "end_local": "2026-08-11T05:00:00",
        },
    )
    assert frozen.status_code == 409, frozen.text
    assert frozen.json()["detail"]["code"] == "CONFLICT_VERSION"


def test_a_superseded_id_is_not_previewable_or_submittable(
    entry_client: TestClient,
) -> None:
    root = _create(entry_client)
    edited = entry_client.patch(
        f"{PREFIX}/{root['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "version": 1,
            "start_local": "2026-08-10T19:00:00",
            "end_local": "2026-08-11T07:00:00",
        },
    )
    assert edited.status_code == 200, edited.text

    stale_preview = entry_client.get(
        f"{PREFIX}/{root['id']}/compensation-preview", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert stale_preview.status_code == 404, "a stale id is never silently redirected"
    assert stale_preview.json()["detail"]["code"] == "NOT_FOUND"

    stale_package = entry_client.post(
        "/api/v1/me/submissions/OCD_COMPENSATION",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "scope": {"assignment_id": root["id"]},
            "preview_fingerprint": "0" * 64,
            "client_request_id": str(uuid.uuid4()),
            "recipients": ["recipient"],
        },
    )
    assert stale_package.status_code == 404, stale_package.text


def test_a_superseded_id_is_refused_on_create_call(entry_client: TestClient) -> None:
    root = _create(entry_client)
    edited = entry_client.patch(
        f"{PREFIX}/{root['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "version": 1,
            "start_local": "2026-08-10T19:00:00",
            "end_local": "2026-08-11T07:00:00",
        },
    )
    assert edited.status_code == 200, edited.text
    refused = entry_client.post(
        "/api/v1/me/ocd-calls",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "ocd_assignment_id": root["id"],
            "call_start_local": "2026-08-10T23:00:00",
            "call_end_local": "2026-08-10T23:30:00",
            "mode": "REMOTE",
        },
    )
    assert refused.status_code == 404, refused.text


def test_a_superseded_id_and_a_stale_version_token_fail_differently(
    entry_client: TestClient, db_session: Session
) -> None:
    """404 for a stale IDENTITY, 409 for a stale VERSION — never the same answer."""
    root = _create(entry_client)
    edited = entry_client.patch(
        f"{PREFIX}/{root['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "version": 1,
            "start_local": "2026-08-10T19:00:00",
            "end_local": "2026-08-11T07:00:00",
        },
    )
    head_id = edited.json()["id"]

    stale_id = entry_client.patch(
        f"{PREFIX}/{root['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "version": 2,
            "start_local": "2026-08-10T20:00:00",
            "end_local": "2026-08-11T06:00:00",
        },
    )
    assert stale_id.status_code == 404, stale_id.text
    assert stale_id.json()["detail"]["code"] == "NOT_FOUND"

    stale_version = entry_client.patch(
        f"{PREFIX}/{head_id}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "version": 1,
            "start_local": "2026-08-10T20:00:00",
            "end_local": "2026-08-11T06:00:00",
        },
    )
    assert stale_version.status_code == 409, stale_version.text
    assert stale_version.json()["detail"]["code"] == "CONFLICT_VERSION"

    assert db_session.scalar(select(OcdAssignment).where(OcdAssignment.version == 3)) is None, (
        "neither refusal may append a version"
    )


# --- 50-52: disclosure, election and the chain -----------------------------


def test_correction_after_disclosure_keeps_the_package_byte_stable(
    entry_client: TestClient, db_session: Session
) -> None:
    """Only DELETION is refused after disclosure (``disclosure/guards.py``)."""
    root = _create(entry_client, start="2026-08-10T18:00:00", end="2026-08-11T08:00:00")
    _elect(entry_client, root["id"])
    preview = entry_client.get(
        f"{PREFIX}/{root['id']}/compensation-preview", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert preview.status_code == 200, preview.text
    first = entry_client.post(
        "/api/v1/me/submissions/OCD_COMPENSATION",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "scope": {"assignment_id": root["id"]},
            "preview_fingerprint": preview.json()["preview_fingerprint"],
            "client_request_id": str(uuid.uuid4()),
            "recipients": ["recipient"],
        },
    )
    assert first.status_code == 200, first.text
    frozen_payload = first.json()["payload"]

    corrected = entry_client.patch(
        f"{PREFIX}/{root['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "version": 1,
            "start_local": "2026-08-10T20:00:00",
            "end_local": "2026-08-11T06:00:00",
        },
    )
    assert corrected.status_code == 200, corrected.text

    packages = entry_client.get("/api/v1/me/submissions", headers=DEVELOPER_PRINCIPAL_HEADER)
    assert packages.status_code == 200, packages.text
    stored = [item for item in packages.json() if item["id"] == first.json()["id"]]
    assert stored and stored[0]["payload"] == frozen_payload, (
        "the already-submitted package must stay byte-stable across a correction"
    )

    new_preview = entry_client.get(
        f"{PREFIX}/{corrected.json()['id']}/compensation-preview",
        headers=DEVELOPER_PRINCIPAL_HEADER,
    )
    assert new_preview.status_code == 200, new_preview.text
    assert new_preview.json()["preview_fingerprint"] != preview.json()["preview_fingerprint"]

    second = entry_client.post(
        "/api/v1/me/submissions/OCD_COMPENSATION",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "scope": {"assignment_id": corrected.json()["id"]},
            "preview_fingerprint": new_preview.json()["preview_fingerprint"],
            "client_request_id": str(uuid.uuid4()),
            "recipients": ["recipient"],
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["version"] == 2
    assert second.json()["supersedes_id"] == first.json()["id"]
    after = entry_client.get("/api/v1/me/submissions", headers=DEVELOPER_PRINCIPAL_HEADER)
    prior = next(item for item in after.json() if item["id"] == first.json()["id"])
    assert prior["status"] == "SUPERSEDED"


def test_the_election_resolves_over_the_chain_in_both_directions(
    entry_client: TestClient,
) -> None:
    """An election binds to the EXACT version row the employee selected on."""
    root = _create(entry_client)
    elected = entry_client.post(
        "/api/v1/me/compensation-elections",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"context_type": "OCD", "context_id": root["id"], "mode": "PAY"},
    )
    assert elected.status_code == 200, elected.text

    edited = entry_client.patch(
        f"{PREFIX}/{root['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "version": 1,
            "start_local": "2026-08-10T19:00:00",
            "end_local": "2026-08-11T07:00:00",
        },
    )
    assert edited.status_code == 200, edited.text

    # (a) elected on the ROOT, read from the HEAD — the election must survive.
    after_edit = entry_client.get(
        f"{PREFIX}/{edited.json()['id']}/compensation-preview", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert after_edit.status_code == 200, after_edit.text
    assert after_edit.json()["election"]["mode"] == "PAY"

    # (b) elected on the HEAD directly, read from the HEAD.
    head_election = entry_client.post(
        "/api/v1/me/compensation-elections",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"context_type": "OCD", "context_id": edited.json()["id"], "mode": "TIME"},
    )
    assert head_election.status_code == 200, head_election.text
    read_back = entry_client.get(
        "/api/v1/me/compensation-elections",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        params={"context_type": "OCD", "context_id": edited.json()["id"]},
    )
    assert read_back.status_code == 200, read_back.text
    assert read_back.json()["mode"] == "TIME"


def test_an_election_on_a_predecessor_is_accepted(entry_client: TestClient) -> None:
    """``_validate_context`` deliberately keeps NO head filter (§4.8)."""
    root = _create(entry_client)
    edited = entry_client.patch(
        f"{PREFIX}/{root['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "version": 1,
            "start_local": "2026-08-10T19:00:00",
            "end_local": "2026-08-11T07:00:00",
        },
    )
    assert edited.status_code == 200, edited.text

    on_predecessor = entry_client.post(
        "/api/v1/me/compensation-elections",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"context_type": "OCD", "context_id": root["id"], "mode": "PAY"},
    )
    assert on_predecessor.status_code == 200, on_predecessor.text
    found = entry_client.get(
        f"{PREFIX}/{edited.json()['id']}/compensation-preview", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert found.json()["election"]["mode"] == "PAY", (
        "an election on a predecessor must still resolve from the head"
    )


# --- 53-56: provenance, payload, replay, visibility ------------------------


def test_an_imported_ocd_assignment_is_not_employee_editable(
    entry_client: TestClient, db_session: Session
) -> None:
    seeded = seed_ocd_assignment(
        db_session,
        _employee(db_session),
        start_at=datetime.fromisoformat("2026-08-10T18:00:00+02:00"),
        end_at=datetime.fromisoformat("2026-08-11T08:00:00+02:00"),
    )
    assert seeded.source == "TEST_SEED"
    for response in (
        entry_client.patch(
            f"{PREFIX}/{seeded.id}",
            headers=DEVELOPER_PRINCIPAL_HEADER,
            json={
                "version": 1,
                "start_local": "2026-08-10T19:00:00",
                "end_local": "2026-08-11T07:00:00",
            },
        ),
        entry_client.post(
            f"{PREFIX}/{seeded.id}/cancel",
            headers=DEVELOPER_PRINCIPAL_HEADER,
            json={"version": 1},
        ),
    ):
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "CONFLICT_VERSION"


def test_the_ocd_compensation_payload_states_the_plan_source_and_the_reported_notice(
    entry_client: TestClient,
) -> None:
    root = _create(entry_client, reported_notice_local="2026-08-01T09:00:00")
    _elect(entry_client, root["id"])
    preview = entry_client.get(
        f"{PREFIX}/{root['id']}/compensation-preview", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["plan_source"] == "EMPLOYEE_RECORDED"
    assert preview.json()["reported_notice_at"] is not None

    created = entry_client.post(
        "/api/v1/me/submissions/OCD_COMPENSATION",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "scope": {"assignment_id": root["id"]},
            "preview_fingerprint": preview.json()["preview_fingerprint"],
            "client_request_id": str(uuid.uuid4()),
            "recipients": ["recipient"],
        },
    )
    assert created.status_code == 200, created.text
    payload = created.json()["payload"]
    assert payload["plan_source"] == "EMPLOYEE_RECORDED"
    assert payload["reported_notice_at"] is not None
    assert not any(key in payload for key in ("findings", "amount", "credit"))


def test_ocd_entry_replay_survives_a_flag_flip(entry_client_factory, db_session: Session) -> None:
    """D20: an already-committed same-owner retry is not a new mutation."""
    on = entry_client_factory(shift=False, ocd=True)
    request_id = str(uuid.uuid4())
    created = _create(on, request_id=request_id)

    off = entry_client_factory(shift=False, ocd=False)
    replay = off.post(PREFIX, headers=DEVELOPER_PRINCIPAL_HEADER, json=_create_body(request_id))
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == created["id"]

    fresh = off.post(PREFIX, headers=DEVELOPER_PRINCIPAL_HEADER, json=_create_body())
    assert fresh.status_code == 422, fresh.text
    assert fresh.json()["detail"]["code"] == "POLICY_BLOCKED"


def test_ocd_routes_do_not_expand_hr_or_other_employee_visibility(
    entry_client: TestClient,
) -> None:
    body = _create_body()
    created = entry_client.post(PREFIX, headers=DEVELOPER_PRINCIPAL_HEADER, json=body)
    assert created.status_code == 201, created.text
    assignment_id = created.json()["id"]

    calls = [
        ("post", PREFIX, {"json": _create_body()}),
        ("patch", f"{PREFIX}/{assignment_id}", {"json": {"version": 1}}),
        ("post", f"{PREFIX}/{assignment_id}/cancel", {"json": {"version": 1}}),
    ]
    for method, path, kwargs in calls:
        hr = getattr(entry_client, method)(path, headers=HR_HEADER, **kwargs)
        assert hr.status_code == 403, f"HR must not reach {method.upper()} {path}"

    # Another employee's id is a byte-identical 404 — the same answer as an id that
    # never existed, so the route is not an existence oracle.
    foreign = entry_client.patch(
        f"{PREFIX}/{assignment_id}",
        headers=OTHER_EMPLOYEE_HEADER,
        json={
            "version": 1,
            "start_local": "2026-08-10T20:00:00",
            "end_local": "2026-08-11T06:00:00",
        },
    )
    unknown = entry_client.patch(
        f"{PREFIX}/{uuid.uuid4()}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "version": 1,
            "start_local": "2026-08-10T20:00:00",
            "end_local": "2026-08-11T06:00:00",
        },
    )
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json()


# --- 59: the two active predicates stay separate ---------------------------


def test_the_shift_and_ocd_active_predicates_are_separate() -> None:
    """§4.5.1: two enums, no shared member, no shared semantics."""
    from app.enums import ShiftAssignmentStatus

    assert frozenset({ShiftAssignmentStatus.PLANNED}) == ACTIVE_ASSIGNMENT_STATUSES
    assert frozenset({OcdAssignmentStatus.SCHEDULED}) == OCD_ACTIVE_STATUSES
    assert not set(ACTIVE_ASSIGNMENT_STATUSES) & {OcdAssignmentStatus.SCHEDULED}
    assert OcdAssignmentStatus.SCHEDULED in OCD_ACTIVE_STATUSES
    assert OcdAssignmentStatus.CANCELLED not in OCD_ACTIVE_STATUSES
    assert OcdAssignmentStatus.SUPERSEDED not in OCD_ACTIVE_STATUSES


# --- 64-67: the reported notice is presence-merged -------------------------


@pytest.fixture()
def noticed(entry_client: TestClient) -> Iterator[dict[str, Any]]:
    yield _create(entry_client, reported_notice_local="2026-08-01T09:00:00")


def test_a_patch_without_reported_notice_preserves_it(
    entry_client: TestClient, db_session: Session, noticed: dict[str, Any]
) -> None:
    edited = entry_client.patch(
        f"{PREFIX}/{noticed['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "version": 1,
            "start_local": "2026-08-10T19:00:00",
            "end_local": "2026-08-11T07:00:00",
        },
    )
    assert edited.status_code == 200, edited.text
    head = db_session.get(OcdAssignment, uuid.UUID(edited.json()["id"]))
    assert head is not None and head.reported_notice_at is not None, (
        "an omitted reported_notice_local must PRESERVE the predecessor's instant"
    )


def test_a_patch_can_change_reported_notice(
    entry_client: TestClient, db_session: Session, noticed: dict[str, Any]
) -> None:
    edited = entry_client.patch(
        f"{PREFIX}/{noticed['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "version": 1,
            "reported_notice_local": "2026-08-02T07:30:00",
        },
    )
    assert edited.status_code == 200, edited.text
    head = db_session.get(OcdAssignment, uuid.UUID(edited.json()["id"]))
    assert head is not None and head.reported_notice_at is not None
    assert head.reported_notice_at.astimezone(BERLIN).isoformat().startswith("2026-08-02T07:30")


def test_a_patch_can_clear_reported_notice(
    entry_client: TestClient, db_session: Session, noticed: dict[str, Any]
) -> None:
    """An explicit ``null`` must CLEAR — this is what proves presence, not truthiness."""
    edited = entry_client.patch(
        f"{PREFIX}/{noticed['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 1, "reported_notice_local": None},
    )
    assert edited.status_code == 200, edited.text
    head = db_session.get(OcdAssignment, uuid.UUID(edited.json()["id"]))
    assert head is not None and head.reported_notice_at is None


def test_a_predecessor_keeps_its_original_reported_notice(
    entry_client: TestClient, db_session: Session, noticed: dict[str, Any]
) -> None:
    """Append-only lineage: no correction may mutate an earlier row in place."""
    first = entry_client.patch(
        f"{PREFIX}/{noticed['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 1, "reported_notice_local": "2026-08-02T07:30:00"},
    )
    assert first.status_code == 200, first.text
    second = entry_client.patch(
        f"{PREFIX}/{first.json()['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 2, "reported_notice_local": None},
    )
    assert second.status_code == 200, second.text

    root = db_session.get(OcdAssignment, uuid.UUID(noticed["id"]))
    middle = db_session.get(OcdAssignment, uuid.UUID(first.json()["id"]))
    head = db_session.get(OcdAssignment, uuid.UUID(second.json()["id"]))
    assert root is not None and root.reported_notice_at is not None
    assert root.reported_notice_at.astimezone(BERLIN).isoformat().startswith("2026-08-01T09:00")
    assert middle is not None and middle.reported_notice_at is not None
    assert middle.reported_notice_at.astimezone(BERLIN).isoformat().startswith("2026-08-02T07:30")
    assert head is not None and head.reported_notice_at is None


def test_a_VOIDED_call_still_freezes_the_availability(
    entry_client: TestClient, db_session: Session
) -> None:
    """EINV-29: the freeze is on ANY current call, ``VOIDED`` included.

    This is the rule's sharpest edge, and the one a later refactor is most likely to
    "simplify" away: filtering the freeze to ``RECORDED`` calls would leave every
    other freeze test green. ``void_call`` deletes the linked work interval and keeps
    the voided call as lineage, so letting the plan be corrected or cancelled
    afterwards would make an availability that once carried a disclosed call
    silently disappear — the disclosed package would still reference a call whose
    availability had been rewritten underneath it.

    MUTATION PROOF: narrowing ``_assert_ocd_plan_mutable``'s call predicate to
    ``OcdCallStatus.RECORDED`` turns this red and nothing else.
    """
    created = _create(entry_client)
    called = entry_client.post(
        "/api/v1/me/ocd-calls",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "ocd_assignment_id": created["id"],
            "call_start_local": "2026-08-10T23:00:00",
            "call_end_local": "2026-08-10T23:30:00",
            "mode": "REMOTE",
        },
    )
    assert called.status_code == 200, called.text

    voided = entry_client.post(
        f"/api/v1/me/ocd-calls/{called.json()['id']}/void",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": called.json()["version"]},
    )
    assert voided.status_code == 200, voided.text
    assert voided.json()["status"] == "VOIDED"

    # The voided call is still a CURRENT call of this availability, so the plan is
    # frozen — the employee's route is to record a new call, not to rewrite the plan.
    corrected = entry_client.patch(
        f"{PREFIX}/{created['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "version": 1,
            "start_local": "2026-08-10T20:00:00",
            "end_local": "2026-08-11T06:00:00",
        },
    )
    assert corrected.status_code == 409, corrected.text
    assert corrected.json()["detail"]["code"] == "CONFLICT_VERSION"

    cancelled = entry_client.post(
        f"{PREFIX}/{created['id']}/cancel",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 1},
    )
    assert cancelled.status_code == 409, cancelled.text
    assert cancelled.json()["detail"]["code"] == "CONFLICT_VERSION"

    assert db_session.scalar(select(OcdAssignment).where(OcdAssignment.version == 2)) is None, (
        "neither refusal may append a version"
    )


def test_a_cancelled_availability_cannot_be_submitted_as_compensation(
    entry_client: TestClient,
) -> None:
    """EINV-32: a non-active plan is retained LINEAGE, never a disclosable scope.

    The disclosure resolver applied the head predicate but not the OCD domain's
    active-status predicate, so a CANCELLED availability resolved a disclosable
    period and the recipient projection then computed a duty classification from
    it — carrying hourly-wage units for a plan the employee had retracted, with no
    ``status`` field in the payload for the recipient to notice. The frontend gates
    its submit control on ``SCHEDULED``, so only a direct API call reached it, which
    is exactly why this needs a backend test.

    The asymmetry is deliberate and matches the shift side (§4.7.1): the EMPLOYEE's
    own ``compensation-preview`` still resolves a cancelled head — they may see what
    it would have been — while the RECIPIENT path must not.
    """
    root = _create(entry_client)
    _elect(entry_client, root["id"])
    cancelled = entry_client.post(
        f"{PREFIX}/{root['id']}/cancel",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 1},
    )
    assert cancelled.status_code == 200, cancelled.text
    head_id = cancelled.json()["id"]

    # The employee keeps their own view.
    own = entry_client.get(
        f"{PREFIX}/{head_id}/compensation-preview", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert own.status_code == 200, own.text

    # The recipient path must refuse, with the disclosure path's own nondisclosing
    # 404 — a distinct code from the employee path, never a silent redirect.
    preview = entry_client.get(
        f"{PREFIX}/{head_id}/compensation-preview", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    fingerprint = preview.json()["preview_fingerprint"]
    submitted = entry_client.post(
        "/api/v1/me/submissions/OCD_COMPENSATION",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "scope": {"assignment_id": head_id},
            "preview_fingerprint": fingerprint,
            "client_request_id": str(uuid.uuid4()),
            "recipients": ["recipient"],
        },
    )
    assert submitted.status_code == 404, submitted.text
    assert submitted.json()["detail"]["code"] == "NOT_FOUND"

    listed = entry_client.get("/api/v1/me/submissions", headers=DEVELOPER_PRINCIPAL_HEADER)
    assert listed.json() == [], "a refused scope must not leave a package behind"
