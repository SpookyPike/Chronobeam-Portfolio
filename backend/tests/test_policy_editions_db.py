"""F4 edition selection, persistence and failure semantics (DB integration).

Where ``test_policy_editions.py`` pins the code registry, this file pins the
hybrid: the DB rows, the effective-date selection that decides which edition a
month resolves, the reproducibility anchor a frozen snapshot records, and the
fail-closed behaviour when an edition is missing or unregistered.
"""

import uuid
from datetime import date, datetime

import pytest
from app.disclosure.fingerprint import payload_fingerprint
from app.disclosure.service import _freeze_month_snapshot
from app.enums import PolicyVersionStatus
from app.errors import PolicyEditionUnavailable
from app.identity.models import Employee
from app.policy.editions import (
    DEFAULT_ORDINARY_PARAMETERS,
    EDITIONS,
    ORDINARY_RULE_SET_V1,
    SOURCE_MANIFEST_HASH,
    OrdinaryTimeParameters,
    PolicyEdition,
    resolve_edition,
)
from app.policy.models import PeriodCalculationSnapshot, PolicyVersion
from app.policy.service import (
    PURPOSE_POLICY_KEYS,
    _month_inputs,
    calculate_employee_period,
    policy_version_for_purpose,
)
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .conftest import DEVELOPER_PRINCIPAL_HEADER, OTHER_EMPLOYEE_HEADER

FINDINGS = "/api/v1/me/findings"
MONTHS = "/api/v1/me/months"
EXPORT = "/api/v1/me/reports/legacy/work-time-record"
ORDINARY_KEY = "ordinary-working-time"


def _employee(session: Session, subject: str = "employee") -> Employee:
    employee = session.scalar(select(Employee).where(Employee.idp_subject == subject))
    assert employee is not None
    return employee


def _ordinary(session: Session) -> PolicyVersion:
    row = session.scalar(select(PolicyVersion).where(PolicyVersion.policy_key == ORDINARY_KEY))
    assert row is not None
    return row


def _assert_policy_blocked(response, *, month: str, chain_month: str) -> None:
    """The domain error shape: 409 POLICY_BLOCKED, never a 500."""
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "POLICY_BLOCKED"
    assert detail["details"]["month"] == month
    assert detail["details"]["chain_month"] == chain_month


# ---------------------------------------------------------------------------
# EINV-09 — every recorded edition is executable
# ---------------------------------------------------------------------------


def test_every_recorded_engine_rule_set_is_registered(db_session: Session) -> None:
    """EINV-09 against the real schema: migration and registry ship together.

    A row whose rule set is absent from ``EDITIONS`` is a *phantom edition* — a
    frozen result naming an edition that cannot be resolved or executed.
    """
    rows = db_session.execute(
        select(
            PolicyVersion.policy_key,
            PolicyVersion.version,
            PolicyVersion.engine_rule_set,
            PolicyVersion.source_manifest_hash,
        )
    ).all()
    assert rows, "the migrations must seed at least one edition"
    for policy_key, version, rule_set, manifest_hash in rows:
        edition = EDITIONS.get(rule_set)
        assert edition is not None, f"{policy_key}/{version} asserts unregistered {rule_set}"
        # The registry is the code-side mirror of the row's identity: the label
        # a preview displays and the provenance it was built against.
        assert edition.version == version
        assert edition.source_manifest_hash == manifest_hash


def test_the_sibling_purpose_editions_are_resolvable(db_session: Session) -> None:
    """F4 U2: the OCD/overtime/shift labels a frozen package asserts are rows.

    Before F4 these were literals in the preview builders with no
    ``policy_version`` row behind them, so ``submission_package.policy_version_id``
    could never be set.
    """
    for purpose, policy_key in PURPOSE_POLICY_KEYS.items():
        payload = {
            "SHIFT_REPORT": {"policy_version": "2026-06-shadow-v1"},
            "OVERTIME_PAID": {"policy_version": "2026-ot-v1"},
            "OVERTIME_TIME_COMP": {"policy_version": "2026-ot-v1"},
            "OCD_COMPENSATION": {"policy_version": "2026-ocd-v1"},
        }[purpose.name]
        row = policy_version_for_purpose(db_session, purpose, payload, date(2026, 7, 1))
        assert row.policy_key == policy_key
        assert row.engine_rule_set in EDITIONS


