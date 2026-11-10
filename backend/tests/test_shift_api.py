"""Shift workflow integration and source-safe policy cases."""

import uuid
from datetime import datetime
from typing import Any

import pytest
from app.enums import ShiftDeviationType
from app.identity.models import Employee
from app.integrations.shift_plan import ShiftAssignmentSeed
from app.shift.models import ShiftAssignment, ShiftDeviation, ShiftPlanRevision
from app.shift.policy import evaluate_shift
from app.shift.report import ShiftReportInputs, build_shift_report
from app.shift.schemas import ShiftFindingOut
from app.shift.service import seed_shift_assignment
from app.worktime.models import WorkInterval
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .conftest import DEVELOPER_PRINCIPAL_HEADER, HR_HEADER, OTHER_EMPLOYEE_HEADER


def _employee(session: Session) -> Employee:
    employee = session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    return employee


def _seed_shift(session: Session, start: str = "2026-07-31T19:00:00+02:00") -> ShiftAssignment:
    end = "2026-08-01T07:00:00+02:00"
    assignment = seed_shift_assignment(
        session,
        _employee(session),
        ShiftAssignmentSeed(
            plan_key="synthetic-night-plan",
            plan_name="Synthetic night plan",
            planned_start_at=datetime.fromisoformat(start),
            planned_end_at=datetime.fromisoformat(end),
            shift_kind="NIGHT",
            source="TEST_SEED",
            source_ref="shift-plan-fixture-v1",
        ),
    )
    session.flush()
    return assignment


def _shifts(client: TestClient) -> list[dict[str, Any]]:
    response = client.get(
        "/api/v1/me/shifts?from=2026-07-01&to=2026-08-02",
        headers=DEVELOPER_PRINCIPAL_HEADER,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_SHIFT_T01_exact_plan_confirmation_is_one_action_and_idempotent(
    db_client: TestClient, db_session: Session
) -> None:
    assignment = _seed_shift(db_session)
    loaded = _shifts(db_client)[0]
    revision_id = loaded["approved_revision"]["id"]

    response = db_client.post(
        f"/api/v1/me/shifts/{assignment.id}/confirm",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "approved_revision_id": revision_id,
            "client_request_id": "b786e410-462f-4960-bba1-8cc3c8298b37",
        },
    )
    assert response.status_code == 200, response.text
    confirmed = response.json()
    assert confirmed["actual_start_at"] == "2026-07-31T17:00:00Z"
    assert confirmed["actual_end_at"] == "2026-08-01T05:00:00Z"
    assert confirmed["deviation"] is None
    assert "SHIFT-002" not in {finding["rule_id"] for finding in confirmed["findings"]}

    replay = db_client.post(
        f"/api/v1/me/shifts/{assignment.id}/confirm",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "approved_revision_id": revision_id,
            "client_request_id": "b786e410-462f-4960-bba1-8cc3c8298b37",
        },
    )
    assert replay.status_code == 200
    intervals = db_session.scalars(
        select(WorkInterval).where(WorkInterval.linked_shift_assignment_id == assignment.id)
    ).all()
    assert len(intervals) == 1
    assert intervals[0].work_type == "SHIFT"


def test_confirm_rejects_stale_approved_revision_without_creating_actual_fact(
    db_client: TestClient, db_session: Session
) -> None:
    assignment = _seed_shift(db_session)
    original_revision = assignment.approved_revision_id
    assert original_revision is not None
    new_revision = ShiftPlanRevision(
        shift_assignment_id=assignment.id,
        revision_no=2,
        planned_start_at=datetime.fromisoformat("2026-07-31T20:00:00+02:00"),
        planned_end_at=datetime.fromisoformat("2026-08-01T08:00:00+02:00"),
        changed_at=datetime.fromisoformat("2026-07-30T09:00:00+02:00"),
        communicated_at=datetime.fromisoformat("2026-07-30T09:00:00+02:00"),
        decision_at=None,
        decision_maker_type=None,
        reason=None,
        source="TEST_SEED",
    )
    db_session.add(new_revision)
    db_session.flush()
    assignment.approved_revision_id = new_revision.id
    response = db_client.post(
        f"/api/v1/me/shifts/{assignment.id}/confirm",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"approved_revision_id": str(original_revision)},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CONFLICT_SHIFT_PLAN_CHANGED"
    assert not db_session.scalars(
        select(WorkInterval).where(WorkInterval.linked_shift_assignment_id == assignment.id)
    ).all()


