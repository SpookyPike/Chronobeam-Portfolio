"""F8.1 — the overtime authorization window is BERLIN-anchored at the boundary.

Before this slice the three overtime window schemas accepted a NAIVE datetime
with no naive-input contract, and the service wrote the payload straight into a
``timestamptz`` column. PostgreSQL therefore interpreted the naive value in the
**database session timezone** (UTC in the container), so a stored endpoint sat
1-2 hours off the wall time the employee typed and the whole window moved with
the server's timezone. The window is a calculation input — ``_covers`` and the
``TRAVEL-SUNDAY-001`` suppression both read it — so a wrong window is not
cosmetic.

The suite runs under ``TZ=UTC`` (``conftest``), so every assertion here is proven
under a NON-Berlin host timezone.
"""

import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime

import pytest
from app.errors import OvertimeAuthorizationDecisionRefused
from app.identity.models import Employee
from app.ocd_overtime.models import OvertimeAuthorization
from app.ocd_overtime.schemas import (
    OvertimeAuthorizationIn,
    OvertimeAuthorizationPatch,
)
from app.ocd_overtime.service import _local_day
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .conftest import DEVELOPER_PRINCIPAL_HEADER, decide_authorization

AUTHZ_URL = "/api/v1/me/overtime-authorizations"

#: 08:00 Berlin on a CEST day is 06:00 UTC. The naive value is what the employee
#: types; the instant is what must be persisted.
SUMMER_WALL = "2026-07-01T08:00"
SUMMER_INSTANT = datetime(2026, 7, 1, 6, 0, tzinfo=UTC)

#: Fall-back day: 02:30 occurs twice, and 29 March 02:30 does not occur at all.
AMBIGUOUS_WALL = "2026-10-25T02:30"
NONEXISTENT_WALL = "2026-03-29T02:30"


def employee(session: Session) -> Employee:
    row = session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert row is not None
    return row


@contextmanager
def _host_timezone(name: str) -> Iterator[None]:
    """Run a block under another HOST timezone, then restore the pinned one."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:  # pragma: no cover - conftest always pins TZ
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


# ---------------------------------------------------------------------------
# The input contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "schema",
    [
        OvertimeAuthorizationIn,
        OvertimeAuthorizationPatch,
    ],
)
def test_the_employee_window_schemas_refuse_an_aware_wall_time(schema: type) -> None:
    """A wall time is a NAIVE Berlin time; an offset is refused, not reinterpreted.

    Accepting an offset would create a second definition of the same window
    ``OvertimeAuthorizationDecisionIn`` is deliberately NOT
    in this list: the operator CLI resolves its own wall times and hands that
    schema an aware instant — see ``test_the_seam_accepts_both_window_forms``.
    """
    with pytest.raises(ValidationError) as caught:
        schema(valid_from=datetime(2026, 7, 1, 8, 0, tzinfo=UTC))
    assert "naive" in str(caught.value)


# ---------------------------------------------------------------------------
# The round trip — the discriminating guard
# ---------------------------------------------------------------------------


def test_a_naive_window_round_trips_to_the_berlin_instant_the_employee_typed(
    db_client: TestClient, db_session: Session
) -> None:
    """08:00 typed in Berlin must be stored as 06:00Z, not 08:00Z.

    This is the guard for the defect: under the old code the naive payload went
    into a ``timestamptz`` column through the DB session timezone (UTC), so the
    stored instant was 08:00Z — two hours late in summer — and the window moved
    with the server's timezone.
    """
    created = db_client.post(
        AUTHZ_URL,
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"valid_from": SUMMER_WALL, "valid_to": "2026-07-01T17:00"},
    )
    assert created.status_code == 200, created.text
    assert datetime.fromisoformat(created.json()["valid_from"]) == SUMMER_INSTANT

    row = db_session.get(OvertimeAuthorization, uuid.UUID(created.json()["id"]))
    assert row is not None
    assert row.valid_from.astimezone(UTC) == SUMMER_INSTANT
    # The other endpoint is anchored by the same rule, not just the first.
    assert row.valid_to is not None
    assert row.valid_to.astimezone(UTC) == datetime(2026, 7, 1, 15, 0, tzinfo=UTC)


def test_a_corrected_window_is_anchored_too(db_client: TestClient, db_session: Session) -> None:
    """The PATCH path merges by presence, so it anchors before it merges.

    A correction replaces one endpoint and keeps the other from the predecessor;
    anchoring only the create path would leave the corrected window half wrong.
    """
    created = db_client.post(
        AUTHZ_URL, headers=DEVELOPER_PRINCIPAL_HEADER, json={"valid_from": SUMMER_WALL}
    )
    assert created.status_code == 200, created.text
    patched = db_client.patch(
        f"{AUTHZ_URL}/{created.json()['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": 1, "valid_to": "2026-07-01T17:00"},
    )
    assert patched.status_code == 200, patched.text
    # A correction is a NEW version row, so read the row the PATCH returned.
    row = db_session.get(OvertimeAuthorization, uuid.UUID(patched.json()["id"]))
    assert row is not None
    assert row.valid_from.astimezone(UTC) == SUMMER_INSTANT
    assert row.valid_to is not None
    assert row.valid_to.astimezone(UTC) == datetime(2026, 7, 1, 15, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# DST is detected, never guessed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("wall", "kind"), [(AMBIGUOUS_WALL, "ambiguous"), (NONEXISTENT_WALL, "nonexistent")]
)
def test_a_dst_ambiguous_or_nonexistent_window_is_refused(
    db_client: TestClient, wall: str, kind: str
) -> None:
    """02:30 on the fall-back day happens twice; 02:30 on the gap day never happens.

    Both must be refused with the candidate instants named. Before F8.1 both were
    silently accepted as UTC wall times, i.e. the service guessed an instant the
    employee never typed.
    """
    refused = db_client.post(
        AUTHZ_URL, headers=DEVELOPER_PRINCIPAL_HEADER, json={"valid_from": wall}
    )
    assert refused.status_code == 422, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "VALIDATION_DST_AMBIGUOUS"
    assert detail["details"]["kind"] == kind
    if kind == "ambiguous":
        assert len(detail["details"]["candidates"]) == 2


def test_a_sub_minute_window_is_refused(db_client: TestClient) -> None:
    """The chokepoint's minute-resolution rule applies to this family too."""
    refused = db_client.post(
        AUTHZ_URL, headers=DEVELOPER_PRINCIPAL_HEADER, json={"valid_from": "2026-07-01T08:00:30"}
    )
    assert refused.status_code == 422, refused.text
    message = refused.json()["detail"]["message"]
    assert "to the minute" in message
    assert "local" not in message