def test_a_payload_asserting_an_unresolvable_edition_fails_closed(db_session: Session) -> None:
    """A package must not be stored claiming an edition that cannot be resolved."""
    from app.enums import SubmissionPurpose

    with pytest.raises(PolicyEditionUnavailable):
        policy_version_for_purpose(
            db_session,
            SubmissionPurpose.OVERTIME_PAID,
            {"policy_version": "not-installed"},
            date(2026, 7, 1),
        )


# ---------------------------------------------------------------------------
# F4 D7 — table integrity guards
# ---------------------------------------------------------------------------


def test_one_edition_per_key_and_effective_date(db_session: Session) -> None:
    """A shared effective date would let string ordering pick the edition."""
    ordinary = _ordinary(db_session)
    with pytest.raises(IntegrityError) as error:
        db_session.add(
            PolicyVersion(
                policy_key=ORDINARY_KEY,
                version="2026-06-collision",
                effective_from=ordinary.effective_from,
                effective_to=None,
                status=PolicyVersionStatus.PROVISIONAL,
                engine_rule_set=ORDINARY_RULE_SET_V1,
                source_manifest_hash=SOURCE_MANIFEST_HASH,
            )
        )
        db_session.flush()
    assert "uq_policy_version_effective_from" in str(error.value)
    db_session.rollback()


def test_an_edition_range_cannot_end_before_it_starts(db_session: Session) -> None:
    with pytest.raises(IntegrityError) as error:
        db_session.add(
            PolicyVersion(
                policy_key="synthetic-range",
                version="inverted",
                effective_from=date(2026, 9, 1),
                effective_to=date(2026, 8, 1),
                status=PolicyVersionStatus.PROVISIONAL,
                engine_rule_set=ORDINARY_RULE_SET_V1,
                source_manifest_hash=SOURCE_MANIFEST_HASH,
            )
        )
        db_session.flush()
    assert "ck_policy_version_range" in str(error.value)
    db_session.rollback()


# ---------------------------------------------------------------------------
# F4 D5 — missing / unregistered edition is a domain error, never a 500
# ---------------------------------------------------------------------------


def test_a_month_without_an_edition_is_refused_on_every_public_read(
    db_client: TestClient,
) -> None:
    """A known, policy-caused unavailability is 409 POLICY_BLOCKED, not 500.

    Reachable on the findings view, the month summary and the export, all of
    which used to surface a bare ``RuntimeError`` as ``INTERNAL_ERROR``.
    """
    _assert_policy_blocked(
        db_client.get(f"{FINDINGS}?period=2026-01", headers=DEVELOPER_PRINCIPAL_HEADER),
        month="2026-01",
        chain_month="2026-01",
    )
    _assert_policy_blocked(
        db_client.get(f"{MONTHS}/2026-01", headers=DEVELOPER_PRINCIPAL_HEADER),
        month="2026-01",
        chain_month="2026-01",
    )
    _assert_policy_blocked(
        db_client.get(f"{EXPORT}?month=2026-01", headers=DEVELOPER_PRINCIPAL_HEADER),
        month="2026-01",
        chain_month="2026-01",
    )


def test_an_unregistered_recorded_edition_is_refused_not_served(
    db_client: TestClient, db_session: Session
) -> None:
    """F4 D6.4: a recorded rule set with no registry entry fails closed."""
    ordinary = _ordinary(db_session)
    ordinary.engine_rule_set = "phantom-rule-set-v9"
    db_session.flush()
    _assert_policy_blocked(
        db_client.get(f"{FINDINGS}?period=2026-07", headers=DEVELOPER_PRINCIPAL_HEADER),
        month="2026-07",
        chain_month="2026-07",
    )


