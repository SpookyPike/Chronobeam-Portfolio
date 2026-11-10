"""Legacy-compatible transition export tests.

Covers: Work Time Record determinism over engine values, frozen-payload
submission exports (later fact edits must not rewrite an already-submitted
artifact), per-purpose dispatch, CSV escaping/formula-injection safety,
PROVISIONAL contract honesty, self-scope privacy, and the rule that private
exports are not audited while submissions remain the disclosure act.
"""

import csv
import io
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from app.audit.models import AuditEvent
from app.disclosure.models import SubmissionPackage, SubmissionPackageStatus
from app.enums import SubmissionPurpose
from app.identity.models import Employee
from app.reporting import legacy_csv
from app.reporting.legacy_csv import (
    OCD_HEADER,
    OVERTIME_HEADER,
    SHIFT_HEADER,
    render_ocd_compensation,
    render_overtime,
    render_shift_report,
)
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .conftest import DEVELOPER_PRINCIPAL_HEADER, OTHER_EMPLOYEE_HEADER
from .test_submission_package_api import _create_shift_package

WTR_URL = "/api/v1/me/reports/legacy/work-time-record"


def _package_envelope(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": str(uuid.UUID("11111111-1111-1111-1111-111111111111")),
        "version": 2,
        "status": "SUPERSEDED",
        "supersedes_id": str(uuid.UUID("22222222-2222-2222-2222-222222222222")),
    }
    base.update(overrides)
    return base


def _csv_rows(text: str) -> list[list[str]]:
    return [row for row in csv.reader(io.StringIO(text))]


# ---------------------------------------------------------------------------
# Pure renderer tests (deterministic, no DB)
# ---------------------------------------------------------------------------


def test_shift_renderer_stable_schema_envelope_and_provisional_marker() -> None:
    payload = {
        "report_type": "SHIFT_REPORT",
        "policy_version": "2026-shift-v1",
        "policy_status": "PROVISIONAL",
        "employee_identifier": "emp-0001",
        "shift_assignment_id": str(uuid.uuid4()),
        "planned_start_at": "2026-07-31T19:00:00+02:00",
        "planned_end_at": "2026-08-01T07:00:00+02:00",
        "actual_start_at": None,
        "actual_end_at": None,
        "deviation_type": None,
        "noticed_or_decided_at": None,
        "decision_maker_type": None,
        "reason": None,
        "paid_break_status": "EXPLICIT_FACT_REQUIRED",
    }
    csv_text = render_shift_report(_package_envelope(), payload)
    rows = list(csv.reader(io.StringIO(csv_text)))
    assert rows[0] == list(SHIFT_HEADER)
    assert rows[1][0] == "REPORT"
    assert rows[1][1] == "PROVISIONAL"  # report_contract_status honesty
    assert "SUPERSEDED" in rows[1]
    # blank (never invented) values for absent optional fields:
    assert csv_text.count(",,") > 0
    # reruns byte-identical:
    assert render_shift_report(_package_envelope(), payload) == csv_text


def test_csv_escapes_delimiters_and_neutralizes_formula_injection() -> None:
    payload = {
        "report_type": "SHIFT_REPORT",
        "policy_version": "v",
        "policy_status": "CONFIRMED",
        "employee_identifier": 'Smith; "quoted", ünïcøde\nnewline',
        "shift_assignment_id": "x",
        "planned_start_at": '=HYPERLINK("http://evil")',
        "planned_end_at": "+SUM(1,2)",
        "actual_start_at": "-2+3",
        "actual_end_at": "@CMD",
        "deviation_type": None,
        "noticed_or_decided_at": None,
        "decision_maker_type": None,
        "reason": "\t=1+1",
        "paid_break_status": "EXPLICIT_FACT_REQUIRED",
    }
    csv_text = render_shift_report(_package_envelope(), payload)
    rows = list(csv.reader(io.StringIO(csv_text)))
    report = dict(zip(rows[0], rows[1], strict=True))
    for column in (
        "planned_start_at",
        "planned_end_at",
        "actual_start_at",
        "actual_end_at",
        "reason",
    ):
        assert report[column].startswith("'"), column
    # quoting/Unicode survive the round-trip:
    assert "ünïcøde" in report["employee_identifier"]
    assert '"quoted"' in report["employee_identifier"]


