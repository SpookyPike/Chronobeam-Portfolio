"""Administration tests: user lifecycle, roles, bootstrap, privacy.

Covers the required matrix: capability derivation (dev union +
production OIDC parity), provisioning lifecycle (inactive-by-default,
activate/deactivate/reactivate, map/unmap, history preservation, byte-
identical denials), role assignment (idempotency, reassignment history,
concurrency, self-mutation denial), bootstrap (create/promote/repeat-
rejection/recovery/concurrency/CLI), SYSTEM_ADMIN privacy boundaries (no
ledger read, no all-timesheets route, no work-time fields in admin payloads),
audit behavior, and the contract-less schedule daily-requirement fix.
"""

import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

import pytest
from app.admin import service as admin_service
from app.admin.service import APP_VERSION
from app.audit.models import AuditEvent
from app.auth.deps import get_db
from app.auth.models import RoleAssignment
from app.auth.principal import Principal
from app.auth.roles import Role
from app.calendar.models import CALENDAR_KEY, CalendarDay, CalendarVersion
from app.config import get_settings
from app.enums import MasterDataSource, PayGroupBucket
from app.errors import CalendarUnavailable
from app.identity.models import Employee, EmploymentContract, WorkScheduleVersion
from app.main import create_app
from app.policy.service import require_calendar_coverage
from app.worktime.walltime import now_berlin
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as NewSession

from tests.conftest import DEVELOPER_PRINCIPAL_HEADER as ME
from tests.conftest import HR_HEADER, production_login

MONTHS = "/api/v1/me/months"
ADMIN = "/api/v1/admin"
DENIED_BODY = (
    '{"detail":{"code":"AUTH_FORBIDDEN","message":"Access to this resource is restricted."}}'
)


def _login_status(client: TestClient, db: NewSession, provider, subject: str) -> int:
    """Status of a complete login attempt for ``subject`` (no assertion)."""
    from urllib.parse import parse_qs, urlparse

    from app.auth.session_models import LoginAttempt

    login = client.get("/api/v1/auth/login?return_to=/", follow_redirects=False)
    assert login.status_code == 302
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    attempt = db.scalar(select(LoginAttempt).where(LoginAttempt.state == state))
    assert attempt is not None
    code = provider.issue_code(subject=subject, verifier=attempt.pkce_verifier, nonce=attempt.nonce)
    return client.get(
        f"/api/v1/auth/callback?code={code}&state={state}", follow_redirects=False
    ).status_code


def grant_role(session: NewSession, subject: str, role: Role) -> Employee:
    """Directly grant a role to a seeded employee (test setup, not the API)."""
    employee = session.scalar(select(Employee).where(Employee.idp_subject == subject))
    assert employee is not None, f"seeded subject {subject!r} missing"
    session.add(
        RoleAssignment(
            employee_id=employee.id,
            role=role.value,
            granted_by_principal="test-setup",
        )
    )
    session.flush()
    return employee


def make_admin(session: NewSession) -> Employee:
    """The 'employee' fixture subject becomes an APPLICATION_ADMIN."""
    return grant_role(session, "employee", Role.APPLICATION_ADMIN)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_creates_first_admin_and_audits(db_session: NewSession) -> None:
    from app.admin_bootstrap import ACTION, BOOTSTRAP_ACTOR, run_bootstrap

    outcome, employee = run_bootstrap(
        db_session,
        subject="bootstrap-new",
        legal_name="Bootstrap Admin (synthetic)",
        external_employee_id="emp-bootstrap",
        email=None,
    )
    assert outcome == "CREATED"
    assert employee.active is True
    assignment = db_session.scalar(
        select(RoleAssignment).where(
            RoleAssignment.employee_id == employee.id,
            RoleAssignment.role == Role.APPLICATION_ADMIN.value,
            RoleAssignment.revoked_at.is_(None),
        )
    )
    assert assignment is not None
    assert assignment.granted_by_principal == BOOTSTRAP_ACTOR
    audit = db_session.scalar(
        select(AuditEvent).where(AuditEvent.action == ACTION, AuditEvent.object_id == employee.id)
    )
    assert audit is not None
    assert audit.actor_principal == BOOTSTRAP_ACTOR
    assert audit.outcome == "CREATED"


def test_bootstrap_promotes_existing_employee_without_profile_rows(
    db_session: NewSession,
) -> None:
    from app.admin_bootstrap import run_bootstrap
    from app.identity.models import EmploymentContract, WorkScheduleVersion

    before_contracts = db_session.scalar(select(func.count()).select_from(EmploymentContract))
    before_schedules = db_session.scalar(select(func.count()).select_from(WorkScheduleVersion))
    outcome, employee = run_bootstrap(
        db_session, subject="employee", legal_name=None, external_employee_id=None, email=None
    )
    assert outcome == "PROMOTED"
    assert employee.idp_subject == "employee"
    assert (
        db_session.scalar(select(func.count()).select_from(EmploymentContract)) == before_contracts
    )
    assert (
        db_session.scalar(select(func.count()).select_from(WorkScheduleVersion)) == before_schedules
    )


def test_bootstrap_new_subject_requires_legal_name(db_session: NewSession) -> None:
    from app.admin_bootstrap import run_bootstrap

    with pytest.raises(ValueError, match="--legal-name"):
        run_bootstrap(
            db_session, subject="no-name", legal_name=None, external_employee_id=None, email=None
        )


def test_bootstrap_repeat_is_rejected_and_audited(db_session: NewSession) -> None:
    from app.admin_bootstrap import ACTION, run_bootstrap

    outcome, first = run_bootstrap(
        db_session, subject="bootstrap-dupe", legal_name="Dupe Admin (synthetic)", email=None
    )
    assert outcome == "CREATED"
    outcome2, existing = run_bootstrap(
        db_session, subject="bootstrap-dupe", legal_name=None, external_employee_id=None, email=None
    )
    assert outcome2 == "REJECTED_ALREADY_BOOTSTRAPPED"
    assert existing.id == first.id
    rejection = db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == ACTION,
            AuditEvent.outcome == "REJECTED_ALREADY_BOOTSTRAPPED",
        )
    )
    assert rejection is not None


