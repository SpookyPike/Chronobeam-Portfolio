"""F0 regression reproductions — domain state machines and lineage.

Baseline freeze for the confirmed state/lineage defects recorded by the
targeted closure audit (2026-09-17, §3): terminal states that are not terminal,
calculation contexts that read superseded versions, status assertions that
outlive the facts they describe, derived linkages that contradict their fact,
and day allocation that loses elapsed time.

Each test asserts the INVARIANT from ``docs/INVARIANTS.md`` rather than the
current behaviour. **F3 closed four of them and their ``xfail(strict=True)``
markers are gone** — the tests now pass because the invariant holds, not
because the reproduction was weakened. ``EINV-17`` is the one whose SHAPE had
to change: its reproduction posted a sub-minute span, which the F3.8
minute-resolution contract now rejects, so the allocation invariant moved to
the pure engine (``tests/test_policy_engine.py``,
``tests/test_travel_engine.py``) and its command-boundary half lives in
``tests/test_worktime_api.py``.

All of these run through the real HTTP API on the shared rolled-back test
transaction, so they leave nothing behind.
"""

from datetime import UTC, date, datetime

import pytest
from app.identity.models import Employee
from app.ocd_overtime.models import OcdAssignment, OcdAssignmentStatus
from app.worktime.models import WorkInterval, WorkPeriod
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.conftest import DEVELOPER_PRINCIPAL_HEADER as ME

INTERVALS = "/api/v1/me/work-intervals"
MONTHS = "/api/v1/me/months"
OCD = "/api/v1/me/ocd"
OCD_CALLS = "/api/v1/me/ocd-calls"

# 2032 keeps these fixtures clear of every seeded and demo month.
WORK_DAY = "2032-01-05"
MONTH_2032_03 = "2032-03"
MONTH_2032_04 = "2032-04"


@pytest.fixture()
def employee(db_session: Session) -> Employee:
    row = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert row is not None
    return row


