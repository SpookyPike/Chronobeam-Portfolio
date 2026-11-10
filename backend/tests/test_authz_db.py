"""P0 authorization matrix on the REAL /me/* routes (L3 integration).

ACCESS-001/002/012/013, adjacent-employee IDOR (ACCESS-T02), existence-oracle
guard (M4), production OIDC boundary end-to-end (M7): a real browser-session
login reads the own month; an unmapped subject cannot obtain a session at all
(403 + zero rows written), and a bearer token is not a credential.
"""

import uuid
from collections.abc import Iterator

import pytest
from app.auth.deps import get_db
from app.config import get_settings
from app.identity.models import Employee
from app.main import create_app
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.conftest import (
    DEVELOPER_PRINCIPAL_HEADER as ME,
)
from tests.conftest import (
    HR_HEADER,
    RECIPIENT_HEADER,
    SUPERVISOR_HEADER,
    TECHNICAL_ADMIN_HEADER,
    production_login,
)
from tests.conftest import (
    OTHER_EMPLOYEE_HEADER as ME_B,
)

MONTHS = "/api/v1/me/months"
INTERVALS = "/api/v1/me/work-intervals"
NON_EMPLOYEE_HEADERS = [HR_HEADER, SUPERVISOR_HEADER, RECIPIENT_HEADER, TECHNICAL_ADMIN_HEADER]

DENIED_BODY = (
    '{"detail":{"code":"AUTH_FORBIDDEN","message":"Access to this resource is restricted."}}'
)


def test_ACCESS_T01_employee_reads_own_month(db_client: TestClient) -> None:
    response = db_client.post(
        INTERVALS,
        headers=ME,
        json={
            "start_local": "2026-07-15T08:00:00",
            "end_local": "2026-07-15T12:00:00",
            "work_type": "ON_SITE",
        },
    )
    assert response.status_code == 201
    month = db_client.get(f"{MONTHS}/2026-07", headers=ME).json()
    assert month["interval_count"] == 1
    assert month["days"][0]["date"] == "2026-07-15"


@pytest.mark.parametrize("headers", NON_EMPLOYEE_HEADERS)
def test_ACCESS_T03_T04_T05_hr_supervisor_recipient_admin_denied(
    db_client: TestClient, headers: dict[str, str]
) -> None:
    """No ledger bytes for HR/supervisor/recipient/technical-admin, and all
    four denials are byte-identical (ACCESS-002/012/013, ARCH-AUTH-002)."""
    for path in (f"{MONTHS}/2026-07", f"{MONTHS}/2026-07/days/2026-07-15"):
        response = db_client.get(path, headers=headers)
        assert response.status_code == 403
        assert response.text == DENIED_BODY


def test_ACCESS_T02_employee_b_interval_id_indistinguishable_from_nonexistent(
    db_client: TestClient, db_session: Session
) -> None:
    """A's interval id is invisible to B: 404 byte-identical to a random UUID
    (IDOR-shaped proof; ownership inside the lookup predicate, M4)."""
    created = db_client.post(
        INTERVALS,
        headers=ME,
        json={
            "start_local": "2026-07-16T08:00:00",
            "end_local": "2026-07-16T12:00:00",
            "work_type": "REMOTE",
        },
    ).json()
    foreign = db_client.patch(f"{INTERVALS}/{created['id']}", headers=ME_B, json={"version": 1})
    missing = db_client.patch(f"{INTERVALS}/{uuid.uuid4()}", headers=ME_B, json={"version": 1})
    assert foreign.status_code == 404
    assert foreign.text == missing.text


def test_ACCESS_T02_b_cannot_see_a_data_in_any_scope(db_client: TestClient) -> None:
    db_client.post(
        INTERVALS,
        headers=ME,
        json={
            "start_local": "2026-07-17T08:00:00",
            "end_local": "2026-07-17T12:00:00",
            "work_type": "ON_SITE",
        },
    )
    # B's own view of the same month/day is B's (empty) ledger
    month = db_client.get(f"{MONTHS}/2026-07", headers=ME_B).json()
    assert month["interval_count"] == 0
    day = db_client.get(f"{MONTHS}/2026-07/days/2026-07-17", headers=ME_B).json()
    assert day["intervals"] == []


def test_unmapped_inactive_employee_gets_identical_denial(
    db_client: TestClient, db_session: Session
) -> None:
    """active=false maps to the one byte-identical 403 (M4)."""
    employee_b = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee-b"))
    assert employee_b is not None
    employee_b.active = False
    db_session.flush()

    response = db_client.get(f"{MONTHS}/2026-07", headers=ME_B)
    assert response.status_code == 403
    assert response.text == DENIED_BODY


