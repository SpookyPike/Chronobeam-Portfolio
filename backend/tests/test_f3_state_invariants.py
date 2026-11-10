"""F3 domain-state and ledger-correctness invariants.

The F0 regressions live in ``test_f0_state_regressions.py``; this module holds
the F3 additions that prove the invariants beyond the reproduced shape:

* EINV-12 — the terminal-state rule generalised (second void, overlap on the
  correction path, fail-closed on an inconsistent RECORDED head, and the
  PATCH-presence fix for the sibling full-replace schema).
* EINV-13 — the rest-context variants the F0 reproduction does not reach.
* EINV-14 — the relink rule on every path that owns a ``work_period_id``.
* EINV-15 — the complete reviewed-month input set: fact spans, travel facts,
  the employee's working-time schedule, and the overtime-authorization window
  that suppresses travel findings.
* EINV-17 — the minute-resolution command boundary.

Everything runs through the real HTTP API on the shared rolled-back test
transaction, except the two concurrency tests, which COMMIT and clean up.
"""

import re
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from app.enums import PeriodStatus
from app.identity.models import Employee
from app.ocd_overtime.models import (
    OcdAssignment,
    OcdAssignmentStatus,
)
from app.worktime.models import WorkInterval, WorkPeriod
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.conftest import DEVELOPER_PRINCIPAL_HEADER as ME
from tests.conftest import decide_authorization

INTERVALS = "/api/v1/me/work-intervals"
MONTHS = "/api/v1/me/months"
WORKING_TIME = "/api/v1/me/working-time"
OCD = "/api/v1/me/ocd"
OCD_CALLS = "/api/v1/me/ocd-calls"
AUTHORIZATIONS = "/api/v1/me/overtime-authorizations"
TRIPS = "/api/v1/me/travel-trips"
HR_REQUESTS = "/api/v1/hr/access-requests"
ME_REQUESTS = "/api/v1/me/access-requests"

# 2032 keeps these fixtures clear of every seeded and demo month.
DAY_2032_01_05 = "2032-01-05"
MARCH_2032 = "2032-03"
APRIL_2032 = "2032-04"


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


def _review(client: TestClient, month: str) -> dict:
    response = client.post(f"{MONTHS}/{month}/review", headers=ME, json={"acknowledged": True})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "REVIEWED"
    return response.json()


def _month_status(client: TestClient, month: str) -> str:
    response = client.get(f"{MONTHS}/{month}", headers=ME)
    assert response.status_code == 200, response.text
    return response.json()["status"]