def _create_interval(
    client: TestClient, start_local: str, end_local: str, work_type: str = "ON_SITE"
) -> dict:
    response = client.post(
        INTERVALS,
        headers=ME,
        json={"start_local": start_local, "end_local": end_local, "work_type": work_type},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _current_ocd_intervals(session: Session, employee: Employee) -> list[WorkInterval]:
    """Current-version OCD_CALL intervals (the ledger's own predicate)."""
    newer = select(WorkInterval.id).where(WorkInterval.supersedes_id == WorkInterval.id)
    return list(
        session.scalars(
            select(WorkInterval).where(
                WorkInterval.employee_id == employee.id,
                WorkInterval.work_type == "OCD_CALL",
                ~WorkInterval.id.in_(newer),
            )
        ).all()
    )


def _ocd_assignment(
    session: Session, employee: Employee, start_at: datetime, end_at: datetime
) -> OcdAssignment:
    assignment = OcdAssignment(
        employee_id=employee.id,
        start_at=start_at,
        end_at=end_at,
        scheduled_at=start_at,
        source="EMPLOYEE_DECLARED",
        status=OcdAssignmentStatus.SCHEDULED,
    )
    session.add(assignment)
    session.flush()
    return assignment


def test_EINV_12_voided_ocd_call_cannot_be_resurrected_by_patch(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """A voided call must not be able to produce effective work again.

    EINV-12 (closed in F3): ``VOIDED`` is terminal for ordinary mutation. The
    reproduction used to leave the call mutable, so a PATCH regenerated its
    linked work interval as a fresh root — effective work reappearing from a
    terminal fact. Both halves are asserted here: the command is refused, AND
    no effective interval exists afterwards.
    """
    assignment = _ocd_assignment(
        db_session,
        employee,
        datetime(2032, 1, 5, 20, 0, tzinfo=UTC),
        datetime(2032, 1, 6, 4, 0, tzinfo=UTC),
    )
    created = db_client.post(
        OCD_CALLS,
        headers=ME,
        json={
            "ocd_assignment_id": str(assignment.id),
            "call_start_local": f"{WORK_DAY}T22:00:00",
            "call_end_local": f"{WORK_DAY}T23:00:00",
            "mode": "REMOTE",
            "comment": "first call",
        },
    )
    assert created.status_code == 200, created.text
    call = created.json()
    assert len(_current_ocd_intervals(db_session, employee)) == 1

    voided = db_client.post(
        f"{OCD_CALLS}/{call['id']}/void",
        headers=ME,
        json={"version": call["version"], "reason": "cancelled"},
    )
    assert voided.status_code == 200, voided.text
    db_session.expire_all()
    assert _current_ocd_intervals(db_session, employee) == []
    assert voided.json()["linked_interval_id"] is None

    # An ordinary edit of the voided head must be REFUSED — including a time
    # edit, which is the shape that used to regenerate the interval.
    for body in (
        {"version": voided.json()["version"], "comment": "edited after void"},
        {
            "version": voided.json()["version"],
            "call_start_local": f"{WORK_DAY}T23:00:00",
            "call_end_local": f"{WORK_DAY}T23:30:00",
        },
    ):
        refused = db_client.patch(f"{OCD_CALLS}/{voided.json()['id']}", headers=ME, json=body)
        assert refused.status_code == 409, refused.text
    db_session.expire_all()
    resurrected = _current_ocd_intervals(db_session, employee)
    assert resurrected == [], (
        f"a VOIDED call regenerated {len(resurrected)} effective OCD_CALL interval(s)"
    )
    # The terminal fact itself is untouched: the head is still the voided row.
    head = db_client.get(f"{OCD}?from={WORK_DAY}&to=2032-01-06", headers=ME).json()
    assert [x["status"] for x in head[0]["calls"]] == ["VOIDED"]


def test_EINV_13_ocd_rest_findings_ignore_superseded_versions(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """A superseded version must not influence a compliance finding.

    Facts: the CURRENT work version ends 09:00 Berlin, the call starts 21:30
    Berlin — a 12.5 h rest gap, i.e. compliant.  The superseded version ended
    21:00 Berlin, which would leave ~0.5 h and raise OCD-005 if the calculation
    consumed it. EINV-13 (closed in F3): ``list_ocd`` builds its rest context
    from the canonical current-version query.
    """
    interval = _create_interval(db_client, f"{WORK_DAY}T06:00:00", f"{WORK_DAY}T21:00:00")
    corrected = db_client.patch(
        f"{INTERVALS}/{interval['id']}",
        headers=ME,
        json={
            "version": interval["version"],
            "reason": "corrected end",
            "start_local": f"{WORK_DAY}T06:00:00",
            "end_local": f"{WORK_DAY}T09:00:00",
            "work_type": "ON_SITE",
        },
    )
    assert corrected.status_code == 200, corrected.text

    assignment = _ocd_assignment(
        db_session,
        employee,
        datetime(2032, 1, 5, 20, 0, tzinfo=UTC),
        datetime(2032, 1, 6, 4, 0, tzinfo=UTC),
    )
    call = db_client.post(
        OCD_CALLS,
        headers=ME,
        json={
            "ocd_assignment_id": str(assignment.id),
            "call_start_local": f"{WORK_DAY}T21:30:00",
            "call_end_local": f"{WORK_DAY}T22:00:00",
            "mode": "REMOTE",
        },
    )
    assert call.status_code == 200, call.text

    listed = db_client.get(f"{OCD}?from={WORK_DAY}&to=2032-01-06", headers=ME)
    assert listed.status_code == 200, listed.text
    rule_ids = [finding["rule_id"] for item in listed.json() for finding in item["findings"]]
    assert "OCD-005" not in rule_ids, f"rest warning raised from a superseded version: {rule_ids}"


def test_EINV_15_reviewed_month_is_invalidated_by_later_facts(db_client: TestClient) -> None:
    """A review assertion must not outlive the facts it was made about.

    EINV-15 (closed in F3) with the BOUNDED contract: ``REVIEWED`` means the
    employee reviewed the factual entries belonging to that month, so a change
    to those facts reopens it. An earlier month's correction does not cascade
    into later months (see ``test_review_of_an_unrelated_month_survives``).
    """
    _create_interval(db_client, f"{MONTH_2032_03}-01T08:00:00", f"{MONTH_2032_03}-01T16:00:00")
    reviewed = db_client.post(
        f"{MONTHS}/{MONTH_2032_03}/review", headers=ME, json={"acknowledged": True}
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "REVIEWED"

    # A 12 h day exceeds the daily maximum and raises a CRITICAL finding.
    _create_interval(db_client, f"{MONTH_2032_03}-02T08:00:00", f"{MONTH_2032_03}-02T20:00:00")

    after = db_client.get(f"{MONTHS}/{MONTH_2032_03}", headers=ME)
    assert after.status_code == 200, after.text
    assert after.json()["status"] != "REVIEWED", (
        "month still asserts REVIEWED after facts it did not cover were added"
    )


def test_EINV_14_cross_month_correction_relinks_the_work_period(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """A persisted derived linkage must agree with the fact it derives from.

    EINV-14 (closed in F3): the correction re-derives the owning period from
    the NEW version's own Berlin start month instead of copying the
    predecessor's.
    """
    interval = _create_interval(
        db_client, f"{MONTH_2032_03}-31T10:00:00", f"{MONTH_2032_03}-31T12:00:00"
    )
    db_session.expire_all()
    original = db_session.get(WorkInterval, interval["id"])
    assert original is not None
    march_period_id = original.work_period_id

    corrected = db_client.patch(
        f"{INTERVALS}/{interval['id']}",
        headers=ME,
        json={
            "version": interval["version"],
            "reason": "moved into April",
            "start_local": f"{MONTH_2032_04}-03T10:00:00",
            "end_local": f"{MONTH_2032_04}-03T12:00:00",
            "work_type": "ON_SITE",
        },
    )
    assert corrected.status_code == 200, corrected.text
    new_version_id = corrected.json()["id"]

    db_session.expire_all()
    new_row = db_session.get(WorkInterval, new_version_id)
    assert new_row is not None
    period = db_session.get(WorkPeriod, new_row.work_period_id)
    assert period is not None
    assert period.period_month == date(2032, 4, 1), (
        "corrected interval still points at the "
        f"{period.period_month} period (was {march_period_id})"
    )
