"""Opaque-session expiry, login browser binding and production response boundaries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from app.admin import service as admin_service
from app.admin.schemas import AdminUserPatch
from app.auth import oidc, session_service
from app.auth.principal import Principal
from app.auth.session_models import AuthSession, LoginAttempt
from app.auth.session_service import COOKIE_NAME, LOGIN_COOKIE_NAME, token_hash
from app.config import Settings, get_settings
from app.identity.models import Employee
from app.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from tests.oidc_fake import FakeIdP


def _settings(**values: int) -> Settings:
    return Settings(
        env="test",
        session_idle_ttl_seconds=values.get("idle", 30),
        session_absolute_ttl_seconds=values.get("absolute", 60),
    )


def test_find_session_extends_idle_but_never_absolute(db_session: Session, monkeypatch) -> None:
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    initial = datetime(2026, 9, 13, tzinfo=UTC)
    monkeypatch.setattr(session_service, "now", lambda: initial)
    token, record = session_service.create_session(
        db_session,
        employee_id=employee.id,
        subject="employee",
        settings=_settings(idle=10, absolute=15),
    )
    assert record.idle_expires_at == initial + timedelta(seconds=10)
    monkeypatch.setattr(session_service, "now", lambda: initial + timedelta(seconds=8))
    found = session_service.find_session(db_session, token, _settings(idle=10, absolute=15))
    assert found is not None
    assert found.last_seen_at == initial + timedelta(seconds=8)
    assert found.idle_expires_at == initial + timedelta(seconds=15)


def test_csrf_session_lookup_does_not_extend_idle_window(db_session: Session, monkeypatch) -> None:
    """CSRF validation leaves the request auth lookup as the sole TTL writer."""
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    initial = datetime(2026, 9, 13, tzinfo=UTC)
    monkeypatch.setattr(session_service, "now", lambda: initial)
    token, record = session_service.create_session(
        db_session,
        employee_id=employee.id,
        subject="employee",
        settings=_settings(idle=10, absolute=60),
    )
    monkeypatch.setattr(session_service, "now", lambda: initial + timedelta(seconds=8))
    found = session_service.find_session(
        db_session, token, _settings(idle=10, absolute=60), touch=False
    )
    assert found is not None
    assert found.last_seen_at == record.last_seen_at == initial
    assert found.idle_expires_at == record.idle_expires_at == initial + timedelta(seconds=10)


def test_admin_deactivation_revokes_existing_opaque_sessions(db_session: Session) -> None:
    """The supported user lifecycle cannot leave a session revivable later."""
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    token, record = session_service.create_session(
        db_session,
        employee_id=employee.id,
        subject="employee",
        settings=_settings(),
    )
    admin_service.update_user(
        db_session,
        Principal(subject="operator", display_name="Operator"),
        employee.id,
        AdminUserPatch(active=False),
    )
    assert db_session.get(AuthSession, record.id) is None
    assert session_service.find_session(db_session, token, _settings()) is None


@pytest.mark.parametrize("touch", [False, True])
def test_find_session_rejects_exact_idle_and_absolute_expiry_without_global_pruning(
    db_session: Session, monkeypatch, touch: bool
) -> None:
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    initial = datetime(2026, 9, 13, tzinfo=UTC)
    monkeypatch.setattr(session_service, "now", lambda: initial)
    idle_token, idle = session_service.create_session(
        db_session,
        employee_id=employee.id,
        subject="employee",
        settings=_settings(idle=1, absolute=50),
    )
    absolute_token, absolute = session_service.create_session(
        db_session,
        employee_id=employee.id,
        subject="employee",
        settings=_settings(idle=50, absolute=1),
    )
    # Isolate the absolute deadline from the idle deadline, which creation
    # normally caps at absolute expiry. Either deadline alone must reject.
    absolute.idle_expires_at = initial + timedelta(seconds=50)
    live_token, live = session_service.create_session(
        db_session,
        employee_id=employee.id,
        subject="employee",
        settings=_settings(idle=30, absolute=60),
    )
    expired_attempt = LoginAttempt(
        state=session_service.new_secret(),
        pkce_verifier="test-verifier",
        nonce="test-nonce",
        return_to="/month",
        browser_binding_hash=token_hash("test-binding"),
        expires_at=initial + timedelta(seconds=1),
    )
    live_attempt = LoginAttempt(
        state=session_service.new_secret(),
        pkce_verifier="test-verifier",
        nonce="test-nonce",
        return_to="/month",
        browser_binding_hash=token_hash("test-binding"),
        expires_at=initial + timedelta(seconds=30),
    )
    db_session.add_all([expired_attempt, live_attempt])
    db_session.flush()
    monkeypatch.setattr(session_service, "now", lambda: initial + timedelta(seconds=1))
    assert (
        session_service.find_session(
            db_session, idle_token, _settings(idle=1, absolute=50), touch=touch
        )
        is None
    )
    assert (
        session_service.find_session(
            db_session, absolute_token, _settings(idle=50, absolute=1), touch=touch
        )
        is None
    )
    assert session_service.find_session(db_session, live_token, _settings(), touch=touch) is live
    # Expired rows unrelated to the valid lookup, including login attempts,
    # remain until an explicit cleanup boundary. They never authenticate.
    assert db_session.get(AuthSession, idle.id) is idle
    assert db_session.get(AuthSession, absolute.id) is absolute
    assert db_session.get(LoginAttempt, expired_attempt.state) is expired_attempt
    session_service.prune_expired(db_session)
    assert db_session.get(AuthSession, idle.id) is None
    assert db_session.get(AuthSession, absolute.id) is None
    assert db_session.get(LoginAttempt, expired_attempt.state) is None
    assert db_session.get(AuthSession, live.id) is live
    assert db_session.get(LoginAttempt, live_attempt.state) is live_attempt


@pytest.mark.parametrize("touch", [False, True])
@pytest.mark.parametrize("valid_token", [False, True])
def test_ordinary_session_lookup_never_runs_global_delete(
    db_session: Session, monkeypatch, touch: bool, valid_token: bool
) -> None:
    """DP-AUTH-002: read-only CSRF validation performs no database writes."""
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    initial = datetime(2026, 9, 13, tzinfo=UTC)
    monkeypatch.setattr(session_service, "now", lambda: initial)
    token, _ = session_service.create_session(
        db_session, employee_id=employee.id, subject="employee", settings=_settings()
    )
    db_session.expire_all()
    monkeypatch.setattr(session_service, "now", lambda: initial + timedelta(seconds=1))
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement.lstrip().split(maxsplit=1)[0].upper())

    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        found = session_service.find_session(
            db_session, token if valid_token else "unknown-token", _settings(), touch=touch
        )
        db_session.flush()
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert (found is not None) == valid_token
    assert "DELETE" not in statements
    assert statements.count("SELECT") == (2 if valid_token else 1)
    assert statements.count("UPDATE") == (1 if touch and valid_token else 0)
    assert "INSERT" not in statements


def test_session_touch_is_rolled_back_with_its_transaction(
    db_session: Session, monkeypatch
) -> None:
    """Sliding expiry remains part of the request transaction, not an early commit."""
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    initial = datetime(2026, 9, 13, tzinfo=UTC)
    monkeypatch.setattr(session_service, "now", lambda: initial)
    token, record = session_service.create_session(
        db_session,
        employee_id=employee.id,
        subject="employee",
        settings=_settings(idle=10, absolute=60),
    )
    monkeypatch.setattr(session_service, "now", lambda: initial + timedelta(seconds=8))
    savepoint = db_session.begin_nested()
    found = session_service.find_session(db_session, token, _settings(idle=10, absolute=60))
    assert found is not None
    assert found is record
    db_session.flush()
    assert found.idle_expires_at == initial + timedelta(seconds=18)
    savepoint.rollback()
    db_session.refresh(record)
    assert record.last_seen_at == initial
    assert record.idle_expires_at == initial + timedelta(seconds=10)
    assert record.absolute_expires_at == initial + timedelta(seconds=60)


def test_key_rotation_rejects_retired_tokens_but_not_opaque_sessions(
    fake_idp: FakeIdP, monkeypatch
) -> None:
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
    old = fake_idp.mint()
    assert oidc.validate_production_token(old) == "employee"
    fake_idp.rotate()
    fake_idp.retire("key-1")
    # The new, unknown kid forces exactly the intended JWKS refresh; cached
    # old keys remain valid only until that normal cache boundary.
    assert oidc.validate_production_token(fake_idp.mint()) == "employee"
    with pytest.raises(oidc.InvalidOidcToken):
        oidc.validate_production_token(old)
    get_settings.cache_clear()


def test_production_headers_docs_and_500_envelope(monkeypatch, fake_idp: FakeIdP) -> None:
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
    with TestClient(app) as client:
        response = client.get("/health", headers={"X-Request-ID": "client-value"})
        assert response.headers["X-Request-ID"] != "client-value"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "camera=()" in response.headers["Permissions-Policy"]
        assert response.headers["Content-Security-Policy"] == (
            "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; object-src 'none'; "
            "script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
            "font-src 'self'"
        )
        assert all(
            client.get(path).status_code == 404 for path in ("/openapi.json", "/docs", "/redoc")
        )
        # The no-store policy is scoped to the API: a non-API response (and the
        # built assets) stays cacheable, so this control pins the scope. The
        # positive API-side assertion lives in test_oidc_flow.py, where the
        # client already owns its database.
        assert "Cache-Control" not in response.headers
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Login CSRF: the OIDC attempt is bound to the browser that started it
# ---------------------------------------------------------------------------


def _deletion_header(response) -> str | None:
    """The Set-Cookie header deleting the pre-auth binding cookie, if any."""
    for value in response.headers.get_list("set-cookie"):
        if value.startswith(f"{LOGIN_COOKIE_NAME}="):
            return value
    return None


def _assert_binding_cleared(response) -> None:
    """A terminal callback response deletes the pre-auth cookie properly.

    A ``__Host-`` deletion without Secure + Path=/ is silently ignored by a
    browser, so the attributes are part of the assertion, not decoration.
    """
    header = _deletion_header(response)
    assert header is not None, "the callback did not clear the pre-auth cookie"
    lowered = header.lower()
    assert "secure" in lowered
    assert "path=/" in lowered
    assert "max-age=0" in lowered
    assert "domain=" not in lowered


def _start_login(client: TestClient) -> tuple[str, LoginAttempt | None]:
    login = client.get("/api/v1/auth/login?return_to=/month", follow_redirects=False)
    assert login.status_code == 302
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    return state, None


def test_login_sets_a_pre_auth_binding_cookie_distinct_from_the_attempt(
    production_db_client_factory, db_session: Session
) -> None:
    client = production_db_client_factory()
    state, _ = _start_login(client)
    attempt = db_session.scalar(select(LoginAttempt).where(LoginAttempt.state == state))
    assert attempt is not None
    binding = client.cookies.get(LOGIN_COOKIE_NAME)
    assert binding
    # The stored value is the hash, never the cookie value itself.
    assert attempt.browser_binding_hash == token_hash(binding)
    assert attempt.browser_binding_hash != binding
    # It is not a credential for anything else.
    assert binding not in {attempt.state, attempt.pkce_verifier, attempt.nonce}


def test_callback_without_the_binding_cookie_is_rejected_and_cleared(
    production_db_client_factory, db_session: Session, fake_idp: FakeIdP
) -> None:
    """A callback delivered to a browser that never started the login fails."""
    initiating = production_db_client_factory()
    state, _ = _start_login(initiating)
    attempt = db_session.scalar(select(LoginAttempt).where(LoginAttempt.state == state))
    assert attempt is not None
    code = fake_idp.issue_code(verifier=attempt.pkce_verifier, nonce=attempt.nonce)

    other_browser = production_db_client_factory()
    callback = other_browser.get(
        f"/api/v1/auth/callback?code={code}&state={state}", follow_redirects=False
    )
    assert callback.status_code == 401
    assert callback.json()["detail"]["code"] == "AUTH_INVALID_CALLBACK"
    assert other_browser.cookies.get(COOKIE_NAME) is None
    _assert_binding_cleared(callback)
    # The legitimate attempt is preserved: a wrong browser must not burn it.
    assert db_session.scalar(select(LoginAttempt).where(LoginAttempt.state == state)) is not None


def test_callback_with_a_mismatched_binding_is_rejected_and_cleared(
    production_db_client_factory, db_session: Session, fake_idp: FakeIdP
) -> None:
    initiating = production_db_client_factory()
    state, _ = _start_login(initiating)
    attempt = db_session.scalar(select(LoginAttempt).where(LoginAttempt.state == state))
    assert attempt is not None
    code = fake_idp.issue_code(verifier=attempt.pkce_verifier, nonce=attempt.nonce)

    attacker = production_db_client_factory()
    attacker.cookies.set(LOGIN_COOKIE_NAME, "not-the-right-binding")
    callback = attacker.get(
        f"/api/v1/auth/callback?code={code}&state={state}", follow_redirects=False
    )
    assert callback.status_code == 401
    assert attacker.cookies.get(COOKIE_NAME) is None
    _assert_binding_cleared(callback)
    assert db_session.scalar(select(LoginAttempt).where(LoginAttempt.state == state)) is not None


def test_successful_callback_clears_the_binding_and_sets_the_session(
    production_db_client_factory, db_session: Session, fake_idp: FakeIdP
) -> None:
    client = production_db_client_factory()
    state, _ = _start_login(client)
    attempt = db_session.scalar(select(LoginAttempt).where(LoginAttempt.state == state))
    assert attempt is not None
    code = fake_idp.issue_code(verifier=attempt.pkce_verifier, nonce=attempt.nonce)
    callback = client.get(
        f"/api/v1/auth/callback?code={code}&state={state}", follow_redirects=False
    )
    assert callback.status_code == 303
    set_cookies = callback.headers.get_list("set-cookie")
    # Two Set-Cookie headers: the session is set, the binding is deleted.
    assert any(value.startswith(f"{COOKIE_NAME}=") for value in set_cookies)
    _assert_binding_cleared(callback)
    assert client.cookies.get(LOGIN_COOKIE_NAME) is None


@pytest.mark.parametrize("scenario", ["unknown_state", "expired_state", "inactive_employee"])
def test_every_terminal_callback_path_clears_the_binding(
    production_db_client_factory, db_session: Session, fake_idp: FakeIdP, scenario: str
) -> None:
    """Each failure path returns a Response that clears the binding cookie."""
    client = production_db_client_factory()
    binding = "terminal-path-binding"
    state = f"terminal-{scenario}"
    if scenario == "inactive_employee":
        employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
        assert employee is not None
        employee.active = False
        db_session.flush()
        db_session.add(
            LoginAttempt(
                state=state,
                pkce_verifier="verifier",
                nonce="nonce",
                return_to="/",
                browser_binding_hash=token_hash(binding),
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        db_session.flush()
        code = fake_idp.issue_code(subject="employee", verifier="verifier", nonce="nonce")
        expected = 403
    elif scenario == "expired_state":
        db_session.add(
            LoginAttempt(
                state=state,
                pkce_verifier="verifier",
                nonce="nonce",
                return_to="/",
                browser_binding_hash=token_hash(binding),
                expires_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
        db_session.flush()
        code = "irrelevant"
        expected = 401
    else:
        code = "irrelevant"
        expected = 401
    client.cookies.set(LOGIN_COOKIE_NAME, binding)
    response = client.get(
        f"/api/v1/auth/callback?code={code}&state={state}", follow_redirects=False
    )
    assert response.status_code == expected
    _assert_binding_cleared(response)
    assert client.cookies.get(COOKIE_NAME) is None


def test_discovery_failure_at_the_callback_reports_unavailability(
    production_db_client_factory, db_session: Session, fake_idp: FakeIdP, monkeypatch
) -> None:
    """A recoverable IdP outage is 503, not "your login link is invalid".

    Discovery is deliberately separated from the code exchange: an outage is a
    server-side condition a retry fixes, while a bad code is a terminal client
    error. Collapsing them tells the employee to start over when waiting would
    have worked.
    """
    client = production_db_client_factory()
    state, _ = _start_login(client)
    attempt = db_session.scalar(select(LoginAttempt).where(LoginAttempt.state == state))
    assert attempt is not None
    code = fake_idp.issue_code(verifier=attempt.pkce_verifier, nonce=attempt.nonce)

    def unreachable(_settings: Settings) -> dict[str, object]:
        raise OSError("discovery endpoint unreachable")

    monkeypatch.setattr("app.auth.routes.discovery", unreachable)
    callback = client.get(
        f"/api/v1/auth/callback?code={code}&state={state}", follow_redirects=False
    )
    assert callback.status_code == 503
    assert callback.json()["detail"]["code"] == "AUTH_UNAVAILABLE"
    assert client.cookies.get(COOKIE_NAME) is None
    _assert_binding_cleared(callback)


def test_binding_cookie_is_not_accepted_for_a_different_attempt(
    production_db_client_factory, db_session: Session, fake_idp: FakeIdP
) -> None:
    """Two concurrent logins keep separate bindings: one cannot complete the other."""
    first = production_db_client_factory()
    first_state, _ = _start_login(first)
    first_attempt = db_session.scalar(select(LoginAttempt).where(LoginAttempt.state == first_state))
    assert first_attempt is not None
    first_binding = first.cookies.get(LOGIN_COOKIE_NAME)

    second = production_db_client_factory()
    second_state, _ = _start_login(second)
    second_attempt = db_session.scalar(
        select(LoginAttempt).where(LoginAttempt.state == second_state)
    )
    assert second_attempt is not None
    assert second.cookies.get(LOGIN_COOKIE_NAME) != first_binding

    code = fake_idp.issue_code(verifier=first_attempt.pkce_verifier, nonce=first_attempt.nonce)
    crossed = second.get(
        f"/api/v1/auth/callback?code={code}&state={first_state}", follow_redirects=False
    )
    assert crossed.status_code == 401
    assert second.cookies.get(COOKIE_NAME) is None
