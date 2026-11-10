"""F6.1/F6.2 — overtime authorization authority and submission coverage.

``ACCESS-017``/``EINV-23``: **authority is a property of the WRITER.** The
employee ``/api/v1/me/*`` path can only produce a ``REQUESTED``/``CANCELLED``
request; an ``APPROVED``/``REJECTED`` decision with an institutional provenance
comes from ``app.ocd_overtime.service.decide_authorization`` (the operator CLI,
and a future external HR absence-system adapter through the same function). That one predicate is
applied at EVERY consumer that treats a row as an authorization — the submission
preview, the travel calculation and the review-invalidation reopen — so a legacy
employee-self-declared ``APPROVED`` row is authoritative nowhere.

``ACCESS-006``/``EINV-24``: the submission gate is decided on the same facts the
frozen artefact records, so it lives in ``overtime_preview`` — the function the
package is recomputed from — with three DISTINCT failure modes.
"""

import contextlib
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, cast

import pytest
from app.audit.models import AuditEvent
from app.db import constraint_name, lock_employee_facts
from app.enums import PeriodStatus, WorkType
from app.identity.models import Employee
from app.main import create_app  # noqa: F401 - re-exported for the surface guard
from app.ocd_overtime.models import (
    EMPLOYEE_SOURCE,
    INSTITUTIONAL_SOURCE,
    OvertimeAuthorization,
    OvertimeAuthStatus,
)
from app.worktime.service import create_interval_instant
from app.worktime.walltime import resolve_wall_time
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.conftest import DEVELOPER_PRINCIPAL_HEADER as ME
from tests.conftest import OTHER_EMPLOYEE_HEADER, decide_authorization

AUTHORIZATIONS = "/api/v1/me/overtime-authorizations"
PREVIEW = "/api/v1/me/overtime/preview"
MONTHS = "/api/v1/me/months"
SUBMISSIONS = "/api/v1/me/submissions"
FINDINGS = "/api/v1/me/findings"
TRIPS = "/api/v1/me/travel-trips"
INTERVALS = "/api/v1/me/work-intervals"

# 2032 keeps every fixture clear of the seeded and demo months.
#
# F8.1 — the window constants are NAIVE Berlin wall times, exactly as an HTTP
# payload must now be (the service anchors them at its boundary). The rows these
# tests write DIRECTLY through the ORM go through ``_berlin`` below, so the HTTP
# fixtures and the ORM fixtures share ONE window definition and cannot drift.
MARCH = "2032-03"
APRIL = "2032-04"
WINDOW_FROM = f"{MARCH}-01T00:00:00"
WINDOW_TO = f"{APRIL}-01T00:00:00"

#: The three ACCESS-006 messages, imported by identity so a message change is a
#: test change rather than a silently-passing assertion.
from app.ocd_overtime.service import (  # noqa: E402 - after the constants it documents
    ACCESS_006_NO_REFERENCE,
    ACCESS_006_NOT_AUTHORITATIVE,
    ACCESS_006_OUTSIDE_WINDOW,
    WT_CORE_009_NOT_PRIOR_AUTHORIZED,
)


def _employee(session: Session, subject: str = "employee") -> Employee:
    row = session.scalar(select(Employee).where(Employee.idp_subject == subject))
    assert row is not None, subject
    return row