def test_overtime_and_ocd_renderers_use_disjoint_schemas() -> None:
    package = _package_envelope(status="SUBMITTED")
    overtime = render_overtime(
        package,
        {
            "report_type": "OVERTIME",
            "policy_version": "2026-ot-v1",
            "policy_status": "CONFIRMED",
            "employee_identifier": "emp-0001",
            "period_start": "2026-08-01",
            "period_end": "2026-08-31",
            "authorizations": [
                {
                    "id": "a1",
                    "valid_from": "2026-08-01T00:00:00+02:00",
                    "valid_to": None,
                    "purpose": "beamtime",
                    "status": "ACTIVE",
                    "source_ref": None,
                }
            ],
            "overtime_intervals": [
                {
                    "interval_id": "i1",
                    "start_at": "2026-08-05T18:00:00+02:00",
                    "end_at": "2026-08-05T20:00:00+02:00",
                    "work_type": "OVERTIME_PAID",
                    "elapsed_seconds": 7200,
                    "linked_authorization_id": "a1",
                    "version": 1,
                }
            ],
            "classified_components": [
                {"rule_id": "ALLOW-002", "component": "SUPPLEMENT", "minutes": 120}
            ],
            "election": {"id": None, "mode": None, "version": None},
        },
    )
    rows = list(csv.reader(io.StringIO(overtime)))
    assert rows[0] == list(OVERTIME_HEADER)
    record_types = [row[0] for row in rows[1:]]
    assert record_types == ["REPORT", "AUTH", "INTERVAL", "COMPONENT"]
    # no ledger/interval leakage from the other report families:
    assert "start_at" not in rows[0]  # only interval_start_at/valid_from exist
    assert not any("ocd_assignment_id" in col for col in rows[0])

    ocd = render_ocd_compensation(
        package,
        {
            "report_type": "OCD_COMPENSATION",
            "policy_version": "2026-ocd-v1",
            "policy_status": "PROVISIONAL",
            "employee_identifier": "emp-0001",
            "ocd_assignment_id": "o1",
            "assignment_start_at": "2026-08-01T00:00:00+02:00",
            "assignment_end_at": "2026-08-31T23:59:59+02:00",
            "calls": [
                {
                    "call_id": "c1",
                    "mode": "REMOTE",
                    "call_start_at": "2026-08-03T02:00:00+02:00",
                    "call_end_at": "2026-08-03T02:30:00+02:00",
                    "travel_out_minutes": 10,
                    "travel_back_minutes": None,
                    "span_start_at": "2026-08-03T02:00:00+02:00",
                    "span_end_at": "2026-08-03T02:30:00+02:00",
                    "elapsed_seconds": 1800,
                }
            ],
            "classified_components": [
                {"rule_id": "OCD-NIGHT", "component": "NIGHT", "minutes": 30}
            ],
            "duty": {
                "duty_minutes": 720,
                "duty_kind": "DUTY_GE_12H",
                "units": 1,
                "basis_points": 20,
                "short_notice": True,
            },
            "election": {"id": "e1", "mode": "TIME_OFF", "version": 1},
        },
    )
    ocd_rows = list(csv.reader(io.StringIO(ocd)))
    assert ocd_rows[0] == list(OCD_HEADER)
    assert [row[0] for row in ocd_rows[1:]] == ["REPORT", "CALL", "COMPONENT"]
    assert "PROVISIONAL" in ocd_rows[1]


# ---------------------------------------------------------------------------
# Work Time Record endpoint (engine values, determinism, self-scope)
# ---------------------------------------------------------------------------


