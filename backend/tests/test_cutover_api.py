"""Cutover opening-state seed tests (CUTOVER-T01/T02).

Proves: placement, provenance, capability boundary, non-disclosing target
errors, duplicate safety (service check + DB constraint), audit durability
without balance leakage, and the clean-cutover pre-facts guard.
"""

import uuid
from datetime import date
from typing import Any

import pytest
from app.audit.models import AuditEvent
from app.balance.models import BalanceLedgerEntry
from app.cutover.service import ENTRY_TYPE_CUTOVER_SEED
from app.identity.models import Employee
from app.policy.models import PolicyVersion
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .conftest import (
    DEVELOPER_PRINCIPAL_HEADER,
    HR_HEADER,
    OTHER_EMPLOYEE_HEADER,
    RECIPIENT_HEADER,
    SUPERVISOR_HEADER,
    TECHNICAL_ADMIN_HEADER,
    production_login,
)

OPERATOR_HEADER = {"X-Chronobeam-Dev-Principal": "cutover-operator"}
SEED_URL = "/api/v1/cutover/opening-balances"
CUTOVER_MONTH = "2026-08"  # first Chronobeam month M; seed lands 2026-07-31
DENIED_BODY = (
    '{"detail":{"code":"AUTH_FORBIDDEN","message":"Access to this resource is restricted."}}'
)


def _employee(session: Session, subject: str = "employee") -> Employee:
    employee = session.scalar(select(Employee).where(Employee.idp_subject == subject))
    assert employee is not None
    return employee


def _seed(
    client: TestClient,
    *,
    external_id: str = "emp-0001",
    month: str = CUTOVER_MONTH,
    minutes: int = 1104,  # 18:24 — the documented July closing figure
    headers: dict[str, str] = OPERATOR_HEADER,
) -> Any:
    return client.post(
        SEED_URL,
        headers=headers,
        json={
            "employee_external_id": external_id,
            "cutover_month": month,
            "minutes": minutes,
        },
    )


def _create_interval(client: TestClient, start_local: str, end_local: str) -> dict[str, Any]:
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
    return response.json()