def _request(client: TestClient, **body: Any) -> dict[str, Any]:
    response = client.post(AUTHORIZATIONS, headers=ME, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _interval(
    session: Session, start: str, end: str, work_type: WorkType = WorkType.OVERTIME_PAID
) -> Any:
    row, _ = create_interval_instant(
        session,
        _employee(session),
        start_at=datetime.fromisoformat(start),
        end_at=datetime.fromisoformat(end),
        work_type=work_type,
        comment=None,
    )
    return row


def _link(client: TestClient, interval_id: Any, version: int, authorization_id: Any) -> Any:
    return client.patch(
        f"{INTERVALS}/{interval_id}",
        headers=ME,
        json={"version": version, "overtime_authorization_id": str(authorization_id)},
    )


def _berlin(value: str) -> datetime:
    """The Berlin instant of a fixture string (F8.1's own rule).

    A naive fixture string is a Berlin wall time and is resolved through the
    product's resolver; an aware string is already an instant (the
    version-recorded timestamps ``RECORDED_BEFORE``/``RECORDED_AFTER`` are aware
    on purpose, because they are compared against a work instant). Used by every
    helper that writes a window DIRECTLY through the ORM, so those rows carry the
    same instant the API would have stored for the same wall time.
    """
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else resolve_wall_time(parsed)


def _legacy_row(
    session: Session,
    *,
    valid_from: str,
    valid_to: str | None = None,
    status: OvertimeAuthStatus = OvertimeAuthStatus.APPROVED,
    source: str = EMPLOYEE_SOURCE,
) -> OvertimeAuthorization:
    """A pre-F6 row: the employee could declare ``status``/``authorized_by``."""
    row = OvertimeAuthorization(
        employee_id=_employee(session).id,
        valid_from=_berlin(valid_from),
        valid_to=_berlin(valid_to) if valid_to else None,
        status=status,
        source=source,
        version=1,
    )
    session.add(row)
    session.flush()
    return row


def _preview(client: TestClient, period: str = MARCH) -> dict[str, Any]:
    response = client.get(PREVIEW, headers=ME, params={"from": f"{period}-01", "to": "2032-05-01"})
    assert response.status_code == 200, response.text
    return response.json()


def _messages(preview: dict[str, Any]) -> list[str]:
    return [item["message"] for item in preview["findings"] if item["rule_id"] == "ACCESS-006"]


def _month_status(client: TestClient, month: str) -> str:
    response = client.get(f"{MONTHS}/{month}", headers=ME)
    assert response.status_code == 200, response.text
    return response.json()["status"]


def _review(client: TestClient, month: str) -> None:
    response = client.post(f"{MONTHS}/{month}/review", headers=ME, json={"acknowledged": True})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == PeriodStatus.REVIEWED


def _election(client: TestClient, authorization_id: Any, mode: str = "PAY") -> None:
    response = client.post(
        "/api/v1/me/compensation-elections",
        headers=ME,
        json={"context_type": "OVERTIME", "context_id": str(authorization_id), "mode": mode},
    )
    assert response.status_code == 200, response.text


def _sunday_travel(client: TestClient, start: str = f"{MARCH}-06") -> None:
    """A trip whose Sunday (2032-03-07) carries ACTIVE_WORK."""
    trip = client.post(
        TRIPS,
        headers=ME,
        json={
            "start_local": f"{start}T06:00:00",
            "end_local": f"{MARCH}-08T22:00:00",
            "start_origin": "HOME",
        },
    )
    assert trip.status_code == 201, trip.text
    segment = client.post(
        f"{TRIPS}/{trip.json()['id']}/segments",
        headers=ME,
        json={
            "start_local": f"{MARCH}-07T08:00:00",
            "end_local": f"{MARCH}-07T12:00:00",
            "classification": "ACTIVE_WORK",
            "basis": "ACTUAL",
        },
    )
    assert segment.status_code == 201, segment.text


def _travel_findings(client: TestClient) -> list[str]:
    response = client.get(FINDINGS, headers=ME, params={"period": MARCH})
    assert response.status_code == 200, response.text
    return [item["rule_id"] for item in response.json()["findings"]]


# ---------------------------------------------------------------------------
# ACCESS-017 — who may author an authoritative value
# ---------------------------------------------------------------------------


def test_ACCESS_017_an_employee_cannot_create_an_approved_authorization(
    db_client: TestClient,
) -> None:
    """The employee surface cannot even EXPRESS an institutional field.

    ``extra="forbid"`` is what makes this discriminating: pydantic's default is
    ``ignore``, so without it a smuggled ``status``/``authorized_by``/``source``
    would be silently dropped instead of refused.
    """
    for body in (
        {"valid_from": WINDOW_FROM, "status": "APPROVED"},
        {"valid_from": WINDOW_FROM, "authorized_by": "woco"},
        {"valid_from": WINDOW_FROM, "source": "INSTITUTIONAL_APPROVAL"},
        {"valid_from": WINDOW_FROM, "status": "REJECTED"},
    ):
        response = db_client.post(AUTHORIZATIONS, headers=ME, json=body)
        assert response.status_code == 422, (body, response.text)
        assert response.json()["detail"]["code"] == "VALIDATION_FAILED"


def test_ACCESS_017_an_employee_cannot_approve_their_own_request(
    db_client: TestClient,
) -> None:
    created = _request(db_client, valid_from=WINDOW_FROM, valid_to=WINDOW_TO)
    assert created["status"] == "REQUESTED"
    assert created["authorized_by"] is None
    assert created["source"] == EMPLOYEE_SOURCE

    approved = db_client.patch(
        f"{AUTHORIZATIONS}/{created['id']}",
        headers=ME,
        json={"version": created["version"], "status": "APPROVED"},
    )
    assert approved.status_code == 422, approved.text
    assert approved.json()["detail"]["code"] == "VALIDATION_FAILED"

    head = created
    for allowed in ("CANCELLED", "REQUESTED"):
        response = db_client.patch(
            f"{AUTHORIZATIONS}/{head['id']}",
            headers=ME,
            json={"version": head["version"], "status": allowed},
        )
        assert response.status_code == 200, (allowed, response.text)
        assert response.json()["status"] == allowed
        head = response.json()


def test_ACCESS_017_the_employee_write_path_refuses_an_institutionally_decided_chain(
    db_client: TestClient, db_session: Session
) -> None:
    """A decided authorization is not employee-versionable — and nothing changed.

    Refused as 422 ``VALIDATION_FAILED`` rather than 403: ``auth/deps.py``
    documents one byte-identical body for every ``/me/*`` denial and a second 403
    body would break that property.
    """
    created = _request(db_client, valid_from=WINDOW_FROM, valid_to=WINDOW_TO)
    head = decide_authorization(
        db_session, _employee(db_session), uuid.UUID(created["id"]), authorized_by="woco"
    )
    db_session.expire_all()

    for body in (
        {"version": head.version, "status": "CANCELLED"},
        {"version": head.version, "purpose": "employee rewrites the decision"},
        {"version": head.version, "valid_to": f"{MARCH}-15T00:00:00"},
    ):
        response = db_client.patch(f"{AUTHORIZATIONS}/{head.id}", headers=ME, json=body)
        assert response.status_code == 422, (body, response.text)
        assert response.json()["detail"]["code"] == "VALIDATION_FAILED"

    # Re-read through the PRODUCT surface: the head is unchanged and no version
    # was created by the refused commands.
    listed = db_client.get(
        AUTHORIZATIONS, headers=ME, params={"from": f"{MARCH}-01", "to": "2032-05-01"}
    )
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    assert len(rows) == 1, "a refused employee write created a version"
    assert rows[0]["id"] == str(head.id)
    assert rows[0]["version"] == head.version
    assert rows[0]["status"] == "APPROVED"
    assert rows[0]["source"] == INSTITUTIONAL_SOURCE


def test_ACCESS_017_an_unknown_source_is_not_employee_writable(
    db_client: TestClient, db_session: Session
) -> None:
    """Deny by default: an unrecognised provenance is not employee-writable.

    A future adapter's provenance value must not accidentally become writable by
    the employee just because the employee predicate is written as a blocklist.
    """
    row = _legacy_row(
        db_session,
        valid_from=WINDOW_FROM,
        status=OvertimeAuthStatus.REQUESTED,
        source="FUTURE_ADAPTER_V2",
    )
    response = db_client.patch(
        f"{AUTHORIZATIONS}/{row.id}", headers=ME, json={"version": 1, "purpose": "mine now"}
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "VALIDATION_FAILED"


def test_ACCESS_017_the_operator_decision_is_versioned_and_attributed(
    db_client: TestClient, db_session: Session
) -> None:
    created = _request(db_client, valid_from=WINDOW_FROM, valid_to=WINDOW_TO, purpose="release")
    head = decide_authorization(
        db_session,
        _employee(db_session),
        uuid.UUID(created["id"]),
        authorized_by="woco",
        source_ref="CHG-123",
    )
    assert head.version == created["version"] + 1
    assert head.status is OvertimeAuthStatus.APPROVED
    assert head.source == INSTITUTIONAL_SOURCE
    assert head.authorized_by == "woco"
    assert head.source_ref == "CHG-123"
    # The decision must NOT inherit the employee's provenance (the employee
    # correction copies it; the institutional writer sets its own).
    assert head.source != EMPLOYEE_SOURCE

    prior = db_session.get(OvertimeAuthorization, uuid.UUID(created["id"]))
    assert prior is not None
    assert prior.source == EMPLOYEE_SOURCE
    assert prior.status is OvertimeAuthStatus.REQUESTED
    assert prior.supersedes_id is None
    decided = db_session.get(OvertimeAuthorization, head.id)
    assert decided is not None
    assert decided.supersedes_id == prior.id

    # The decision refuses a row that is no longer the chain head, so two
    # decisions cannot fork the chain.
    from app.errors import OvertimeAuthorizationDecisionRefused

    try:
        decide_authorization(
            db_session, _employee(db_session), uuid.UUID(created["id"]), authorized_by="woco"
        )
    except OvertimeAuthorizationDecisionRefused:
        pass
    else:  # pragma: no cover - the refusal is the contract
        raise AssertionError("a superseded row was decided a second time")


def test_ACCESS_017_no_route_can_author_an_authoritative_approval(
    db_client: TestClient,
) -> None:
    """An authority decision has NO HTTP surface at all (ACCESS-017).

    The decision is operator-CLI/adapter only. So no employee-reachable request
    schema may carry an institutional field, and the institutional schema must
    not be referenced by any ``/me/*`` request body.
    """
    spec = cast(FastAPI, db_client.app).openapi()
    schemas = spec["components"]["schemas"]
    referenced: set[str] = set()
    for path, methods in spec["paths"].items():
        assert not any(word in path for word in ("decide", "approve", "reject")), path
        if not path.startswith("/api/v1/me/"):
            continue
        for operation in methods.values():
            body = (operation.get("requestBody") or {}).get("content", {})
            for media in body.values():
                name = media.get("schema", {}).get("$ref", "").rsplit("/", 1)[-1]
                if name:
                    referenced.add(name)
    assert "OvertimeAuthorizationDecisionIn" not in referenced, (
        "the institutional decision schema must not be reachable through /me/*"
    )
    for name in referenced:
        properties = set(schemas.get(name, {}).get("properties", {}))
        assert not properties & {"authorized_by", "source"}, (name, properties)


def test_ACCESS_017_an_employee_cannot_read_another_employees_authorization(
    db_client: TestClient, db_session: Session
) -> None:
    created = _request(db_client, valid_from=WINDOW_FROM)
    other = db_client.get(
        f"{AUTHORIZATIONS}",
        headers=OTHER_EMPLOYEE_HEADER,
        params={"from": f"{MARCH}-01", "to": "2032-05-01"},
    )
    assert other.status_code == 200
    assert [row for row in other.json() if row["id"] == created["id"]] == []


# ---------------------------------------------------------------------------
# ACCESS-006 — the three submission-gate modes
# ---------------------------------------------------------------------------


def test_ACCESS_006_the_three_failure_modes_are_distinct(
    db_client: TestClient, db_session: Session
) -> None:
    """One rule id, three messages — the rule id alone cannot discriminate.

    The pre-existing assertion keys on ``rule_id`` + ``status`` only, which would
    pass for any of the three modes; F6 therefore asserts the MESSAGE.
    """
    unlinked = _interval(db_session, f"{MARCH}-10T08:00:00+01:00", f"{MARCH}-10T09:00:00+01:00")
    assert _messages(_preview(db_client)) == [ACCESS_006_NO_REFERENCE], unlinked.id

    requested = _request(db_client, valid_from=WINDOW_FROM, valid_to=WINDOW_TO)
    linked = _link(db_client, unlinked.id, 1, requested["id"])
    assert linked.status_code == 200, linked.text
    assert _messages(_preview(db_client)) == [ACCESS_006_NOT_AUTHORITATIVE]

    head = decide_authorization(
        db_session,
        _employee(db_session),
        uuid.UUID(requested["id"]),
        authorized_by="woco",
        valid_from=f"{MARCH}-01T00:00:00+01:00",
        valid_to=f"{MARCH}-02T00:00:00+01:00",
    )
    assert _messages(_preview(db_client)) == [ACCESS_006_OUTSIDE_WINDOW], head.id


def test_ACCESS_006_an_interval_outside_the_window_blocks_the_preview(
    db_client: TestClient, db_session: Session
) -> None:
    """Containment, not overlap — and BOTH edges are outside.

    A window of one day: an interval ending exactly at ``valid_from`` and one
    starting exactly at ``valid_to`` are both partially covered claims, and
    ``split_interval`` is the employee's remedy.
    """
    before = _interval(db_session, f"{MARCH}-09T23:00:00+01:00", f"{MARCH}-10T00:00:00+01:00")
    after = _interval(db_session, f"{MARCH}-11T00:00:00+01:00", f"{MARCH}-11T01:00:00+01:00")
    created = _request(db_client, valid_from=f"{MARCH}-10T00:00:00")
    head = decide_authorization(
        db_session,
        _employee(db_session),
        uuid.UUID(created["id"]),
        authorized_by="woco",
        valid_from=f"{MARCH}-10T00:00:00+01:00",
        valid_to=f"{MARCH}-11T00:00:00+01:00",
    )
    for interval in (before, after):
        linked = _link(db_client, interval.id, 1, head.id)
        assert linked.status_code == 200, linked.text

    messages = _messages(_preview(db_client))
    assert messages == [ACCESS_006_OUTSIDE_WINDOW], messages


def test_ACCESS_006_the_window_end_is_exclusive(db_client: TestClient, db_session: Session) -> None:
    """``[valid_from, valid_to)`` — an interval ending AT ``valid_to`` is covered.

    The complement of the containment test: without this, a `<=`/`<` slip would
    make every window one instant short and the previous test could not tell.
    """
    inside = _interval(db_session, f"{MARCH}-10T23:00:00+01:00", f"{MARCH}-11T00:00:00+01:00")
    created = _request(db_client, valid_from=f"{MARCH}-10T00:00:00")
    head = decide_authorization(
        db_session,
        _employee(db_session),
        uuid.UUID(created["id"]),
        authorized_by="woco",
        valid_from=f"{MARCH}-10T00:00:00+01:00",
        valid_to=f"{MARCH}-11T00:00:00+01:00",
    )
    linked = _link(db_client, inside.id, 1, head.id)
    assert linked.status_code == 200, linked.text
    assert _messages(_preview(db_client)) == []


def test_ACCESS_006_a_superseded_link_is_resolved_to_the_head(
    db_client: TestClient, db_session: Session
) -> None:
    """THE stale-link hole: a superseded link used to produce NO finding at all.

    The link names the row the employee attached; the approval supersedes it. The
    preview must walk to the chain head, project it, and judge THAT — otherwise
    the payload carries a ``linked_authorization_id`` resolving to nothing and the
    interval silently passes the gate it should fail (or, as here, fails one it
    should pass).
    """
    interval = _interval(db_session, f"{MARCH}-10T08:00:00+01:00", f"{MARCH}-10T09:00:00+01:00")
    created = _request(db_client, valid_from=WINDOW_FROM, valid_to=WINDOW_TO)
    linked = _link(db_client, interval.id, 1, created["id"])
    assert linked.status_code == 200, linked.text

    head = decide_authorization(
        db_session, _employee(db_session), uuid.UUID(created["id"]), authorized_by="woco"
    )
    preview = _preview(db_client)
    assert _messages(preview) == []
    projection = preview["overtime_intervals"][0]
    assert projection["linked_authorization_id"] == str(head.id), (
        "the superseded link was not resolved to the current chain head"
    )
    assert [row["id"] for row in preview["authorizations"]] == [str(head.id)]
    assert preview["authorizations"][0]["status"] == "APPROVED"

    # And the removal direction: the authority rejects the request it superseded.
    rejected = decide_authorization(
        db_session,
        _employee(db_session),
        head.id,
        status="REJECTED",
        authorized_by="woco",
    )
    preview = _preview(db_client)
    assert _messages(preview) == [ACCESS_006_NOT_AUTHORITATIVE]
    assert preview["overtime_intervals"][0]["linked_authorization_id"] == str(rejected.id)


def test_ACCESS_006_a_requested_authorization_blocks_the_preview_but_not_the_facts(
    db_client: TestClient, db_session: Session
) -> None:
    """The refusal is at the SEAL, never at factual capture (INV-04/INV-15)."""
    interval = _interval(db_session, f"{MARCH}-10T08:00:00+01:00", f"{MARCH}-10T09:00:00+01:00")
    created = _request(db_client, valid_from=WINDOW_FROM, valid_to=WINDOW_TO)
    linked = _link(db_client, interval.id, 1, created["id"])
    assert linked.status_code == 200, linked.text

    assert _messages(_preview(db_client)) == [ACCESS_006_NOT_AUTHORITATIVE]
    day = db_client.get(f"{MONTHS}/{MARCH}/days/{MARCH}-10", headers=ME)
    assert day.status_code == 200, day.text
    # The link is an ordinary versioned correction, so the CURRENT head is the
    # row the link command returned.
    rows = [row for row in day.json()["intervals"] if row["id"] == linked.json()["id"]]
    assert len(rows) == 1, "the blocked preview removed the employee's own fact"
    assert rows[0]["version"] == 2, "the link correction was undone by the blocked preview"


def test_ACCESS_006_a_blocked_preview_refuses_the_package(
    db_client: TestClient, db_session: Session
) -> None:
    """EINV-24: the gate cannot be bypassed by calling the submission directly."""
    from app.disclosure.models import SubmissionPackage

    interval = _interval(db_session, f"{MARCH}-10T08:00:00+01:00", f"{MARCH}-10T09:00:00+01:00")
    created = _request(db_client, valid_from=WINDOW_FROM, valid_to=WINDOW_TO)
    assert _link(db_client, interval.id, 1, created["id"]).status_code == 200
    _election(db_client, created["id"])

    preview = db_client.get(
        PREVIEW, headers=ME, params={"from": f"{MARCH}-01", "to": f"{APRIL}-01"}
    )
    assert preview.status_code == 200, preview.text
    created_package = db_client.post(
        f"{SUBMISSIONS}/OVERTIME_PAID",
        headers=ME,
        json={
            "scope": {"month": MARCH},
            "preview_fingerprint": preview.json()["preview_fingerprint"],
            "client_request_id": str(uuid.uuid4()),
            "recipients": ["recipient"],
        },
    )
    assert created_package.status_code == 422, created_package.text
    assert created_package.json()["detail"]["code"] == "SUBMISSION_MISSING_REQUIRED_DATA"
    assert db_session.scalars(select(SubmissionPackage)).all() == []


# ---------------------------------------------------------------------------
# The one predicate, at EVERY consumer (D2b)
# ---------------------------------------------------------------------------


def test_ACCESS_017_a_legacy_self_declared_approval_is_not_authoritative_anywhere(
    db_client: TestClient, db_session: Session
) -> None:
    """The discriminating regression for D2b, asserted at BOTH consumers.

    A pre-F6 row (``APPROVED`` + ``EMPLOYEE_RECORDED``) used to seal a submission
    *and* suppress a compliance finding. It must now do neither — and the
    positive control (an authoritative row in the same setup) must still do both,
    so the test cannot pass by the finding being globally absent.
    """
    from app.disclosure.models import SubmissionPackage

    _sunday_travel(db_client)
    interval = _interval(db_session, f"{MARCH}-10T08:00:00+01:00", f"{MARCH}-10T09:00:00+01:00")
    legacy = _legacy_row(db_session, valid_from=WINDOW_FROM, valid_to=WINDOW_TO)
    assert _link(db_client, interval.id, 1, legacy.id).status_code == 200
    _election(db_client, legacy.id)

    # (a) the submission gate refuses it
    preview = db_client.get(
        PREVIEW, headers=ME, params={"from": f"{MARCH}-01", "to": f"{APRIL}-01"}
    )
    assert preview.status_code == 200, preview.text
    blocked = db_client.post(
        f"{SUBMISSIONS}/OVERTIME_PAID",
        headers=ME,
        json={
            "scope": {"month": MARCH},
            "preview_fingerprint": preview.json()["preview_fingerprint"],
            "client_request_id": str(uuid.uuid4()),
            "recipients": ["recipient"],
        },
    )
    assert blocked.status_code == 422, blocked.text
    assert db_session.scalars(select(SubmissionPackage)).all() == []

    # (b) it does not suppress the Sunday-travel finding
    assert "TRAVEL-SUNDAY-001" in _travel_findings(db_client)

    # (c) positive control: an AUTHORITATIVE window does suppress it
    authoritative = _legacy_row(
        db_session, valid_from=WINDOW_FROM, valid_to=WINDOW_TO, source=INSTITUTIONAL_SOURCE
    )
    assert "TRAVEL-SUNDAY-001" not in _travel_findings(db_client), authoritative.id


def test_ACCESS_017_an_unknown_provenance_does_not_suppress_the_travel_finding(
    db_client: TestClient, db_session: Session
) -> None:
    """Deny by default at the travel consumer too, not only at the write path."""
    _sunday_travel(db_client)
    _legacy_row(db_session, valid_from=WINDOW_FROM, valid_to=WINDOW_TO, source="FUTURE_ADAPTER_V2")
    assert "TRAVEL-SUNDAY-001" in _travel_findings(db_client)


def test_the_travel_finding_appears_on_a_month_with_a_legacy_row(
    db_client: TestClient, db_session: Session
) -> None:
    """The named D2b consequence, pinned rather than discovered.

    Findings are not a calculation input, so the tightening that adds
    ``TRAVEL-SUNDAY-001`` to a month containing a legacy self-declared row
    reopens NOTHING. A REVIEWED month can therefore display a finding it was
    reviewed without — recorded, never suppressed to avoid it.
    """
    _sunday_travel(db_client)
    legacy = _legacy_row(db_session, valid_from=WINDOW_FROM, valid_to=WINDOW_TO)
    assert "TRAVEL-SUNDAY-001" in _travel_findings(db_client)
    assert _month_status(db_client, MARCH) == PeriodStatus.OPEN

    # The code change reopens nothing: with the legacy row replaced by an
    # authoritative one and the month reviewed, a REVIEWED month stays REVIEWED.
    assert legacy.status is OvertimeAuthStatus.APPROVED
    _review(db_client, MARCH)
    assert _month_status(db_client, MARCH) == PeriodStatus.REVIEWED


def test_correcting_a_linked_interval_still_succeeds_after_a_superseding_decision(
    db_client: TestClient, db_session: Session
) -> None:
    """The explicit guard against reintroducing the link-time trap.

    Enforcing authority at LINK time would break the ordinary workflow: the
    employee links the request, the authority approves (superseding it), and the
    employee must still be able to correct their own interval's times.
    """
    interval = _interval(db_session, f"{MARCH}-10T08:00:00+01:00", f"{MARCH}-10T09:00:00+01:00")
    created = _request(db_client, valid_from=WINDOW_FROM, valid_to=WINDOW_TO)
    linked = _link(db_client, interval.id, 1, created["id"])
    assert linked.status_code == 200, linked.text
    decide_authorization(
        db_session, _employee(db_session), uuid.UUID(created["id"]), authorized_by="woco"
    )

    corrected = db_client.patch(
        f"{INTERVALS}/{linked.json()['id']}",
        headers=ME,
        json={"version": 2, "end_local": f"{MARCH}-10T09:30:00", "comment": "ran long"},
    )
    assert corrected.status_code == 200, corrected.text
    # The link survived the correction and still resolves to the authoritative
    # head, so the preview stays clean (`IntervalOut` does not expose the link;
    # the preview is where it is readable).
    assert _messages(_preview(db_client)) == []
    assert _preview(db_client)["overtime_intervals"][0]["linked_authorization_id"] is not None


# ---------------------------------------------------------------------------
# EINV-15 — the reopen set equals the authoritative input set (D2c)
# ---------------------------------------------------------------------------


def _review_both(client: TestClient) -> None:
    for month in (MARCH, APRIL):
        _review(client, month)


def _reviewed_both(client: TestClient) -> bool:
    return all(_month_status(client, month) == PeriodStatus.REVIEWED for month in (MARCH, APRIL))


def test_EINV_15_the_reopen_set_matches_the_authoritative_input_set(
    db_client: TestClient, db_session: Session
) -> None:
    """A ``source`` change is the transition the input set is keyed on.

    An ``APPROVED``+employee-sourced row reopens NOTHING (it is not an input);
    the same window becoming authoritative reopens its months; and a metadata-only
    restatement of an authoritative head still reopens nothing.
    """
    interval = _interval(db_session, f"{MARCH}-10T08:00:00+01:00", f"{MARCH}-10T16:00:00+01:00")
    assert interval.id is not None
    legacy = _legacy_row(db_session, valid_from=WINDOW_FROM, valid_to=WINDOW_TO)
    _review_both(db_client)
    assert _reviewed_both(db_client)

    # The legacy APPROVED row is not an input: the months stay REVIEWED.
    assert _month_status(db_client, MARCH) == PeriodStatus.REVIEWED
    assert _month_status(db_client, APRIL) == PeriodStatus.REVIEWED

    # Becoming authoritative reopens the window's months.
    head = decide_authorization(db_session, _employee(db_session), legacy.id, authorized_by="woco")
    assert _month_status(db_client, MARCH) == PeriodStatus.OPEN
    assert _month_status(db_client, APRIL) == PeriodStatus.REVIEWED

    # A metadata-only restatement of an authoritative head reopens nothing.
    _review(db_client, MARCH)
    decide_authorization(
        db_session, _employee(db_session), head.id, authorized_by="woco", purpose="clarified"
    )
    assert _month_status(db_client, MARCH) == PeriodStatus.REVIEWED


def test_EINV_15_a_changed_authoritative_window_reopens_the_union(
    db_client: TestClient, db_session: Session
) -> None:
    """``union(window(before), window(after))`` — including the months only OLD covered."""
    _interval(db_session, f"{MARCH}-10T08:00:00+01:00", f"{MARCH}-10T16:00:00+01:00")
    _interval(db_session, f"{APRIL}-10T08:00:00+01:00", f"{APRIL}-10T16:00:00+01:00")
    created = _request(db_client, valid_from=WINDOW_FROM)
    head = decide_authorization(
        db_session,
        _employee(db_session),
        uuid.UUID(created["id"]),
        authorized_by="woco",
        valid_from=f"{MARCH}-01T00:00:00+01:00",
        valid_to=f"{APRIL}-01T00:00:00+02:00",
    )
    _review_both(db_client)
    assert _reviewed_both(db_client)

    decide_authorization(
        db_session,
        _employee(db_session),
        head.id,
        authorized_by="woco",
        valid_from=f"{APRIL}-01T00:00:00+02:00",
        valid_to="2032-05-01T00:00:00+02:00",
    )
    assert _month_status(db_client, MARCH) == PeriodStatus.OPEN, (
        "the month only the OLD authoritative window covered did not reopen"
    )
    assert _month_status(db_client, APRIL) == PeriodStatus.OPEN


def test_EINV_15_a_non_authoritative_decision_reopens_nothing(
    db_client: TestClient, db_session: Session
) -> None:
    """``None -> None`` reopens nothing, even when the window moved.

    The employee path can only ever produce this shape, so an ordinary request,
    cancel or window correction must leave review markers alone.
    """
    _interval(db_session, f"{MARCH}-10T08:00:00+01:00", f"{MARCH}-10T16:00:00+01:00")
    _interval(db_session, f"{APRIL}-10T08:00:00+01:00", f"{APRIL}-10T16:00:00+01:00")
    legacy = _legacy_row(db_session, valid_from=WINDOW_FROM, valid_to=WINDOW_TO)
    _review_both(db_client)

    moved = db_client.patch(
        f"{AUTHORIZATIONS}/{legacy.id}",
        headers=ME,
        json={
            "version": 1,
            "valid_from": f"{APRIL}-01T00:00:00",
            "valid_to": "2032-05-01T00:00:00",
        },
    )
    assert moved.status_code == 200, moved.text
    assert _reviewed_both(db_client), "a non-authoritative row reopened a reviewed month"


def test_EINV_15_approving_reopens_the_new_window(
    db_client: TestClient, db_session: Session
) -> None:
    """``REJECTED -> APPROVED`` (authoritative) reopens the new window."""
    _interval(db_session, f"{MARCH}-10T08:00:00+01:00", f"{MARCH}-10T16:00:00+01:00")
    _interval(db_session, f"{APRIL}-10T08:00:00+01:00", f"{APRIL}-10T16:00:00+01:00")
    created = _request(db_client, valid_from=WINDOW_FROM)
    rejected = decide_authorization(
        db_session,
        _employee(db_session),
        uuid.UUID(created["id"]),
        status="REJECTED",
        authorized_by="woco",
        valid_from=f"{MARCH}-01T00:00:00+01:00",
        valid_to=f"{APRIL}-01T00:00:00+02:00",
    )
    _review_both(db_client)
    assert _reviewed_both(db_client), "a REJECTED row reopened a reviewed month"

    decide_authorization(db_session, _employee(db_session), rejected.id, authorized_by="woco")
    assert _month_status(db_client, MARCH) == PeriodStatus.OPEN
    assert _month_status(db_client, APRIL) == PeriodStatus.REVIEWED


# ---------------------------------------------------------------------------
# Concurrency — the chain cannot fork, and a loser never acknowledges success
# ---------------------------------------------------------------------------


def _committed_request(engine: Engine) -> tuple[uuid.UUID, uuid.UUID]:
    """A committed REQUESTED authorization for the seeded employee.

    The race tests need rows both real sessions can see, so they commit and clean
    up (the ``test_identity_admin_cli`` / ``test_review_surface`` pattern).
    """
    with Session(engine) as session, session.begin():
        employee = session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
        assert employee is not None
        row = OvertimeAuthorization(
            employee_id=employee.id,
            valid_from=_berlin(WINDOW_FROM),
            valid_to=_berlin(WINDOW_TO),
            status=OvertimeAuthStatus.REQUESTED,
            source=EMPLOYEE_SOURCE,
            version=1,
        )
        session.add(row)
        session.flush()
        return employee.id, row.id


def _cleanup_authorizations(engine: Engine, root_id: uuid.UUID) -> None:
    """Delete the chain this test can have written, leaves first."""
    with Session(engine) as session, session.begin():
        ids = [root_id]
        while True:
            children = [
                value
                for value in session.scalars(
                    select(OvertimeAuthorization.id).where(
                        OvertimeAuthorization.supersedes_id.in_(ids)
                    )
                ).all()
                if value not in ids
            ]
            if not children:
                break
            ids.extend(children)
        session.execute(
            delete(AuditEvent).where(
                AuditEvent.action == "OVERTIME_AUTHORIZATION_DECIDE",
                AuditEvent.object_id.in_(ids),
            )
        )
        for value in reversed(ids):
            session.execute(delete(OvertimeAuthorization).where(OvertimeAuthorization.id == value))


def _decide_institutionally(
    session: Session, employee_id: uuid.UUID, authorization_id: uuid.UUID, **decision: Any
) -> Any:
    """The SERVICE writer, resolved through a real ``Employee`` instance.

    The module-level ``decide_authorization`` name is the test helper (it takes an
    ``Employee``), so the concurrency tests go through this one wrapper rather than
    shadowing either.
    """
    employee = session.get(Employee, employee_id)
    assert employee is not None
    return decide_authorization(session, employee, authorization_id, **decision)


def _wait_until_blocked(engine: Engine, pid: int, timeout: float = 15.0) -> bool:
    """Wait until the given backend is OBSERVED waiting on a lock (never a sleep)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as probe:
            waiting = probe.scalar(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": pid},
            )
        if waiting == "Lock":
            return True
        time.sleep(0.02)
    return False


def _successors(engine: Engine, root_id: uuid.UUID) -> list[OvertimeAuthorization]:
    with Session(engine) as session:
        return list(
            session.scalars(
                select(OvertimeAuthorization).where(OvertimeAuthorization.supersedes_id == root_id)
            ).all()
        )


def test_employee_patch_and_institutional_decision_cannot_fork_the_chain(
    db_engine: Engine,
) -> None:
    """The institutional writer LOSES the race and is refused, not silently applied.

    Both writers take ``lock_employee_facts`` first, so they serialize; the loser
    re-reads a head that already has a successor and refuses.
    ``uq_overtime_authorization_supersedes_id`` makes two successors impossible,
    so exactly one version survives either way.
    """
    from app.errors import OvertimeAuthorizationDecisionRefused
    from app.ocd_overtime.service import patch_authorization

    employee_id, root_id = _committed_request(db_engine)
    employee_session = Session(db_engine)
    worker = Session(db_engine)
    try:
        worker_pid = int(worker.scalar(text("SELECT pg_backend_pid()")))
        employee_session.begin()
        lock_employee_facts(employee_session, employee_id)
        outcome: list[str] = []

        def decide() -> None:
            try:
                _decide_institutionally(worker, employee_id, root_id, authorized_by="woco")
                worker.commit()
                outcome.append("decided")
            except OvertimeAuthorizationDecisionRefused:
                worker.rollback()
                outcome.append("refused")
            except Exception as exc:  # pragma: no cover - failure path
                worker.rollback()
                outcome.append(f"error:{type(exc).__name__}")

        thread = threading.Thread(target=decide)
        thread.start()
        assert _wait_until_blocked(db_engine, worker_pid), (
            "the institutional decision never waited on the employee-fact lock"
        )
        employee = employee_session.get(Employee, employee_id)
        assert employee is not None
        patch_authorization(
            employee_session,
            employee,
            root_id,
            _patch(version=1, purpose="employee wins"),
        )
        employee_session.commit()
        thread.join(timeout=30)
        assert not thread.is_alive()
        assert outcome == ["refused"], outcome

        successors = _successors(db_engine, root_id)
        assert len(successors) == 1, "the chain forked"
        assert successors[0].source == EMPLOYEE_SOURCE
    finally:
        employee_session.close()
        worker.close()
        _cleanup_authorizations(db_engine, root_id)


def test_a_losing_concurrent_write_never_acknowledges_success(db_engine: Engine) -> None:
    """The same interleave from the RESPONSE side: the employee loses.

    The employee's stale correction must not be acknowledged: it is refused with
    the exact non-disclosing 404 (the row it targets is no longer the head), and
    no row it wrote survives.
    """
    from app.ocd_overtime.service import patch_authorization
    from fastapi import HTTPException

    employee_id, root_id = _committed_request(db_engine)
    decider = Session(db_engine)
    worker = Session(db_engine)
    try:
        worker_pid = int(worker.scalar(text("SELECT pg_backend_pid()")))
        decider.begin()
        lock_employee_facts(decider, employee_id)
        outcome: list[str] = []

        def correct() -> None:
            try:
                employee = worker.get(Employee, employee_id)
                assert employee is not None
                patch_authorization(worker, employee, root_id, _patch(version=1, purpose="stale"))
                worker.commit()
                outcome.append("patched")
            except HTTPException as exc:
                worker.rollback()
                outcome.append(f"http:{exc.status_code}")
            except Exception as exc:  # pragma: no cover - failure path
                worker.rollback()
                outcome.append(f"error:{type(exc).__name__}")

        thread = threading.Thread(target=correct)
        thread.start()
        assert _wait_until_blocked(db_engine, worker_pid), (
            "the employee correction never waited on the employee-fact lock"
        )
        _decide_institutionally(decider, employee_id, root_id, authorized_by="woco")
        decider.commit()
        thread.join(timeout=30)
        assert not thread.is_alive()
        assert outcome == ["http:404"], outcome

        successors = _successors(db_engine, root_id)
        assert len(successors) == 1
        assert successors[0].source == INSTITUTIONAL_SOURCE
    finally:
        decider.close()
        worker.close()
        _cleanup_authorizations(db_engine, root_id)


def _patch(**body: Any) -> Any:
    from app.ocd_overtime.schemas import OvertimeAuthorizationPatch

    return OvertimeAuthorizationPatch(**body)


# ---------------------------------------------------------------------------
# The operator CLI — the concrete trusted writer
# ---------------------------------------------------------------------------


def test_ACCESS_017_the_approval_is_durable_before_the_cli_reports_success(
    db_engine: Engine, capsys: Any
) -> None:
    """The CLI owns its transaction: it commits BEFORE printing success.

    A separate session reads the decision afterwards, so a missing commit cannot
    hide behind a dependency teardown (there is none — it is a CLI).
    """
    from app.overtime_admin import main as cli

    employee_id, root_id = _committed_request(db_engine)
    try:
        assert (
            cli(
                [
                    "decide",
                    "--employee-id",
                    str(employee_id),
                    "--authorization",
                    str(root_id),
                    "--status",
                    "APPROVED",
                    "--authorized-by",
                    "woco",
                ]
            )
            == 0
        )
        assert "decide OK" in capsys.readouterr().out
        with Session(db_engine) as fresh:
            head = fresh.scalar(
                select(OvertimeAuthorization).where(OvertimeAuthorization.supersedes_id == root_id)
            )
        assert head is not None, "the CLI reported success on an uncommitted decision"
        assert head.status is OvertimeAuthStatus.APPROVED
        assert head.source == INSTITUTIONAL_SOURCE
        assert head.authorized_by == "woco"
    finally:
        _cleanup_authorizations(db_engine, root_id)


def test_ACCESS_017_the_cli_dry_run_writes_nothing(db_engine: Engine, capsys: Any) -> None:
    from app.overtime_admin import main as cli

    employee_id, root_id = _committed_request(db_engine)
    try:
        assert (
            cli(
                [
                    "decide",
                    "--employee-id",
                    str(employee_id),
                    "--authorization",
                    str(root_id),
                    "--status",
                    "REJECTED",
                    "--authorized-by",
                    "woco",
                    "--dry-run",
                ]
            )
            == 0
        )
        assert "nothing written" in capsys.readouterr().out
        assert _successors(db_engine, root_id) == []

        # A superseded target is refused with exit code 2, never a partial write.
        assert (
            cli(
                [
                    "decide",
                    "--employee-id",
                    str(employee_id),
                    "--authorization",
                    str(uuid.uuid4()),
                    "--status",
                    "APPROVED",
                    "--authorized-by",
                    "woco",
                ]
            )
            == 2
        )
        assert _successors(db_engine, root_id) == []
    finally:
        _cleanup_authorizations(db_engine, root_id)


# ---------------------------------------------------------------------------
# Reviewer round 1 — the two backend findings
# ---------------------------------------------------------------------------


def test_ACCESS_017_the_institutional_decision_is_audited(
    db_client: TestClient, db_session: Session
) -> None:
    """Every sibling operator writer records its action; so must this one.

    The decision changes an authoritative chain and is taken by an operator, so
    the accountable record cannot be the row alone. The audit row is written in
    the SAME transaction as the decision, and a refused decision writes none.
    """
    created = _request(db_client, valid_from=WINDOW_FROM)
    head = decide_authorization(
        db_session,
        _employee(db_session),
        uuid.UUID(created["id"]),
        authorized_by="woco",
        actor="OVERTIME_CLI",
    )
    db_session.flush()
    events = db_session.scalars(
        select(AuditEvent).where(
            AuditEvent.action == "OVERTIME_AUTHORIZATION_DECIDE",
            AuditEvent.object_id == head.id,
        )
    ).all()
    assert len(events) == 1, events
    assert events[0].actor_principal == "OVERTIME_CLI"
    assert events[0].outcome == "SUCCESS"
    assert events[0].employee_subject_id == _employee(db_session).id
    # No employee comment/reason text can reach the audit metadata (DP-LOG-001).
    assert events[0].metadata_json is None

    from app.errors import OvertimeAuthorizationDecisionRefused

    with contextlib.suppress(OvertimeAuthorizationDecisionRefused):
        decide_authorization(
            db_session, _employee(db_session), uuid.UUID(created["id"]), authorized_by="woco"
        )
    db_session.flush()
    assert (
        len(
            db_session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "OVERTIME_AUTHORIZATION_DECIDE",
                    AuditEvent.object_id == head.id,
                )
            ).all()
        )
        == 1
    ), "a refused decision wrote an audit row"


def test_the_compensation_election_survives_an_institutional_decision(
    db_client: TestClient, db_session: Session
) -> None:
    """request -> election -> approval -> submit must work.

    The election is bound to the row the employee selected it on, and the decision
    SUPERSEDES that row. Resolving the election on the head alone loses it and
    refuses an ordinary submission for a missing election — so it is resolved over
    the chain, head first.
    """
    interval = _interval(db_session, f"{MARCH}-10T08:00:00+01:00", f"{MARCH}-10T09:00:00+01:00")
    created = _request(db_client, valid_from=WINDOW_FROM, valid_to=WINDOW_TO)
    _election(db_client, created["id"], mode="PAY")
    assert _link(db_client, interval.id, 1, created["id"]).status_code == 200

    head = decide_authorization(
        db_session, _employee(db_session), uuid.UUID(created["id"]), authorized_by="woco"
    )
    preview = db_client.get(
        PREVIEW, headers=ME, params={"from": f"{MARCH}-01", "to": f"{APRIL}-01"}
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["election"]["mode"] == "PAY", "the election was lost with the request"
    assert _messages(preview.json()) == []

    submitted = db_client.post(
        f"{SUBMISSIONS}/OVERTIME_PAID",
        headers=ME,
        json={
            "scope": {"month": MARCH},
            "preview_fingerprint": preview.json()["preview_fingerprint"],
            "client_request_id": str(uuid.uuid4()),
            "recipients": ["recipient"],
        },
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["payload"]["overtime_intervals"][0]["linked_authorization_id"] == str(
        head.id
    )


# ---------------------------------------------------------------------------
# Reviewer round 2 — the chain ROOT walk and the CLI wall-time resolution
# ---------------------------------------------------------------------------


def test_the_compensation_election_survives_a_correction_before_the_link(
    db_client: TestClient, db_session: Session
) -> None:
    """request -> election -> CORRECT -> link the corrected head -> approve -> submit.

    One chain is one logical authorization and the employee may attach the link to
    ANY version of it, so an election recorded on an EARLIER version than the
    linked one must still be found. Walking successors forward from the LINKED row
    alone (instead of from the chain's root) loses it and refuses an ordinary
    submission for a missing election.
    """
    interval = _interval(db_session, f"{MARCH}-12T08:00:00+01:00", f"{MARCH}-12T09:00:00+01:00")
    created = _request(db_client, valid_from=WINDOW_FROM, valid_to=WINDOW_TO)
    _election(db_client, created["id"], mode="PAY")

    # The employee corrects their own request. This SUPERSEDES `created`, and the
    # election stays bound to the row it was selected on.
    corrected = db_client.patch(
        f"{AUTHORIZATIONS}/{created['id']}",
        headers=ME,
        json={"version": 1, "purpose": "Beamtime support (corrected)"},
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["id"] != created["id"], "a correction must version the row"
    # The link names the CURRENT head — the corrected row, which is NOT the row
    # carrying the election.
    assert _link(db_client, interval.id, 1, corrected.json()["id"]).status_code == 200

    head = decide_authorization(
        db_session, _employee(db_session), uuid.UUID(corrected.json()["id"]), authorized_by="woco"
    )
    preview = db_client.get(
        PREVIEW, headers=ME, params={"from": f"{MARCH}-01", "to": f"{APRIL}-01"}
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["election"] is not None, (
        "the election was lost when the request was corrected before the link"
    )
    assert preview.json()["election"]["mode"] == "PAY"
    assert _messages(preview.json()) == []

    submitted = db_client.post(
        f"{SUBMISSIONS}/OVERTIME_PAID",
        headers=ME,
        json={
            "scope": {"month": MARCH},
            "preview_fingerprint": preview.json()["preview_fingerprint"],
            "client_request_id": str(uuid.uuid4()),
            "recipients": ["recipient"],
        },
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["payload"]["overtime_intervals"][0]["linked_authorization_id"] == str(
        head.id
    )


def test_the_cli_refuses_an_unmappable_or_ambiguous_berlin_wall_time(capsys: Any) -> None:
    """A naive --from/--to is a Berlin wall time, and a DST transition makes some
    wall times unmappable or double-valued. Stamping BERLIN on directly accepts
    2032-03-28T02:30 (an hour that does not exist) and silently picks one of the
    two occurrences of 2032-10-31T02:30 — a wrong window, which `_covers` and the
    travel suppression both read.
    """
    import argparse

    from app.overtime_admin import _parse_instant
    from app.overtime_admin import main as cli

    parser = argparse.ArgumentParser()
    # An unambiguous naive wall time still resolves (the operator need not always
    # write an offset), and an explicit offset is taken as given.
    summer = _parse_instant(parser, "--from", "2032-06-01T00:00")
    assert summer is not None and summer.utcoffset() == timedelta(hours=2)
    explicit = _parse_instant(parser, "--from", "2032-11-01T00:00:00+01:00")
    assert explicit is not None and explicit.utcoffset() == timedelta(hours=1)

    base = [
        "decide",
        "--employee-id",
        str(uuid.uuid4()),
        "--authorization",
        str(uuid.uuid4()),
        "--status",
        "APPROVED",
        "--authorized-by",
        "woco",
    ]
    # 2032-03-28 is the spring-forward Sunday (02:00-02:59 does not exist) and
    # 2032-10-31 the fall-back Sunday (02:00-02:59 happens twice).
    for value in ("2032-03-28T02:30", "2032-10-31T02:30"):
        with pytest.raises(SystemExit) as exit_info:
            cli([*base, "--from", value])
        assert exit_info.value.code == 2
        assert "explicit UTC offset" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Reviewer round 3 — the chain walk is employee-scoped in BOTH directions
# ---------------------------------------------------------------------------


def test_a_cross_employee_successor_is_not_followed(
    db_client: TestClient, db_session: Session
) -> None:
    """``supersedes_id`` links a row to the version it replaces, so the DATABASE
    must refuse an edge that crosses employees (`EINV-26`, migration `0017`): a
    restored or imported row belonging to another employee superseding this one's
    request would resolve a foreign institutional approval as this employee's
    head, expose it in their projection and let it authorize their submission.

    The test pins BOTH halves — the storage guarantee, and the reader behaviour
    that must still hold for a database restored before the constraint existed.
    That state is forged by dropping the FK inside the test transaction
    (transactional DDL: the rollback restores it).
    """
    from app.disclosure.models import SubmissionPackage

    interval = _interval(db_session, f"{MARCH}-14T08:00:00+01:00", f"{MARCH}-14T09:00:00+01:00")
    created = _request(db_client, valid_from=WINDOW_FROM, valid_to=WINDOW_TO)
    _election(db_client, created["id"], mode="PAY")
    assert _link(db_client, interval.id, 1, created["id"]).status_code == 200

    def _forge() -> OvertimeAuthorization:
        """A row the storage layer must refuse and no application writer produces."""
        return OvertimeAuthorization(
            employee_id=_employee(db_session, "employee-b").id,
            valid_from=_berlin(WINDOW_FROM),
            valid_to=_berlin(WINDOW_TO),
            status=OvertimeAuthStatus.APPROVED,
            source=INSTITUTIONAL_SOURCE,
            authorized_by="attacker",
            version=2,
            supersedes_id=uuid.UUID(created["id"]),
        )

    # Storage half: the cross-employee edge cannot be written at all.
    with pytest.raises(IntegrityError) as refused, db_session.begin_nested():
        db_session.add(_forge())
    assert constraint_name(refused.value) == "fk_overtime_authorization_supersedes_same_owner", (
        refused.value
    )

    # Reader half: a database restored before `0017` can still carry the edge, so
    # the walk must stay employee-scoped in BOTH directions.
    db_session.execute(
        text(
            "ALTER TABLE overtime_authorization "
            "DROP CONSTRAINT fk_overtime_authorization_supersedes_same_owner"
        )
    )
    forged = _forge()
    db_session.add(forged)
    db_session.flush()

    preview = db_client.get(
        PREVIEW, headers=ME, params={"from": f"{MARCH}-01", "to": f"{APRIL}-01"}
    )
    assert preview.status_code == 200, preview.text
    assert str(forged.id) not in preview.text, "another employee's approval was exposed"
    assert [row["id"] for row in preview.json()["authorizations"]] == [created["id"]]
    # The linked row itself is not an institutional approval, so the interval is
    # blocked rather than authorized by the foreign decision.
    assert _messages(preview.json()) == [ACCESS_006_NOT_AUTHORITATIVE]

    refused = db_client.post(
        f"{SUBMISSIONS}/OVERTIME_PAID",
        headers=ME,
        json={
            "scope": {"month": MARCH},
            "preview_fingerprint": preview.json()["preview_fingerprint"],
            "client_request_id": str(uuid.uuid4()),
            "recipients": ["recipient"],
        },
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "SUBMISSION_MISSING_REQUIRED_DATA"
    assert db_session.scalars(select(SubmissionPackage)).all() == []

    # The foreign row must not make the employee's OWN row non-current either:
    # an unscoped anti-join would hide it from the list and refuse the link.
    listed = db_client.get(
        AUTHORIZATIONS, headers=ME, params={"from": f"{MARCH}-01", "to": f"{APRIL}-01"}
    )
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()] == [created["id"]]
    second = _interval(db_session, f"{MARCH}-15T08:00:00+01:00", f"{MARCH}-15T09:00:00+01:00")
    assert _link(db_client, second.id, 1, created["id"]).status_code == 200


def test_the_restore_verifier_reports_a_cross_employee_lineage(
    db_client: TestClient, db_session: Session
) -> None:
    """The read-path guards fail closed, and the operator must also SEE the corrupt
    state. `EINV-26`'s constraint now refuses the edge at write time, but a
    database restored before `0017` can still carry it, so only the verifier can
    tell that a restored or hand-edited row supersedes ANOTHER employee's row —
    and it tells for every versioned table at once, derived from the metadata, so
    a table added later cannot be forgotten. The state is forged by dropping the
    FK inside the test transaction (transactional DDL).
    """
    from app.backup import _domain_checks

    created = _request(db_client, valid_from=WINDOW_FROM, valid_to=WINDOW_TO)
    db_session.flush()
    db_session.expire_all()
    clean = _domain_checks(db_session)["overtime"]
    assert not [entry for entry in clean if "another employee" in entry], clean

    db_session.execute(
        text(
            "ALTER TABLE overtime_authorization "
            "DROP CONSTRAINT fk_overtime_authorization_supersedes_same_owner"
        )
    )
    db_session.add(
        OvertimeAuthorization(
            employee_id=_employee(db_session, "employee-b").id,
            valid_from=_berlin(WINDOW_FROM),
            valid_to=_berlin(WINDOW_TO),
            status=OvertimeAuthStatus.APPROVED,
            source=INSTITUTIONAL_SOURCE,
            authorized_by="attacker",
            version=2,
            supersedes_id=uuid.UUID(created["id"]),
        )
    )
    db_session.flush()
    db_session.expire_all()

    failures = _domain_checks(db_session)["overtime"]
    assert any("another employee" in entry for entry in failures), failures


# ---------------------------------------------------------------------------
# WT-CORE-009 — prior-ness of the overtime authorization (POL-09)
# ---------------------------------------------------------------------------
#
# ``POL-09`` requires overtime to be authorized in advance, with a retroactive
# exception inside a bounded window. Only the prior-ness half is live, as an
# employee-private WARNING; the numeric four-week bound is BLOCKED (its
# measurement origin is unstated — OPEN-20), so nothing here asserts a bound.

#: A month the rule governs (the overtime policy is in force from 2026-06-01) and one
#: clear of the seeded and demo months. Every fixture below writes the VERSION
#: RECORDED TIMESTAMP explicitly: ``created_at`` is ``server_default=now()``, so
#: the real clock cannot express the as-of orderings this rule must
#: discriminate — every row a test writes would share one instant.
SEPT = "2032-09"
OCT = "2032-10"
SEPT_FROM = f"{SEPT}-01"
OCT_FROM = f"{OCT}-01"
SEPT_WINDOW_FROM = f"{SEPT}-01T00:00:00"
SEPT_WINDOW_TO = f"{OCT}-01T00:00:00"
WORK_FROM = f"{SEPT}-08T09:00:00+02:00"
WORK_TO = f"{SEPT}-08T17:00:00+02:00"
RECORDED_BEFORE = f"{SEPT}-01T00:00:00+02:00"
RECORDED_AFTER = f"{SEPT}-10T00:00:00+02:00"

#: A month the rule does NOT govern, and the reachable one: the calendar edition
#: covers it from 2026-01-01 (so the preview computes normally), while the rule's
#: own source is in force only from 2026-06-01. There is no pre-cutover write
#: guard, so a period before the rule's ``valid_from`` is genuinely reachable.
PRE_RULE = "2026-03"


def _auth_version(
    session: Session,
    *,
    recorded_at: str,
    valid_from: str = SEPT_WINDOW_FROM,
    valid_to: str | None = SEPT_WINDOW_TO,
    status: OvertimeAuthStatus = OvertimeAuthStatus.APPROVED,
    source: str = INSTITUTIONAL_SOURCE,
    supersedes: OvertimeAuthorization | None = None,
) -> OvertimeAuthorization:
    """One chain version with an EXPLICIT version-recorded timestamp.

    ``created_at`` carries no ``onupdate`` and no application path rewrites it,
    so setting it here is a fixture concern rather than a production mutation.
    """
    row = OvertimeAuthorization(
        employee_id=_employee(session).id,
        valid_from=_berlin(valid_from),
        valid_to=_berlin(valid_to) if valid_to else None,
        status=status,
        source=source,
        authorized_by="woco" if source == INSTITUTIONAL_SOURCE else None,
        version=1 if supersedes is None else supersedes.version + 1,
        supersedes_id=None if supersedes is None else supersedes.id,
        created_at=_berlin(recorded_at),
    )
    session.add(row)
    session.flush()
    return row


def _wt_core_009(preview: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in preview["findings"] if item["rule_id"] == "WT-CORE-009"]


def _preview_window(client: TestClient, start: str, end: str) -> dict[str, Any]:
    """``_preview`` with an explicit range (its ``to`` is pinned to MARCH+2)."""
    response = client.get(PREVIEW, headers=ME, params={"from": start, "to": end})
    assert response.status_code == 200, response.text
    return response.json()


def test_WT_CORE_009_a_decision_recorded_after_the_work_is_reported(
    db_client: TestClient, db_session: Session
) -> None:
    """§16.1 — the institutional decision postdates the work, so it is retroactive.

    ``ACCESS-006`` PASSES here (the chain head IS an authoritative approval that
    covers the interval), so the advisory is the only finding: the interval stays
    submittable, and the employee is told the authorization was retroactive.
    """
    interval = _interval(db_session, WORK_FROM, WORK_TO)
    requested = _auth_version(
        db_session,
        recorded_at=RECORDED_BEFORE,
        status=OvertimeAuthStatus.REQUESTED,
        source=EMPLOYEE_SOURCE,
    )
    decision = _auth_version(db_session, recorded_at=RECORDED_AFTER, supersedes=requested)
    assert _link(db_client, interval.id, 1, decision.id).status_code == 200

    preview = _preview_window(db_client, SEPT_FROM, OCT_FROM)
    assert _messages(preview) == [], "the head covers the work, so ACCESS-006 passes"
    found = _wt_core_009(preview)
    assert len(found) == 1, found
    assert found[0]["status"] == "WARNING", "prior-ness is advisory, never a refusal"
    assert found[0]["message"] == WT_CORE_009_NOT_PRIOR_AUTHORIZED


def test_WT_CORE_009_an_interval_with_no_reference_is_also_reported(
    db_client: TestClient, db_session: Session
) -> None:
    """No reference satisfies NEITHER half of the predicate, so the advisory fires.

    An interval with no linked authorization is not prior-authorized — it is not
    authorized at all. ``ACCESS-006`` mode (i) already refuses the submission, and
    the advisory is emitted ALONGSIDE it: the refusal is about the seal, the
    advisory is about the rule. The advisory must never be the blocking finding.
    """
    _interval(db_session, WORK_FROM, WORK_TO)
    preview = _preview_window(db_client, SEPT_FROM, OCT_FROM)
    assert _messages(preview) == [ACCESS_006_NO_REFERENCE]
    found = _wt_core_009(preview)
    assert len(found) == 1, found
    assert found[0]["status"] == "WARNING"
    blocked = [
        item["rule_id"] for item in preview["findings"] if item["status"] == "BLOCKED_POLICY"
    ]
    assert blocked == ["ACCESS-006"], "the advisory became a blocking finding"


def test_WT_CORE_009_no_warning_when_an_approval_already_covered_the_work(
    db_client: TestClient, db_session: Session
) -> None:
    """§16.2 — recorded before the work, and its own window covers it completely."""
    interval = _interval(db_session, WORK_FROM, WORK_TO)
    approval = _auth_version(db_session, recorded_at=RECORDED_BEFORE)
    assert _link(db_client, interval.id, 1, approval.id).status_code == 200
    assert _wt_core_009(_preview_window(db_client, SEPT_FROM, OCT_FROM)) == []


def test_WT_CORE_009_as_of_control_a_post_work_extension_still_warns(
    db_client: TestClient, db_session: Session
) -> None:
    """§16.3 — the counterexample that kills ``min(created_at)``.

    An approval covering the work is recorded BEFORE the work; a re-decision
    recorded before the work NARROWS the window so the work is no longer covered;
    only after the work does a third decision extend it back over the work. The
    Sep 8 work was therefore never authorized when it began.

    The as-of state must be the LATEST version recorded at or before the work's
    start. Taking the EARLIEST (``min(created_at)``) sees the original covering
    approval and SUPPRESSES the warning; taking the chain HEAD sees the extension
    and does the same. Only the as-of head gets it right.
    """
    interval = _interval(db_session, WORK_FROM, WORK_TO)
    original = _auth_version(db_session, recorded_at=RECORDED_BEFORE)
    narrowed = _auth_version(
        db_session,
        recorded_at=f"{SEPT}-05T00:00:00+02:00",
        valid_to=f"{SEPT}-06T00:00:00+02:00",
        supersedes=original,
    )
    extension = _auth_version(db_session, recorded_at=RECORDED_AFTER, supersedes=narrowed)
    assert _link(db_client, interval.id, 1, extension.id).status_code == 200

    preview = _preview_window(db_client, SEPT_FROM, OCT_FROM)
    assert _messages(preview) == [], "the extension makes the head cover the work"
    assert len(_wt_core_009(preview)) == 1


def test_WT_CORE_009_as_of_control_a_withdrawal_before_the_work_still_warns(
    db_client: TestClient, db_session: Session
) -> None:
    """§16.4 — a historic approval cannot launder a later one.

    The approval is recorded, REJECTED before the work starts, and a new approval
    is recorded after it. The earliest historic approval must not make the work
    look prior-authorized.
    """
    interval = _interval(db_session, WORK_FROM, WORK_TO)
    approval = _auth_version(db_session, recorded_at=RECORDED_BEFORE)
    withdrawal = _auth_version(
        db_session,
        recorded_at=f"{SEPT}-05T00:00:00+02:00",
        status=OvertimeAuthStatus.REJECTED,
        supersedes=approval,
    )
    reapproval = _auth_version(db_session, recorded_at=RECORDED_AFTER, supersedes=withdrawal)
    assert _link(db_client, interval.id, 1, reapproval.id).status_code == 200

    preview = _preview_window(db_client, SEPT_FROM, OCT_FROM)
    assert _messages(preview) == [], "the final re-approval covers the work"
    assert len(_wt_core_009(preview)) == 1


def test_WT_CORE_009_as_of_control_a_later_re_decision_does_not_invent_a_warning(
    db_client: TestClient, db_session: Session
) -> None:
    """§16.5 — a genuine covering approval is not broken by a later re-decision.

    The case the as-of reconstruction must NOT break: an authoritative approval
    that already covered the work is followed by a later re-decision, which must
    not invalidate the prior-ness that genuinely existed when the work began.
    """
    interval = _interval(db_session, WORK_FROM, WORK_TO)
    original = _auth_version(db_session, recorded_at=RECORDED_BEFORE)
    redecision = _auth_version(db_session, recorded_at=RECORDED_AFTER, supersedes=original)
    assert _link(db_client, interval.id, 1, redecision.id).status_code == 200
    assert _wt_core_009(_preview_window(db_client, SEPT_FROM, OCT_FROM)) == []


def test_WT_CORE_009_never_blocks_submission_or_reopens_a_reviewed_month(
    db_client: TestClient, db_session: Session
) -> None:
    """§16.6 + ``EINV-25`` — advisory, and findings are not calculation inputs.

    A case the shipped rule set explicitly PERMITS (retroactive authorization) must not
    be forbidden, so the month stays reviewable and submittable with the warning
    present — and reporting it must not reopen the month.
    """
    interval = _interval(db_session, WORK_FROM, WORK_TO)
    requested = _auth_version(
        db_session,
        recorded_at=RECORDED_BEFORE,
        status=OvertimeAuthStatus.REQUESTED,
        source=EMPLOYEE_SOURCE,
    )
    decision = _auth_version(db_session, recorded_at=RECORDED_AFTER, supersedes=requested)
    assert _link(db_client, interval.id, 1, decision.id).status_code == 200
    _election(db_client, decision.id, mode="PAY")

    preview = _preview_window(db_client, SEPT_FROM, OCT_FROM)
    assert _messages(preview) == [], "the interval is not blocked by policy"
    assert len(_wt_core_009(preview)) == 1

    _review(db_client, SEPT)
    assert _month_status(db_client, SEPT) == PeriodStatus.REVIEWED
    after = _preview_window(db_client, SEPT_FROM, OCT_FROM)
    assert len(_wt_core_009(after)) == 1, "the advisory is still reported"
    assert _month_status(db_client, SEPT) == PeriodStatus.REVIEWED, "a finding reopened the month"

    submitted = db_client.post(
        f"{SUBMISSIONS}/OVERTIME_PAID",
        headers=ME,
        json={
            "scope": {"month": SEPT},
            "preview_fingerprint": after["preview_fingerprint"],
            "client_request_id": str(uuid.uuid4()),
            "recipients": ["recipient"],
        },
    )
    assert submitted.status_code == 200, submitted.text


def test_WT_CORE_009_does_not_change_the_recipient_payload_fingerprint(
    db_client: TestClient, db_session: Session
) -> None:
    """§17 — the advisory is employee-private and must not disturb the seal.

    ``FINGERPRINT_EXCLUDE`` drops ``findings``, so the seal the employee previews
    is the seal a submission verifies and is computed over the payload WITHOUT
    the advisory. Removing ``findings`` from the exclusion would silently make
    every existing package unverifiable.
    """
    from app.disclosure.fingerprint import FINGERPRINT_EXCLUDE, payload_dict, payload_fingerprint

    interval = _interval(db_session, WORK_FROM, WORK_TO)
    requested = _auth_version(
        db_session,
        recorded_at=RECORDED_BEFORE,
        status=OvertimeAuthStatus.REQUESTED,
        source=EMPLOYEE_SOURCE,
    )
    decision = _auth_version(db_session, recorded_at=RECORDED_AFTER, supersedes=requested)
    assert _link(db_client, interval.id, 1, decision.id).status_code == 200

    preview = _preview_window(db_client, SEPT_FROM, OCT_FROM)
    assert len(_wt_core_009(preview)) == 1
    assert "findings" in FINGERPRINT_EXCLUDE
    payload = payload_dict(preview)
    assert "findings" not in payload, "a private finding leaked into the recipient payload"
    assert preview["preview_fingerprint"] == payload_fingerprint(payload)


def test_WT_CORE_009_is_scoped_to_the_rules_effective_dating(
    db_client: TestClient, db_session: Session
) -> None:
    """§16.8 — the rule is period-scoped, and it adds NO new failure code.

    The overtime policy is in force from 2026-06-01, so a period before it must produce
    no finding. The reachable pre-rule period is a month the calendar edition
    covers (from 2026-01-01) that no ordinary policy edition covers (the only one
    starts 2026-06-01) — and there is no pre-cutover write guard, so an interval
    and an authorization window CAN be recorded there. The preview must keep the
    status code such a period already had: an advisory must never turn it into a
    failure. Mutation-proof: dropping the effective-date comparison emits a 2026
    rule into a period it does not govern.
    """
    interval = _interval(
        db_session, f"{PRE_RULE}-10T08:00:00+01:00", f"{PRE_RULE}-10T09:00:00+01:00"
    )
    requested = _auth_version(
        db_session,
        recorded_at=f"{PRE_RULE}-01T00:00:00+01:00",
        valid_from=f"{PRE_RULE}-01T00:00:00+01:00",
        valid_to=f"{PRE_RULE}-31T00:00:00+01:00",
        status=OvertimeAuthStatus.REQUESTED,
        source=EMPLOYEE_SOURCE,
    )
    assert _link(db_client, interval.id, 1, requested.id).status_code == 200

    assert _wt_core_009(_preview_window(db_client, f"{PRE_RULE}-01", f"{PRE_RULE}-31")) == []

    # The SAME fixture DOES report it in a month the rule governs, so this test
    # cannot pass by the finding being globally absent.
    governed = _interval(db_session, WORK_FROM, WORK_TO)
    later = _auth_version(
        db_session,
        recorded_at=RECORDED_BEFORE,
        status=OvertimeAuthStatus.REQUESTED,
        source=EMPLOYEE_SOURCE,
    )
    decision = _auth_version(db_session, recorded_at=RECORDED_AFTER, supersedes=later)
    assert _link(db_client, governed.id, 1, decision.id).status_code == 200
    assert len(_wt_core_009(_preview_window(db_client, SEPT_FROM, OCT_FROM))) == 1