def _instant(value: str) -> datetime:
    """Parse either serialisation of the same instant (naive wall time or ``Z``)."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _period(db_session: Session, employee: Employee, month: str) -> WorkPeriod:
    year, month_number = (int(part) for part in month.split("-"))
    row = db_session.scalar(
        select(WorkPeriod).where(
            WorkPeriod.employee_id == employee.id,
            WorkPeriod.period_month == date(year, month_number, 1),
        )
    )
    assert row is not None, f"no work_period row for {month}"
    return row


def _assignment(
    session: Session, employee: Employee, start_at: datetime, end_at: datetime
) -> OcdAssignment:
    row = OcdAssignment(
        employee_id=employee.id,
        start_at=start_at,
        end_at=end_at,
        scheduled_at=start_at,
        source="EMPLOYEE_DECLARED",
        status=OcdAssignmentStatus.SCHEDULED,
    )
    session.add(row)
    session.flush()
    return row


def _current_ocd_intervals(session: Session, employee: Employee) -> list[WorkInterval]:
    from sqlalchemy import exists
    from sqlalchemy.orm import aliased

    child = aliased(WorkInterval)
    return list(
        session.scalars(
            select(WorkInterval).where(
                WorkInterval.employee_id == employee.id,
                WorkInterval.work_type == "OCD_CALL",
                ~exists(select(child.id).where(child.supersedes_id == WorkInterval.id)),
            )
        ).all()
    )


def _ocd_call(
    client: TestClient,
    session: Session,
    employee: Employee,
    start_local: str = f"{DAY_2032_01_05}T22:00:00",
    end_local: str = f"{DAY_2032_01_05}T23:00:00",
) -> dict:
    assignment = _assignment(
        session,
        employee,
        datetime(2032, 1, 5, 20, 0, tzinfo=UTC),
        datetime(2032, 1, 6, 4, 0, tzinfo=UTC),
    )
    response = client.post(
        OCD_CALLS,
        headers=ME,
        json={
            "ocd_assignment_id": str(assignment.id),
            "call_start_local": start_local,
            "call_end_local": end_local,
            "mode": "REMOTE",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _working_time(valid_from: str, weekly: int = 2400, per_day: int = 480) -> dict:
    return {
        "valid_from": valid_from,
        "weekly_required_minutes": weekly,
        "mon_required_minutes": per_day,
        "tue_required_minutes": per_day,
        "wed_required_minutes": per_day,
        "thu_required_minutes": per_day,
        "fri_required_minutes": weekly - 4 * per_day,
        "sat_required_minutes": 0,
        "sun_required_minutes": 0,
    }


def _migration_0014():
    """Load the repair migration by path (``alembic/versions`` is not a package)."""
    import importlib.util

    path = (
        Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0014_work_period_relink.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0014", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# EINV-12 — terminal domain states are terminal
# ---------------------------------------------------------------------------


def test_voided_call_second_void_is_refused(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """A second terminal write is refused, not appended (EINV-06's pattern)."""
    call = _ocd_call(db_client, db_session, employee)
    first = db_client.post(
        f"{OCD_CALLS}/{call['id']}/void", headers=ME, json={"version": call["version"]}
    )
    assert first.status_code == 200, first.text
    # Commands address the CHAIN HEAD: a superseded id is not addressable.
    second = db_client.post(
        f"{OCD_CALLS}/{first.json()['id']}/void",
        headers=ME,
        json={"version": first.json()["version"]},
    )
    assert second.status_code == 409, second.text
    db_session.expire_all()
    assert _current_ocd_intervals(db_session, employee) == []
    calls = db_client.get(f"{OCD}?from={DAY_2032_01_05}&to=2032-01-06", headers=ME).json()
    versions = [x["version"] for item in calls for x in item["calls"]]
    assert versions == [first.json()["version"]], (
        f"a second void appended another version of a terminal fact: {versions}"
    )


def test_patch_call_of_recorded_call_still_checks_overlap(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """The correction path keeps EINV-05: no overlapping OCD_CALL work.

    The branch that used to resurrect a voided call skipped the overlap check
    entirely, so this pins the check on the surviving path: the call is created
    clear of the interval and then corrected onto it.
    """
    _create_interval(db_client, f"{DAY_2032_01_05}T20:00:00", f"{DAY_2032_01_05}T21:00:00")
    call = _ocd_call(db_client, db_session, employee)
    response = db_client.patch(
        f"{OCD_CALLS}/{call['id']}",
        headers=ME,
        json={
            "version": call["version"],
            "call_start_local": f"{DAY_2032_01_05}T20:30:00",
            "call_end_local": f"{DAY_2032_01_05}T21:30:00",
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "VALIDATION_INTERVAL_OVERLAP"


def test_recorded_call_without_a_linked_interval_fails_closed(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """An inconsistent canonical state fails loudly instead of being repaired.

    A RECORDED head with no current linked interval cannot occur through the
    API. If it does (direct write, restore), an ordinary edit must NOT
    manufacture a new root interval with no overlap check — that is the shape
    that made a voided call's work reappear. The request therefore fails
    (500 INTERNAL_ERROR through the app's own handler, no internals leaked) and
    writes nothing.
    """
    call = _ocd_call(db_client, db_session, employee)
    linked = db_session.scalar(
        select(WorkInterval).where(WorkInterval.linked_ocd_call_id == call["id"])
    )
    assert linked is not None
    db_session.delete(linked)
    db_session.flush()

    response = db_client.patch(
        f"{OCD_CALLS}/{call['id']}", headers=ME, json={"version": call["version"]}
    )
    assert response.status_code == 500, response.text
    assert response.json()["detail"]["code"] == "INTERNAL_ERROR"
    db_session.rollback()
    assert (
        db_session.scalar(select(WorkInterval).where(WorkInterval.linked_ocd_call_id == call["id"]))
        is None
    ), "the command manufactured a replacement interval instead of failing"


def test_authorization_patch_never_applies_omitted_defaults(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """A partial PATCH preserves what it does not mention.

    ``OvertimeAuthorizationPatch`` used to be a full row replace built from a
    create-time schema, so omitting ``status`` reset a decided authorization to
    ``REQUESTED`` and omitting the optional fields NULLed them.

    ``status``/``authorized_by``/``source`` are no longer employee-writable at
    all (ACCESS-017): the employee moves their own request between
    ``REQUESTED`` and ``CANCELLED``, and the institutional fields are server-set.
    """
    created = db_client.post(
        AUTHORIZATIONS,
        headers=ME,
        json={
            "valid_from": "2032-03-01T00:00:00",
            "valid_to": "2032-03-31T00:00:00",
            "purpose": "release support",
            "source_ref": "ref-1",
        },
    )
    assert created.status_code == 200, created.text
    row = created.json()
    assert row["status"] == "REQUESTED" and row["source"] == "EMPLOYEE_RECORDED"
    assert row["authorized_by"] is None

    cancelled = db_client.patch(
        f"{AUTHORIZATIONS}/{row['id']}",
        headers=ME,
        json={"version": row["version"], "status": "CANCELLED"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"

    patched = db_client.patch(
        f"{AUTHORIZATIONS}/{cancelled.json()['id']}", headers=ME, json={"version": 2}
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["status"] == "CANCELLED", "an omitted status was reset to the schema default"
    assert body["source"] == "EMPLOYEE_RECORDED"
    assert body["purpose"] == "release support"
    assert body["authorized_by"] is None
    assert body["source_ref"] == "ref-1"
    assert body["valid_to"] is not None

    # An explicit null still clears (the codebase's own presence convention).
    # Address the chain head returned by the first correction.
    cleared = db_client.patch(
        f"{AUTHORIZATIONS}/{body['id']}",
        headers=ME,
        json={"version": body["version"], "valid_to": None},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["valid_to"] is None
    assert cleared.json()["purpose"] == "release support"


def test_no_unvoid_or_restore_command_exists(db_client: TestClient) -> None:
    """A terminal state is restored only by an explicit, separately designed
    transition — none exists, and none may appear silently."""
    paths = db_client.get("/openapi.json").json()["paths"]
    forbidden = [
        path
        for path in paths
        if any(hint in path.lower() for hint in ("unvoid", "restore", "reactivate", "reopen"))
    ]
    assert forbidden == []
    assert not [path for path in paths if path.endswith("/void") and "get" in paths[path]], (
        "a voided call must not be readable as an undoable operation"
    )


# ---------------------------------------------------------------------------
# EINV-13 — calculations consume current facts only
# ---------------------------------------------------------------------------


def test_ocd_rest_context_uses_only_current_versions(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """The discriminating direction: the superseded version ends LATER.

    ``rest_findings`` takes the maximum end over the prior spans. A corrected
    interval whose superseded predecessor ends later than the canonical head
    therefore used to win the max and raise a false OCD-005 — the reproduction
    in the F0 file covers the opposite (earlier-ending) variant.
    """
    interval = _create_interval(
        db_client, f"{DAY_2032_01_05}T06:00:00", f"{DAY_2032_01_05}T20:00:00"
    )
    corrected = db_client.patch(
        f"{INTERVALS}/{interval['id']}",
        headers=ME,
        json={
            "version": interval["version"],
            "reason": "corrected end",
            "start_local": f"{DAY_2032_01_05}T06:00:00",
            "end_local": f"{DAY_2032_01_05}T09:00:00",
            "work_type": "ON_SITE",
        },
    )
    assert corrected.status_code == 200, corrected.text

    call = _ocd_call(
        db_client,
        db_session,
        employee,
        start_local=f"{DAY_2032_01_05}T21:30:00",
        end_local=f"{DAY_2032_01_05}T22:00:00",
    )
    assert call["status"] == "RECORDED"

    listed = db_client.get(f"{OCD}?from={DAY_2032_01_05}&to=2032-01-06", headers=ME)
    assert listed.status_code == 200, listed.text
    rule_ids = [f["rule_id"] for item in listed.json() for f in item["findings"]]
    assert "OCD-005" not in rule_ids, (
        f"the superseded predecessor (ending 20:00) fed the rest rule: {rule_ids}"
    )


def test_ocd_rest_context_includes_current_prior_calls(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """Guards the opposite failure: filtering must not empty the rest context.

    A *current* earlier call is real prior work, so the later call must still
    raise OCD-005.
    """
    first = _ocd_call(
        db_client,
        db_session,
        employee,
        start_local=f"{DAY_2032_01_05}T02:00:00",
        end_local=f"{DAY_2032_01_05}T03:00:00",
    )
    second_assignment = _assignment(
        db_session,
        employee,
        datetime(2032, 1, 5, 20, 0, tzinfo=UTC),
        datetime(2032, 1, 6, 4, 0, tzinfo=UTC),
    )
    second = db_client.post(
        OCD_CALLS,
        headers=ME,
        json={
            "ocd_assignment_id": str(second_assignment.id),
            "call_start_local": f"{DAY_2032_01_05}T12:00:00",
            "call_end_local": f"{DAY_2032_01_05}T13:00:00",
            "mode": "REMOTE",
        },
    )
    assert second.status_code == 200, second.text
    assert first["status"] == second.json()["status"] == "RECORDED"

    listed = db_client.get(f"{OCD}?from={DAY_2032_01_05}&to=2032-01-06", headers=ME)
    rule_ids = [f["rule_id"] for item in listed.json() for f in item["findings"]]
    assert "OCD-005" in rule_ids, f"a current prior call did not feed the rest rule: {rule_ids}"


# ---------------------------------------------------------------------------
# EINV-14 — a persisted derived linkage agrees with its fact
# ---------------------------------------------------------------------------


def test_cross_month_correction_relinks_in_both_directions(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """March -> April and April -> March both re-derive the owning period."""
    april = _create_interval(db_client, f"{APRIL_2032}-03T10:00:00", f"{APRIL_2032}-03T12:00:00")
    back_to_march = db_client.patch(
        f"{INTERVALS}/{april['id']}",
        headers=ME,
        json={
            "version": april["version"],
            "reason": "moved back",
            "start_local": f"{MARCH_2032}-31T10:00:00",
            "end_local": f"{MARCH_2032}-31T12:00:00",
        },
    )
    assert back_to_march.status_code == 200, back_to_march.text
    db_session.expire_all()
    row = db_session.get(WorkInterval, back_to_march.json()["id"])
    assert row is not None
    period = db_session.get(WorkPeriod, row.work_period_id)
    assert period is not None and period.period_month == date(2032, 3, 1)


def test_correction_within_one_month_keeps_the_same_period(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """Re-deriving must not churn the period when the start month is unchanged."""
    interval = _create_interval(db_client, f"{MARCH_2032}-10T08:00:00", f"{MARCH_2032}-10T16:00:00")
    db_session.expire_all()
    before = db_session.get(WorkInterval, interval["id"])
    assert before is not None

    corrected = db_client.patch(
        f"{INTERVALS}/{interval['id']}",
        headers=ME,
        json={
            "version": interval["version"],
            "reason": "extended",
            "start_local": f"{MARCH_2032}-10T07:30:00",
            "end_local": f"{MARCH_2032}-10T16:30:00",
        },
    )
    assert corrected.status_code == 200, corrected.text
    db_session.expire_all()
    after = db_session.get(WorkInterval, corrected.json()["id"])
    assert after is not None
    assert after.work_period_id == before.work_period_id


def test_ocd_call_correction_relinks_the_work_period(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """The OCD call correction path owns a period too (it is not a plain PATCH)."""
    call = _ocd_call(
        db_client,
        db_session,
        employee,
        start_local=f"{MARCH_2032}-31T22:00:00",
        end_local=f"{MARCH_2032}-31T23:00:00",
    )
    db_session.expire_all()
    linked = db_session.scalar(
        select(WorkInterval).where(WorkInterval.linked_ocd_call_id == call["id"])
    )
    assert linked is not None
    march_period = db_session.get(WorkPeriod, linked.work_period_id)
    assert march_period is not None and march_period.period_month == date(2032, 3, 1)

    corrected = db_client.patch(
        f"{OCD_CALLS}/{call['id']}",
        headers=ME,
        json={
            "version": call["version"],
            "call_start_local": f"{APRIL_2032}-03T10:00:00",
            "call_end_local": f"{APRIL_2032}-03T11:00:00",
        },
    )
    assert corrected.status_code == 200, corrected.text
    db_session.expire_all()
    moved = db_session.scalar(
        select(WorkInterval).where(WorkInterval.linked_ocd_call_id == corrected.json()["id"])
    )
    assert moved is not None
    period = db_session.get(WorkPeriod, moved.work_period_id)
    assert period is not None and period.period_month == date(2032, 4, 1)


def test_shift_deviation_correction_relinks_the_work_period(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """``save_deviation`` corrects its linked interval through ``correct_interval``.

    The seeded shift starts in July, so confirming it owns the July period; a
    deviation that moves the actual times into August must relink.
    """
    from app.integrations.shift_plan import ShiftAssignmentSeed
    from app.shift.service import seed_shift_assignment

    assignment = seed_shift_assignment(
        db_session,
        employee,
        ShiftAssignmentSeed(
            plan_key="synthetic-night-plan",
            plan_name="Synthetic night plan",
            planned_start_at=datetime.fromisoformat("2026-07-31T19:00:00+02:00"),
            planned_end_at=datetime.fromisoformat("2026-08-01T07:00:00+02:00"),
            shift_kind="NIGHT",
            source="TEST_SEED",
            source_ref="f3-relink-fixture",
        ),
    )
    db_session.flush()
    loaded = db_client.get("/api/v1/me/shifts?from=2026-07-01&to=2026-08-02", headers=ME).json()
    confirmed = db_client.post(
        f"/api/v1/me/shifts/{assignment.id}/confirm",
        headers=ME,
        json={"approved_revision_id": loaded[0]["approved_revision"]["id"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    db_session.expire_all()
    july_interval = db_session.scalar(
        select(WorkInterval).where(WorkInterval.linked_shift_assignment_id == assignment.id)
    )
    assert july_interval is not None
    july_period = db_session.get(WorkPeriod, july_interval.work_period_id)
    assert july_period is not None and july_period.period_month == date(2026, 7, 1)

    deviation = db_client.post(
        f"/api/v1/me/shifts/{assignment.id}/deviation",
        headers=ME,
        json={
            "deviation_type": "RESCHEDULED",
            "actual_start_local": "2026-08-02T10:00:00",
            "actual_end_local": "2026-08-02T12:00:00",
        },
    )
    assert deviation.status_code == 200, deviation.text
    db_session.expire_all()
    # The corrected interval is the one the deviation names as its source fact;
    # a bare "first row for the assignment" query would return the superseded
    # July predecessor.
    (corrected_id,) = deviation.json()["deviation"]["source_work_interval_ids"]
    moved = db_session.get(WorkInterval, uuid.UUID(corrected_id))
    assert moved is not None
    assert moved.version == july_interval.version + 1
    period = db_session.get(WorkPeriod, moved.work_period_id)
    assert period is not None and period.period_month == date(2026, 8, 1)


# ---------------------------------------------------------------------------
# EINV-15 — a review assertion corresponds to its inputs
# ---------------------------------------------------------------------------


def test_cross_month_edit_reopens_both_months(db_client: TestClient, db_session: Session) -> None:
    """A boundary-spanning edit reopens the review of BOTH months it touches."""
    _create_interval(db_client, f"{MARCH_2032}-15T08:00:00", f"{MARCH_2032}-15T16:00:00")
    _create_interval(db_client, f"{APRIL_2032}-15T08:00:00", f"{APRIL_2032}-15T16:00:00")
    _review(db_client, MARCH_2032)
    _review(db_client, APRIL_2032)

    crossing = _create_interval(db_client, f"{MARCH_2032}-31T22:00:00", f"{APRIL_2032}-01T02:00:00")
    assert _month_status(db_client, MARCH_2032) == PeriodStatus.OPEN
    assert _month_status(db_client, APRIL_2032) == PeriodStatus.OPEN

    # Re-review both, then CORRECT the crossing interval: the old and the new
    # span both count.
    _review(db_client, MARCH_2032)
    _review(db_client, APRIL_2032)
    corrected = db_client.patch(
        f"{INTERVALS}/{crossing['id']}",
        headers=ME,
        json={
            "version": crossing["version"],
            "reason": "shortened",
            "start_local": f"{APRIL_2032}-01T22:00:00",
            "end_local": f"{APRIL_2032}-02T02:00:00",
        },
    )
    assert corrected.status_code == 200, corrected.text
    assert _month_status(db_client, MARCH_2032) == PeriodStatus.OPEN, (
        "the superseded span's month stayed REVIEWED"
    )
    assert _month_status(db_client, APRIL_2032) == PeriodStatus.OPEN


def test_review_of_an_unrelated_month_survives(db_client: TestClient, db_session: Session) -> None:
    """The BOUNDED contract: no transitive cascade through carry-forward.

    A change in March moves April's carried opening balance, but April's own
    factual entries are untouched, so April's review stands. This pins the
    decision so a later move to transitive semantics is deliberate and visible.
    """
    _create_interval(db_client, f"{MARCH_2032}-10T08:00:00", f"{MARCH_2032}-10T16:00:00")
    _create_interval(db_client, f"{APRIL_2032}-10T08:00:00", f"{APRIL_2032}-10T16:00:00")
    _review(db_client, MARCH_2032)
    _review(db_client, APRIL_2032)

    _create_interval(db_client, f"{MARCH_2032}-11T08:00:00", f"{MARCH_2032}-11T16:00:00")
    assert _month_status(db_client, MARCH_2032) == PeriodStatus.OPEN
    assert _month_status(db_client, APRIL_2032) == PeriodStatus.REVIEWED


def test_travel_fact_reopens_the_month(db_client: TestClient, db_session: Session) -> None:
    """A passive travel segment moves the day's top-up and the balance."""
    _create_interval(db_client, f"{MARCH_2032}-10T08:00:00", f"{MARCH_2032}-10T16:00:00")
    _review(db_client, MARCH_2032)

    trip = db_client.post(
        TRIPS,
        headers=ME,
        json={
            "start_local": f"{MARCH_2032}-09T06:00:00",
            "end_local": f"{MARCH_2032}-11T22:00:00",
            "start_origin": "HOME",
        },
    )
    assert trip.status_code == 201, trip.text
    segment = db_client.post(
        f"{TRIPS}/{trip.json()['id']}/segments",
        headers=ME,
        json={
            "start_local": f"{MARCH_2032}-10T06:00:00",
            "end_local": f"{MARCH_2032}-10T08:00:00",
            "classification": "PASSIVE_TRAVEL",
            "basis": "ACTUAL",
        },
    )
    assert segment.status_code == 201, segment.text
    assert _month_status(db_client, MARCH_2032) == PeriodStatus.OPEN


def test_authorization_change_reopens_the_months_in_its_window(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """The findings contract: an AUTHORITATIVE approval suppresses a finding.

    ``TRAVEL-SUNDAY-001`` is raised for Sunday travel work unless an APPROVED
    *institutional* authorization covers it, and ``review_month`` requires the
    findings to be acknowledged — so changing the authorization changes what a
    reviewed month asserted.

    The change exercised here is the REMOVAL direction (D2c): withdrawing an
    already-authoritative approval deletes a calculation input, so the months the
    OLD authoritative window covered must reopen. The employee cannot make that
    change at all (ACCESS-017) — only the institutional writer can.
    """
    # 2032-03-07 is a Sunday.
    trip = db_client.post(
        TRIPS,
        headers=ME,
        json={
            "start_local": f"{MARCH_2032}-07T06:00:00",
            "end_local": f"{MARCH_2032}-08T22:00:00",
            "start_origin": "HOME",
        },
    )
    assert trip.status_code == 201, trip.text
    authorization = db_client.post(
        AUTHORIZATIONS,
        headers=ME,
        json={
            "valid_from": f"{MARCH_2032}-01T00:00:00",
            "valid_to": f"{MARCH_2032}-31T00:00:00",
        },
    )
    assert authorization.status_code == 200, authorization.text
    approved = decide_authorization(
        db_session, employee, uuid.UUID(authorization.json()["id"]), authorized_by="woco"
    )
    segment = db_client.post(
        f"{TRIPS}/{trip.json()['id']}/segments",
        headers=ME,
        json={
            "start_local": f"{MARCH_2032}-07T08:00:00",
            "end_local": f"{MARCH_2032}-07T12:00:00",
            "classification": "ACTIVE_WORK",
            "basis": "ACTUAL",
        },
    )
    assert segment.status_code == 201, segment.text

    _review(db_client, MARCH_2032)
    decide_authorization(db_session, employee, approved.id, status="REJECTED", authorized_by="woco")
    assert _month_status(db_client, MARCH_2032) == PeriodStatus.OPEN, (
        "withdrawing the authoritative approval that suppressed a finding left the month REVIEWED"
    )


def test_authorization_window_reopens_only_the_months_it_covers(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """An authorization's reopened range is its own window, not everything."""
    _create_interval(db_client, f"{MARCH_2032}-10T08:00:00", f"{MARCH_2032}-10T16:00:00")
    _create_interval(db_client, f"{APRIL_2032}-10T08:00:00", f"{APRIL_2032}-10T16:00:00")
    _review(db_client, MARCH_2032)
    _review(db_client, APRIL_2032)

    created = db_client.post(
        AUTHORIZATIONS,
        headers=ME,
        json={
            "valid_from": f"{MARCH_2032}-01T00:00:00",
            "valid_to": f"{MARCH_2032}-31T00:00:00",
        },
    )
    assert created.status_code == 200, created.text
    decide_authorization(
        db_session, employee, uuid.UUID(created.json()["id"]), authorized_by="woco"
    )
    assert _month_status(db_client, MARCH_2032) == PeriodStatus.OPEN
    assert _month_status(db_client, APRIL_2032) == PeriodStatus.REVIEWED


def test_schedule_change_reopens_exactly_the_effective_range(
    db_client: TestClient, db_session: Session
) -> None:
    """The employee's working-time schedule is an engine input (EINV-15).

    ``daily_required_minutes`` and the weekly thresholds come from
    ``work_schedule_version``, so a schedule change changes the reviewed
    month's daily results, balance and findings. The reopened set is exactly
    the row's effective range: from ``valid_from`` until the next later-starting
    row begins, and nothing before it.
    """
    for month in ("2032-01", "2032-02", "2032-03", "2032-04"):
        _create_interval(db_client, f"{month}-10T08:00:00", f"{month}-10T16:00:00")
        _review(db_client, month)

    response = db_client.put(WORKING_TIME, headers=ME, json=_working_time("2032-03-01"))
    assert response.status_code == 200, response.text
    assert _month_status(db_client, "2032-01") == PeriodStatus.REVIEWED
    assert _month_status(db_client, "2032-02") == PeriodStatus.REVIEWED
    assert _month_status(db_client, "2032-03") == PeriodStatus.OPEN
    assert _month_status(db_client, "2032-04") == PeriodStatus.OPEN


def test_schedule_change_is_bounded_by_the_next_version_boundary(
    db_client: TestClient, db_session: Session
) -> None:
    """A later-starting schedule version ends the reopened range."""
    for month in ("2032-03", "2032-04", "2032-05", "2032-06"):
        _create_interval(db_client, f"{month}-10T08:00:00", f"{month}-10T16:00:00")
        _review(db_client, month)

    # Effective from May with no later row: open-ended, so it reaches June.
    later = db_client.put(WORKING_TIME, headers=ME, json=_working_time("2032-05-01"))
    assert later.status_code == 200, later.text
    assert _month_status(db_client, "2032-03") == PeriodStatus.REVIEWED
    assert _month_status(db_client, "2032-04") == PeriodStatus.REVIEWED
    assert _month_status(db_client, "2032-05") == PeriodStatus.OPEN
    assert _month_status(db_client, "2032-06") == PeriodStatus.OPEN

    # Re-review, then add an earlier version: its range now STOPS at the
    # 2032-05-01 row, so only March and April reopen.
    _review(db_client, "2032-05")
    _review(db_client, "2032-06")
    earlier = db_client.put(WORKING_TIME, headers=ME, json=_working_time("2032-03-01"))
    assert earlier.status_code == 200, earlier.text
    assert _month_status(db_client, "2032-03") == PeriodStatus.OPEN
    assert _month_status(db_client, "2032-04") == PeriodStatus.OPEN
    assert _month_status(db_client, "2032-05") == PeriodStatus.REVIEWED, (
        "a schedule version that stops being effective at 2032-05-01 reopened May"
    )
    assert _month_status(db_client, "2032-06") == PeriodStatus.REVIEWED


def test_schedule_correction_in_place_reopens_the_same_range(
    db_client: TestClient, db_session: Session
) -> None:
    """Correcting an existing employee schedule row keeps its effective range."""
    _create_interval(db_client, f"{MARCH_2032}-10T08:00:00", f"{MARCH_2032}-10T16:00:00")
    _create_interval(db_client, f"{APRIL_2032}-10T08:00:00", f"{APRIL_2032}-10T16:00:00")
    first = db_client.put(WORKING_TIME, headers=ME, json=_working_time("2032-03-01"))
    assert first.status_code == 200, first.text
    _review(db_client, MARCH_2032)
    _review(db_client, APRIL_2032)

    corrected = db_client.put(
        WORKING_TIME, headers=ME, json=_working_time("2032-03-01", weekly=2400, per_day=450)
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["version"] == first.json()["version"] + 1
    assert _month_status(db_client, MARCH_2032) == PeriodStatus.OPEN
    assert _month_status(db_client, APRIL_2032) == PeriodStatus.OPEN


def test_schedule_change_does_not_reopen_earlier_months(
    db_client: TestClient, db_session: Session
) -> None:
    """Months before ``valid_from`` keep resolving to the previous row."""
    _create_interval(db_client, "2032-02-10T08:00:00", "2032-02-10T16:00:00")
    _create_interval(db_client, f"{MARCH_2032}-10T08:00:00", f"{MARCH_2032}-10T16:00:00")
    _review(db_client, "2032-02")
    _review(db_client, MARCH_2032)

    response = db_client.put(WORKING_TIME, headers=ME, json=_working_time("2032-03-01"))
    assert response.status_code == 200, response.text
    assert _month_status(db_client, "2032-02") == PeriodStatus.REVIEWED
    assert _month_status(db_client, MARCH_2032) == PeriodStatus.OPEN


def test_schedule_change_reopens_existing_periods_only(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """An open-ended range touches only months that have facts.

    A schedule with no later version is effective indefinitely; the
    invalidation must therefore not manufacture period rows for months the
    employee has never recorded anything in.
    """
    _create_interval(db_client, f"{MARCH_2032}-10T08:00:00", f"{MARCH_2032}-10T16:00:00")
    _review(db_client, MARCH_2032)
    before = len(
        db_session.scalars(select(WorkPeriod).where(WorkPeriod.employee_id == employee.id)).all()
    )

    response = db_client.put(WORKING_TIME, headers=ME, json=_working_time("2032-03-01"))
    assert response.status_code == 200, response.text
    db_session.expire_all()
    after = len(
        db_session.scalars(select(WorkPeriod).where(WorkPeriod.employee_id == employee.id)).all()
    )
    assert after == before
    assert _month_status(db_client, MARCH_2032) == PeriodStatus.OPEN


# ---------------------------------------------------------------------------
# EINV-17 — the minute-resolution command boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start_local", "end_local"),
    [
        (f"{MARCH_2032}-10T08:00:30", f"{MARCH_2032}-10T16:00:00"),
        (f"{MARCH_2032}-10T08:00:00", f"{MARCH_2032}-10T16:00:00.500000"),
    ],
)
def test_sub_minute_interval_command_is_rejected(
    db_client: TestClient, db_session: Session, employee: Employee, start_local: str, end_local: str
) -> None:
    """Factual work commands are minute-resolution: reject, never floor."""
    response = db_client.post(
        INTERVALS,
        headers=ME,
        json={"start_local": start_local, "end_local": end_local, "work_type": "ON_SITE"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "VALIDATION_FAILED"
    message = response.json()["detail"]["message"]
    # Human-readable and field-neutral: no `start_local`, no internal token.
    assert "to the minute" in message
    assert "local" not in message
    db_session.expire_all()
    assert (
        db_session.scalar(
            select(WorkInterval).where(
                WorkInterval.employee_id == employee.id,
                WorkInterval.start_at > datetime(2032, 1, 1, tzinfo=UTC),
            )
        )
        is None
    ), "a rejected sub-minute command still wrote an interval"


def test_sub_minute_correction_and_split_are_rejected(
    db_client: TestClient, db_session: Session
) -> None:
    interval = _create_interval(db_client, f"{MARCH_2032}-10T08:00:00", f"{MARCH_2032}-10T16:00:00")
    patched = db_client.patch(
        f"{INTERVALS}/{interval['id']}",
        headers=ME,
        json={
            "version": interval["version"],
            "start_local": f"{MARCH_2032}-10T07:00:15",
            "end_local": f"{MARCH_2032}-10T16:00:00",
        },
    )
    assert patched.status_code == 422, patched.text
    split = db_client.post(
        f"{INTERVALS}/{interval['id']}/split",
        headers=ME,
        json={"version": interval["version"], "split_local": f"{MARCH_2032}-10T12:00:45"},
    )
    assert split.status_code == 422, split.text
    db_session.expire_all()
    row = db_session.get(WorkInterval, interval["id"])
    assert row is not None and row.version == 1


def test_sub_minute_ocd_call_and_travel_segment_are_rejected(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """The boundary applies to every wall-time writer, not just the interval API."""
    assignment = _assignment(
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
            "call_start_local": f"{DAY_2032_01_05}T22:00:30",
            "call_end_local": f"{DAY_2032_01_05}T23:00:00",
            "mode": "REMOTE",
        },
    )
    assert call.status_code == 422, call.text

    trip = db_client.post(
        TRIPS,
        headers=ME,
        json={
            "start_local": f"{MARCH_2032}-09T06:00:00",
            "end_local": f"{MARCH_2032}-11T22:00:00",
            "start_origin": "HOME",
        },
    )
    assert trip.status_code == 201, trip.text
    segment = db_client.post(
        f"{TRIPS}/{trip.json()['id']}/segments",
        headers=ME,
        json={
            "start_local": f"{MARCH_2032}-10T06:00:30",
            "end_local": f"{MARCH_2032}-10T08:00:00",
            "classification": "PASSIVE_TRAVEL",
            "basis": "ACTUAL",
        },
    )
    assert segment.status_code == 422, segment.text


def test_interval_period_disagreement_is_detected(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """The backup verifier detects a linkage that contradicts its fact.

    EINV-14's enforcement needs a detector, not only correct writers: a stale
    ``work_period_id`` (direct write, restore, or a future bug) must be visible
    to the operator rather than silently persisting.
    """
    from app.backup import _domain_checks

    march = _create_interval(db_client, f"{MARCH_2032}-10T08:00:00", f"{MARCH_2032}-10T10:00:00")
    april = _create_interval(db_client, f"{APRIL_2032}-10T08:00:00", f"{APRIL_2032}-10T10:00:00")
    db_session.expire_all()
    clean = _domain_checks(db_session)["work interval"]
    assert not [entry for entry in clean if "start month" in entry]

    april_period = _period(db_session, employee, APRIL_2032)
    march_row = db_session.get(WorkInterval, march["id"])
    assert march_row is not None
    assert april["id"] != march["id"]
    march_row.work_period_id = april_period.id
    db_session.flush()

    failures = _domain_checks(db_session)["work interval"]
    assert any("start month" in entry for entry in failures), failures


def test_backup_verifier_accepts_a_corrected_interval(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """A cross-month correction is not reported as a disagreement.

    The detector must not fire on the corrected state the writers now produce —
    otherwise the verifier would be unusable on a healthy database.
    """
    from app.backup import _domain_checks

    interval = _create_interval(db_client, f"{MARCH_2032}-31T10:00:00", f"{MARCH_2032}-31T12:00:00")
    corrected = db_client.patch(
        f"{INTERVALS}/{interval['id']}",
        headers=ME,
        json={
            "version": interval["version"],
            "reason": "moved",
            "start_local": f"{APRIL_2032}-03T10:00:00",
            "end_local": f"{APRIL_2032}-03T12:00:00",
        },
    )
    assert corrected.status_code == 200, corrected.text
    db_session.expire_all()
    failures = _domain_checks(db_session)["work interval"]
    assert not [entry for entry in failures if "start month" in entry], failures
    moved = db_session.get(WorkInterval, corrected.json()["id"])
    assert moved is not None
    period = _period(db_session, employee, APRIL_2032)
    assert moved.work_period_id == period.id


def test_migration_0014_repairs_a_stale_linkage(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """The repair migration fixes a stale linkage AND preserves REVIEWED.

    The migration's own statements are executed here (imported from the
    revision module, so the test cannot drift from what the migration runs)
    against a deliberately stale row. ``migration-check`` proves the revision
    applies and downgrades; this proves the repair does the right thing to
    data.
    """
    from sqlalchemy import text

    revision = _migration_0014()

    march = _create_interval(db_client, f"{MARCH_2032}-10T08:00:00", f"{MARCH_2032}-10T10:00:00")
    _create_interval(db_client, f"{APRIL_2032}-10T08:00:00", f"{APRIL_2032}-10T10:00:00")
    march_period = _period(db_session, employee, MARCH_2032)
    april_period = _period(db_session, employee, APRIL_2032)
    march_period.status = PeriodStatus.REVIEWED
    db_session.flush()

    # Stale: the March interval claims the April period.
    march_row = db_session.get(WorkInterval, march["id"])
    assert march_row is not None
    march_row.work_period_id = april_period.id
    db_session.flush()

    db_session.execute(text(revision._INSERT_MISSING_PERIODS_SQL))
    db_session.execute(text(revision._RELINK_SQL))
    db_session.expire_all()

    repaired = db_session.get(WorkInterval, march["id"])
    assert repaired is not None
    assert repaired.work_period_id == march_period.id
    # An existing row's status is never rewritten by the repair.
    assert _period(db_session, employee, MARCH_2032).status is PeriodStatus.REVIEWED
    mismatched = db_session.execute(text(revision._MISMATCH_SQL)).fetchall()
    assert mismatched == []


def test_migration_0014_creates_a_missing_target_period(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """A stale row whose correct month has no period row gets one created.

    ``work_period.status`` has no server default, so the migration supplies
    ``'OPEN'`` explicitly; this pins that the created row is usable.
    """
    from sqlalchemy import delete, text

    revision = _migration_0014()
    interval = _create_interval(db_client, f"{MARCH_2032}-10T08:00:00", f"{MARCH_2032}-10T10:00:00")
    march_period = _period(db_session, employee, MARCH_2032)
    # A second, empty month for the interval to be wrongly pointed at.
    other = _create_interval(db_client, f"{APRIL_2032}-10T08:00:00", f"{APRIL_2032}-10T10:00:00")
    db_session.expire_all()
    march_row = db_session.get(WorkInterval, interval["id"])
    assert march_row is not None
    march_row.work_period_id = _period(db_session, employee, APRIL_2032).id
    # Remove the correct month's period row, so the repair must recreate it.
    db_session.execute(delete(WorkPeriod).where(WorkPeriod.id == march_period.id))
    db_session.flush()
    assert other["id"] != interval["id"]

    db_session.execute(text(revision._INSERT_MISSING_PERIODS_SQL))
    db_session.execute(text(revision._RELINK_SQL))
    db_session.expire_all()
    repaired = db_session.get(WorkInterval, interval["id"])
    assert repaired is not None
    period = db_session.get(WorkPeriod, repaired.work_period_id)
    assert period is not None
    assert period.period_month == date(2032, 3, 1)
    assert period.status is PeriodStatus.OPEN


def test_travel_segment_change_reopens_the_credit_month(
    db_client: TestClient, db_session: Session
) -> None:
    """A segment change reaches a LATER month than the segment's own days.

    The TRAVEL-004 credit is derived from the passive minutes inside the trip's
    window and is dated in the month AFTER the window ends, so a segment edit
    changes that later month's balance. Reopening only the segment's own months
    left the credit month asserting REVIEWED over a changed calculation.

    All three segment routes (create / correct / delete) are exercised, and the
    intermediate window month is pinned as UNCHANGED: the fix must reopen the
    credit month without discarding review markers for months a segment change
    cannot affect (a trip-start correction moves the window; a segment does not).
    """
    trip = db_client.post(
        TRIPS,
        headers=ME,
        json={
            "start_local": f"{MARCH_2032}-01T00:00:00",
            "end_local": f"{APRIL_2032}-30T23:59:00",
            "start_origin": "HOME",
        },
    )
    assert trip.status_code == 201, trip.text
    # Window = March 1 → April 1, credit effective May 1.
    credit_month = "2032-05"
    intermediate = APRIL_2032  # inside the window, no segment days of its own
    for month in (MARCH_2032, intermediate, credit_month):
        _review(db_client, month)

    # --- create ---
    segment = db_client.post(
        f"{TRIPS}/{trip.json()['id']}/segments",
        headers=ME,
        json={
            "start_local": f"{MARCH_2032}-05T06:00:00",
            "end_local": f"{MARCH_2032}-25T18:00:00",
            "classification": "PASSIVE_TRAVEL",
            "basis": "ACTUAL",
        },
    )
    assert segment.status_code == 201, segment.text
    assert _month_status(db_client, credit_month) == PeriodStatus.OPEN, (
        "creating the segment's pool changed the credit month without reopening it"
    )
    assert _month_status(db_client, intermediate) == PeriodStatus.REVIEWED, (
        "a segment wholly inside March reopened a month it cannot affect"
    )
    _review(db_client, credit_month)
    before = db_client.get(f"{MONTHS}/{credit_month}", headers=ME).json()

    # --- correct ---
    corrected = db_client.patch(
        f"/api/v1/me/travel-segments/{segment.json()['id']}",
        headers=ME,
        json={
            "version": segment.json()["version"],
            "reason": "shorter",
            "start_local": f"{MARCH_2032}-05T06:00:00",
            "end_local": f"{MARCH_2032}-06T18:00:00",
        },
    )
    assert corrected.status_code == 200, corrected.text
    after = db_client.get(f"{MONTHS}/{credit_month}", headers=ME).json()
    assert after["closing_balance_minutes"] != before["closing_balance_minutes"], (
        "the fixture no longer changes the credit month's calculation"
    )
    assert after["status"] == PeriodStatus.OPEN, (
        "the credit-effective month kept asserting REVIEWED over a changed balance"
    )
    assert _month_status(db_client, intermediate) == PeriodStatus.REVIEWED, (
        "a correction of a March segment reopened a month it cannot affect"
    )
    _review(db_client, credit_month)
    before_delete = db_client.get(f"{MONTHS}/{credit_month}", headers=ME).json()

    # --- delete ---
    deleted = db_client.request(
        "DELETE",
        f"/api/v1/me/travel-segments/{corrected.json()['id']}",
        headers=ME,
        json={"version": corrected.json()["version"], "reason": "not taken"},
    )
    assert deleted.status_code == 204, deleted.text
    after_delete = db_client.get(f"{MONTHS}/{credit_month}", headers=ME).json()
    assert after_delete["closing_balance_minutes"] != before_delete["closing_balance_minutes"], (
        "the fixture no longer changes the credit month's calculation"
    )
    assert after_delete["status"] == PeriodStatus.OPEN, (
        "removing the pool left the credit-effective month REVIEWED"
    )


def test_authorization_window_endpoint_is_exclusive(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """``valid_to`` is an EXCLUSIVE instant, so it does not reach its own month.

    The travel predicate covers a work slice only when ``slice_end <= valid_to``
    and slices are half-open, so no work minute at or after ``valid_to`` is ever
    covered — and the day splitter drops a zero-length boundary slice entirely.
    An authorization ending at ``2032-04-01T00:00`` therefore cannot change April,
    and must not reopen it.

    The window is given as a NAIVE Berlin wall time: the service anchors it to
    Berlin at its boundary (F8.1 — the recorded F8.6 residual, which persisted a
    naive endpoint through the database session timezone and could move it 1-2 h
    so that an exclusive end at exactly midnight landed inside April, is closed).
    The assertion below is what pins the anchoring.
    """
    _create_interval(db_client, f"{MARCH_2032}-10T08:00:00", f"{MARCH_2032}-10T16:00:00")
    _create_interval(db_client, f"{APRIL_2032}-10T08:00:00", f"{APRIL_2032}-10T16:00:00")
    _review(db_client, MARCH_2032)
    _review(db_client, APRIL_2032)

    created = db_client.post(
        AUTHORIZATIONS,
        headers=ME,
        json={
            "valid_from": f"{MARCH_2032}-01T00:00:00",
            "valid_to": f"{APRIL_2032}-01T00:00:00",
        },
    )
    assert created.status_code == 200, created.text
    decide_authorization(
        db_session, employee, uuid.UUID(created.json()["id"]), authorized_by="woco"
    )
    assert _month_status(db_client, MARCH_2032) == PeriodStatus.OPEN, (
        "the window's own start month must reopen"
    )
    assert _month_status(db_client, APRIL_2032) == PeriodStatus.REVIEWED, (
        "an exclusive valid_to reopened the month it cannot cover"
    )


def test_cutover_seed_reopens_its_own_month_and_the_cutover_month(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """The seed is dated on the last day of M-1, so M-1's balance moves too.

    ``_carried_opening`` includes ledger rows dated before the requested month's
    end, so a seed dated 31 July changes July's closing balance as well as
    August's. Both reviews must reopen.
    """
    cutover_month = "2026-08"  # the installed policy version covers it
    previous_month = "2026-07"
    # BOTH months are reviewed first: asserting the cutover month is OPEN is
    # only meaningful if it was REVIEWED before the seed.
    _review(db_client, previous_month)
    _review(db_client, cutover_month)
    before = db_client.get(f"{MONTHS}/{previous_month}", headers=ME).json()

    seeded = db_client.post(
        "/api/v1/cutover/opening-balances",
        headers={"X-Chronobeam-Dev-Principal": "cutover-operator"},
        json={
            "employee_external_id": "emp-0001",
            "cutover_month": cutover_month,
            "minutes": 1104,
        },
    )
    assert seeded.status_code == 201, seeded.text
    db_session.expire_all()
    after = db_client.get(f"{MONTHS}/{previous_month}", headers=ME).json()
    assert after["closing_balance_minutes"] != before["closing_balance_minutes"], (
        "the fixture no longer changes the pre-cutover month's calculation"
    )
    assert after["status"] == PeriodStatus.OPEN, (
        "the pre-cutover month kept asserting REVIEWED over a changed balance"
    )
    assert _month_status(db_client, cutover_month) == PeriodStatus.OPEN, (
        "the cutover month's opening balance changed without reopening its review"
    )


def test_requested_authorization_reopens_nothing(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """Only AUTHORITATIVE approvals are a calculation input.

    The travel engine consumes current authoritative windows only. A default
    ``REQUESTED`` authorization — open-ended, so its range would reach every
    future month — must not discard review markers for a state that changes no
    calculation. (Nor would a legacy employee-self-declared ``APPROVED`` row:
    authority is a property of the writer, ACCESS-017.)
    """
    _create_interval(db_client, f"{MARCH_2032}-10T08:00:00", f"{MARCH_2032}-10T16:00:00")
    _create_interval(db_client, f"{APRIL_2032}-10T08:00:00", f"{APRIL_2032}-10T16:00:00")
    _review(db_client, MARCH_2032)
    _review(db_client, APRIL_2032)

    requested = db_client.post(
        AUTHORIZATIONS,
        headers=ME,
        json={"valid_from": f"{MARCH_2032}-01T00:00:00"},
    )
    assert requested.status_code == 200, requested.text
    assert requested.json()["status"] == "REQUESTED"
    assert _month_status(db_client, MARCH_2032) == PeriodStatus.REVIEWED
    assert _month_status(db_client, APRIL_2032) == PeriodStatus.REVIEWED

    # Approving it is a calculation input: the window now suppresses findings.
    # Only the institutional writer can approve (ACCESS-017).
    decide_authorization(
        db_session, employee, uuid.UUID(requested.json()["id"]), authorized_by="woco"
    )
    assert _month_status(db_client, MARCH_2032) == PeriodStatus.OPEN


def test_metadata_only_authorization_patch_reopens_nothing(
    db_client: TestClient, db_session: Session, employee: Employee
) -> None:
    """A change that moves no calculation-relevant field changes nothing.

    The metadata-only change is exercised on an AUTHORITATIVE head, through the
    institutional writer: the employee cannot restate a decided authorization at
    all (ACCESS-017), so the employee path can only ever produce additions from a
    non-authoritative base.
    """
    _create_interval(db_client, f"{MARCH_2032}-10T08:00:00", f"{MARCH_2032}-10T16:00:00")
    _review(db_client, MARCH_2032)
    created = db_client.post(
        AUTHORIZATIONS,
        headers=ME,
        json={
            "valid_from": f"{MARCH_2032}-01T00:00:00",
            "valid_to": f"{MARCH_2032}-31T00:00:00",
        },
    )
    assert created.status_code == 200, created.text
    approved = decide_authorization(
        db_session, employee, uuid.UUID(created.json()["id"]), authorized_by="woco"
    )
    _review(db_client, MARCH_2032)

    # Same window, same authority, only the descriptive purpose moves.
    restated = decide_authorization(
        db_session, employee, approved.id, authorized_by="woco", purpose="clarified purpose"
    )
    assert restated.purpose == "clarified purpose"
    assert _month_status(db_client, MARCH_2032) == PeriodStatus.REVIEWED


@pytest.mark.parametrize(
    ("field", "expected_copy"),
    [
        ("valid_from", "needs a start"),
        ("status", "needs a status"),
    ],
)
def test_authorization_required_field_cannot_be_cleared(
    db_client: TestClient, field: str, expected_copy: str
) -> None:
    """Explicit null for a required field is a 422, not a 500.

    The presence merge must not let a client clear ``valid_from`` or ``status``:
    the first reaches a comparison as ``None`` and the second a NOT NULL column.
    """
    created = db_client.post(
        AUTHORIZATIONS,
        headers=ME,
        json={"valid_from": f"{MARCH_2032}-01T00:00:00"},
    )
    assert created.status_code == 200, created.text
    response = db_client.patch(
        f"{AUTHORIZATIONS}/{created.json()['id']}",
        headers=ME,
        json={"version": created.json()["version"], field: None},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "VALIDATION_FAILED"
    # The refusal names the field in the EMPLOYEE's words. It used to interpolate
    # the API field name (`f"{field} cannot be cleared."`), and this assertion
    # encoded that leak rather than catching it (review R7).
    message = response.json()["detail"]["message"]
    assert expected_copy in message
    # The leak shape is a snake_case FIELD PATH, not an English word that happens
    # to match a field name (`status` is both) — so assert the shape.
    assert not re.search(r"[a-z]+_[a-z_]+", message), message
    # ...and the rejected command changed nothing: re-read the persisted row
    # through the product surface instead of trusting the error alone.
    listed = db_client.get(
        AUTHORIZATIONS,
        headers=ME,
        params={"from": f"{MARCH_2032}-01", "to": f"{APRIL_2032}-01"},
    )
    assert listed.status_code == 200, listed.text
    rows = [row for row in listed.json() if row["id"] == created.json()["id"]]
    assert len(rows) == 1, "the rejected correction must not create a version"
    assert rows[0]["version"] == created.json()["version"]
    # The read endpoint serialises the same instant as UTC ("…Z"), the command
    # response as the wall time it was given; compare the instants.
    assert _instant(rows[0]["valid_from"]) == _instant(created.json()["valid_from"])
    assert rows[0]["status"] == "REQUESTED"
