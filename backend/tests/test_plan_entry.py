"""Employee-entered planned shifts: create / correct / withdraw (pilot slice).

The switch is per DOMAIN and defaults OFF, so every test that exercises a write
path builds a client with the switch explicitly ON. Tests that pin the OFF
behaviour use the plain ``db_client`` (whose environment leaves both switches
unset) or a factory-built client, and never depend on another test's environment.
"""

import uuid
from datetime import datetime
from typing import Any

from app.auth.principal import Capability
from app.enums import EMPLOYEE_SOURCE, ShiftAssignmentStatus
from app.identity.models import Employee
from app.integrations.shift_plan import ShiftAssignmentSeed
from app.shift.models import ShiftAssignment, ShiftPlan, ShiftPlanRevision
from app.shift.service import seed_shift_assignment
from app.worktime.models import Correction, WorkInterval
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from .conftest import DEVELOPER_PRINCIPAL_HEADER, HR_HEADER, OTHER_EMPLOYEE_HEADER

SHIFTS = "/api/v1/me/shifts"


def _employee(session: Session) -> Employee:
    employee = session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    return employee


def _create(
    client: TestClient,
    *,
    start: str = "2026-07-10T09:00:00",
    end: str = "2026-07-10T17:00:00",
    kind: str | None = "DAY",
    command: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "planned_start_local": start,
        "planned_end_local": end,
        "shift_kind": kind,
        "client_request_id": command or str(uuid.uuid4()),
    }
    response = client.post(SHIFTS, headers=DEVELOPER_PRINCIPAL_HEADER, json=payload)
    assert response.status_code == 201, response.text  # a real create
    return response.json()


def _shift_ids(
    client: TestClient, *, start: str = "2026-07-01", end: str = "2026-09-01"
) -> list[str]:
    response = client.get(f"{SHIFTS}?from={start}&to={end}", headers=DEVELOPER_PRINCIPAL_HEADER)
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()]


def _containers(session: Session, employee: Employee) -> list[ShiftPlan]:
    return list(
        session.scalars(
            select(ShiftPlan).where(ShiftPlan.plan_key == f"employee:{employee.id}")
        ).all()
    )


def test_employee_creates_own_planned_shift(entry_client: TestClient, db_session: Session) -> None:
    """The plan is a normal plan for every downstream consumer, and non-institutional."""
    body = _create(entry_client)
    employee = _employee(db_session)

    assignment = db_session.get(ShiftAssignment, uuid.UUID(body["id"]))
    assert assignment is not None
    assert assignment.employee_id == employee.id
    assert assignment.status is ShiftAssignmentStatus.PLANNED
    assert assignment.approved_revision_id is not None

    revision = db_session.get(ShiftPlanRevision, assignment.approved_revision_id)
    assert revision is not None
    assert revision.revision_no == 1
    # Provenance on BOTH the container and the revision: this is what makes the
    # record non-institutional, and what a later authoritative source keys on.
    assert revision.source == EMPLOYEE_SOURCE
    assert revision.communicated_at is None  # D9: never inferred

    plan = db_session.get(ShiftPlan, assignment.shift_plan_id)
    assert plan is not None
    assert plan.source == EMPLOYEE_SOURCE
    assert plan.source_ref is None
    assert plan.plan_key == f"employee:{employee.id}"

    assert body["id"] in _shift_ids(entry_client)


def test_employee_cannot_create_an_assignment_for_another_employee(
    entry_client: TestClient, db_session: Session
) -> None:
    """``employee_id`` is never a request field; smuggling one fails loudly."""
    other = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee-b"))
    assert other is not None
    response = entry_client.post(
        SHIFTS,
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "planned_start_local": "2026-07-10T09:00:00",
            "planned_end_local": "2026-07-10T17:00:00",
            "client_request_id": str(uuid.uuid4()),
            "employee_id": str(other.id),
        },
    )
    assert response.status_code == 422, response.text

    # ...and the created row belongs to the SESSION employee regardless.
    body = _create(entry_client)
    assignment = db_session.get(ShiftAssignment, uuid.UUID(body["id"]))
    assert assignment is not None and assignment.employee_id == _employee(db_session).id