def test_confirm_rejects_existing_deviation_as_not_exact(
    db_client: TestClient, db_session: Session
) -> None:
    assignment = _seed_shift(db_session)
    revision_id = str(assignment.approved_revision_id)
    deviation = db_client.post(
        f"/api/v1/me/shifts/{assignment.id}/deviation",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "deviation_type": "SHORTENED",
            "actual_start_local": "2026-07-31T19:00:00",
            "actual_end_local": "2026-08-01T02:30:00",
        },
    )
    assert deviation.status_code == 200, deviation.text

    response = db_client.post(
        f"/api/v1/me/shifts/{assignment.id}/confirm",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"approved_revision_id": revision_id},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CONFLICT_SHIFT_PLAN_CHANGED"


def test_confirm_namespaces_shared_command_id_per_shift(
    db_client: TestClient, db_session: Session
) -> None:
    first = _seed_shift(db_session)
    second = seed_shift_assignment(
        db_session,
        _employee(db_session),
        ShiftAssignmentSeed(
            plan_key="synthetic-night-plan",
            plan_name="Synthetic night plan",
            planned_start_at=datetime.fromisoformat("2026-08-02T19:00:00+02:00"),
            planned_end_at=datetime.fromisoformat("2026-08-03T07:00:00+02:00"),
            shift_kind="NIGHT",
            source="TEST_SEED",
            source_ref="shift-plan-fixture-v1",
        ),
    )
    command_id = "b786e410-462f-4960-bba1-8cc3c8298b37"
    for assignment in (first, second):
        response = db_client.post(
            f"/api/v1/me/shifts/{assignment.id}/confirm",
            headers=DEVELOPER_PRINCIPAL_HEADER,
            json={
                "approved_revision_id": str(assignment.approved_revision_id),
                "client_request_id": command_id,
            },
        )
        assert response.status_code == 200, response.text
    assert (
        len(
            db_session.scalars(
                select(WorkInterval).where(
                    WorkInterval.linked_shift_assignment_id.in_([first.id, second.id])
                )
            ).all()
        )
        == 2
    )


def test_SHIFT_T02_shortening_keeps_actual_fact_separate_from_plan_and_cross_midnight(
    db_client: TestClient, db_session: Session
) -> None:
    assignment = _seed_shift(db_session)
    response = db_client.post(
        f"/api/v1/me/shifts/{assignment.id}/deviation",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "deviation_type": "SHORTENED",
            "actual_start_local": "2026-07-31T19:00:00",
            "actual_end_local": "2026-08-01T02:30:00",
            "noticed_or_decided_local": "2026-08-01T02:00:00",
            "decision_maker_type": "SHIFT_LEAD_REPORTED",
            "reason": "Operational run finished early.",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["approved_revision"]["planned_end_at"] == "2026-08-01T05:00:00Z"
    assert payload["actual_end_at"] == "2026-08-01T02:30:00+02:00"
    assert payload["deviation"]["deviation_type"] == "SHORTENED"
    assert payload["deviation"]["decision_maker_type"] == "SHIFT_LEAD_REPORTED"
    interval = db_session.scalar(
        select(WorkInterval).where(WorkInterval.linked_shift_assignment_id == assignment.id)
    )
    assert interval is not None
    assert (
        interval.start_at != assignment.planned_start_at
        or interval.end_at != assignment.planned_end_at
    )


def test_SHIFT_T04_overrun_over_10h_is_retained_with_critical_finding(
    db_client: TestClient, db_session: Session
) -> None:
    assignment = _seed_shift(db_session)
    response = db_client.post(
        f"/api/v1/me/shifts/{assignment.id}/deviation",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "deviation_type": "OVERRUN_GT_10H",
            "actual_start_local": "2026-07-31T19:00:00",
            "actual_end_local": "2026-08-01T06:00:00",
            "reason": "Synthetic test overrun.",
        },
    )
    assert response.status_code == 200, response.text
    assert any(
        finding["rule_id"] == "SHIFT-012" and finding["status"] == "CRITICAL"
        for finding in response.json()["findings"]
    )
    assert (
        db_session.scalar(
            select(WorkInterval).where(WorkInterval.linked_shift_assignment_id == assignment.id)
        )
        is not None
    )