def test_adjacent_employee_and_month_idor(db_client: TestClient) -> None:
    """Adjacent employee/month probing reveals nothing: no endpoint accepts an
    employee id, and months outside the own data return empty 200s."""
    db_client.post(
        INTERVALS,
        headers=ME,
        json={
            "start_local": "2026-07-18T08:00:00",
            "end_local": "2026-07-18T12:00:00",
            "work_type": "ON_SITE",
        },
    )
    june = db_client.get(f"{MONTHS}/2026-06", headers=ME).json()
    assert june["interval_count"] == 0
    august = db_client.get(f"{MONTHS}/2026-08", headers=ME).json()
    assert august["interval_count"] == 0
    # B probing A's month shape gets B's own empty ledger: same envelope
    # shape (only period_month identifies the month itself)
    b_july = db_client.get(f"{MONTHS}/2026-07", headers=ME_B).json()
    assert b_july["period_month"] == "2026-07-01"
    assert b_july["interval_count"] == 0
    assert b_july["days"] == []
    assert b_july["status"] == june["status"] == "OPEN"


# ---------------------------------------------------------------------------
# Production OIDC boundary end-to-end
# ---------------------------------------------------------------------------


@pytest.fixture()
def oidc_keys() -> tuple[bytes, str]:
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
    db_session: Session,
    oidc_keys: tuple[bytes, str],
    monkeypatch: pytest.MonkeyPatch,
    fake_idp,
) -> Iterator[TestClient]:
    """Production-config TestClient with the OIDC boundary configured and the
    test session wired as the DB (valid tokens only)."""
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
    # HTTPS base URL: the session cookie is Secure, so an http:// client would
    # never send it back.
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client
    get_settings.cache_clear()


def test_production_session_login_employee_reads_own_month(
    production_db_client: TestClient, fake_idp, db_session: Session
) -> None:
    """The seeded employee subject 'employee' maps to OWN_LEDGER_* and reads
    the ledger through a real browser session (DP-AUTH-001 seam)."""
    production_login(production_db_client, db_session, fake_idp, subject="employee")
    response = production_db_client.get(f"{MONTHS}/2026-07")
    assert response.status_code == 200
    assert response.json()["interval_count"] == 0
    identity = production_db_client.get("/api/v1/me")
    assert identity.status_code == 200
    assert identity.json()["subject"] == "employee"


def test_production_unmapped_subject_cannot_obtain_a_session(
    production_db_client: TestClient, fake_idp, db_session: Session
) -> None:
    """An unmapped subject never reaches a session, and NO employee row is
    written (no JIT provisioning — ``OPEN-05``)."""
    count_before = db_session.scalar(select(func.count()).select_from(Employee))
    login = production_db_client.get("/api/v1/auth/login?return_to=/", follow_redirects=False)
    assert login.status_code == 302
    from urllib.parse import parse_qs, urlparse

    from app.auth.session_models import LoginAttempt

    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    attempt = db_session.scalar(select(LoginAttempt).where(LoginAttempt.state == state))
    assert attempt is not None
    code = fake_idp.issue_code(
        subject="stranger-subject", verifier=attempt.pkce_verifier, nonce=attempt.nonce
    )
    callback = production_db_client.get(
        f"/api/v1/auth/callback?code={code}&state={state}", follow_redirects=False
    )
    assert callback.status_code == 403
    assert callback.json()["detail"]["code"] == "AUTH_FORBIDDEN"
    assert production_db_client.get("/api/v1/me").status_code == 401
    assert production_db_client.get(f"{MONTHS}/2026-07").status_code == 401
    count_after = db_session.scalar(select(func.count()).select_from(Employee))
    assert count_after == count_before


def test_production_bearer_token_is_not_a_credential(
    production_db_client: TestClient, fake_idp
) -> None:
    """A well-formed ID token in Authorization is ignored: no bearer path."""
    response = production_db_client.get(
        f"{MONTHS}/2026-07", headers={"Authorization": f"Bearer {fake_idp.mint('employee')}"}
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_REQUIRED"


def test_production_garbage_token_rejected(production_db_client: TestClient) -> None:
    response = production_db_client.get(
        f"{MONTHS}/2026-07", headers={"Authorization": "Bearer not.a.token"}
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_REQUIRED"


def test_production_missing_token_rejected(production_db_client: TestClient) -> None:
    response = production_db_client.get(f"{MONTHS}/2026-07")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_REQUIRED"