def test_a_plan_edit_keeps_the_window_columns_in_lockstep(
    entry_client: TestClient, db_session: Session
) -> None:
    """The head cache is a denormalized copy; a stale one hides the shift."""
    body = _create(entry_client, start="2026-07-10T09:00:00", end="2026-07-10T17:00:00")
    moved = entry_client.patch(
        f"{SHIFTS}/{body['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "approved_revision_id": body["approved_revision"]["id"],
            "planned_start_local": "2026-07-20T09:00:00",
            "planned_end_local": "2026-07-20T17:00:00",
        },
    )
    assert moved.status_code == 200, moved.text

    # Returned in the NEW window, and NOT in the old one.
    assert body["id"] in _shift_ids(entry_client, start="2026-07-15", end="2026-07-25")
    assert body["id"] not in _shift_ids(entry_client, start="2026-07-05", end="2026-07-15")

    assignment = db_session.get(ShiftAssignment, uuid.UUID(body["id"]))
    assert assignment is not None
    revision = db_session.get(ShiftPlanRevision, assignment.approved_revision_id)
    assert revision is not None
    # The invariant, asserted directly: cache == head revision.
    assert assignment.planned_start_at == revision.planned_start_at
    assert assignment.planned_end_at == revision.planned_end_at
    assert revision.revision_no == 2


def test_creating_two_shifts_reuses_one_employee_container(
    entry_client: TestClient, db_session: Session
) -> None:
    _create(entry_client, start="2026-07-10T09:00:00", end="2026-07-10T17:00:00")
    _create(entry_client, start="2026-07-11T09:00:00", end="2026-07-11T17:00:00")
    containers = _containers(db_session, _employee(db_session))
    assert len(containers) == 1


def test_container_valid_from_is_the_monotonic_coverage_minimum(
    entry_client: TestClient, db_session: Session
) -> None:
    """D21: coverage metadata, not the first entry's date frozen forever."""
    _create(entry_client, start="2026-07-10T09:00:00", end="2026-07-10T17:00:00")
    containers = _containers(db_session, _employee(db_session))
    assert containers[0].valid_from.isoformat() == "2026-07-10"

    # A LATER entry for an EARLIER date must extend coverage backwards, or the
    # container would claim validity starting after its own contents.
    _create(entry_client, start="2026-06-01T09:00:00", end="2026-06-01T17:00:00")
    containers = _containers(db_session, _employee(db_session))
    assert containers[0].valid_from.isoformat() == "2026-06-01"

    # And a later date never moves it forward again.
    _create(entry_client, start="2026-08-01T09:00:00", end="2026-08-01T17:00:00")
    containers = _containers(db_session, _employee(db_session))
    assert containers[0].valid_from.isoformat() == "2026-06-01"


def test_plan_create_requires_a_client_request_id(entry_client: TestClient) -> None:
    """A create must be retry-safe; without the key a retry would double-create."""
    response = entry_client.post(
        SHIFTS,
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "planned_start_local": "2026-07-10T09:00:00",
            "planned_end_local": "2026-07-10T17:00:00",
        },
    )
    assert response.status_code == 422, response.text


def test_a_stale_revision_token_is_refused_on_edit_and_withdraw(
    entry_client: TestClient, db_session: Session
) -> None:
    body = _create(entry_client)
    stale = body["approved_revision"]["id"]
    first = entry_client.patch(
        f"{SHIFTS}/{body['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "approved_revision_id": stale,
            "planned_start_local": "2026-07-10T10:00:00",
            "planned_end_local": "2026-07-10T18:00:00",
        },
    )
    assert first.status_code == 200, first.text

    for response in (
        entry_client.patch(
            f"{SHIFTS}/{body['id']}",
            headers=DEVELOPER_PRINCIPAL_HEADER,
            json={
                "approved_revision_id": stale,
                "planned_start_local": "2026-07-10T11:00:00",
                "planned_end_local": "2026-07-10T19:00:00",
            },
        ),
        entry_client.post(
            f"{SHIFTS}/{body['id']}/withdraw",
            headers=DEVELOPER_PRINCIPAL_HEADER,
            json={"approved_revision_id": stale},
        ),
    ):
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "CONFLICT_SHIFT_PLAN_CHANGED"

    assignment = db_session.get(ShiftAssignment, uuid.UUID(body["id"]))
    assert assignment is not None
    assert assignment.status is ShiftAssignmentStatus.PLANNED
    revisions = list(
        db_session.scalars(
            select(ShiftPlanRevision).where(ShiftPlanRevision.shift_assignment_id == assignment.id)
        ).all()
    )
    assert len(revisions) == 2  # the successful edit only