def test_SHIFT_010_cancel_near_boundary_is_blocked_without_authoritative_interpretation() -> None:
    findings = evaluate_shift(
        deviation_type=ShiftDeviationType.CANCELLED,
        actual_start_at=None,
        actual_end_at=None,
        communicated_at=datetime.fromisoformat("2026-07-28T19:01:00+02:00"),
        authoritative_compensation_enabled=False,
        cancellation_3d_enabled=False,
    )
    blocked = next(finding for finding in findings if finding.rule_id == "SHIFT-010")
    assert blocked.status == "BLOCKED_POLICY"
    assert "72" not in blocked.message
    assert "calendar" not in blocked.message.lower()


def test_SHIFT_009_cancellation_timing_is_blocked_without_the_communicated_time() -> None:
    """SHIFT-009: the timing classification needs ``communicated_at``.

    Without it the result is BLOCKED_POLICY (the timing cannot be classified);
    with it the timing is retained for provisional review and NO compensation is
    calculated from it.

    INTENTIONAL-FOR-NOW, NOT CORRECT: the reference instant the service passes is
    ``communicated_at`` — the plan's PUBLICATION time — while ``POL-02`` measures the
    notice against the planned start. This test therefore pins a known defect rather
    than the source rule. It is recorded as a named residual in ``docs/INVARIANTS.md``
    (the ``SHIFT-009`` ``communicated_at`` paragraph) and deliberately
    NOT fixed in the employee plan-entry slice, which changes no cancellation timing
    semantics. A future fixer should expect to CHANGE this test, not preserve it.
    """
    blocked = evaluate_shift(
        deviation_type=ShiftDeviationType.CANCELLED,
        actual_start_at=None,
        actual_end_at=None,
        communicated_at=None,
        authoritative_compensation_enabled=False,
        cancellation_3d_enabled=False,
    )
    finding = next(item for item in blocked if item.rule_id == "SHIFT-009")
    assert finding.status == "BLOCKED_POLICY"

    provisional = evaluate_shift(
        deviation_type=ShiftDeviationType.CANCELLED,
        actual_start_at=None,
        actual_end_at=None,
        communicated_at=datetime.fromisoformat("2026-07-01T09:00:00+02:00"),
        authoritative_compensation_enabled=False,
        cancellation_3d_enabled=False,
    )
    finding = next(item for item in provisional if item.rule_id == "SHIFT-009")
    assert finding.status == "WARNING"
    assert "no compensation was calculated" in finding.message


def test_SHIFT_011_overrun_is_provisional_and_an_over_10h_overrun_is_not_shift_011() -> None:
    """SHIFT-011 covers the 7:48-10 h overrun band only.

    Its severity tracks whether authoritative compensation is enabled (INFO) or
    still provisional (WARNING); a >10 h overrun is SHIFT-012 CRITICAL instead,
    and SHIFT-011 must not also fire.
    """

    def overrun(end_local: str, *, authoritative: bool) -> list:
        return evaluate_shift(
            deviation_type=ShiftDeviationType.OVERRUN,
            actual_start_at=datetime.fromisoformat("2026-07-01T19:00:00+02:00"),
            actual_end_at=datetime.fromisoformat(end_local),
            communicated_at=None,
            authoritative_compensation_enabled=authoritative,
            cancellation_3d_enabled=False,
        )

    provisional = next(
        item
        for item in overrun("2026-07-02T02:00:00+02:00", authoritative=False)
        if item.rule_id == "SHIFT-011"
    )
    assert provisional.status == "WARNING"
    authoritative = next(
        item
        for item in overrun("2026-07-02T02:00:00+02:00", authoritative=True)
        if item.rule_id == "SHIFT-011"
    )
    assert authoritative.status == "INFO"

    long = overrun("2026-07-02T06:00:00+02:00", authoritative=True)  # 11 h
    ids = [item.rule_id for item in long]
    assert "SHIFT-012" in ids
    assert "SHIFT-011" not in ids
    assert next(item for item in long if item.rule_id == "SHIFT-012").status == "CRITICAL"