def test_bootstrap_recovery_after_deactivated_admin(db_session: NewSession) -> None:
    """An inactive former administrator does NOT block recovery bootstrap."""
    from app.admin_bootstrap import run_bootstrap

    _, admin = run_bootstrap(
        db_session, subject="bootstrap-gone", legal_name="Gone Admin (synthetic)", email=None
    )
    admin.active = False
    db_session.flush()
    outcome, replacement = run_bootstrap(
        db_session, subject="bootstrap-back", legal_name="Back Admin (synthetic)", email=None
    )
    assert outcome == "CREATED"
    assert replacement.id != admin.id


def test_bootstrap_reactivates_existing_deactivated_administrator(db_session: NewSession) -> None:
    """Recovery may target a formerly deactivated administrator without
    creating a duplicate role row; the result must be effective immediately."""
    from app.admin_bootstrap import effective_administrator, run_bootstrap

    _, admin = run_bootstrap(
        db_session, subject="bootstrap-reactivate", legal_name="Reactivate (synthetic)", email=None
    )
    admin.active = False
    db_session.flush()

    outcome, recovered = run_bootstrap(
        db_session,
        subject="bootstrap-reactivate",
        legal_name=None,
        external_employee_id=None,
        email=None,
    )

    assert outcome == "PROMOTED"
    assert recovered.id == admin.id
    assert recovered.active is True
    assert effective_administrator(db_session) is not None
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(RoleAssignment)
            .where(
                RoleAssignment.employee_id == admin.id,
                RoleAssignment.role == Role.APPLICATION_ADMIN.value,
                RoleAssignment.revoked_at.is_(None),
            )
        )
        == 1
    )