def test_cutover_t02_seed_opens_first_month_with_exactly_seeded_minutes(
    db_client: TestClient, db_session: Session
) -> None:
    created = _seed(db_client, minutes=1104)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["occurred_on"] == "2026-07-31"
    assert body["previous_period"] == "2026-07"
    assert body["confirmed_by"] == "cutover-operator"
    # Invariant 1: month M opens with exactly the seeded minutes.
    findings = db_client.get(
        "/api/v1/me/findings?period=2026-08", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    assert findings.status_code == 200
    assert findings.json()["opening_balance_minutes"] == 1104


def test_balance_continuity_next_month_needs_no_second_seed(
    db_client: TestClient, db_session: Session
) -> None:
    _seed(db_client, minutes=120)
    # One worked August day moves the balance away from the bare seed.
    _create_interval(db_client, "2026-08-03T09:00:00", "2026-08-03T17:00:00")
    august = db_client.get("/api/v1/me/findings?period=2026-08", headers=DEVELOPER_PRINCIPAL_HEADER)
    assert august.json()["opening_balance_minutes"] == 120
    august_closing = august.json()["closing_balance_minutes"]
    assert august_closing != 120
    september = db_client.get(
        "/api/v1/me/findings?period=2026-09", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    # Invariant 2: M+1 opens with exactly month M's derived closing — the
    # employee never re-enters a balance and no second seed exists.
    assert september.json()["opening_balance_minutes"] == august_closing
    october = db_client.get(
        "/api/v1/me/findings?period=2026-10", headers=DEVELOPER_PRINCIPAL_HEADER
    ).json()
    # The chain is transitive: October opens from September's derived close.
    assert october["opening_balance_minutes"] == september.json()["closing_balance_minutes"]


def test_seed_provenance_fields_and_nulls(db_client: TestClient, db_session: Session) -> None:
    _seed(db_client, minutes=-45)
    entry = db_session.scalar(
        select(BalanceLedgerEntry).where(BalanceLedgerEntry.entry_type == ENTRY_TYPE_CUTOVER_SEED)
    )
    assert entry is not None
    policy = db_session.scalar(
        select(PolicyVersion).where(PolicyVersion.policy_key == "ordinary-working-time")
    )
    assert policy is not None
    assert entry.source_entity_type == ENTRY_TYPE_CUTOVER_SEED
    assert entry.source_entity_id is None
    assert entry.work_period_id is None
    assert entry.occurred_on == date(2026, 7, 31)
    assert entry.minutes == -45  # negative carry-in (owed hours) is legitimate
    assert entry.policy_version_id == policy.id
    assert entry.metadata_json == {
        "previous_system": "EXCEL",
        "previous_period": "2026-07",
        "confirmed_by": "cutover-operator",
    }


@pytest.mark.parametrize(
    "headers",
    [
        DEVELOPER_PRINCIPAL_HEADER,
        OTHER_EMPLOYEE_HEADER,
        HR_HEADER,
        SUPERVISOR_HEADER,
        RECIPIENT_HEADER,
        TECHNICAL_ADMIN_HEADER,
    ],
)
def test_only_cutover_operator_may_seed_capability_absence_is_denial(
    headers: dict[str, str], db_client: TestClient, db_session: Session
) -> None:
    denied = _seed(db_client, headers=headers)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "AUTH_FORBIDDEN"
    rows = db_session.scalars(
        select(BalanceLedgerEntry).where(BalanceLedgerEntry.entry_type == ENTRY_TYPE_CUTOVER_SEED)
    ).all()
    assert rows == []  # denial never writes


def test_unknown_or_inactive_target_is_nondisclosing_404(db_client: TestClient) -> None:
    unknown = _seed(db_client, external_id="emp-does-not-exist")
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["code"] == "NOT_FOUND"


def test_duplicate_seed_is_409_service_check_and_db_constraint(
    db_client: TestClient, db_session: Session
) -> None:
    assert _seed(db_client).status_code == 201
    duplicate = _seed(db_client, minutes=999)
    assert duplicate.status_code == 409
    body = duplicate.json()["detail"]
    assert body["code"] == "CONFLICT_CUTOVER_SEED"
    assert body["details"]["reason"] == "DUPLICATE_SEED"
    # The rejected attempt never mutated canonical data:
    assert (
        db_session.scalar(
            select(BalanceLedgerEntry.minutes).where(
                BalanceLedgerEntry.entry_type == ENTRY_TYPE_CUTOVER_SEED
            )
        )
        == 1104
    )
    # Race-level guard is authoritative even if the pre-check is bypassed:
    employee = _employee(db_session)
    policy = db_session.scalar(select(PolicyVersion))
    assert policy is not None
    db_session.add(
        BalanceLedgerEntry(
            employee_id=employee.id,
            occurred_on=date(2026, 7, 31),
            entry_type=ENTRY_TYPE_CUTOVER_SEED,
            minutes=1,
            source_entity_type=ENTRY_TYPE_CUTOVER_SEED,
            policy_version_id=policy.id,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_audits_are_durable_and_never_record_the_balance(
    db_client: TestClient, db_session: Session
) -> None:
    _seed(db_client, minutes=1104)
    _seed(db_client, minutes=2222)  # rejected duplicate (same transaction)
    audits = db_session.scalars(select(AuditEvent).where(AuditEvent.action == "CUTOVER_SEED")).all()
    outcomes = {audit.outcome for audit in audits}
    assert outcomes == {"CREATED", "REJECTED_DUPLICATE"}
    for audit in audits:
        assert audit.metadata_json is not None
        # DP-LOG-001: neither amount appears in audit payloads.
        serialized = str(audit.metadata_json)
        assert "1104" not in serialized and "2222" not in serialized
        assert set(audit.metadata_json) <= {"month", "error_code"}


def test_clean_cutover_guard_rejects_pre_month_facts(
    db_client: TestClient, db_session: Session
) -> None:
    # Employee already has a live July interval: seeding a July-closing
    # balance for August would double-count legacy work.
    _create_interval(db_client, "2026-07-06T09:00:00", "2026-07-06T17:00:00")
    rejected = _seed(db_client)
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["details"]["reason"] == "PRE_CUTOVER_FACTS"
    assert (
        db_session.scalar(
            select(BalanceLedgerEntry).where(
                BalanceLedgerEntry.entry_type == ENTRY_TYPE_CUTOVER_SEED
            )
        )
        is None
    )
    # Facts INSIDE/AFTER M remain allowed (controlled late seeding while the
    # July interval is this month's own work, not a pre-cutover fact).
    late_ok = _seed(db_client, month="2026-07")
    assert late_ok.status_code == 201, late_ok.text


def test_month_validation_and_extra_fields_rejected(db_client: TestClient) -> None:
    bad_month = db_client.post(
        SEED_URL,
        headers=OPERATOR_HEADER,
        json={"employee_external_id": "emp-0001", "cutover_month": "2026-13", "minutes": 5},
    )
    assert bad_month.status_code == 422
    smuggled = db_client.post(
        SEED_URL,
        headers=OPERATOR_HEADER,
        json={
            "employee_external_id": "emp-0001",
            "cutover_month": "2026-08",
            "minutes": 5,
            "workbook_base64": "UEsDBg",  # there is NO workbook channel — by design
        },
    )
    assert smuggled.status_code == 422


# ---------------------------------------------------------------------------
# Reviewer follow-ups (findings 4-6)
# ---------------------------------------------------------------------------


def test_policy_not_installed_for_month_is_clean_422(db_client: TestClient) -> None:
    """Out-of-range months reject with 422, never 500."""
    response = _seed(db_client, month="2026-05")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VALIDATION_INVALID_MONTH"


@pytest.fixture()
def operator_oidc_keys() -> tuple[bytes, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private_pem, public_pem.decode()


@pytest.fixture()
def production_operator_client(
    db_session: Session,
    operator_oidc_keys: tuple[bytes, str],
    monkeypatch: pytest.MonkeyPatch,
    fake_idp,
) -> Any:
    """Production config with a configured allowlist."""
    from app.auth.deps import get_db
    from app.config import get_settings
    from app.main import create_app

    monkeypatch.setenv("CHRONOBEAM_ENV", "production")
    monkeypatch.setenv("CHRONOBEAM_PUBLIC_BASE_URL", "https://chronobeam.test")
    monkeypatch.setenv("CHRONOBEAM_OIDC_DISCOVERY_URL", fake_idp.discovery_url)
    monkeypatch.setenv("CHRONOBEAM_OIDC_ISSUER", fake_idp.issuer)
    monkeypatch.setenv("CHRONOBEAM_OIDC_CLIENT_ID", fake_idp.client_id)
    monkeypatch.setenv("CHRONOBEAM_OIDC_AUDIENCE", fake_idp.audience)
    monkeypatch.setenv(
        "CHRONOBEAM_DATABASE_URL",
        "postgresql+psycopg://chronobeam:pilot_test@127.0.0.1:5433/chronobeam",
    )
    monkeypatch.setenv("CHRONOBEAM_CUTOVER_OPERATOR_SUBJECTS", '["cutover-svc"]')
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    # HTTPS base URL: the session cookie is Secure.
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client
    get_settings.cache_clear()


def test_production_allowlist_only_operator_can_seed(
    production_operator_client: TestClient,
    fake_idp,
    db_session: Session,
) -> None:
    # Authentication is a browser session, so the operator subject needs a
    # mapped+active employee row to complete a login; the ALLOWLIST, not that
    # row, is what grants CUTOVER_SEED_WRITE.
    db_session.add(
        Employee(legal_name="Cutover Service (synthetic)", idp_subject="cutover-svc", active=True)
    )
    db_session.flush()

    payload = {"employee_external_id": "emp-0001", "cutover_month": CUTOVER_MONTH, "minutes": 33}
    anonymous = production_operator_client.post(SEED_URL, json=payload)
    assert anonymous.status_code == 401  # no credential at all
    as_employee = production_operator_client.post(
        SEED_URL,
        headers=production_login(
            production_operator_client, db_session, fake_idp, subject="employee"
        ),
        json=payload,
    )
    assert as_employee.status_code == 403  # being an employee is not an operator
    assert as_employee.text == DENIED_BODY
    operator = production_operator_client.post(
        SEED_URL,
        headers=production_login(
            production_operator_client, db_session, fake_idp, subject="cutover-svc"
        ),
        json=payload,
    )
    assert operator.status_code == 201, operator.text
    assert operator.json()["confirmed_by"] == "cutover-svc"
    row = db_session.scalar(
        select(BalanceLedgerEntry).where(BalanceLedgerEntry.entry_type == ENTRY_TYPE_CUTOVER_SEED)
    )
    assert row is not None
    assert row.metadata_json is not None
    assert row.metadata_json["confirmed_by"] == "cutover-svc"


def test_concurrent_duplicate_seeds_leave_exactly_one_row(
    db_engine: Any,
) -> None:
    """Exercise the real race (two live transactions).

    Thread A holds an uncommitted CUTOVER_SEED insert; thread B passes the
    pre-check (A invisible under READ COMMITTED) and collides with the
    partial unique index on flush — proving the savepoint/expunge path and
    that the loser still commits its rejection audit.
    """
    import threading
    import time

    from app.auth.principal import Capability, Principal
    from app.cutover.service import (
        CutoverSeedCreated,
        CutoverSeedRejected,
        seed_opening_balance,
    )
    from sqlalchemy import delete
    from sqlalchemy.orm import Session as NewSession

    setup = NewSession(db_engine)
    race_employee = Employee(
        idp_subject="race-c",
        external_employee_id="emp-race-c",
        legal_name="Race Test (synthetic)",
        active=True,
    )
    setup.add(race_employee)
    setup.commit()
    race_id = race_employee.id
    operator = Principal(
        subject="race-operator",
        display_name="race",
        capabilities=frozenset({Capability.CUTOVER_SEED_WRITE}),
    )
    session_a = NewSession(db_engine)
    session_b = NewSession(db_engine)
    outcome_b: dict[str, str] = {}
    try:
        result_a = seed_opening_balance(
            session_a,
            operator,
            employee_external_id="emp-race-c",
            cutover_month=date(2026, 8, 1),
            minutes=777,
        )
        assert isinstance(result_a, CutoverSeedCreated)  # NOT yet committed

        def _loser() -> None:
            result = seed_opening_balance(
                session_b,
                operator,
                employee_external_id="emp-race-c",
                cutover_month=date(2026, 8, 1),
                minutes=888,
            )
            outcome_b["reason"] = (
                result.reason if isinstance(result, CutoverSeedRejected) else "CREATED"
            )
            session_b.commit()

        thread = threading.Thread(target=_loser)
        thread.start()
        time.sleep(0.5)  # B is now blocked on the unique index
        session_a.commit()
        thread.join(30)
        assert not thread.is_alive()
        assert outcome_b["reason"] == "DUPLICATE_SEED"

        check = NewSession(db_engine)
        try:
            seeds = check.scalars(
                select(BalanceLedgerEntry).where(
                    BalanceLedgerEntry.employee_id == race_id,
                    BalanceLedgerEntry.entry_type == ENTRY_TYPE_CUTOVER_SEED,
                )
            ).all()
            assert len(seeds) == 1
            assert seeds[0].minutes == 777
            rejected = check.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.action == "CUTOVER_SEED",
                    AuditEvent.employee_subject_id == race_id,
                    AuditEvent.outcome == "REJECTED_DUPLICATE",
                )
            )
            assert rejected == 1  # durable past the rolled-back insert
        finally:
            check.close()
    finally:
        session_a.close()
        session_b.close()
        cleanup = NewSession(db_engine)
        cleanup.execute(delete(BalanceLedgerEntry).where(BalanceLedgerEntry.employee_id == race_id))
        cleanup.execute(delete(AuditEvent).where(AuditEvent.employee_subject_id == race_id))
        cleanup.execute(delete(Employee).where(Employee.id == race_id))
        cleanup.commit()
        cleanup.close()