def test_SHIFT_report_preview_excludes_unrelated_ordinary_interval(
    db_client: TestClient, db_session: Session
) -> None:
    assignment = _seed_shift(db_session)
    revision_id = _shifts(db_client)[0]["approved_revision"]["id"]
    confirmed = db_client.post(
        f"/api/v1/me/shifts/{assignment.id}/confirm",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"approved_revision_id": revision_id},
    )
    assert confirmed.status_code == 200
    ordinary = db_client.post(
        "/api/v1/me/work-intervals",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": "2026-07-31T08:00:00",
            "end_local": "2026-07-31T12:00:00",
            "work_type": "ON_SITE",
        },
    )
    assert ordinary.status_code == 201
    preview = db_client.get(
        f"/api/v1/me/shifts/{assignment.id}/report-preview",
        headers=DEVELOPER_PRINCIPAL_HEADER,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["report_type"] == "SHIFT_REPORT"
    assert ordinary.json()["id"] not in preview.text
    assert "08:00" not in preview.text


def test_shift_report_builder_has_no_unrelated_interval_input() -> None:
    """ACCESS-004: the package shape cannot accidentally serialize a month."""
    report = build_shift_report(
        ShiftReportInputs(
            policy_version="2026-06-shadow-v1",
            policy_status="PROVISIONAL",
            employee_identifier="emp-0001",
            shift_assignment_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            planned_start_at=datetime.fromisoformat("2026-07-31T19:00:00+02:00"),
            planned_end_at=datetime.fromisoformat("2026-08-01T07:00:00+02:00"),
            actual_start_at=datetime.fromisoformat("2026-07-31T19:00:00+02:00"),
            actual_end_at=datetime.fromisoformat("2026-08-01T02:30:00+02:00"),
            deviation_type=ShiftDeviationType.SHORTENED,
            noticed_or_decided_at=None,
            decision_maker_type=None,
            reason=None,
            findings=[
                ShiftFindingOut(rule_id="SHIFT-001", status="INFO", message="Separate facts.")
            ],
            paid_break_status="EXPLICIT_FACT_REQUIRED",
            plan_source="EMPLOYEE_RECORDED",
            compensation_review_status="NOT_APPLICABLE",
        )
    )
    assert set(report.model_dump()) == {
        "report_type",
        "policy_version",
        "policy_status",
        "employee_identifier",
        "shift_assignment_id",
        "planned_start_at",
        "planned_end_at",
        "actual_start_at",
        "actual_end_at",
        "deviation_type",
        "noticed_or_decided_at",
        "decision_maker_type",
        "reason",
        "findings",
        "paid_break_status",
        "plan_source",
        "compensation_review_status",
        "preview_fingerprint",  # seal over the recipient payload
    }


def test_shift_seed_replay_is_idempotent(db_session: Session) -> None:
    first = _seed_shift(db_session)
    second = _seed_shift(db_session)
    assert first.id == second.id
    assert len(db_session.scalars(select(ShiftAssignment)).all()) == 1


def test_shift_seed_rejects_naive_timestamps(db_session: Session) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        seed_shift_assignment(
            db_session,
            _employee(db_session),
            ShiftAssignmentSeed(
                plan_key="naive-plan",
                plan_name="Naive plan",
                planned_start_at=datetime(2026, 7, 31, 19),
                planned_end_at=datetime(2026, 8, 1, 7),
                shift_kind="NIGHT",
                source="TEST_SEED",
            ),
        )


@pytest.mark.parametrize(
    ("actual_start_at", "actual_end_at"),
    [
        (datetime.fromisoformat("2026-07-31T19:00:00+02:00"), None),
        (
            datetime.fromisoformat("2026-07-31T19:00:00+02:00"),
            datetime.fromisoformat("2026-07-31T18:00:00+02:00"),
        ),
    ],
    ids=["unpaired", "reversed"],
)
def test_shift_deviation_database_constraint_rejects_invalid_actuals(
    db_session: Session, actual_start_at: datetime, actual_end_at: datetime | None
) -> None:
    assignment = _seed_shift(db_session)
    db_session.add(
        ShiftDeviation(
            employee_id=_employee(db_session).id,
            shift_assignment_id=assignment.id,
            actual_start_at=actual_start_at,
            actual_end_at=actual_end_at,
            deviation_type=ShiftDeviationType.SHORTENED,
            source_work_interval_ids=[],
            version=1,
        )
    )
    with pytest.raises(IntegrityError, match="ck_shift_deviation_actual_order"):
        db_session.flush()
    db_session.rollback()


def test_shift_routes_do_not_expand_hr_or_other_employee_visibility(
    db_client: TestClient, db_session: Session
) -> None:
    assignment = _seed_shift(db_session)
    assert (
        db_client.get(
            "/api/v1/me/shifts?from=2026-07-01&to=2026-08-02", headers=HR_HEADER
        ).status_code
        == 403
    )
    assert (
        db_client.post(
            f"/api/v1/me/shifts/{assignment.id}/confirm",
            headers=HR_HEADER,
            json={"approved_revision_id": str(assignment.approved_revision_id)},
        ).status_code
        == 403
    )
    assert (
        db_client.get(
            f"/api/v1/me/shifts/{assignment.id}/report-preview",
            headers=OTHER_EMPLOYEE_HEADER,
        ).status_code
        == 404
    )


def test_deviation_accepts_context_without_authority_validation(
    db_client: TestClient, db_session: Session
) -> None:
    """``SHIFT-013``: ``decision_maker_type`` is free text, validated against nothing.

    RENAMED from ``test_cancelled_shift_accepts_context_without_authority_validation``,
    which was a lie once the non-active-plan guard landed: the test no longer forces a
    CANCELLED assignment, so its name asserted a precondition it did not establish —
    a green test that could not fail for the reason it claimed. The assertion is
    unchanged and still meaningful; only the name was wrong.

    The cancelled case it used to cover is now unreachable BY DESIGN (only an ACTIVE
    plan is deviable) and is covered where it belongs:
    ``test_shift_cancellation.py::test_a_cancellation_preserves_the_plan_and_records_the_context``
    exercises a real cancellation — a CANCELLED deviation on a PLANNED plan, which is
    what actually happens, since the plan must survive as the compensation basis.
    """
    assignment = _seed_shift(db_session)
    response = db_client.post(
        f"/api/v1/me/shifts/{assignment.id}/deviation",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "deviation_type": "CANCELLED",
            "noticed_or_decided_local": "2026-07-29T10:00:00",
            "decision_maker_type": "UNVALIDATED_REPORTED_ROLE",
            "reason": "Cancellation communicated by shift planning.",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["deviation"]["decision_maker_type"] == "UNVALIDATED_REPORTED_ROLE"


def test_cancel_after_exact_confirmation_removes_unsubmitted_actual_fact(
    db_client: TestClient, db_session: Session
) -> None:
    assignment = _seed_shift(db_session)
    revision_id = _shifts(db_client)[0]["approved_revision"]["id"]
    assert (
        db_client.post(
            f"/api/v1/me/shifts/{assignment.id}/confirm",
            headers=DEVELOPER_PRINCIPAL_HEADER,
            json={"approved_revision_id": revision_id},
        ).status_code
        == 200
    )
    cancelled = db_client.post(
        f"/api/v1/me/shifts/{assignment.id}/deviation",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"deviation_type": "CANCELLED", "reason": "Shift cancelled."},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["actual_start_at"] is None
    assert cancelled.json()["actual_end_at"] is None
    assert not db_session.scalars(
        select(WorkInterval).where(WorkInterval.linked_shift_assignment_id == assignment.id)
    ).all()


def test_splitting_a_shift_actual_keeps_the_assignment_link(
    db_client: TestClient, db_session: Session
) -> None:
    assignment = _seed_shift(db_session)
    revision_id = _shifts(db_client)[0]["approved_revision"]["id"]
    assert (
        db_client.post(
            f"/api/v1/me/shifts/{assignment.id}/confirm",
            headers=DEVELOPER_PRINCIPAL_HEADER,
            json={"approved_revision_id": revision_id},
        ).status_code
        == 200
    )
    interval = db_session.scalar(
        select(WorkInterval).where(WorkInterval.linked_shift_assignment_id == assignment.id)
    )
    assert interval is not None
    response = db_client.post(
        f"/api/v1/me/work-intervals/{interval.id}/split",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"version": interval.version, "split_local": "2026-08-01T01:00:00"},
    )
    assert response.status_code == 200, response.text
    linked = db_session.scalars(
        select(WorkInterval).where(WorkInterval.linked_shift_assignment_id == assignment.id)
    ).all()
    assert len(linked) == 3  # original, superseding first fragment, and second fragment
    assert {item.linked_shift_assignment_id for item in linked} == {assignment.id}
    card = db_client.get(
        "/api/v1/me/shifts?from=2026-07-01&to=2026-08-02",
        headers=DEVELOPER_PRINCIPAL_HEADER,
    )
    assert card.status_code == 200, card.text
    assert card.json()[0]["actual_start_at"] is not None
    assert card.json()[0]["actual_end_at"] is not None
    preview = db_client.get(
        f"/api/v1/me/shifts/{assignment.id}/report-preview",
        headers=DEVELOPER_PRINCIPAL_HEADER,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["actual_start_at"] is not None
    assert preview.json()["actual_end_at"] is not None
    deviation = db_client.post(
        f"/api/v1/me/shifts/{assignment.id}/deviation",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "deviation_type": "MINOR_CHANGE",
            "actual_start_local": "2026-07-31T19:00:00",
            "actual_end_local": "2026-08-01T07:00:00",
            "reason": "Structured context added after correcting split facts.",
        },
    )
    assert deviation.status_code == 200, deviation.text


def test_a_recorded_paid_break_satisfies_SHIFT_003(
    db_client: TestClient, db_session: Session
) -> None:
    """SHIFT-003 requires explicit break facts.

    Once the employee records one and links it to the assignment, both the
    private shift view and the report projection must stop asserting that no
    break fact exists: the report was stating the opposite of a fact the
    employee had already recorded.
    """
    assignment = _seed_shift(db_session)
    before = _shifts(db_client)[0]
    assert before["paid_break_status"] == "EXPLICIT_FACT_REQUIRED"
    assert "SHIFT-003" in {item["rule_id"] for item in before["findings"]}

    created = db_client.post(
        "/api/v1/me/break-intervals",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": "2026-07-31T23:00:00",
            "end_local": "2026-07-31T23:30:00",
            "kind": "PAID_SHIFT_BREAK",
            "linked_shift_assignment_id": str(assignment.id),
        },
    )
    assert created.status_code == 201, created.text

    after = _shifts(db_client)[0]
    assert after["paid_break_status"] == "RECORDED"
    assert "SHIFT-003" not in {item["rule_id"] for item in after["findings"]}

    preview = db_client.get(
        f"/api/v1/me/shifts/{assignment.id}/report-preview",
        headers=DEVELOPER_PRINCIPAL_HEADER,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["paid_break_status"] == "RECORDED"
    assert "SHIFT-003" not in {item["rule_id"] for item in preview.json()["findings"]}


def test_several_paid_breaks_can_be_linked_to_one_shift(
    db_client: TestClient, db_session: Session
) -> None:
    """A long shift can hold more than one paid break, and the private view says so.

    The requirement is satisfied by ANY linked paid break, so the status stays
    RECORDED — but the FACTS must be listed, because the shift card shows them and
    the employee adds a second one from there. The list is resolved by the
    assignment LINK rather than by day, so a break after midnight is included even
    though the day panel's own list would not show it on the shift's start date.
    """
    assignment = _seed_shift(db_session)
    for start, end in (
        ("2026-07-31T23:00:00", "2026-07-31T23:30:00"),
        ("2026-08-01T03:00:00", "2026-08-01T03:30:00"),
    ):
        created = db_client.post(
            "/api/v1/me/break-intervals",
            headers=DEVELOPER_PRINCIPAL_HEADER,
            json={
                "start_local": start,
                "end_local": end,
                "kind": "PAID_SHIFT_BREAK",
                "linked_shift_assignment_id": str(assignment.id),
            },
        )
        assert created.status_code == 201, created.text

    loaded = _shifts(db_client)[0]
    assert loaded["paid_break_status"] == "RECORDED"
    # Both facts, in the payload the card reads — not merely the derived status.
    assert [item["start_local"] for item in loaded["paid_breaks"]] == [
        "2026-07-31T23:00:00",
        "2026-08-01T03:00:00",
    ]
    # The DISCLOSED projection stays minimal: a status, never the break times.
    preview = db_client.get(
        f"/api/v1/me/shifts/{assignment.id}/report-preview",
        headers=DEVELOPER_PRINCIPAL_HEADER,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["paid_break_status"] == "RECORDED"
    assert "paid_breaks" not in preview.json()


def test_an_unpaid_break_does_not_claim_the_paid_break_fact(
    db_client: TestClient, db_session: Session
) -> None:
    """Negative control: SHIFT-003's subject is the PAID shift break, so an
    ordinary unpaid break must not silently satisfy the requirement."""
    assignment = _seed_shift(db_session)
    created = db_client.post(
        "/api/v1/me/break-intervals",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": "2026-07-31T23:00:00",
            "end_local": "2026-07-31T23:30:00",
            "kind": "UNPAID",
            "linked_shift_assignment_id": str(assignment.id),
        },
    )
    assert created.status_code == 201, created.text
    loaded = _shifts(db_client)[0]
    assert loaded["paid_break_status"] == "EXPLICIT_FACT_REQUIRED"
    assert "SHIFT-003" in {item["rule_id"] for item in loaded["findings"]}


def test_a_break_linked_to_a_shift_must_fall_inside_it(
    db_client: TestClient, db_session: Session
) -> None:
    """Ownership is not enough: the link resolves SHIFT-003's status, so a break
    that never occurred in the shift must not be linkable to it."""
    assignment = _seed_shift(db_session)
    outside = db_client.post(
        "/api/v1/me/break-intervals",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": "2026-08-10T23:00:00",
            "end_local": "2026-08-10T23:30:00",
            "kind": "PAID_SHIFT_BREAK",
            "linked_shift_assignment_id": str(assignment.id),
        },
    )
    assert outside.status_code == 422, outside.text
    assert outside.json()["detail"]["code"] == "VALIDATION_FAILED"
    assert _shifts(db_client)[0]["paid_break_status"] == "EXPLICIT_FACT_REQUIRED"