def test_the_window_order_is_compared_on_instants(db_client: TestClient) -> None:
    """The comparison happens after anchoring, so it compares instants.

    It previously compared naive payloads after ``astimezone(UTC)``, which read a
    naive value in the HOST timezone — the same defect one step earlier.
    """
    reversed_window = db_client.post(
        AUTHZ_URL,
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"valid_from": SUMMER_WALL, "valid_to": "2026-07-01T07:00"},
    )
    assert reversed_window.status_code == 422, reversed_window.text
    # The message is human-readable by contract (BACKEND_SPEC §11) and names no
    # API field: the response is read by the employee, not by the client author.
    message = reversed_window.json()["detail"]["message"]
    assert "must be after" in message
    assert "valid_to" not in message and "valid_from" not in message


# ---------------------------------------------------------------------------
# The institutional seam keeps its own contract
# ---------------------------------------------------------------------------


def _request(session: Session, employee_row: Employee) -> uuid.UUID:
    """An employee REQUESTED row, so an institutional decision can follow it."""
    from app.ocd_overtime.schemas import OvertimeAuthorizationIn as In
    from app.ocd_overtime.service import create_authorization

    row = create_authorization(
        session,
        employee_row,
        In(valid_from=datetime(2026, 7, 1, 8, 0), valid_to=datetime(2026, 7, 1, 17, 0)),
    )
    return row.id


def test_the_seam_accepts_both_window_forms(db_session: Session) -> None:
    """The CLI pre-resolves wall times, so the seam must accept an aware instant.

    Adding a naive-only validator to ``OvertimeAuthorizationDecisionIn`` would
    break ``app/overtime_admin.py`` — the one existing caller — because
    ``_parse_instant`` deliberately resolves through ``resolve_wall_time`` so a
    DST-ambiguous ``--from`` is refused with the candidates named. Both forms must
    therefore land on the SAME instant.
    """
    employee_row = employee(db_session)
    aware = decide_authorization(
        db_session,
        employee_row,
        _request(db_session, employee_row),
        valid_from=SUMMER_INSTANT,
        authorized_by="woco",
    )
    naive = decide_authorization(
        db_session,
        employee_row,
        _request(db_session, employee_row),
        valid_from=datetime(2026, 7, 1, 8, 0),
        authorized_by="woco",
    )
    assert aware.valid_from.astimezone(UTC) == SUMMER_INSTANT
    assert naive.valid_from.astimezone(UTC) == SUMMER_INSTANT


def test_the_seam_refuses_an_ambiguous_naive_window_with_its_own_error(
    db_session: Session,
) -> None:
    """The seam reports through its domain exception, never ``HTTPException``."""
    employee_row = employee(db_session)
    with pytest.raises(OvertimeAuthorizationDecisionRefused) as caught:
        decide_authorization(
            db_session,
            employee_row,
            _request(db_session, employee_row),
            valid_from=datetime(2026, 10, 25, 2, 30),
        )
    assert "ambiguous" in str(caught.value)


# ---------------------------------------------------------------------------
# The retained host-independent fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host_zone", ["UTC", "UTC-14", "UTC+12"])
def test_local_day_reads_a_naive_instant_as_berlin_not_as_the_host(host_zone: str) -> None:
    """``_local_day`` keeps its naive branch as defence in depth (F8.1).

    The write paths anchor before persisting, so no route reaches this branch any
    more; it is retained because the fallback it prevents is HOST-dependent. Under
    a +14 host, ``astimezone()`` on a naive 23:00 would report the NEXT day and
    silently move a month boundary.
    """
    with _host_timezone(host_zone):
        assert _local_day(datetime(2026, 7, 1, 23, 0)) == date(2026, 7, 1)
        assert _local_day(datetime(2026, 7, 1, 0, 30)) == date(2026, 7, 1)