def _create_interval(client: TestClient, start_local: str, end_local: str) -> None:
    response = client.post(
        "/api/v1/me/work-intervals",
        headers=DEVELOPER_PRINCIPAL_HEADER,
        json={
            "start_local": start_local,
            "end_local": end_local,
            "work_type": "ON_SITE",
            "client_request_id": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 201, response.text


def test_work_time_record_export_renders_engine_values_deterministically(
    db_client: TestClient,
) -> None:
    _create_interval(db_client, "2026-07-06T09:00:00", "2026-07-06T17:00:00")
    first = db_client.get(f"{WTR_URL}?month=2026-07", headers=DEVELOPER_PRINCIPAL_HEADER)
    second = db_client.get(f"{WTR_URL}?month=2026-07", headers=DEVELOPER_PRINCIPAL_HEADER)
    assert first.status_code == 200
    assert second.status_code == 200
    # invariant 10: byte-identical re-render from the same facts.
    assert first.content == second.content
    assert first.headers["content-type"].startswith("text/csv")
    assert "attachment" in first.headers["content-disposition"]
    assert first.content.startswith("\ufeff".encode())
    text = first.content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    header = rows[0]
    month_row = next(row for row in rows[1:] if row[0] == "MONTH")
    fields = dict(zip(header, month_row, strict=True))
    findings = db_client.get(
        "/api/v1/me/findings?period=2026-07", headers=DEVELOPER_PRINCIPAL_HEADER
    ).json()
    assert int(fields["closing_balance_minutes"]) == findings["closing_balance_minutes"]
    assert fields["policy_version"] == "2026-06-ordinary-v1"
    assert fields["report_contract_status"] == "PROVISIONAL"
    daily = [dict(zip(header, row, strict=True)) for row in rows[1:] if row[0] == "DAILY"]
    july_6 = next(row for row in daily if row["day"] == "2026-07-06")
    assert july_6["worked_minutes"] == "480"
    assert july_6["required_minutes"] == "348"
    assert july_6["delta_minutes"] == "132"
    interval_rows = [row for row in rows[1:] if row[0] == "INTERVAL"]
    assert len(interval_rows) == 1
    assert interval_rows[0][header.index("work_type")] == "ON_SITE"


def test_work_time_record_export_is_self_scoped(db_client: TestClient) -> None:
    _create_interval(db_client, "2026-07-06T09:00:00", "2026-07-06T17:00:00")
    a_text = db_client.get(
        f"{WTR_URL}?month=2026-07", headers=DEVELOPER_PRINCIPAL_HEADER
    ).content.decode("utf-8-sig")
    b_response = db_client.get(f"{WTR_URL}?month=2026-07", headers=OTHER_EMPLOYEE_HEADER)
    assert b_response.status_code == 200
    b_text = b_response.content.decode("utf-8-sig")
    # Employee B's export is B's own month: A's identifiers and interval
    # rows never appear, and B never names an employee parameter.
    assert "emp-0001" not in b_text
    assert "2026-07-06T09:00" not in b_text
    assert "emp-0001" in a_text


def test_invalid_month_rejected(db_client: TestClient) -> None:
    bad = db_client.get(f"{WTR_URL}?month=2026-7", headers=DEVELOPER_PRINCIPAL_HEADER)
    assert bad.status_code == 422


# ---------------------------------------------------------------------------
# Submission exports: frozen payload, dispatch, ownership, audit
# ---------------------------------------------------------------------------


def test_submission_export_freezes_payload_survives_later_edits(
    db_client: TestClient, db_session: Session
) -> None:
    created = _create_shift_package(db_client, db_session)
    url = f"/api/v1/me/reports/legacy/submissions/{created['id']}"
    first = db_client.get(url, headers=DEVELOPER_PRINCIPAL_HEADER)
    assert first.status_code == 200
    assert first.content.startswith("\ufeff".encode())
    text = first.content.decode("utf-8-sig")
    assert "SHIFT_REPORT" not in text.split("\r\n")[0]  # header is fixed schema
    # pydantic json dumps freeze instants as UTC — the exported artifact
    # renders exactly the stored payload, not a re-derived local time.
    assert "2026-07-31T17:00:00Z" in text  # planned shift from payload
    assert "PROVISIONAL" in text  # policy_status AND report_contract_status
    # invariant 11: a later live-fact edit must not change the artifact.
    _create_interval(db_client, "2026-08-04T09:00:00", "2026-08-04T17:00:00")
    second = db_client.get(url, headers=DEVELOPER_PRINCIPAL_HEADER)
    assert second.content == first.content


def test_submission_export_ownership_and_dispatch(
    db_client: TestClient, db_session: Session
) -> None:
    created = _create_shift_package(db_client, db_session)
    url = f"/api/v1/me/reports/legacy/submissions/{created['id']}"
    foreign = db_client.get(url, headers=OTHER_EMPLOYEE_HEADER)
    missing = db_client.get(
        f"/api/v1/me/reports/legacy/submissions/{uuid.uuid4()}",
        headers=OTHER_EMPLOYEE_HEADER,
    )
    # invariant 15: foreign id and unknown id are byte-identical 404s.
    assert foreign.status_code == 404
    assert missing.status_code == 404
    assert foreign.content == missing.content

    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    unsupported = SubmissionPackage(
        employee_id=employee.id,
        purpose=SubmissionPurpose.WORK_TIME_RECORD,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        version=1,
        payload_json={},
        status=SubmissionPackageStatus.SUBMITTED,
    )
    db_session.add(unsupported)
    db_session.flush()
    refused = db_client.get(
        f"/api/v1/me/reports/legacy/submissions/{unsupported.id}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
    )
    assert refused.status_code == 422


def test_private_exports_are_not_audited_but_submissions_are(
    db_client: TestClient, db_session: Session
) -> None:
    created = _create_shift_package(db_client, db_session)
    before = db_session.scalar(select(func.count()).select_from(AuditEvent))
    db_client.get(f"{WTR_URL}?month=2026-07", headers=DEVELOPER_PRINCIPAL_HEADER)
    db_client.get(
        f"/api/v1/me/reports/legacy/submissions/{created['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
    )
    after = db_session.scalar(select(func.count()).select_from(AuditEvent))
    # invariant 16: exports disclose nothing by themselves -> no read audit;
    # the earlier SUBMISSION_PACKAGE_CREATE audit remains the disclosure act.
    assert after == before
    # SCOPED to THIS package, deliberately. `create_app` declares an app-level
    # `commit_before_response` dependency, so EVERY route call COMMITS the shared
    # session and `conftest`'s per-test rollback cannot undo it — meaning a global
    # count of SUBMISSION_PACKAGE_CREATE rows accumulates across the whole run and
    # across runs. Asserting it equals 1 therefore passed or failed on test
    # ORDERING and on how often the suite had been run, which is a green that
    # proves nothing. The claim actually under test is that creating a package is
    # audited exactly once, and that is what this asserts.
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.action == "SUBMISSION_PACKAGE_CREATE",
                AuditEvent.object_id == uuid.UUID(created["id"]),
            )
        )
        == 1
    )


