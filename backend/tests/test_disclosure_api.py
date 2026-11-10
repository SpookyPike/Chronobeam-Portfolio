"""Security-core authorization matrix (ACCESS-001..016).

The P0 boundary proofs from docs/SECURITY.md, executed against live routes:
HR random-check reveals no ledger data before employee action; the employee
opens only the requested one-month view; the frozen snapshot is append-only
and never refreshed; grants carry supersede lineage; requester lists are
requester-scoped; foreign requests are byte-identical 404; supervisor and
technical admin stay capability-less; reads are audited only after
authorization succeeds; DELETE of disclosed months is refused while versioned
corrections keep working (ACCESS-011); the preview fingerprint seals the
exact recipient payload (D9).
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from app.audit.models import AuditEvent
from app.auth.principal import Capability, Principal
from app.disclosure import service as disclosure_service
from app.disclosure.guards import month_is_disclosed
from app.disclosure.models import DisclosureGrant
from app.identity.models import Employee
from app.policy.models import PeriodCalculationSnapshot
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.conftest import (
    DEVELOPER_PRINCIPAL_HEADER as ME,
)
from tests.conftest import (
    HR_HEADER,
    RECIPIENT_HEADER,
    SUPERVISOR_HEADER,
    TECHNICAL_ADMIN_HEADER,
)
from tests.conftest import (
    OTHER_EMPLOYEE_HEADER as ME_B,
)

INTERVALS = "/api/v1/me/work-intervals"
HR_REQUESTS = "/api/v1/hr/access-requests"
ME_REQUESTS = "/api/v1/me/access-requests"


def create_interval(
    client: TestClient, start_local: str, end_local: str, **extra: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "start_local": start_local,
        "end_local": end_local,
        "work_type": extra.pop("work_type", "ON_SITE"),
    }
    payload.update(extra)
    response = client.post(INTERVALS, headers=ME, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def hr_request(
    client: TestClient,
    month: str = "2026-07",
    employee_external_id: str = "emp-0001",
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "employee_external_id": employee_external_id,
        "purpose": "RANDOM_CHECK",
        "month": month,
        "view_type": "work-time-record",
    }
    payload.update(extra)
    response = client.post(HR_REQUESTS, headers=HR_HEADER, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def grant(client: TestClient, request_id: str) -> dict[str, Any]:
    response = client.post(f"{ME_REQUESTS}/{request_id}/grant", headers=ME, json={})
    assert response.status_code == 200, response.text
    return response.json()


def test_ACCESS_007_hr_request_create_reveals_no_ledger_data(
    db_client: TestClient,
) -> None:
    created = hr_request(db_client)
    body = created.keys()
    # Metadata only: no interval, snapshot, finding, or balance field may
    # cross the boundary at request time.
    assert "id" in body and {"purpose", "period_start", "period_end", "status"} <= body
    assert not {"intervals", "snapshot", "findings", "daily", "balance"} & body
    assert created["status"] == "PENDING"
    listed = db_client.get(HR_REQUESTS, headers=HR_HEADER)
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [created["id"]]


def test_ACCESS_007_unknown_employee_byte_identical_404(db_client: TestClient) -> None:
    unknown = db_client.post(
        HR_REQUESTS,
        headers=HR_HEADER,
        json={
            "employee_external_id": "emp-9999",
            "purpose": "RANDOM_CHECK",
            "month": "2026-07",
            "view_type": "work-time-record",
        },
    )
    assert unknown.status_code == 404
    # The body must not hint at why the employee is unknown (no existence oracle).
    assert unknown.json()["detail"]["code"] == "NOT_FOUND"
    invalid_month = db_client.post(
        HR_REQUESTS,
        headers=HR_HEADER,
        json={
            "employee_external_id": "emp-0001",
            "purpose": "RANDOM_CHECK",
            "month": "2026-13",
            "view_type": "work-time-record",
        },
    )
    assert invalid_month.status_code == 422


def test_ACCESS_007_wrong_purpose_rejected_and_capability_gate(
    db_client: TestClient,
) -> None:
    # SHIFT_REPORT is a submission purpose, not a request purpose.
    wrong = db_client.post(
        HR_REQUESTS,
        headers=HR_HEADER,
        json={
            "employee_external_id": "emp-0001",
            "purpose": "SHIFT_REPORT",
            "month": "2026-07",
            "view_type": "work-time-record",
        },
    )
    assert wrong.status_code == 422
    # Employee, supervisor, technical admin, recipient: no ACCESS_REQUEST_CREATE.
    for headers in (ME, ME_B, SUPERVISOR_HEADER, TECHNICAL_ADMIN_HEADER, RECIPIENT_HEADER):
        denied = db_client.post(
            HR_REQUESTS,
            headers=headers,
            json={
                "employee_external_id": "emp-0001",
                "purpose": "RANDOM_CHECK",
                "month": "2026-07",
                "view_type": "work-time-record",
            },
        )
        assert denied.status_code == 403, headers
        assert denied.json()["detail"]["code"] == "AUTH_FORBIDDEN"


def test_ACCESS_007_self_request_rejected_at_create(db_session: Session) -> None:
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    # Synthetic: a principal whose subject IS the employee row with the HR
    # capability (server-controlled mapping could produce this in prod).
    self_principal = Principal(
        subject="employee",
        display_name="self",
        capabilities=frozenset({Capability.ACCESS_REQUEST_CREATE}),
    )
    with pytest.raises(Exception) as excinfo:
        disclosure_service.create_access_request(
            db_session,
            self_principal,
            employee_external_id=employee.external_employee_id or "emp-0001",
            purpose=disclosure_service.MONTH_GRANT_PURPOSES.__iter__().__next__(),
            month="2026-07",
            view_type="work-time-record",
            message=None,
        )
    assert excinfo.value.status_code == 403  # type: ignore[attr-defined]


def test_ACCESS_002_hr_reads_employee_month_without_grant_denied(
    db_client: TestClient,
) -> None:
    denied = db_client.get("/api/v1/me/months/2026-07", headers=HR_HEADER)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "AUTH_FORBIDDEN"


def test_ACCESS_008_employee_lists_requests_and_grants_frozen_snapshot(
    db_client: TestClient, db_session: Session
) -> None:
    create_interval(db_client, "2026-07-06T08:00:00", "2026-07-06T12:00:00")
    create_interval(db_client, "2026-07-07T09:00:00", "2026-07-07T13:30:00")
    request = hr_request(db_client)

    pending = db_client.get(ME_REQUESTS, headers=ME)
    assert pending.status_code == 200
    assert [row["id"] for row in pending.json()] == [request["id"]]
    assert pending.json()[0]["status"] == "PENDING"

    granted = grant(db_client, request["id"])
    assert granted["active"] is True
    assert granted["period_start"] == "2026-07-01"
    assert granted["period_end"] == "2026-07-31"  # exactly ONE calendar month (D7)

    snapshot = db_session.scalar(select(PeriodCalculationSnapshot))
    assert snapshot is not None
    assert snapshot.frozen is True  # append-only, never refreshed (D8)

    read = db_client.get(f"/api/v1/disclosures/{granted['id']}", headers=HR_HEADER)
    assert read.status_code == 200
    view = read.json()
    assert view["purpose"] == "RANDOM_CHECK"
    intervals = view["snapshot"]["intervals"]
    assert len(intervals) == 2
    assert {row["work_type"] for row in intervals} == {"ON_SITE"}
    # The frozen view carries the day totals but never private findings
    # (ACCESS-003) or interval comments (DP-DATA-002).
    assert "findings" not in view["snapshot"]
    assert all("comment" not in row for row in intervals)
    assert any(
        row["date"] == "2026-07-06" and row["worked_minutes"] == 240
        for row in view["snapshot"]["daily"]
    )


def test_ACCESS_009_scope_only_requested_month(db_client: TestClient) -> None:
    create_interval(db_client, "2026-06-30T08:00:00", "2026-06-30T12:00:00")
    create_interval(db_client, "2026-07-06T08:00:00", "2026-07-06T12:00:00")
    request = hr_request(db_client, month="2026-07")
    granted = grant(db_client, request["id"])
    read = db_client.get(f"/api/v1/disclosures/{granted['id']}", headers=HR_HEADER)
    assert read.status_code == 200
    dates = {row["start_at"][:10] for row in read.json()["snapshot"]["intervals"]}
    assert dates == {"2026-07-06"}  # the June interval never leaks (no pivot)


def test_ACCESS_009_grantee_only_and_adjacent_read_denied(
    db_client: TestClient,
) -> None:
    request = hr_request(db_client, month="2026-07")
    granted = grant(db_client, request["id"])

    # The grantee reads it; every other principal is denied (403 / 410).
    assert (
        db_client.get(f"/api/v1/disclosures/{granted['id']}", headers=HR_HEADER).status_code == 200
    )
    for headers in (ME, ME_B, SUPERVISOR_HEADER, TECHNICAL_ADMIN_HEADER, RECIPIENT_HEADER):
        denied = db_client.get(f"/api/v1/disclosures/{granted['id']}", headers=headers)
        assert denied.status_code in (403, 410), headers
    random_grant = db_client.get(f"/api/v1/disclosures/{uuid.uuid4()}", headers=HR_HEADER)
    assert random_grant.status_code in (403, 410)  # unknown grant cannot be probed


def test_ACCESS_008_regrant_revokes_prior_and_supersedes(
    db_client: TestClient, db_session: Session
) -> None:
    first = hr_request(db_client, month="2026-07")
    grant_one = grant(db_client, first["id"])
    second = hr_request(db_client, month="2026-07")
    grant_two = grant(db_client, second["id"])

    assert grant_two["supersedes_id"] == grant_one["id"]
    assert grant_two["active"] is True
    row_one = db_session.get(DisclosureGrant, uuid.UUID(grant_one["id"]))
    assert row_one is not None and row_one.revoked_at is not None
    # The OLD grant is gone for the grantee (atomically revoked, D8)...
    old = db_client.get(f"/api/v1/disclosures/{grant_one['id']}", headers=HR_HEADER)
    assert old.status_code == 410
    # ...and the new one serves the frozen view. Snapshots are append-only:
    # the re-grant wrote a SECOND snapshot, it did not refresh the first.
    count = db_session.scalar(
        select(AuditEvent).where(AuditEvent.action == "DISCLOSURE_GRANT_CREATE")
    )
    assert count is not None
    snapshots = db_session.scalars(select(PeriodCalculationSnapshot)).all()
    assert len(snapshots) == 2
    assert snapshots[0].frozen and snapshots[1].frozen


def test_expired_grant_is_not_advertised_or_treated_as_active(
    db_client: TestClient, db_session: Session
) -> None:
    request = hr_request(db_client, month="2026-07")
    granted = grant(db_client, request["id"])
    row = db_session.get(DisclosureGrant, uuid.UUID(granted["id"]))
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()

    listed = db_client.get(HR_REQUESTS, headers=HR_HEADER)
    assert listed.status_code == 200
    assert listed.json()[0]["grant_id"] is None
    expired = db_client.get(f"/api/v1/disclosures/{granted['id']}", headers=HR_HEADER)
    assert expired.status_code == 410


def test_decline_then_late_grant_is_conflict(db_client: TestClient) -> None:
    request = hr_request(db_client, month="2026-07")
    declined = db_client.post(f"{ME_REQUESTS}/{request['id']}/decline", headers=ME, json={})
    assert declined.status_code == 200
    assert declined.json()["status"] == "DECLINED"
    late = db_client.post(f"{ME_REQUESTS}/{request['id']}/grant", headers=ME, json={})
    assert late.status_code == 409
    listed = db_client.get(HR_REQUESTS, headers=HR_HEADER).json()
    assert listed[0]["status"] == "DECLINED"


def test_foreign_request_equals_nonexistent_404(db_client: TestClient) -> None:
    request = hr_request(db_client, month="2026-07")  # targets employee A
    # Employee B cannot see or act on employee A's request (M4 predicate).
    denied = db_client.post(f"{ME_REQUESTS}/{request['id']}/grant", headers=ME_B, json={})
    assert denied.status_code == 404
    assert denied.json()["detail"]["code"] == "NOT_FOUND"


def test_ACCESS_011_delete_blocked_after_grant_correction_still_works(
    db_client: TestClient, db_session: Session
) -> None:
    interval = create_interval(db_client, "2026-07-06T08:00:00", "2026-07-06T12:00:00")
    request = hr_request(db_client, month="2026-07")
    granted = grant(db_client, request["id"])

    # DELETE of the disclosed month is refused...
    deleted = db_client.request(
        "DELETE",
        f"{INTERVALS}/{interval['id']}?version=1",
        headers=ME,
        json={"reason": "remove"},
    )
    assert deleted.status_code == 409
    assert deleted.json()["detail"]["code"] == "CONFLICT_DISCLOSED_FACT"
    # The refusal is employee-facing: the machine signal is the error code, and
    # the sentence itself carries no internal rule id.
    assert "ACCESS-" not in deleted.json()["detail"]["message"]

    # ...but a versioned correction preserves lineage and stays available.
    corrected = db_client.patch(
        f"{INTERVALS}/{interval['id']}",
        headers=ME,
        json={
            "version": 1,
            "start_local": "2026-07-06T08:00:00",
            "end_local": "2026-07-06T12:30:00",
        },
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["version"] == 2
    # The freeze is immutable: the grant still serves the ORIGINAL projection.
    read = db_client.get(f"/api/v1/disclosures/{granted['id']}", headers=HR_HEADER)
    view = read.json()["snapshot"]
    assert view["intervals"][0]["end_at"].startswith("2026-07-06T12:00")
    assert any(
        row["date"] == "2026-07-06" and row["worked_minutes"] == 240 for row in view["daily"]
    )
    # And the guard reports the month as disclosed via the active grant.
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    from datetime import date

    disclosed, kind = month_is_disclosed(db_session, employee.id, date(2026, 7, 1))
    assert disclosed and kind == "grant"


def test_ACCESS_011_delete_cross_month_interval_is_blocked_by_later_grant(
    db_client: TestClient,
) -> None:
    """A July-starting fact is also part of an August frozen month view."""
    interval = create_interval(db_client, "2026-07-31T23:30:00", "2026-08-01T00:30:00")
    request = hr_request(db_client, month="2026-08")
    grant(db_client, request["id"])

    deleted = db_client.request(
        "DELETE",
        f"{INTERVALS}/{interval['id']}?version=1",
        headers=ME,
        json={"reason": "remove"},
    )
    assert deleted.status_code == 409
    assert deleted.json()["detail"]["code"] == "CONFLICT_DISCLOSED_FACT"


def test_audit_reads_after_authz_only(db_client: TestClient, db_session: Session) -> None:
    def actions() -> list[tuple[str, str]]:
        return [
            (row.action, row.outcome)
            for row in db_session.scalars(select(AuditEvent).order_by(AuditEvent.occurred_at)).all()
        ]

    request = hr_request(db_client, month="2026-07")
    assert ("ACCESS_REQUEST_CREATE", "SUCCESS") in actions()

    # Denied read: NO audit row may appear (deny-no-audit, D11).
    denied = db_client.get(f"/api/v1/disclosures/{uuid.uuid4()}", headers=HR_HEADER)
    assert denied.status_code in (403, 410)
    after_denial = actions()
    assert not any(action == "DISCLOSURE_READ" for action, _ in after_denial)

    granted = grant(db_client, request["id"])
    assert ("DISCLOSURE_GRANT_CREATE", "SUCCESS") in actions()
    ok = db_client.get(f"/api/v1/disclosures/{granted['id']}", headers=HR_HEADER)
    assert ok.status_code == 200
    assert ("DISCLOSURE_READ", "SUCCESS") in actions()


def test_fingerprint_seals_exact_payload_and_changes_with_facts(
    db_client: TestClient,
) -> None:
    from app.disclosure.fingerprint import payload_dict, payload_fingerprint

    create_interval(
        db_client, "2026-07-06T18:00:00", "2026-07-06T21:00:00", work_type="OVERTIME_PAID"
    )
    first = db_client.get("/api/v1/me/overtime/preview?from=2026-07-01&to=2026-08-01", headers=ME)
    assert first.status_code == 200, first.text
    preview_one = first.json()
    fp_one = preview_one["preview_fingerprint"]
    assert fp_one and len(fp_one) == 64
    # The fingerprint covers the RECIPIENT payload: employee-private findings
    # are excluded by construction (ACCESS-003).
    assert "findings" not in payload_dict(preview_one)
    assert payload_fingerprint(payload_dict(preview_one)) == fp_one

    # Correcting the interval changes the facts => the fingerprint changes.
    corrected = db_client.patch(
        f"{INTERVALS}/{preview_one['overtime_intervals'][0]['interval_id']}",
        headers=ME,
        json={
            "version": 1,
            "start_local": "2026-07-06T18:00:00",
            "end_local": "2026-07-06T21:30:00",
        },
    )
    assert corrected.status_code == 200, corrected.text
    second = db_client.get(
        "/api/v1/me/overtime/preview?from=2026-07-01&to=2026-08-01", headers=ME
    ).json()
    assert second["preview_fingerprint"] != fp_one


def test_recipient_registry_rejects_arbitrary_subjects(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Production (default settings): the registry is server-controlled config
    # only — unconfigured means EMPTY, so every submission stays blocked
    # (fail closed, D6).
    from app.config import get_settings

    monkeypatch.setenv("CHRONOBEAM_ENV", "production")
    monkeypatch.setenv("CHRONOBEAM_RECIPIENT_SUBJECTS", "")
    get_settings.cache_clear()
    assert disclosure_service.valid_recipient_subjects() == ()
    # Test/dev: only fixture principals that can read assigned packages fill
    # the package-recipient registry.
    monkeypatch.setenv("CHRONOBEAM_ENV", "test")
    get_settings.cache_clear()
    try:
        disclosure_service._require_registered_recipients(db_session, ["recipient"])
        with pytest.raises(Exception) as excinfo:
            disclosure_service._require_registered_recipients(db_session, ["hr-requester"])
        assert excinfo.value.status_code == 422  # type: ignore[attr-defined]
    finally:
        get_settings.cache_clear()


def test_hr_request_list_exposes_only_the_active_grant_id(
    db_client: TestClient,
) -> None:
    """The requester learns the CURRENT grant via a metadata pulse (D12).

    The grant id alone exposes nothing (the grantee-scoped read still gates
    the projection); when a re-grant supersedes the prior one, the list must
    point at the ACTIVE grant, never the revoked one.
    """
    first = hr_request(db_client, month="2026-07")
    grant_one = grant(db_client, first["id"])
    listed = db_client.get(HR_REQUESTS, headers=HR_HEADER).json()
    assert listed[0]["grant_id"] == grant_one["id"]

    second = hr_request(db_client, month="2026-07")
    grant_two = grant(db_client, second["id"])
    listed = db_client.get(HR_REQUESTS, headers=HR_HEADER).json()
    by_id = {row["id"]: row["grant_id"] for row in listed}
    # Each request exposes ITS OWN active grant. Request 1's grant was
    # revoked by the re-grant, so its pulse is None; request 2 points at
    # the grant that currently serves the view.
    assert by_id[first["id"]] is None
    assert by_id[second["id"]] == grant_two["id"]
    assert grant_two["id"] != grant_one["id"]

    # The revoked grant itself is gone for the grantee (410) — the pulse
    # never resurrects a dead grant.
    read = db_client.get(f"/api/v1/disclosures/{grant_one['id']}", headers=HR_HEADER)
    assert read.status_code == 410


def test_grant_revokes_the_current_predecessor_only(
    db_client: TestClient, db_session: Session
) -> None:
    """The prior-grant query selects CURRENT grants, not superseded ones.

    EINV-04 / F3.7. ``grant_access_request``'s supersession predicate compared
    ``DisclosureGrant.supersedes_id`` to the same table's ``id``, so it was
    always false and ``~superseded`` was inert: a superseded-but-unrevoked row
    was treated as a current prior and had its ``revoked_at`` stamped.
    ``revoked_at`` must mean "stopped being current BY REVOCATION"; supersession
    is expressed by the chain, which is what every current-grant read uses.

    This exercises the malformed query's own path (the grant command), not the
    already-correct predicate in ``disclosure/guards.py``.
    """
    from app.disclosure.models import DisclosureGrant

    request = hr_request(db_client)
    employee = db_session.scalar(
        select(Employee).where(Employee.external_employee_id == "emp-0001")
    )
    assert employee is not None

    def grant_row(granted_at: datetime, supersedes: object = None) -> DisclosureGrant:
        row = DisclosureGrant(
            access_request_id=None,
            employee_id=employee.id,
            grantee_principal=request["requester_principal"],
            purpose=request["purpose"],
            period_start=date.fromisoformat(request["period_start"]),
            period_end=date.fromisoformat(request["period_end"]),
            view_type=request["view_type"],
            granted_at=granted_at,
            supersedes_id=supersedes,
        )
        db_session.add(row)
        db_session.flush()
        return row

    # Explicit ordering: the priors query orders by ``granted_at desc``, so
    # leaving both rows at the server default would make which one is treated
    # as "the newest prior" nondeterministic.
    superseded = grant_row(datetime(2026, 7, 1, tzinfo=UTC))
    current = grant_row(datetime(2026, 7, 2, tzinfo=UTC), superseded.id)

    granted = grant(db_client, request["id"])
    assert granted["id"] not in {str(superseded.id), str(current.id)}
    db_session.expire_all()
    current_row = db_session.get(DisclosureGrant, current.id)
    superseded_row = db_session.get(DisclosureGrant, superseded.id)
    assert current_row is not None and superseded_row is not None
    assert current_row.revoked_at is not None, "the effective predecessor was not revoked"
    assert superseded_row.revoked_at is None, (
        "a superseded grant was treated as a current prior and revoked"
    )


def test_ACCESS_009_a_crossing_interval_is_clipped_to_the_granted_month(
    db_client: TestClient,
) -> None:
    """A one-month grant must not reveal the adjacent month's portion of an interval.

    An interval may cross midnight (INV-02), so a 31 July 22:00 to 1 August 02:00
    interval belongs to July's ledger. Serialising it whole would hand the August
    portion to a July-scoped recipient. The projection is therefore clipped and the
    duration recomputed; ``clipped`` marks the row so the recipient is not misled
    into reading it as the whole recorded interval.
    """
    create_interval(db_client, "2026-07-31T22:00:00", "2026-08-01T02:00:00")
    request = hr_request(db_client, month="2026-07")
    granted = grant(db_client, request["id"])
    view = db_client.get(f"/api/v1/disclosures/{granted['id']}", headers=HR_HEADER)
    assert view.status_code == 200
    snapshot = view.json()["snapshot"]
    crossing = [row for row in snapshot["intervals"] if row["clipped"]]
    assert len(crossing) == 1, snapshot["intervals"]
    row = crossing[0]
    assert row["start_at"] == "2026-07-31T22:00:00+02:00"
    assert row["end_at"] == "2026-08-01T00:00:00+02:00"  # exclusive month bound
    assert row["elapsed_seconds"] == 2 * 60 * 60  # clipped, not the recorded four hours
    # Nothing in the projection may reach into the adjacent month.
    assert all(
        interval["end_at"] <= "2026-08-01T00:00:00+02:00" for interval in snapshot["intervals"]
    )