def test_the_failing_chain_month_is_named(db_client: TestClient, db_session: Session) -> None:
    """The carry-forward chain resolves one edition per month, so the month that
    actually lacks an edition must be reported — not just the requested one."""
    ordinary = _ordinary(db_session)
    ordinary.effective_to = date(2026, 10, 31)
    db_session.add(
        PolicyVersion(
            policy_key=ORDINARY_KEY,
            version="2026-12-test-v2",
            effective_from=date(2026, 12, 1),
            effective_to=None,
            status=PolicyVersionStatus.PROVISIONAL,
            engine_rule_set=ORDINARY_RULE_SET_V1,
            source_manifest_hash=SOURCE_MANIFEST_HASH,
        )
    )
    db_session.flush()
    created = db_client.post(
        "/api/v1/me/work-intervals",
        headers=OTHER_EMPLOYEE_HEADER,
        json={
            "start_local": "2026-11-05T09:00:00",
            "end_local": "2026-11-05T12:00:00",
            "work_type": "ON_SITE",
            "client_request_id": str(uuid.uuid4()),
        },
    )
    assert created.status_code == 201, created.text
    # The gap month itself.
    _assert_policy_blocked(
        db_client.get(f"{FINDINGS}?period=2026-11", headers=OTHER_EMPLOYEE_HEADER),
        month="2026-11",
        chain_month="2026-11",
    )
    # A covered month whose CHAIN passes through the gap.
    _assert_policy_blocked(
        db_client.get(f"{FINDINGS}?period=2026-12", headers=OTHER_EMPLOYEE_HEADER),
        month="2026-12",
        chain_month="2026-11",
    )


# ---------------------------------------------------------------------------
# F4 D1/D2 — resolution is by the month's own effective date, never "latest"
# ---------------------------------------------------------------------------


_V2_EDITION = PolicyEdition(
    rule_set="test-ordinary-v2",
    version="2026-09-test-v2",
    algorithm="ordinary-v1",
    parameters=OrdinaryTimeParameters(
        credit_by_work_type={
            **DEFAULT_ORDINARY_PARAMETERS.credit_by_work_type,
            "OVERTIME_PAID": False,
        }
    ),
    note="test-only credit edition",
)