def test_a_correction_cannot_move_a_linked_break_out_of_its_shift(
    db_client: TestClient, db_session: Session
) -> None:
    """A correction preserves the link, so it must re-validate the new times."""
    assignment = _seed_shift(db_session)
    created = db_client.post(
        "/api/v1/me/break-intervals",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": "2026-07-31T23:00:00",
            "end_local": "2026-07-31T23:30:00",
            "kind": "PAID_SHIFT_BREAK",
            "linked_shift_assignment_id": str(assignment.id),
        },
    )
    assert created.status_code == 201, created.text
    moved = db_client.patch(
        f"/api/v1/me/break-intervals/{created.json()['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "version": 1,
            "start_local": "2026-08-10T23:00:00",
            "end_local": "2026-08-10T23:30:00",
        },
    )
    assert moved.status_code == 422, moved.text
    # The refused correction changed nothing: the shift keeps its recorded fact.
    assert _shifts(db_client)[0]["paid_break_status"] == "RECORDED"


def test_a_break_that_only_grazes_the_shift_is_refused(
    db_client: TestClient, db_session: Session
) -> None:
    """Overlap is not membership: a break must lie ENTIRELY inside the shift.

    The shift is 2026-07-31 19:00 -> 2026-08-01 07:00, so a break ending one
    minute after it starts is almost wholly outside it. Accepting that would mark
    the shift's paid-break requirement met on a fact that did not happen in it.
    """
    assignment = _seed_shift(db_session)
    grazing = db_client.post(
        "/api/v1/me/break-intervals",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": "2026-07-31T18:00:00",
            "end_local": "2026-07-31T19:01:00",
            "kind": "PAID_SHIFT_BREAK",
            "linked_shift_assignment_id": str(assignment.id),
        },
    )
    assert grazing.status_code == 422, grazing.text
    assert _shifts(db_client)[0]["paid_break_status"] == "EXPLICIT_FACT_REQUIRED"