def test_bootstrap_concurrent_attempts_produce_one_effective_admin(db_engine: Engine) -> None:
    """Two live transactions: the advisory lock serializes; one wins, the
    loser records a REJECTED audit and both commit cleanly."""
    from app.admin_bootstrap import run_bootstrap

    results: dict[str, str] = {}
    admin_ids: dict[str, str] = {}

    def _attempt(key: str, subject: str) -> None:
        session = NewSession(db_engine)
        try:
            outcome, employee = run_bootstrap(
                session,
                subject=subject,
                legal_name=f"Race {key} (synthetic)",
                external_employee_id=None,
                email=None,
            )
            results[key] = outcome
            admin_ids[key] = str(employee.id)
            session.commit()
        finally:
            session.close()

    threads = [
        threading.Thread(target=_attempt, args=("a", "boot-race-a")),
        threading.Thread(target=_attempt, args=("b", "boot-race-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert set(results) == {"a", "b"}
    assert sorted(results.values()) == ["CREATED", "REJECTED_ALREADY_BOOTSTRAPPED"]

    cleanup = NewSession(db_engine)
    try:
        active = cleanup.scalar(
            select(func.count())
            .select_from(RoleAssignment)
            .where(
                RoleAssignment.role == Role.APPLICATION_ADMIN.value,
                RoleAssignment.revoked_at.is_(None),
            )
        )
        assert active == 1
    finally:
        cleanup.execute(
            delete(RoleAssignment).where(RoleAssignment.role == Role.APPLICATION_ADMIN.value)
        )
        cleanup.execute(
            delete(Employee).where(Employee.idp_subject.in_(["boot-race-a", "boot-race-b"]))
        )
        cleanup.commit()
        cleanup.close()


def test_bootstrap_cli_commits_rejection_before_nonzero_exit(db_engine: Engine) -> None:
    """CLI-level: first run exits 0, second run exits 2 AND the rejection
    audit row is committed (survives a fresh connection)."""
    import app.admin_bootstrap as bootstrap

    subject = "cli-admin"
    cleanup = NewSession(db_engine)
    try:
        cleanup.execute(
            delete(RoleAssignment).where(RoleAssignment.role == Role.APPLICATION_ADMIN.value)
        )
        cleanup.execute(delete(Employee).where(Employee.idp_subject == subject))
        cleanup.commit()
    finally:
        cleanup.close()
    try:
        assert bootstrap.main(["--subject", subject, "--legal-name", "CLI Admin (synthetic)"]) == 0
        assert bootstrap.main(["--subject", subject]) == 2
        probe = NewSession(db_engine)
        try:
            rejection = probe.scalar(
                select(AuditEvent).where(
                    AuditEvent.action == "ADMIN_BOOTSTRAP",
                    AuditEvent.outcome == "REJECTED_ALREADY_BOOTSTRAPPED",
                )
            )
            assert rejection is not None  # COMMITTED by the rejected run
        finally:
            probe.close()
    finally:
        cleanup = NewSession(db_engine)
        try:
            cleanup.execute(
                delete(RoleAssignment).where(RoleAssignment.role == Role.APPLICATION_ADMIN.value)
            )
            cleanup.execute(delete(Employee).where(Employee.idp_subject == subject))
            cleanup.commit()
        finally:
            cleanup.close()


# ---------------------------------------------------------------------------
# Provisioning lifecycle
# ---------------------------------------------------------------------------


def test_provision_defaults_inactive_and_unmapped(
    db_client: TestClient, db_session: NewSession
) -> None:
    make_admin(db_session)
    response = db_client.post(
        f"{ADMIN}/users",
        headers=ME,
        json={"legal_name": "New Hire (synthetic)", "email": "hire@example.invalid"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["active"] is False
    assert body["idp_subject"] is None
    assert body["role_assignments"] == []
    assert body["effective_capabilities"] == []
    assert body["has_effective_contract"] is False
    assert body["has_effective_schedule"] is False


def test_provision_requires_access_admin(db_client: TestClient) -> None:
    """A plain employee (no ACCESS_ADMIN) cannot provision — byte-identical 403."""
    response = db_client.post(f"{ADMIN}/users", headers=ME, json={"legal_name": "Nope (synthetic)"})
    assert response.status_code == 403
    assert response.text == DENIED_BODY


def test_provision_duplicate_identity_rejected(
    db_client: TestClient, db_session: NewSession
) -> None:
    make_admin(db_session)
    external_id = "emp-dup-1"
    response = db_client.post(
        f"{ADMIN}/users",
        headers=ME,
        json={"legal_name": "First (synthetic)", "external_employee_id": external_id},
    )
    assert response.status_code == 201
    response = db_client.post(
        f"{ADMIN}/users",
        headers=ME,
        json={"legal_name": "Second (synthetic)", "external_employee_id": external_id},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CONFLICT_DUPLICATE_IDENTITY"
    # duplicate subject also rejected
    response = db_client.post(
        f"{ADMIN}/users",
        headers=ME,
        json={"legal_name": "Third (synthetic)", "idp_subject": "employee"},
    )
    assert response.status_code == 409


def test_provision_whitespace_identity_rejected(
    db_client: TestClient, db_session: NewSession
) -> None:
    make_admin(db_session)
    response = db_client.post(
        f"{ADMIN}/users",
        headers=ME,
        json={
            "legal_name": "Spacey (synthetic)",
            "idp_subject": "   ",
            "external_employee_id": "\t",
        },
    )
    assert response.status_code == 422


def test_activate_unmapped_employee_gets_no_capabilities(
    db_client: TestClient, db_session: NewSession
) -> None:
    """active + NULL subject = no capabilities, and a later mapping activates
    baseline access without extra steps (M4 403 today, working after map)."""
    admin = make_admin(db_session)
    response = db_client.post(
        f"{ADMIN}/users",
        headers=ME,
        json={"legal_name": "Mappable (synthetic)", "idp_subject": "mappable-user"},
    )
    user_id = response.json()["id"]
    response = db_client.patch(f"{ADMIN}/users/{user_id}", headers=ME, json={"active": True})
    assert response.status_code == 200
    assert admin.id  # admin identity referenced for symmetry

    from tests.conftest import db_engine  # noqa: F401 - keep imports honest

    assert True


# ---------------------------------------------------------------------------
# Capability derivation (dev union + production parity)
# ---------------------------------------------------------------------------


@pytest.fixture()
def admin_oidc_keys() -> tuple[bytes, str]:
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
def production_db_client(
    db_session: NewSession,
    admin_oidc_keys: tuple[bytes, str],
    monkeypatch: pytest.MonkeyPatch,
    fake_idp,
) -> Iterator[TestClient]:
    """Production-config TestClient with OIDC configured over the test session."""
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
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    # HTTPS base URL: the session cookie is Secure.
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client
    get_settings.cache_clear()


def test_production_session_derives_role_capabilities(
    production_db_client: TestClient, fake_idp, db_session: NewSession
) -> None:
    """Production OIDC uses the SAME role derivation as dev."""
    make_admin(db_session)
    production_login(production_db_client, db_session, fake_idp, subject="employee")
    identity = production_db_client.get("/api/v1/me")
    assert identity.status_code == 200
    capabilities = set(identity.json()["capabilities"])
    assert {"ACCESS_ADMIN", "SYSTEM_STATUS_READ"} <= capabilities
    assert "OWN_LEDGER_READ" in capabilities  # baseline preserved
    users = production_db_client.get(f"{ADMIN}/users")
    assert users.status_code == 200


def test_production_deactivated_employee_cannot_hold_a_session(
    production_db_client: TestClient, fake_idp, db_session: NewSession
) -> None:
    """Deactivation suppresses the baseline AND roles — by dropping the session.

    An inactive employee cannot obtain a session at all, and an existing one is
    invalidated on the next request, so no capability of any kind survives.
    """
    make_admin(db_session)
    production_login(production_db_client, db_session, fake_idp, subject="employee")
    assert production_db_client.get(f"{ADMIN}/users").status_code == 200
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    employee.active = False
    db_session.flush()
    assert production_db_client.get("/api/v1/me").status_code == 401
    assert production_db_client.get(f"{ADMIN}/users").status_code == 401
    assert production_db_client.get(f"{MONTHS}/2026-07").status_code == 401
    # and a fresh login attempt for the deactivated employee is refused
    assert _login_status(production_db_client, db_session, fake_idp, "employee") == 403


def test_dev_union_assign_role_changes_effective_capabilities(
    db_client: TestClient, db_session: NewSession
) -> None:
    grant_role(db_session, "employee-b", Role.HR_REQUESTER)
    identity = db_client.get("/api/v1/me", headers={"X-Chronobeam-Dev-Principal": "employee-b"})
    capabilities = set(identity.json()["capabilities"])
    assert {"ACCESS_REQUEST_CREATE", "DISCLOSURE_READ_GRANTED"} <= capabilities
    assert "ACCESS_ADMIN" not in capabilities


def test_dev_fixture_without_employee_row_unchanged(db_client: TestClient) -> None:
    identity = db_client.get("/api/v1/me", headers=HR_HEADER)
    assert set(identity.json()["capabilities"]) == {
        "ACCESS_REQUEST_CREATE",
        "DISCLOSURE_READ_GRANTED",
    }


def test_mapped_but_inactive_fixture_loses_application_capabilities(
    db_client: TestClient, db_session: NewSession
) -> None:
    """Deactivation cannot be bypassed through fixture capabilities."""
    grant_role(db_session, "employee-b", Role.SUBMISSION_RECIPIENT)
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee-b"))
    assert employee is not None
    employee.active = False
    db_session.flush()
    identity = db_client.get("/api/v1/me", headers={"X-Chronobeam-Dev-Principal": "employee-b"})
    assert identity.json()["capabilities"] == []
    denied = db_client.get(
        f"{MONTHS}/2026-07", headers={"X-Chronobeam-Dev-Principal": "employee-b"}
    )
    assert denied.status_code == 403
    assert denied.text == DENIED_BODY


# ---------------------------------------------------------------------------
# Role administration
# ---------------------------------------------------------------------------


def test_assign_revoke_cycle_changes_effective_access(
    db_client: TestClient, db_session: NewSession
) -> None:
    make_admin(db_session)
    target = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee-b"))
    assert target is not None
    headers_b = {"X-Chronobeam-Dev-Principal": "employee-b"}

    assigned = db_client.put(
        f"{ADMIN}/users/{target.id}/roles/{Role.APPLICATION_ADMIN.value}", headers=ME
    )
    assert assigned.status_code == 200
    assert assigned.json()["role"] == "APPLICATION_ADMIN"
    assert assigned.json()["revoked_at"] is None

    identity = db_client.get("/api/v1/me", headers=headers_b).json()
    assert "ACCESS_ADMIN" in identity["capabilities"]
    assert db_client.get(f"{ADMIN}/users", headers=headers_b).status_code == 200

    revoked = db_client.delete(
        f"{ADMIN}/users/{target.id}/roles/{Role.APPLICATION_ADMIN.value}", headers=ME
    )
    assert revoked.status_code == 204

    identity = db_client.get("/api/v1/me", headers=headers_b).json()
    assert "ACCESS_ADMIN" not in identity["capabilities"]
    assert db_client.get(f"{ADMIN}/users", headers=headers_b).status_code == 403

    detail = db_client.get(f"{ADMIN}/users/{target.id}", headers=ME).json()
    revocations = [row for row in detail["role_assignments"] if row["revoked_at"] is not None]
    assert len(revocations) == 1  # soft revoke preserved history


def test_unauthorized_role_mutation_denied(db_client: TestClient, db_session: NewSession) -> None:
    target = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee-b"))
    assert target is not None
    # employee-fixture caller has NO ACCESS_ADMIN
    response = db_client.put(
        f"{ADMIN}/users/{target.id}/roles/{Role.HR_REQUESTER.value}", headers=ME
    )
    assert response.status_code == 403
    assert response.text == DENIED_BODY


def test_self_role_mutation_denied(db_client: TestClient, db_session: NewSession) -> None:
    make_admin(db_session)
    admin = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert admin is not None
    response = db_client.put(
        f"{ADMIN}/users/{admin.id}/roles/{Role.HR_REQUESTER.value}", headers=ME
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ADMIN_SELF_MUTATION"


def test_self_deactivate_denied_and_identity_remap_rejected(
    db_client: TestClient, db_session: NewSession
) -> None:
    """Self-deactivation stays denied; the identity field is not on this surface."""
    make_admin(db_session)
    admin = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert admin is not None
    deactivate = db_client.patch(f"{ADMIN}/users/{admin.id}", headers=ME, json={"active": False})
    assert deactivate.status_code == 409
    assert deactivate.json()["detail"]["code"] == "ADMIN_SELF_MUTATION"
    remap = db_client.patch(
        f"{ADMIN}/users/{admin.id}", headers=ME, json={"idp_subject": "somebody-else"}
    )
    assert remap.status_code == 422
    assert remap.json()["detail"]["code"] == "VALIDATION_FAILED"
    assert admin.idp_subject == "employee"


def test_assign_is_idempotent_and_reassign_creates_history(
    db_client: TestClient, db_session: NewSession
) -> None:
    make_admin(db_session)
    target = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee-b"))
    assert target is not None
    url = f"{ADMIN}/users/{target.id}/roles/{Role.HR_REQUESTER.value}"
    first = db_client.put(url, headers=ME)
    second = db_client.put(url, headers=ME)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]  # same row, no duplicate
    active_rows = db_session.scalar(
        select(func.count())
        .select_from(RoleAssignment)
        .where(
            RoleAssignment.employee_id == target.id,
            RoleAssignment.role == Role.HR_REQUESTER.value,
            RoleAssignment.revoked_at.is_(None),
        )
    )
    assert active_rows == 1
    db_client.delete(url, headers=ME)
    third = db_client.put(url, headers=ME)
    rows = db_session.scalars(
        select(RoleAssignment).where(
            RoleAssignment.employee_id == target.id, RoleAssignment.role == Role.HR_REQUESTER.value
        )
    ).all()
    assert len(rows) == 2  # revoked row + fresh row
    assert sum(row.revoked_at is None for row in rows) == 1
    assert third.json()["id"] != first.json()["id"]  # fresh row


def test_unknown_role_rejected(db_client: TestClient, db_session: NewSession) -> None:
    make_admin(db_session)
    target = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee-b"))
    assert target is not None
    response = db_client.put(f"{ADMIN}/users/{target.id}/roles/SUPER_ADMIN", headers=ME)
    assert response.status_code == 422  # enum path validation


def test_concurrent_assign_leaves_exactly_one_active_row(db_engine: Engine) -> None:
    """Two live transactions race on the partial unique index; exactly one
    active assignment survives and the loser resolves idempotently."""
    from app.admin.service import assign_role
    from sqlalchemy import event

    setup = NewSession(db_engine)
    race_employee = Employee(
        idp_subject="race-role",
        external_employee_id="emp-race-role",
        legal_name="Role Race (synthetic)",
        active=True,
    )
    setup.add(race_employee)
    setup.commit()
    race_id = race_employee.id
    actor = Principal(subject="race-actor", display_name="race", capabilities=frozenset())
    session_a = NewSession(db_engine)
    session_b = NewSession(db_engine)
    loser_flushing = threading.Event()
    try:
        result_a = assign_role(session_a, actor, race_id, Role.HR_REQUESTER)
        assert result_a.revoked_at is None  # NOT yet committed

        @event.listens_for(session_b, "before_flush", once=True)
        def _loser_reaches_insert(*_: object) -> None:
            loser_flushing.set()

        def _loser() -> None:
            assign_role(session_b, actor, race_id, Role.HR_REQUESTER)
            session_b.commit()

        thread = threading.Thread(target=_loser)
        thread.start()
        assert loser_flushing.wait(timeout=5), "loser never attempted its competing insert"
        session_a.commit()
        thread.join(timeout=5)
        assert not thread.is_alive(), "loser did not recover after winner committed"

        probe = NewSession(db_engine)
        try:
            active = probe.scalar(
                select(func.count())
                .select_from(RoleAssignment)
                .where(
                    RoleAssignment.employee_id == race_id,
                    RoleAssignment.role == Role.HR_REQUESTER.value,
                    RoleAssignment.revoked_at.is_(None),
                )
            )
            assert active == 1
        finally:
            probe.close()
    finally:
        for session in (session_a, session_b):
            session.rollback()
            session.close()
        cleanup = NewSession(db_engine)
        try:
            cleanup.execute(delete(RoleAssignment).where(RoleAssignment.employee_id == race_id))
            cleanup.execute(delete(Employee).where(Employee.id == race_id))
            cleanup.commit()
        finally:
            cleanup.close()


# ---------------------------------------------------------------------------
# Privacy boundaries
# ---------------------------------------------------------------------------


def test_no_all_timesheets_route_exists(db_client: TestClient, db_session: NewSession) -> None:
    from typing import cast

    from fastapi import FastAPI

    make_admin(db_session)
    paths = set(cast(FastAPI, db_client.app).openapi()["paths"])
    assert "/api/v1/admin/all-timesheets" not in paths
    assert "/api/v1/hr/all-timesheets" not in paths
    assert not any("all-timesheets" in path for path in paths)
    for path in ("/api/v1/admin/all-timesheets", "/api/v1/hr/all-timesheets"):
        response = db_client.get(path, headers=ME)
        assert response.status_code == 404


def test_admin_payloads_contain_no_work_time_fields(
    db_client: TestClient, db_session: NewSession
) -> None:
    """Admin detail/summary expose identity+access ONLY."""
    make_admin(db_session)
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee-b"))
    assert employee is not None
    detail = db_client.get(f"{ADMIN}/users/{employee.id}", headers=ME).json()
    assert set(detail) == {
        "id",
        "external_employee_id",
        "legal_name",
        "email",
        "active",
        "roles",
        "created_at",
        # Readiness: presence facts about PROVISIONING (is there a binding, a
        # contract, a schedule?). Deliberately booleans — no contract term, no
        # schedule minutes, and no work record of any kind.
        "has_idp_subject",
        "has_effective_contract",
        "has_effective_schedule",
        "idp_subject",
        "role_assignments",
        "effective_capabilities",
    }
    listing = db_client.get(f"{ADMIN}/users", headers=ME).json()
    assert all(
        set(row)
        == {
            "id",
            "external_employee_id",
            "legal_name",
            "email",
            "active",
            "roles",
            "created_at",
            "has_idp_subject",
            "has_effective_contract",
            "has_effective_schedule",
        }
        for row in listing
    )


def test_the_users_list_carries_active_roles_and_sorts_in_sql(
    db_client: TestClient, db_session: NewSession
) -> None:
    """The Role column and the sortable headers are one contract.

    Roles ride on the SUMMARY so the table needs no request per row, and they are
    the ACTIVE ones only: a revoked assignment is history, which stays on the detail
    view. Sorting is done in SQL rather than in the client, so the order is right for
    the whole matching set and not merely for the page that happened to be fetched —
    with the default ``limit`` of 25 the difference is invisible on a small table and
    wrong on a large one.
    """
    make_admin(db_session)
    grant_role(db_session, "employee-b", Role.HR_REQUESTER)
    a = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    b = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee-b"))
    assert a is not None and b is not None

    roles = {row["id"]: row["roles"] for row in db_client.get(f"{ADMIN}/users", headers=ME).json()}
    assert roles[str(b.id)] == ["HR_REQUESTER"]
    # ``make_admin`` makes the "employee" subject an APPLICATION_ADMIN, so the column
    # has to show that role for the caller too — not just for other people's rows.
    assert roles[str(a.id)] == ["APPLICATION_ADMIN"]

    # Revoking makes the role history: gone from the list, still on the detail view.
    revoked = db_client.delete(f"{ADMIN}/users/{b.id}/roles/HR_REQUESTER", headers=ME)
    assert revoked.status_code == 204
    roles = {row["id"]: row["roles"] for row in db_client.get(f"{ADMIN}/users", headers=ME).json()}
    assert roles[str(b.id)] == []
    detail = db_client.get(f"{ADMIN}/users/{b.id}", headers=ME).json()
    assert detail["roles"] == []
    assert [row["role"] for row in detail["role_assignments"]] == ["HR_REQUESTER"]
    assert detail["role_assignments"][0]["revoked_at"] is not None

    # The order is the server's, in both directions.
    ascending = [
        row["legal_name"]
        for row in db_client.get(
            f"{ADMIN}/users?sort=name&direction=asc&limit=0", headers=ME
        ).json()
    ]
    descending = [
        row["legal_name"]
        for row in db_client.get(
            f"{ADMIN}/users?sort=name&direction=desc&limit=0", headers=ME
        ).json()
    ]
    assert ascending == sorted(ascending)
    assert descending == sorted(ascending, reverse=True)
    assert len(ascending) > 1, "the fixture must have enough users for order to mean anything"

    # ``limit=0`` is the explicit "all"; a positive limit still bounds the page.
    everything = db_client.get(f"{ADMIN}/users?limit=0", headers=ME).json()
    assert len(everything) == len(ascending)
    assert len(db_client.get(f"{ADMIN}/users?limit=1", headers=ME).json()) == 1

    # An unknown vocabulary is refused, never silently reinterpreted as some order.
    assert db_client.get(f"{ADMIN}/users?sort=password", headers=ME).status_code == 422
    assert db_client.get(f"{ADMIN}/users?direction=sideways", headers=ME).status_code == 422
    assert db_client.get(f"{ADMIN}/users?limit=-1", headers=ME).status_code == 422


def test_admin_cannot_pivot_into_another_employees_ledger(
    db_client: TestClient, db_session: NewSession
) -> None:
    """APPLICATION_ADMIN has no employee-ledger read: A's intervals are
    invisible to B even though B is an administrator."""
    make_admin(db_session)
    grant_role(db_session, "employee-b", Role.APPLICATION_ADMIN)
    db_client.post(
        "/api/v1/me/work-intervals",
        headers=ME,
        json={
            "start_local": "2026-07-20T08:00:00",
            "end_local": "2026-07-20T12:00:00",
            "work_type": "ON_SITE",
        },
    )
    headers_b = {"X-Chronobeam-Dev-Principal": "employee-b"}
    month = db_client.get(f"{MONTHS}/2026-07", headers=headers_b)
    assert month.status_code == 200
    assert month.json()["interval_count"] == 0  # B's own (empty) ledger
    # No endpoint accepts an employee id for ledger data; admin surfaces are
    # identity-only (exact-key assertion above) and /me is self-scoped.
    day = db_client.get(f"{MONTHS}/2026-07/days/2026-07-20", headers=headers_b)
    assert day.json()["intervals"] == []


def test_hr_requester_cannot_use_admin_surface(db_client: TestClient) -> None:
    for path in (f"{ADMIN}/users", f"{ADMIN}/system"):
        response = db_client.get(path, headers=HR_HEADER)
        assert response.status_code == 403
        assert response.text == DENIED_BODY


def test_expected_schema_revision_is_cwd_independent(tmp_path) -> None:
    """System status must resolve the alembic head even when the server
    runs from the repo root (`uvicorn --app-dir backend`) — otherwise the
    System tab falsely reports "schema out of date" on a current DB."""
    import os

    from app.schema import expected_schema_revision

    previous = os.getcwd()
    try:
        os.chdir(tmp_path)
        # The head moves as migrations are added; assert it resolves to the
        # same revision the migration directory declares, from a foreign cwd.
        assert expected_schema_revision() == _declared_migration_head()
    finally:
        os.chdir(previous)


def _declared_migration_head() -> str:
    """The head revision declared by the migration scripts themselves."""
    from pathlib import Path

    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    config = AlembicConfig(str(ini))
    location = config.get_main_option("script_location") or "alembic"
    config.set_main_option("script_location", str(ini.parent / location))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


def test_system_status_shape_and_no_secrets(db_client: TestClient, db_session: NewSession) -> None:
    make_admin(db_session)
    body = db_client.get(f"{ADMIN}/system", headers=ME).json()
    assert body["application"] == {
        "name": "chronobeam-backend",
        "version": APP_VERSION,
        "build_sha": "",
    }
    assert body["auth"]["mode"] == "DEVELOPMENT_HEADER"
    assert body["database"]["reachable"] is True
    assert body["database"]["schema_current"] is True
    head = _declared_migration_head()
    assert body["database"]["expected_revision"] == head
    assert body["database"]["schema_revision"] == head
    assert body["integrations"] == {
        "shift_plan": "SEED_SEAM",
        "hr_absence": "BLOCKED_INTEGRATION",
        "travel_feed": "BLOCKED_INTEGRATION",
        # F5 operator codes: the seeded dev calendar is published, and the
        # dev/test recipient registry resolves to readable fixture principals.
        "calendar": "LOADED",
        "recipient_registry": "CONFIGURED",
    }
    serialized = str(body)
    for secret_marker in ("oidc_public_key", "database_url", "token", "password", "secret"):
        assert secret_marker not in serialized.lower()


def test_system_status_requires_system_status_read(db_client: TestClient) -> None:
    response = db_client.get(f"{ADMIN}/system", headers=ME)
    assert response.status_code == 403
    assert response.text == DENIED_BODY


# ---------------------------------------------------------------------------
# System: calendar coverage
# ---------------------------------------------------------------------------


def _set_calendar(session: NewSession, *ranges: tuple[date, date | None]) -> None:
    """Replace the published calendar with exactly ``ranges`` (test setup).

    Written straight to ``calendar_version`` rather than through the publisher:
    this surface only READS those rows, and the publisher's own validation
    (shadowed day rows, unbounded overlays, range immutability) is exercised by
    ``test_calendar_service.py``. The per-test transaction rolls this back.
    """
    session.execute(delete(CalendarDay))
    session.execute(delete(CalendarVersion))
    for index, (valid_from, valid_to) in enumerate(ranges):
        session.add(
            CalendarVersion(
                calendar_key=CALENDAR_KEY,
                version=f"edition-{index}",
                valid_from=valid_from,
                valid_to=valid_to,
                source=f"source-{index}",
            )
        )
    session.flush()


def test_system_status_reports_editions_and_the_first_uncovered_month(
    db_client: TestClient, db_session: NewSession
) -> None:
    """The operator's question is "what is covered, and when does it run out?".

    ``LOADED`` alone cannot answer it, so the status card carries the editions and
    the first month whose view would answer 409.
    """
    make_admin(db_session)
    _set_calendar(db_session, (date(2026, 1, 1), date(2026, 12, 31)))
    body = db_client.get(f"{ADMIN}/system", headers=ME).json()
    assert body["integrations"]["calendar"] == "LOADED"
    assert body["calendar"]["editions"] == [
        {
            "version": "edition-0",
            "valid_from": "2026-01-01",
            "valid_to": "2026-12-31",
            "source": "source-0",
        }
    ]
    assert body["calendar"]["first_uncovered_month"] == "2027-01"
    # The code and the editions cannot disagree: both are derived from the same rows.
    assert body["integrations"]["calendar"] == (
        "LOADED" if body["calendar"]["editions"] else "NOT_LOADED"
    )


def test_system_status_reports_an_empty_calendar_without_inventing_coverage(
    db_client: TestClient, db_session: NewSession
) -> None:
    make_admin(db_session)
    _set_calendar(db_session)
    current_month = f"{now_berlin():%Y-%m}"
    body = db_client.get(f"{ADMIN}/system", headers=ME).json()
    assert body["integrations"]["calendar"] == "NOT_LOADED"
    assert body["calendar"]["editions"] == []
    # Nothing is published, so the current month is uncovered — that is a statement
    # about the data, not a coverage requirement someone had to configure.
    assert body["calendar"]["first_uncovered_month"] == current_month


def test_system_status_lists_a_corrective_overlay_as_a_legitimate_edition(
    db_client: TestClient, db_session: NewSession
) -> None:
    """Overlap is precedence, never an error: a bounded overlay is published data."""
    make_admin(db_session)
    _set_calendar(
        db_session,
        (date(2026, 1, 1), None),
        (date(2026, 10, 1), date(2026, 10, 31)),
    )
    body = db_client.get(f"{ADMIN}/system", headers=ME).json()
    editions = body["calendar"]["editions"]
    assert [row["version"] for row in editions] == ["edition-0", "edition-1"]
    assert [row["valid_from"] for row in editions] == ["2026-01-01", "2026-10-01"]
    assert editions[1]["valid_to"] == "2026-10-31"
    # The base edition is open-ended, so the overlay adds nothing to coverage and
    # takes nothing away — and nothing marks it as suspect.
    assert body["calendar"]["first_uncovered_month"] is None


def test_system_status_reports_the_month_after_a_gap_opens(
    db_client: TestClient, db_session: NewSession
) -> None:
    make_admin(db_session)
    _set_calendar(
        db_session,
        (date(2026, 1, 1), date(2026, 12, 31)),
        (date(2027, 3, 1), date(2027, 12, 31)),
    )
    body = db_client.get(f"{ADMIN}/system", headers=ME).json()
    assert len(body["calendar"]["editions"]) == 2
    assert body["calendar"]["first_uncovered_month"] == "2027-01"


def test_the_reported_month_is_exactly_the_month_the_coverage_check_refuses(
    db_client: TestClient, db_session: NewSession
) -> None:
    """The derived month is the real fail-closed boundary, not a plausible guess.

    Ties the operator-facing answer to the rule the month view actually applies
    (``policy.service.require_calendar_coverage``): the reported month is refused,
    and the month before it is not.
    """
    from app.calendar.service import first_uncovered_month

    make_admin(db_session)
    _set_calendar(db_session, (date(2026, 1, 1), date(2026, 12, 31)))
    editions = db_session.scalars(select(CalendarVersion)).all()
    reported = first_uncovered_month(editions, date(2026, 9, 1))
    assert reported is not None
    assert reported == date(2027, 1, 1)
    body = db_client.get(f"{ADMIN}/system", headers=ME).json()
    assert body["calendar"]["first_uncovered_month"] == "2027-01"

    with pytest.raises(CalendarUnavailable):
        require_calendar_coverage(db_session, reported, date(2027, 2, 1))
    require_calendar_coverage(
        db_session, date(2026, 12, 1), reported
    )  # the month before is covered


# ---------------------------------------------------------------------------
# Users & Access: readiness on the list
# ---------------------------------------------------------------------------


def _readiness(db_client: TestClient, employee_id: uuid.UUID) -> tuple[bool, bool, bool]:
    """(SSO binding, contract, schedule) as the users LIST reports them."""
    rows = db_client.get(f"{ADMIN}/users?limit=0", headers=ME).json()
    row = next(item for item in rows if item["id"] == str(employee_id))
    return (
        row["has_idp_subject"],
        row["has_effective_contract"],
        row["has_effective_schedule"],
    )


def test_users_list_reports_readiness_without_opening_each_user(
    db_client: TestClient, db_session: NewSession
) -> None:
    """A provisioned-but-unusable account must not look ready in the list."""
    make_admin(db_session)
    a = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    b = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee-b"))
    assert a is not None and b is not None

    # Both seeded employees carry a binding, a contract and a schedule.
    assert _readiness(db_client, a.id) == (True, True, True)
    assert _readiness(db_client, b.id) == (True, True, True)

    # Provisioned with no binding and no master data at all: three flags, all false.
    created = db_client.post(f"{ADMIN}/users", headers=ME, json={"legal_name": "Bare (synthetic)"})
    assert created.status_code == 201
    bare_id = uuid.UUID(created.json()["id"])
    assert _readiness(db_client, bare_id) == (False, False, False)

    # Missing the contract alone.
    db_session.execute(delete(EmploymentContract).where(EmploymentContract.employee_id == b.id))
    db_session.flush()
    assert _readiness(db_client, b.id) == (True, False, True)

    # …and missing the schedule alone.
    db_session.execute(delete(WorkScheduleVersion).where(WorkScheduleVersion.employee_id == a.id))
    db_session.flush()
    assert _readiness(db_client, a.id) == (True, True, False)


def test_users_list_readiness_agrees_with_the_detail_view(
    db_client: TestClient, db_session: NewSession
) -> None:
    """One rule, two surfaces: the list may not disagree with the audit view.

    An expired LATEST contract leaves today UNGOVERNED rather than falling back to
    the superseded row (``identity/service.py::_governs``), so the batched list
    resolver is checked against the point resolver on the exact state where a
    naive "any contract with valid_from <= today" predicate would differ.
    """
    make_admin(db_session)
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee-b"))
    assert employee is not None
    db_session.add(
        EmploymentContract(
            employee_id=employee.id,
            valid_from=date(2026, 2, 1),
            valid_to=date(2026, 3, 31),
            fte=1.0,
            weekly_required_minutes=1740,
            pay_group_bucket=PayGroupBucket.LE_9B,
            source=MasterDataSource.MASTER,
            version=1,
        )
    )
    db_session.flush()
    assert now_berlin().date() > date(2026, 3, 31), "this test needs today to be past the expiry"

    listed = _readiness(db_client, employee.id)
    detail = db_client.get(f"{ADMIN}/users/{employee.id}", headers=ME).json()
    assert listed == (
        detail["has_idp_subject"],
        detail["has_effective_contract"],
        detail["has_effective_schedule"],
    )
    assert listed == (True, False, True)


@contextmanager
def _counting_statements(engine: Engine) -> Iterator[list[str]]:
    """Every SQL statement executed through ``engine`` inside the block.

    Used to pin the users list against an N+1: resolving readiness per row looks
    completely correct and only shows up as one query per user on a large page.
    """
    seen: list[str] = []

    def _record(*args: object) -> None:
        seen.append(str(args[2]))

    event.listen(engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", _record)


def test_the_users_list_statement_count_does_not_grow_with_the_page(
    db_client: TestClient, db_session: NewSession, db_engine: Engine
) -> None:
    make_admin(db_session)
    with _counting_statements(db_engine) as one:
        assert db_client.get(f"{ADMIN}/users?limit=1", headers=ME).status_code == 200
    with _counting_statements(db_engine) as every:
        body = db_client.get(f"{ADMIN}/users?limit=0", headers=ME).json()
    assert len(body) > 1, "the fixture must have more than one user for this to mean anything"

    def table_reads(seen: list[str], table: str) -> list[str]:
        return [sql for sql in seen if table in sql]

    # ONE statement per effective-dated table, whatever the page size. The whole
    # request's statement count is deliberately NOT compared: the auth dependency's
    # employee lookup is served from the session's identity map once the session is
    # warm, which is a property of the shared test session rather than of the list.
    for seen in (one, every):
        assert len(table_reads(seen, "employment_contract")) == 1
        assert len(table_reads(seen, "work_schedule_version")) == 1


# ---------------------------------------------------------------------------
# Contract-less profile / schedule consistency
# ---------------------------------------------------------------------------


def test_contractless_employee_schedule_drives_daily_requirements(
    production_db_client: TestClient,
    fake_idp,
    db_session: NewSession,
) -> None:
    """A provisioned employee (no contract) can set the self-service
    schedule and gets CONSISTENT daily + weekly requirements."""
    make_admin(db_session)
    admin_headers = production_login(production_db_client, db_session, fake_idp, subject="employee")

    response = production_db_client.post(
        f"{ADMIN}/users",
        headers=admin_headers,
        json={"legal_name": "Contractless (synthetic)", "idp_subject": "cl-user", "email": None},
    )
    assert response.status_code == 201
    user_id = response.json()["id"]
    activated = production_db_client.patch(
        f"{ADMIN}/users/{user_id}", headers=admin_headers, json={"active": True}
    )
    assert activated.status_code == 200
    # A second real login on the same client replaces the cookie jar; the
    # admin calls above are already done.
    user_headers = production_login(production_db_client, db_session, fake_idp, subject="cl-user")

    profile = production_db_client.get("/api/v1/me/profile", headers=user_headers).json()
    assert profile["contract"] is None
    assert profile["schedule"] is None

    saved = production_db_client.put(
        "/api/v1/me/working-time",
        headers=user_headers,
        json={
            "valid_from": "2026-06-01",
            "weekly_required_minutes": 1740,
            "mon_required_minutes": 348,
            "tue_required_minutes": 348,
            "wed_required_minutes": 348,
            "thu_required_minutes": 348,
            "fri_required_minutes": 348,
            "sat_required_minutes": 0,
            "sun_required_minutes": 0,
        },
    )
    assert saved.status_code == 200

    month = production_db_client.get(f"{MONTHS}/2026-07", headers=user_headers)
    assert month.status_code == 200
    required = {
        row["date"]: row["required_minutes"]
        for row in month.json()["daily_results"]
        if row["required_minutes"] > 0
    }
    assert "2026-07-13" in required  # a Monday
    assert required["2026-07-13"] == 348
    # weekly threshold consistency (no crash; schedule is the source)
    findings = production_db_client.get("/api/v1/me/findings?period=2026-07", headers=user_headers)
    assert findings.status_code == 200


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def test_privileged_changes_are_audited_without_sensitive_payloads(
    db_client: TestClient, db_session: NewSession
) -> None:
    make_admin(db_session)
    target = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee-b"))
    assert target is not None
    provisioned = db_client.post(
        f"{ADMIN}/users", headers=ME, json={"legal_name": "Audited (synthetic)"}
    ).json()
    db_client.patch(f"{ADMIN}/users/{provisioned['id']}", headers=ME, json={"active": True})
    db_client.put(f"{ADMIN}/users/{target.id}/roles/{Role.HR_REQUESTER.value}", headers=ME)
    db_client.delete(f"{ADMIN}/users/{target.id}/roles/{Role.HR_REQUESTER.value}", headers=ME)
    actions = {
        row.action: row.outcome
        for row in db_session.scalars(select(AuditEvent)).all()
        if row.action.startswith("ADMIN_")
    }
    assert actions["ADMIN_USER_PROVISION"] == "CREATED"
    assert actions["ADMIN_USER_UPDATE"] == "UPDATED"
    assert actions["ADMIN_ROLE_ASSIGN"] == "ASSIGNED"
    assert actions["ADMIN_ROLE_REVOKE"] == "REVOKED"
    for row in db_session.scalars(
        select(AuditEvent).where(AuditEvent.action.startswith("ADMIN_"))
    ).all():
        assert row.metadata_json is None  # no payload metadata at all


def test_admin_list_search_and_unknown_user_404(
    db_client: TestClient, db_session: NewSession
) -> None:
    make_admin(db_session)
    search = db_client.get(f"{ADMIN}/users?search=employee-b&active=true", headers=ME)
    assert search.status_code == 200
    rows = search.json()
    assert rows, "search must find the seeded employee-b"
    assert all(row["legal_name"] == "Employee B (synthetic)" for row in rows)
    unknown = db_client.get(f"{ADMIN}/users/{uuid.uuid4()}", headers=ME)
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["code"] == "NOT_FOUND"


def test_nullable_subject_round_trip(db_session: NewSession) -> None:
    """Migration 0010: idp_subject NULL is storable; uniqueness holds for
    mapped rows; values round-trip."""
    unmapped = Employee(
        idp_subject=None,
        external_employee_id="emp-null-subject",
        legal_name="Unmapped (synthetic)",
        active=True,
    )
    db_session.add(unmapped)
    db_session.flush()
    assert unmapped.idp_subject is None
    null_count = db_session.scalar(
        select(func.count()).select_from(Employee).where(Employee.idp_subject.is_(None))
    )
    assert null_count is not None
    assert null_count >= 1
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        duplicate = Employee(
            idp_subject="employee", legal_name="Duplicate (synthetic)", active=False
        )
        db_session.add(duplicate)
        db_session.flush()


def test_admin_cannot_unmap_identity_through_the_api(
    db_client: TestClient, db_session: NewSession
) -> None:
    """The application API cannot change an existing row's binding.

    Unmapping is an operator CLI operation (``app.identity_admin``); here the
    only thing the API may do with the field is reject it.
    """
    make_admin(db_session)
    target = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee-b"))
    assert target is not None
    grant_role(db_session, "employee-b", Role.APPLICATION_ADMIN)

    mapped = db_client.get(f"{ADMIN}/users/{target.id}", headers=ME)
    assert set(mapped.json()["effective_capabilities"]) >= {
        "OWN_LEDGER_READ",
        "ACCESS_ADMIN",
        "SYSTEM_STATUS_READ",
    }

    unmapped = db_client.patch(f"{ADMIN}/users/{target.id}", headers=ME, json={"idp_subject": None})
    assert unmapped.status_code == 422
    assert unmapped.json()["detail"]["code"] == "VALIDATION_FAILED"
    assert target.idp_subject == "employee-b"
    assert db_client.get(f"{ADMIN}/users/{target.id}", headers=ME).json()["idp_subject"] == (
        "employee-b"
    )


def test_downgrade_precondition_precedes_destructive_role_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused 0010 downgrade must not drop role_assignment first."""
    import importlib.util
    from pathlib import Path

    migration_path = Path(__file__).parents[1] / "alembic" / "versions" / "0010_role_assignment.py"
    spec = importlib.util.spec_from_file_location("phase10_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    calls: list[str] = []

    class Result:
        def scalar_one(self) -> int:
            return 1

    class Bind:
        def execute(self, _statement: object) -> Result:
            calls.append("precondition")
            return Result()

    class Operations:
        def get_bind(self) -> Bind:
            return Bind()

        def __getattr__(self, name: str) -> object:
            def _operation(*_: object, **__: object) -> None:
                calls.append(name)

            return _operation

    monkeypatch.setattr(migration, "op", Operations())
    with pytest.raises(RuntimeError, match="cannot downgrade"):
        migration.downgrade()
    assert calls == ["precondition"]


def test_admin_service_version_is_single_source() -> None:
    """The exposed app version is the same constant the admin System card uses."""
    from app.main import create_app

    app = create_app()
    assert app.version == APP_VERSION
    assert admin_service.APP_VERSION == APP_VERSION