def _register_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register a test-only credit edition (not shipped in the real registry)."""
    monkeypatch.setattr(
        "app.policy.editions.EDITIONS", {**EDITIONS, _V2_EDITION.rule_set: _V2_EDITION}
    )


def _publish_v2(db_session: Session) -> None:
    db_session.add(
        PolicyVersion(
            policy_key=ORDINARY_KEY,
            version="2026-09-test-v2",
            effective_from=date(2026, 9, 1),
            effective_to=None,
            status=PolicyVersionStatus.PROVISIONAL,
            engine_rule_set="test-ordinary-v2",
            source_manifest_hash=SOURCE_MANIFEST_HASH,
        )
    )
    db_session.flush()


def test_edition_resolution_follows_the_effective_date(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    """A boundary between adjacent periods, and no "latest policy" leakage."""
    employee = _employee(db_session)
    _register_v2(monkeypatch)
    before_v2 = calculate_employee_period(db_session, employee, date(2026, 7, 1))
    _publish_v2(db_session)

    august = _month_inputs(db_session, employee, date(2026, 8, 1), ())
    september = _month_inputs(db_session, employee, date(2026, 9, 1), ())
    assert august.engine_build == ORDINARY_RULE_SET_V1
    assert august.parameters is DEFAULT_ORDINARY_PARAMETERS
    assert september.engine_build == "test-ordinary-v2"
    assert september.parameters is _V2_EDITION.parameters

    # An OLD month recomputed after a new edition exists is byte-identical: the
    # edition is resolved from the month, not from "the newest row".
    after_v2 = calculate_employee_period(db_session, employee, date(2026, 7, 1))
    assert after_v2 == before_v2
    assert after_v2.input_fingerprint == before_v2.input_fingerprint


def test_an_old_month_is_not_reinterpreted_by_a_later_edition(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    """The protection is behavioural, not just an identity check: a month before
    the boundary keeps producing the v1 NUMBERS."""
    employee = _employee(db_session)
    _register_v2(monkeypatch)
    _publish_v2(db_session)
    august = calculate_employee_period(db_session, employee, date(2026, 8, 1))
    assert all(day.account_credit_minutes == day.worked_minutes for day in august.daily_results), (
        "August must still be computed under v1, where every work type credits"
    )
    assert resolve_edition(_month_inputs(db_session, employee, date(2026, 8, 1), ()).engine_build)


# ---------------------------------------------------------------------------
# F4.5 — the reproducibility anchor is the ENGINE INPUT fingerprint
# ---------------------------------------------------------------------------


def test_a_frozen_snapshot_records_the_engine_input_fingerprint(
    db_session: Session,
) -> None:
    employee = _employee(db_session)
    snapshot, _, _ = _freeze_month_snapshot(db_session, employee, date(2026, 7, 1))
    calculation = calculate_employee_period(db_session, employee, date(2026, 7, 1))
    assert snapshot.input_fingerprint == calculation.input_fingerprint
    # It is NOT the output payload hash (which used to carry a wall-clock stamp
    # and could therefore never be reproduced).
    assert snapshot.input_fingerprint != payload_fingerprint(snapshot.result_json)
    assert "generated_at" not in snapshot.result_json


def test_a_frozen_snapshot_is_byte_stable_when_facts_and_editions_change(
    db_client: TestClient, db_session: Session
) -> None:
    """The frozen projection is authoritative for what was disclosed: neither a
    later fact correction nor a new edition may rewrite it."""
    employee = _employee(db_session)
    snapshot, result, _ = _freeze_month_snapshot(db_session, employee, date(2026, 7, 1))
    frozen_payload = dict(result)
    frozen_fingerprint = snapshot.input_fingerprint

    created = db_client.post(
        "/api/v1/me/work-intervals",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": "2026-07-08T09:00:00",
            "end_local": "2026-07-08T12:00:00",
            "work_type": "ON_SITE",
            "client_request_id": str(uuid.uuid4()),
        },
    )
    assert created.status_code == 201, created.text
    db_session.add(
        PolicyVersion(
            policy_key=ORDINARY_KEY,
            version="2026-10-test-v3",
            effective_from=date(2026, 10, 1),
            effective_to=None,
            status=PolicyVersionStatus.PROVISIONAL,
            engine_rule_set=ORDINARY_RULE_SET_V1,
            source_manifest_hash=SOURCE_MANIFEST_HASH,
        )
    )
    db_session.flush()

    db_session.expire_all()
    stored = db_session.get(PeriodCalculationSnapshot, snapshot.id)
    assert stored is not None
    assert stored.result_json == frozen_payload
    assert stored.input_fingerprint == frozen_fingerprint
    # …while the RECOMPUTATION legitimately reflects the new fact.
    recomputed = calculate_employee_period(db_session, employee, date(2026, 7, 1))
    assert recomputed.input_fingerprint != frozen_fingerprint


def test_a_new_disclosure_appends_a_new_snapshot_number(db_session: Session) -> None:
    employee = _employee(db_session)
    first, _, _ = _freeze_month_snapshot(db_session, employee, date(2026, 7, 1))
    second, _, _ = _freeze_month_snapshot(db_session, employee, date(2026, 7, 1))
    assert (first.snapshot_no, second.snapshot_no) == (1, 2)
    assert first.id != second.id


# ---------------------------------------------------------------------------
# F4.5 §12.5 — carry-forward across an edition boundary
# ---------------------------------------------------------------------------


def _create_interval(
    db_client: TestClient, *, start: str, end: str, work_type: str = "ON_SITE"
) -> dict:
    created = db_client.post(
        "/api/v1/me/work-intervals",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": start,
            "end_local": end,
            "work_type": work_type,
            "client_request_id": str(uuid.uuid4()),
        },
    )
    assert created.status_code == 201, created.text
    return created.json()


def _mark_reviewed(db_client: TestClient, month: str) -> None:
    reviewed = db_client.post(
        f"{MONTHS}/{month}/review",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={"acknowledged": True},
    )
    assert reviewed.status_code == 200, reviewed.text


def _month_status(db_client: TestClient, month: str) -> str:
    response = db_client.get(f"{MONTHS}/{month}", headers=DEVELOPER_PRINCIPAL_HEADER)
    assert response.status_code == 200, response.text
    return response.json()["status"]


def test_an_edition_boundary_does_not_retrofit_carry_forward(
    monkeypatch: pytest.MonkeyPatch, db_client: TestClient, db_session: Session
) -> None:
    """Each chain month resolves its OWN edition: a September edition changes
    September's closing and leaves August's untouched."""
    employee = _employee(db_session)
    _create_interval(db_client, start="2026-07-06T08:00:00", end="2026-07-06T12:00:00")
    # Paid time in AUGUST as well, so a "resolve the newest edition" leak would
    # visibly move August's closing — the half that must not move.
    _create_interval(db_client, start="2026-08-10T08:00:00", end="2026-08-10T12:00:00")
    _create_interval(
        db_client, start="2026-08-10T12:00:00", end="2026-08-10T14:00:00", work_type="OVERTIME_PAID"
    )
    _create_interval(db_client, start="2026-09-07T08:00:00", end="2026-09-07T12:00:00")
    paid = _create_interval(
        db_client, start="2026-09-07T12:00:00", end="2026-09-07T14:00:00", work_type="OVERTIME_PAID"
    )
    assert paid["work_type"] == "OVERTIME_PAID"

    august_before = calculate_employee_period(db_session, employee, date(2026, 8, 1))
    september_before = calculate_employee_period(db_session, employee, date(2026, 9, 1))

    _register_v2(monkeypatch)
    _publish_v2(db_session)

    assert calculate_employee_period(db_session, employee, date(2026, 8, 1)) == august_before
    september_after = calculate_employee_period(db_session, employee, date(2026, 9, 1))
    assert (
        september_after.closing_balance_minutes
        == september_before.closing_balance_minutes - 120  # the 2 h paid interval
    )


def test_a_historical_correction_moves_a_later_closing_without_reopening_its_review(
    db_client: TestClient, db_session: Session
) -> None:
    """Both halves of F3's settled contract, pinned in one test.

    The VALUE does follow the correction through carry-forward; the REVIEWED
    assertion does not, because it is bounded to the months the changed fact's
    own dates touch.
    """
    employee = _employee(db_session)
    created = _create_interval(db_client, start="2026-07-06T08:00:00", end="2026-07-06T12:00:00")
    _mark_reviewed(db_client, "2026-08")
    august_before = calculate_employee_period(db_session, employee, date(2026, 8, 1))

    corrected = db_client.patch(
        f"/api/v1/me/work-intervals/{created['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "version": 1,
            "start_local": "2026-07-06T08:00:00",
            "end_local": "2026-07-06T16:00:00",
            "reason": "synthetic correction",
        },
    )
    assert corrected.status_code == 200, corrected.text

    august_after = calculate_employee_period(db_session, employee, date(2026, 8, 1))
    assert august_after.closing_balance_minutes == august_before.closing_balance_minutes + 240
    assert _month_status(db_client, "2026-08") == "REVIEWED"


def test_publishing_a_future_dated_edition_reopens_nothing(
    db_client: TestClient, db_session: Session
) -> None:
    """A later edition changes no existing month's inputs, so no review
    invalidation path is needed for the policy-edition case (F4 §6)."""
    _create_interval(db_client, start="2026-07-06T08:00:00", end="2026-07-06T12:00:00")
    _mark_reviewed(db_client, "2026-07")
    db_session.add(
        PolicyVersion(
            policy_key=ORDINARY_KEY,
            version="2026-10-test-v3",
            effective_from=date(2026, 10, 1),
            effective_to=None,
            status=PolicyVersionStatus.PROVISIONAL,
            engine_rule_set=ORDINARY_RULE_SET_V1,
            source_manifest_hash=SOURCE_MANIFEST_HASH,
        )
    )
    db_session.flush()
    assert _month_status(db_client, "2026-07") == "REVIEWED"


# ---------------------------------------------------------------------------
# F4.4 — a frozen submission package references a RESOLVABLE edition
# ---------------------------------------------------------------------------


def _shift_assignment(db_session: Session, employee: Employee, *, start: str, end: str):
    from app.integrations.shift_plan import ShiftAssignmentSeed
    from app.shift.service import seed_shift_assignment

    return seed_shift_assignment(
        db_session,
        employee,
        ShiftAssignmentSeed(
            plan_key=f"f4-edition-shift-{start}",
            plan_name="F4 edition test shift",
            planned_start_at=datetime.fromisoformat(start),
            planned_end_at=datetime.fromisoformat(end),
            shift_kind="NIGHT",
            source="TEST_SEED",
            source_ref="f4-policy-edition-test",
        ),
    )


def _submit_shift_report(db_client: TestClient, assignment_id: object, request_id: str):
    preview = db_client.get(
        f"/api/v1/me/shifts/{assignment_id}/report-preview", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert preview.status_code == 200, preview.text
    return db_client.post(
        "/api/v1/me/submissions/SHIFT_REPORT",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "scope": {"assignment_id": str(assignment_id)},
            "preview_fingerprint": preview.json()["preview_fingerprint"],
            "client_request_id": request_id,
            "recipients": ["recipient"],
        },
    )


def test_a_frozen_package_references_a_resolvable_edition(
    db_client: TestClient, db_session: Session
) -> None:
    """``submission_package.policy_version_id`` used to stay NULL, so a frozen
    package claimed an edition only through an unresolvable payload literal."""
    from app.disclosure.models import SubmissionPackage

    employee = _employee(db_session)
    assignment = _shift_assignment(
        db_session,
        employee,
        start="2026-07-31T19:00:00+02:00",
        end="2026-08-01T07:00:00+02:00",
    )
    created = _submit_shift_report(db_client, assignment.id, str(uuid.uuid4()))
    assert created.status_code == 200, created.text
    package = db_session.get(SubmissionPackage, uuid.UUID(created.json()["id"]))
    assert package is not None
    assert package.policy_version_id is not None
    edition_row = db_session.get(PolicyVersion, package.policy_version_id)
    assert edition_row is not None
    assert edition_row.policy_key == "shift-work"
    assert edition_row.version == package.payload_json["policy_version"]
    assert edition_row.engine_rule_set in EDITIONS


def test_a_package_outside_the_editions_effective_range_is_refused(
    db_client: TestClient, db_session: Session
) -> None:
    """The stamped edition must be the one that APPLIED to the package period.

    Stamping the 2026-06 edition onto a pre-2026-06 package would make the
    frozen artefact's own provenance false — and the backup verifier would then
    (correctly) report it. Refuse at create instead.
    """
    from app.disclosure.models import SubmissionPackage

    employee = _employee(db_session)
    assignment = _shift_assignment(
        db_session,
        employee,
        start="2026-05-15T09:00:00+02:00",
        end="2026-05-15T17:00:00+02:00",
    )
    request_id = str(uuid.uuid4())
    response = _submit_shift_report(db_client, assignment.id, request_id)
    _assert_policy_blocked(response, month="2026-05", chain_month="2026-05")
    assert (
        db_session.scalar(
            select(SubmissionPackage.id).where(SubmissionPackage.client_request_id == request_id)
        )
        is None
    )


def test_a_package_asserting_an_unexecutable_edition_is_refused(db_session: Session) -> None:
    """A row whose rule set is unregistered must not be stamped onto a package."""
    from app.enums import SubmissionPurpose

    row = db_session.scalar(select(PolicyVersion).where(PolicyVersion.policy_key == "overtime"))
    assert row is not None
    row.engine_rule_set = "phantom-rule-set-v9"
    db_session.flush()
    with pytest.raises(PolicyEditionUnavailable):
        policy_version_for_purpose(
            db_session,
            SubmissionPurpose.OVERTIME_PAID,
            {"policy_version": "2026-ot-v1"},
            date(2026, 7, 1),
        )


# ---------------------------------------------------------------------------
# F4 D7 — the restore verifier reports a policy-edition conflict
# ---------------------------------------------------------------------------


def test_the_restore_verifier_reports_overlapping_editions(db_session: Session) -> None:
    """A restore must not silently produce an ambiguous edition table."""
    from app.backup import _domain_checks

    db_session.add_all(
        [
            PolicyVersion(
                policy_key="synthetic-overlap",
                version="a",
                effective_from=date(2026, 6, 1),
                effective_to=date(2026, 12, 31),
                status=PolicyVersionStatus.PROVISIONAL,
                engine_rule_set=ORDINARY_RULE_SET_V1,
                source_manifest_hash=SOURCE_MANIFEST_HASH,
            ),
            PolicyVersion(
                policy_key="synthetic-overlap",
                version="b",
                effective_from=date(2026, 9, 1),
                effective_to=None,
                status=PolicyVersionStatus.PROVISIONAL,
                engine_rule_set=ORDINARY_RULE_SET_V1,
                source_manifest_hash=SOURCE_MANIFEST_HASH,
            ),
        ]
    )
    db_session.flush()
    failures = _domain_checks(db_session)
    assert any("overlapping effective ranges" in item for item in failures["policy"])


def test_the_restore_verifier_accepts_a_closed_predecessor_and_open_successor(
    db_session: Session,
) -> None:
    """The overlap predicate must be symmetric: an ordinary edition handover
    (predecessor closed the day before its successor starts, successor
    open-ended) is valid and must NOT be reported."""
    from app.backup import _domain_checks

    db_session.add_all(
        [
            PolicyVersion(
                policy_key="synthetic-handover",
                version="a",
                effective_from=date(2026, 6, 1),
                effective_to=date(2026, 8, 31),
                status=PolicyVersionStatus.PROVISIONAL,
                engine_rule_set=ORDINARY_RULE_SET_V1,
                source_manifest_hash=SOURCE_MANIFEST_HASH,
            ),
            PolicyVersion(
                policy_key="synthetic-handover",
                version="b",
                effective_from=date(2026, 9, 1),
                effective_to=None,
                status=PolicyVersionStatus.PROVISIONAL,
                engine_rule_set=ORDINARY_RULE_SET_V1,
                source_manifest_hash=SOURCE_MANIFEST_HASH,
            ),
        ]
    )
    db_session.flush()
    failures = _domain_checks(db_session)
    assert not any("overlapping effective ranges" in item for item in failures["policy"]), failures[
        "policy"
    ]


def test_the_restore_verifier_accepts_the_shipped_edition_set(db_session: Session) -> None:
    """The seeded editions must satisfy every policy check as shipped."""
    from app.backup import _domain_checks

    failures = _domain_checks(db_session)
    assert failures["policy"] == []