def test_exact_plan_confirmation_works_against_a_manually_entered_plan(
    entry_client: TestClient, db_session: Session
) -> None:
    """The existing confirm command consumes an employee plan with no change."""
    body = _create(entry_client)
    confirmed = entry_client.post(
        f"{SHIFTS}/{body['id']}/confirm",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"approved_revision_id": body["approved_revision"]["id"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    intervals = list(
        db_session.scalars(
            select(WorkInterval).where(
                WorkInterval.linked_shift_assignment_id == uuid.UUID(body["id"])
            )
        ).all()
    )
    assert len(intervals) == 1
    assert intervals[0].work_type.value == "SHIFT"


def test_deviation_against_a_manually_entered_plan(
    entry_client: TestClient, db_session: Session
) -> None:
    body = _create(entry_client)
    response = entry_client.post(
        f"{SHIFTS}/{body['id']}/deviation",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "deviation_type": "SHORTENED",
            "actual_start_local": "2026-07-10T09:00:00",
            "actual_end_local": "2026-07-10T13:00:00",
            "reason": "Left early.",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["deviation"]["deviation_type"] == "SHORTENED"


def test_edit_and_withdraw_are_allowed_before_any_actual(
    entry_client: TestClient, db_session: Session
) -> None:
    body = _create(entry_client)
    edited = entry_client.patch(
        f"{SHIFTS}/{body['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "approved_revision_id": body["approved_revision"]["id"],
            "planned_start_local": "2026-07-10T10:00:00",
            "planned_end_local": "2026-07-10T18:00:00",
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["approved_revision"]["revision_no"] == 2

    withdrawn = entry_client.post(
        f"{SHIFTS}/{body['id']}/withdraw",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"approved_revision_id": edited.json()["approved_revision"]["id"]},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["status"] == "WITHDRAWN"

    # The revision chain survives as lineage, and the act is recorded.
    revisions = list(
        db_session.scalars(
            select(ShiftPlanRevision).where(
                ShiftPlanRevision.shift_assignment_id == uuid.UUID(body["id"])
            )
        ).all()
    )
    assert len(revisions) == 2
    corrections = list(
        db_session.scalars(
            select(Correction).where(Correction.entity_id == uuid.UUID(body["id"]))
        ).all()
    )
    assert len(corrections) == 2  # one for the edit, one for the withdrawal


def test_plan_edit_is_refused_once_a_linked_actual_interval_exists(
    entry_client: TestClient, db_session: Session
) -> None:
    """D3, binding: a plan freezes on the first dependency."""
    body = _create(entry_client)
    entry_client.post(
        f"{SHIFTS}/{body['id']}/confirm",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"approved_revision_id": body["approved_revision"]["id"]},
    )
    revision_id = body["approved_revision"]["id"]
    for response in (
        entry_client.patch(
            f"{SHIFTS}/{body['id']}",
            headers=DEVELOPER_PRINCIPAL_HEADER,
            json={
                "approved_revision_id": revision_id,
                "planned_start_local": "2026-07-10T11:00:00",
                "planned_end_local": "2026-07-10T19:00:00",
            },
        ),
        entry_client.post(
            f"{SHIFTS}/{body['id']}/withdraw",
            headers=DEVELOPER_PRINCIPAL_HEADER,
            json={"approved_revision_id": revision_id},
        ),
    ):
        assert response.status_code == 409, response.text
        # The refusal names the reason AND the next step the employee can take.
        assert "deviation" in response.json()["detail"]["message"].lower()

    assignment = db_session.get(ShiftAssignment, uuid.UUID(body["id"]))
    assert assignment is not None and assignment.status is ShiftAssignmentStatus.PLANNED


def test_plan_edit_and_withdraw_are_refused_once_a_deviation_exists(
    entry_client: TestClient, db_session: Session
) -> None:
    body = _create(entry_client)
    entry_client.post(
        f"{SHIFTS}/{body['id']}/deviation",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "deviation_type": "SHORTENED",
            "actual_start_local": "2026-07-10T09:00:00",
            "actual_end_local": "2026-07-10T13:00:00",
        },
    )
    revision_id = body["approved_revision"]["id"]
    for response in (
        entry_client.patch(
            f"{SHIFTS}/{body['id']}",
            headers=DEVELOPER_PRINCIPAL_HEADER,
            json={
                "approved_revision_id": revision_id,
                "planned_start_local": "2026-07-10T11:00:00",
                "planned_end_local": "2026-07-10T19:00:00",
            },
        ),
        entry_client.post(
            f"{SHIFTS}/{body['id']}/withdraw",
            headers=DEVELOPER_PRINCIPAL_HEADER,
            json={"approved_revision_id": revision_id},
        ),
    ):
        assert response.status_code == 409, response.text


def test_plan_edit_and_withdraw_are_refused_once_a_paid_break_exists(
    entry_client: TestClient, db_session: Session
) -> None:
    """The D3 freeze has NO containment escape hatch."""
    body = _create(entry_client, start="2026-07-10T09:00:00", end="2026-07-10T17:00:00")
    created = entry_client.post(
        "/api/v1/me/break-intervals",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": "2026-07-10T12:00:00",
            "end_local": "2026-07-10T12:30:00",
            "kind": "PAID_SHIFT_BREAK",
            "linked_shift_assignment_id": body["id"],
            "client_request_id": str(uuid.uuid4()),
        },
    )
    assert created.status_code == 201, created.text

    revision_id = body["approved_revision"]["id"]
    # An edit that would still CONTAIN the break is refused too — there is no
    # containment exception, because the break is linked, not span-filtered.
    for response in (
        entry_client.patch(
            f"{SHIFTS}/{body['id']}",
            headers=DEVELOPER_PRINCIPAL_HEADER,
            json={
                "approved_revision_id": revision_id,
                "planned_start_local": "2026-07-10T08:00:00",
                "planned_end_local": "2026-07-10T18:00:00",
            },
        ),
        entry_client.post(
            f"{SHIFTS}/{body['id']}/withdraw",
            headers=DEVELOPER_PRINCIPAL_HEADER,
            json={"approved_revision_id": revision_id},
        ),
    ):
        assert response.status_code == 409, response.text


def test_a_plan_revision_moves_the_shift_report_fingerprint(
    entry_client: TestClient,
) -> None:
    body = _create(entry_client)
    before = entry_client.get(
        f"{SHIFTS}/{body['id']}/report-preview", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert before.status_code == 200, before.text

    entry_client.patch(
        f"{SHIFTS}/{body['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "approved_revision_id": body["approved_revision"]["id"],
            "planned_start_local": "2026-07-10T10:00:00",
            "planned_end_local": "2026-07-10T18:00:00",
        },
    )
    after = entry_client.get(
        f"{SHIFTS}/{body['id']}/report-preview", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert after.status_code == 200, after.text
    assert after.json()["preview_fingerprint"] != before.json()["preview_fingerprint"]


def test_an_imported_plan_is_not_employee_editable_or_withdrawable(
    entry_client: TestClient, db_session: Session
) -> None:
    """Deny by default: only what the employee authored is employee-writable."""
    assignment = seed_shift_assignment(
        db_session,
        _employee(db_session),
        ShiftAssignmentSeed(
            plan_key="synthetic-night-plan",
            plan_name="Synthetic night plan",
            planned_start_at=datetime.fromisoformat("2026-07-31T19:00:00+02:00"),
            planned_end_at=datetime.fromisoformat("2026-08-01T07:00:00+02:00"),
            shift_kind="NIGHT",
            source="TEST_SEED",
            source_ref="shift-plan-fixture-v1",
        ),
    )
    db_session.flush()
    revision_id = assignment.approved_revision_id
    assert revision_id is not None
    for response in (
        entry_client.patch(
            f"{SHIFTS}/{assignment.id}",
            headers=DEVELOPER_PRINCIPAL_HEADER,
            json={
                "approved_revision_id": str(revision_id),
                "planned_start_local": "2026-07-31T20:00:00",
                "planned_end_local": "2026-08-01T08:00:00",
            },
        ),
        entry_client.post(
            f"{SHIFTS}/{assignment.id}/withdraw",
            headers=DEVELOPER_PRINCIPAL_HEADER,
            json={"approved_revision_id": str(revision_id)},
        ),
    ):
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "CONFLICT_SHIFT_PLAN_CHANGED"

    refreshed = db_session.get(ShiftAssignment, assignment.id)
    assert refreshed is not None and refreshed.status is ShiftAssignmentStatus.PLANNED
    assert refreshed.approved_revision_id == revision_id


def test_an_unknown_plan_source_is_not_employee_writable(
    entry_client: TestClient, db_session: Session
) -> None:
    """A future adapter's provenance must not become employee-writable by accident."""
    assignment = seed_shift_assignment(
        db_session,
        _employee(db_session),
        ShiftAssignmentSeed(
            plan_key="future-adapter-plan",
            plan_name=None,
            planned_start_at=datetime.fromisoformat("2026-07-31T19:00:00+02:00"),
            planned_end_at=datetime.fromisoformat("2026-08-01T07:00:00+02:00"),
            shift_kind="NIGHT",
            source="FUTURE_ADAPTER_V2",
            source_ref=None,
        ),
    )
    db_session.flush()
    response = entry_client.post(
        f"{SHIFTS}/{assignment.id}/withdraw",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"approved_revision_id": str(assignment.approved_revision_id)},
    )
    assert response.status_code == 409, response.text


def test_employee_plan_routes_do_not_expand_hr_or_other_employee_visibility(
    entry_client: TestClient, db_session: Session
) -> None:
    body = _create(entry_client)
    revision_id = body["approved_revision"]["id"]

    # HR gets the existing denial on every new route.
    for response in (
        entry_client.post(
            SHIFTS,
            headers=HR_HEADER,
            json={
                "planned_start_local": "2026-07-12T09:00:00",
                "planned_end_local": "2026-07-12T17:00:00",
                "client_request_id": str(uuid.uuid4()),
            },
        ),
        entry_client.patch(
            f"{SHIFTS}/{body['id']}",
            headers=HR_HEADER,
            json={"approved_revision_id": revision_id},
        ),
        entry_client.post(
            f"{SHIFTS}/{body['id']}/withdraw",
            headers=HR_HEADER,
            json={"approved_revision_id": revision_id},
        ),
    ):
        assert response.status_code == 403, response.text

    # Another employee cannot tell the row apart from one that does not exist.
    foreign = entry_client.post(
        f"{SHIFTS}/{body['id']}/withdraw",
        headers=OTHER_EMPLOYEE_HEADER,
        json={"approved_revision_id": revision_id},
    )
    missing = entry_client.post(
        f"{SHIFTS}/{uuid.uuid4()}/withdraw",
        headers=OTHER_EMPLOYEE_HEADER,
        json={"approved_revision_id": revision_id},
    )
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()


def test_shift_plan_entry_is_disabled_when_the_shift_switch_is_off(
    entry_client_factory: Any, entry_client: TestClient, db_session: Session
) -> None:
    """422 POLICY_BLOCKED, not 403: this is a deployment posture, not a denial."""
    body = _create(entry_client)
    revision_id = body["approved_revision"]["id"]

    off = entry_client_factory(shift=False, ocd=True)
    for response in (
        off.post(
            SHIFTS,
            headers=DEVELOPER_PRINCIPAL_HEADER,
            json={
                "planned_start_local": "2026-07-12T09:00:00",
                "planned_end_local": "2026-07-12T17:00:00",
                "client_request_id": str(uuid.uuid4()),
            },
        ),
        off.patch(
            f"{SHIFTS}/{body['id']}",
            headers=DEVELOPER_PRINCIPAL_HEADER,
            json={"approved_revision_id": revision_id},
        ),
        off.post(
            f"{SHIFTS}/{body['id']}/withdraw",
            headers=DEVELOPER_PRINCIPAL_HEADER,
            json={"approved_revision_id": revision_id},
        ),
    ):
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == "POLICY_BLOCKED"

    # Existing rows stay READABLE: turning entry off is not a data change.
    readable = off.get(
        f"{SHIFTS}?from=2026-07-01&to=2026-09-01", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert readable.status_code == 200
    assert body["id"] in [row["id"] for row in readable.json()]


def test_both_capabilities_are_exposed_independently(entry_client_factory: Any) -> None:
    """All four combinations, from the ONE existing identity surface."""
    for shift, ocd in ((True, True), (True, False), (False, True), (False, False)):
        client = entry_client_factory(shift=shift, ocd=ocd)
        response = client.get("/api/v1/me", headers=DEVELOPER_PRINCIPAL_HEADER)
        assert response.status_code == 200, response.text
        capabilities = set(response.json()["capabilities"])
        assert (Capability.SHIFT_PLAN_ENTRY_WRITE.value in capabilities) is shift, (shift, ocd)
        assert (Capability.OCD_PLAN_ENTRY_WRITE.value in capabilities) is ocd, (shift, ocd)
        # The baseline is never disturbed by either switch.
        assert "OWN_LEDGER_WRITE" in capabilities


def test_a_same_owner_create_replay_succeeds_after_the_switch_is_off(
    entry_client_factory: Any, entry_client: TestClient
) -> None:
    """D20: an already-committed command is not a new mutation."""
    command = str(uuid.uuid4())
    first = _create(entry_client, command=command)

    off = entry_client_factory(shift=False, ocd=False)
    replay = off.post(
        SHIFTS,
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "planned_start_local": "2026-07-10T09:00:00",
            "planned_end_local": "2026-07-10T17:00:00",
            "shift_kind": "DAY",
            "client_request_id": command,
        },
    )
    # 200, not 201: a replay is not a new create.
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == first["id"]


def test_a_foreign_command_id_returns_the_same_body_whatever_the_switch_is(
    entry_client_factory: Any, entry_client: TestClient, db_session: Session
) -> None:
    """The response must not become a switch-dependent oracle for another's id.

    The key is unique TABLE-WIDE, so a collision with ANOTHER employee's command id
    is reachable. Returning their row would be a disclosure, and returning a
    different error when the switch is off would leak the row's existence.
    """
    other = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee-b"))
    assert other is not None
    # Seed the other employee's plan, then claim its command key for them.
    theirs = seed_shift_assignment(
        db_session,
        other,
        ShiftAssignmentSeed(
            plan_key="other-employee-plan",
            plan_name=None,
            planned_start_at=datetime.fromisoformat("2026-07-15T09:00:00+02:00"),
            planned_end_at=datetime.fromisoformat("2026-07-15T17:00:00+02:00"),
            shift_kind="DAY",
            source=EMPLOYEE_SOURCE,
            source_ref=None,
        ),
    )
    command = str(uuid.uuid4())
    theirs.client_request_id = uuid.UUID(command)
    db_session.flush()

    payload = {
        "planned_start_local": "2026-07-10T09:00:00",
        "planned_end_local": "2026-07-10T17:00:00",
        "client_request_id": command,
    }
    bodies = []
    for client in (entry_client, entry_client_factory(shift=False, ocd=False)):
        response = client.post(SHIFTS, headers=DEVELOPER_PRINCIPAL_HEADER, json=payload)
        assert response.status_code == 422, response.text
        bodies.append(response.json())
    # Identical, and never POLICY_BLOCKED — an id that exists is never reported as
    # "entry is disabled".
    assert bodies[0] == bodies[1]
    assert bodies[0]["detail"]["code"] == "VALIDATION_FAILED"


def test_plan_creation_refuses_an_impossible_wall_time_and_a_reversed_window(
    entry_client: TestClient,
) -> None:
    """DST ambiguity is never guessed at, and the order check is on instants."""
    # 2026-03-29 02:30 does not exist in Europe/Berlin (spring forward).
    nonexistent = entry_client.post(
        SHIFTS,
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "planned_start_local": "2026-03-29T02:30:00",
            "planned_end_local": "2026-03-29T10:00:00",
            "client_request_id": str(uuid.uuid4()),
        },
    )
    assert nonexistent.status_code == 422, nonexistent.text
    assert nonexistent.json()["detail"]["code"] == "VALIDATION_DST_AMBIGUOUS"

    # 2026-10-25 02:30 happens twice; without a fold the choice is not invented.
    ambiguous = entry_client.post(
        SHIFTS,
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "planned_start_local": "2026-10-25T02:30:00",
            "planned_end_local": "2026-10-25T10:00:00",
            "client_request_id": str(uuid.uuid4()),
        },
    )
    assert ambiguous.status_code == 422, ambiguous.text
    assert ambiguous.json()["detail"]["code"] == "VALIDATION_DST_AMBIGUOUS"

    reversed_window = entry_client.post(
        SHIFTS,
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "planned_start_local": "2026-07-10T17:00:00",
            "planned_end_local": "2026-07-10T09:00:00",
            "client_request_id": str(uuid.uuid4()),
        },
    )
    assert reversed_window.status_code == 422, reversed_window.text