def test_a_break_running_past_the_shift_end_is_refused(
    db_client: TestClient, db_session: Session
) -> None:
    """The other boundary: fully inside at the start, over the end."""
    assignment = _seed_shift(db_session)
    overrun = db_client.post(
        "/api/v1/me/break-intervals",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": "2026-08-01T06:45:00",
            "end_local": "2026-08-01T07:15:00",
            "kind": "PAID_SHIFT_BREAK",
            "linked_shift_assignment_id": str(assignment.id),
        },
    )
    assert overrun.status_code == 422, overrun.text
    assert _shifts(db_client)[0]["paid_break_status"] == "EXPLICIT_FACT_REQUIRED"


def test_a_break_exactly_at_the_shift_boundaries_is_accepted(
    db_client: TestClient, db_session: Session
) -> None:
    """Containment is inclusive: a break from the shift's start to its end fits.

    This is the positive control for the two boundary tests above — without it
    they would also pass if the link were refused outright.
    """
    assignment = _seed_shift(db_session)
    exact = db_client.post(
        "/api/v1/me/break-intervals",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": "2026-07-31T19:00:00",
            "end_local": "2026-08-01T07:00:00",
            "kind": "PAID_SHIFT_BREAK",
            "linked_shift_assignment_id": str(assignment.id),
        },
    )
    assert exact.status_code == 201, exact.text
    assert _shifts(db_client)[0]["paid_break_status"] == "RECORDED"