def test_superseded_package_remains_exportable_with_status(
    db_client: TestClient, db_session: Session
) -> None:
    created = _create_shift_package(db_client, db_session)
    package = db_session.get(SubmissionPackage, uuid.UUID(created["id"]))
    assert package is not None
    package.status = SubmissionPackageStatus.SUPERSEDED
    db_session.flush()
    export = db_client.get(
        f"/api/v1/me/reports/legacy/submissions/{created['id']}",
        headers=DEVELOPER_PRINCIPAL_HEADER,
    )
    assert export.status_code == 200
    assert "SUPERSEDED" in export.content.decode("utf-8-sig")


def test_runtime_app_never_imports_excel_stack() -> None:
    # openpyxl is a dev/test-only dependency and the fixture readers are test
    # tooling: no runtime module may IMPORT either (see the implementation decision log;
    # CUTOVER-T03 production boundary). Import lines only — docstrings may
    # explain the boundary in prose.
    app_dir = Path(__file__).resolve().parents[1] / "app"
    tokens = ("openpyxl", "legacy_fixtures")
    offenders: list[str] = []
    for path in app_dir.rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")) and any(t in stripped for t in tokens):
                offenders.append(f"{path}:{stripped}")
    assert offenders == []


def test_frozen_payload_is_never_recomputed() -> None:
    """Guard: renderers are pure — no DB session, clock, or engine access."""
    source = Path(legacy_csv.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "import app",
        "from app",
        "Session",
        "datetime.now(",
        "calculate_employee_period",
        "select(",
    ):
        assert forbidden not in source
