"""Production OIDC/session regression matrix using the socket-free fake IdP."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from app.auth import oidc
from app.auth import routes as auth_routes
from app.auth.session_models import AuthSession, LoginAttempt
from app.auth.session_service import COOKIE_NAME, token_hash
from app.config import get_settings
from app.identity.models import Employee
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.oidc_fake import FakeIdP


def _callback(client: TestClient, db: Session, provider: FakeIdP, *, subject: str = "employee"):
    login = client.get("/api/v1/auth/login?return_to=/month", follow_redirects=False)
    assert login.status_code == 302
    query = parse_qs(urlparse(login.headers["location"]).query)
    state = query["state"][0]
    attempt = db.scalar(select(LoginAttempt).where(LoginAttempt.state == state))
    assert attempt is not None
    code = provider.issue_code(subject=subject, verifier=attempt.pkce_verifier, nonce=attempt.nonce)
    return client.get(f"/api/v1/auth/callback?code={code}&state={state}", follow_redirects=False)


def test_production_login_cookie_session_and_logout(
    production_db_client_factory, db_session: Session, fake_idp: FakeIdP
) -> None:
    client = production_db_client_factory()
    callback = _callback(client, db_session, fake_idp)
    assert callback.status_code == 303
    cookie = callback.headers["set-cookie"]
    assert "__Host-Chronobeam-Session=" in cookie
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" in cookie
    raw = client.cookies.get(COOKIE_NAME)
    assert raw
    record = db_session.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(raw)))
    assert record is not None and record.csrf_token
    assert raw not in record.token_hash
    me = client.get("/api/v1/me")
    assert me.status_code == 200 and me.json()["subject"] == "employee"
    # The middleware opens a separate transaction; make this fixture's setup
    # visible exactly as a normal request dependency would have committed it.
    db_session.commit()
    logout = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": record.csrf_token})
    assert logout.status_code == 204
    assert client.get("/api/v1/me").status_code == 401
    # This fixture deliberately commits only to let the middleware's separate
    # connection observe the session; persist the endpoint's delete too.
    db_session.commit()


def test_login_callback_is_one_time_and_rejects_bad_return_target(
    production_db_client_factory, db_session: Session, fake_idp: FakeIdP
) -> None:
    client = production_db_client_factory()
    for target in ("https://evil.example/x", "//evil.example/x", r"\evil"):
        assert client.get("/api/v1/auth/login", params={"return_to": target}).status_code == 422
    login = client.get("/api/v1/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    attempt = db_session.scalar(select(LoginAttempt).where(LoginAttempt.state == state))
    assert attempt is not None
    code = fake_idp.issue_code(verifier=attempt.pkce_verifier, nonce=attempt.nonce)
    assert client.get(f"/api/v1/auth/callback?code={code}&state={state}").status_code == 303
    assert client.get(f"/api/v1/auth/callback?code={code}&state={state}").status_code == 401
    assert client.get("/api/v1/auth/callback?code=x&state=unknown").status_code == 401


def test_unknown_and_inactive_subjects_create_no_session(
    production_db_client_factory, db_session: Session, fake_idp: FakeIdP
) -> None:
    client = production_db_client_factory()
    before = len(db_session.scalars(select(AuthSession)).all())
    assert _callback(client, db_session, fake_idp, subject="unknown").status_code == 403
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    employee.active = False
    assert _callback(client, db_session, fake_idp).status_code == 403
    assert len(db_session.scalars(select(AuthSession)).all()) == before


def test_production_tokens_reject_invalid_claims(
    fake_idp: FakeIdP, monkeypatch: pytest.MonkeyPatch
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
    cases = [
        fake_idp.mint(iss="https://wrong.test"),
        fake_idp.mint(aud="wrong"),
        fake_idp.mint(aud=fake_idp.audience, azp="wrong"),
        fake_idp.mint(aud=[fake_idp.audience, "other"], azp="wrong"),
        # multi-valued aud WITHOUT azp: the token does not say which party it
        # was issued to, so it must not authenticate here.
        fake_idp.mint(aud=[fake_idp.audience, "other"]),
        fake_idp.mint(exp=datetime.now(UTC) - timedelta(hours=1)),
        fake_idp.mint(iat=datetime.now(UTC) + timedelta(minutes=2)),
        fake_idp.mint(kid="unknown", signing_kid=fake_idp.active_kid),
    ]
    for token in cases:
        with pytest.raises(oidc.InvalidOidcToken):
            oidc.validate_production_token(token)
    # ... while a single-valued aud without azp stays acceptable: that is
    # exactly what the pilot IdP emits.
    assert oidc.validate_production_token(fake_idp.mint()) == "employee"
    assert (
        oidc.validate_production_token(fake_idp.mint(aud=fake_idp.audience, azp=fake_idp.client_id))
        == "employee"
    )
    get_settings.cache_clear()


def test_unknown_kid_refreshes_jwks_once_and_signature_fails_closed(
    fake_idp: FakeIdP, monkeypatch: pytest.MonkeyPatch
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
    good = fake_idp.mint()
    assert oidc.validate_production_token(good) == "employee"
    before = fake_idp.fetches["jwks"]
    unknown = fake_idp.mint(kid="not-published", signing_kid=fake_idp.active_kid)
    with pytest.raises(oidc.InvalidOidcToken):
        oidc.validate_production_token(unknown)
    assert fake_idp.fetches["jwks"] == before + 1
    fake_idp.add_key("other")
    wrong_signature = fake_idp.mint(kid=fake_idp.active_kid, signing_kid="other")
    with pytest.raises(oidc.InvalidOidcToken):
        oidc.validate_production_token(wrong_signature)
    get_settings.cache_clear()


def test_bearer_token_is_not_a_production_credential(
    production_db_client_factory, fake_idp: FakeIdP
) -> None:
    """The ID-token validator is no longer reachable from a request header.

    It authenticated without a nonce and bypassed the CSRF middleware by
    construction, and no machine consumer used it.
    """
    client = production_db_client_factory()
    response = client.get("/api/v1/me", headers={"Authorization": f"Bearer {fake_idp.mint()}"})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_REQUIRED"
    denied = client.get("/api/v1/me", headers={"X-Chronobeam-Dev-Principal": "employee"})
    assert denied.status_code == 401
    assert denied.json()["detail"]["code"] == "AUTH_REQUIRED"


def test_session_status_and_foreign_origin_csrf(
    production_db_client_factory, db_session: Session, fake_idp: FakeIdP
) -> None:
    client = production_db_client_factory()
    session_status = client.get("/api/v1/auth/session")
    assert session_status.json() == {
        "authenticated": False,
        "csrf_token": None,
        "oidc_mode": True,
    }
    # An API response can carry per-employee facts, so it must not be cached.
    assert session_status.headers["Cache-Control"] == "no-store"
    assert _callback(client, db_session, fake_idp).status_code == 303
    status = client.get("/api/v1/auth/session").json()
    assert status["authenticated"] and status["csrf_token"]
    denied = client.post(
        "/api/v1/auth/logout",
        headers={"Origin": "https://evil.example", "X-CSRF-Token": status["csrf_token"]},
    )
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "CSRF_REJECTED"
    # The denial short-circuits before the route, but it is still an API
    # response and must carry the same boundary headers as any other.
    assert denied.headers["Cache-Control"] == "no-store"
    assert denied.headers["X-Content-Type-Options"] == "nosniff"
    assert denied.headers["Referrer-Policy"] == "no-referrer"
    assert denied.headers["X-Request-ID"]


def test_callback_rejects_wrong_nonce(
    production_db_client_factory, db_session: Session, fake_idp: FakeIdP
) -> None:
    """The ID-token nonce must match the login attempt (replay/confusion)."""
    client = production_db_client_factory()
    # The auth middleware opens its OWN connection, so the OIDC tests commit and
    # their rows survive the per-test rollback in this long-lived test database.
    # Assert the DELTA this callback causes — never a globally empty table, which
    # would only hold on a pristine database (see
    # ``test_unknown_and_inactive_subjects_create_no_session`` for the idiom).
    before = len(db_session.scalars(select(AuthSession)).all())
    login = client.get("/api/v1/auth/login?return_to=/", follow_redirects=False)
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    attempt = db_session.scalar(select(LoginAttempt).where(LoginAttempt.state == state))
    assert attempt is not None
    code = fake_idp.issue_code(verifier=attempt.pkce_verifier, nonce="wrong-nonce")
    assert client.get(f"/api/v1/auth/callback?code={code}&state={state}").status_code == 401
    assert len(db_session.scalars(select(AuthSession)).all()) == before


def test_callback_rejects_expired_login_attempt(
    production_db_client_factory, db_session: Session, fake_idp: FakeIdP
) -> None:
    """Login attempts are short-lived: an expired attempt can never succeed."""
    client = production_db_client_factory()
    before = len(db_session.scalars(select(AuthSession)).all())
    login = client.get("/api/v1/auth/login?return_to=/", follow_redirects=False)
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    attempt = db_session.scalar(select(LoginAttempt).where(LoginAttempt.state == state))
    assert attempt is not None
    attempt.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    code = fake_idp.issue_code(verifier=attempt.pkce_verifier, nonce=attempt.nonce)
    assert client.get(f"/api/v1/auth/callback?code={code}&state={state}").status_code == 401
    assert len(db_session.scalars(select(AuthSession)).all()) == before


def test_callback_code_exchange_failure_is_one_time(
    production_db_client_factory, db_session: Session, fake_idp: FakeIdP
) -> None:
    """A failed exchange consumes the attempt: no retry with the same state."""
    client = production_db_client_factory()
    before = len(db_session.scalars(select(AuthSession)).all())
    login = client.get("/api/v1/auth/login?return_to=/", follow_redirects=False)
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    attempt = db_session.scalar(select(LoginAttempt).where(LoginAttempt.state == state))
    assert attempt is not None
    bad = fake_idp.issue_code(verifier="wrong-verifier", nonce=attempt.nonce)
    assert client.get(f"/api/v1/auth/callback?code={bad}&state={state}").status_code == 401
    good = fake_idp.issue_code(verifier=attempt.pkce_verifier, nonce=attempt.nonce)
    assert client.get(f"/api/v1/auth/callback?code={good}&state={state}").status_code == 401
    assert len(db_session.scalars(select(AuthSession)).all()) == before


def test_callback_failure_consumes_attempt_across_request_rollback(
    db_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The callback state remains consumed when the request dependency rolls back.

    This uses an independent real transaction rather than the shared test
    dependency override, which intentionally does not emulate ``get_db``'s
    commit/rollback lifecycle.
    """
    from types import SimpleNamespace

    from app.auth.session_service import LOGIN_COOKIE_NAME
    from starlette.requests import Request

    state = "rollback-proof-state"
    binding = "rollback-proof-binding"
    with Session(db_engine) as setup:
        setup.add(
            LoginAttempt(
                state=state,
                pkce_verifier="verifier",
                nonce="nonce",
                return_to="/",
                browser_binding_hash=token_hash(binding),
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        setup.commit()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: SimpleNamespace(is_production=True))
    monkeypatch.setattr(auth_routes, "discovery", lambda _: {})

    def failed_exchange(*_args, **_kwargs):
        raise oidc.InvalidOidcToken("synthetic exchange failure")

    monkeypatch.setattr(auth_routes, "exchange_code", failed_exchange)
    request = Request(
        {"type": "http", "headers": [(b"cookie", f"{LOGIN_COOKIE_NAME}={binding}".encode())]}
    )
    try:
        with Session(db_engine) as request_session:
            response = auth_routes.callback(
                request=request, code="bad", state=state, session=request_session
            )
            assert response.status_code == 401
            # Simulate ``get_db``'s error path after the endpoint returns.
            request_session.rollback()
        with Session(db_engine) as verify_session:
            assert verify_session.get(LoginAttempt, state) is None
    finally:
        with Session(db_engine) as cleanup:
            record = cleanup.get(LoginAttempt, state)
            if record is not None:
                cleanup.delete(record)
                cleanup.commit()


def test_deactivated_employee_existing_session_denied_immediately(
    production_db_client_factory, db_session: Session, fake_idp: FakeIdP
) -> None:
    """Deactivation takes effect on the next request: sessions cache no authority.

    The session row itself is dropped (not merely denied downstream), so the
    next request is unauthenticated — and ``/auth/session`` never reports
    ``authenticated: true`` for a deactivated employee.
    """
    client = production_db_client_factory()
    assert _callback(client, db_session, fake_idp).status_code == 303
    assert client.get("/api/v1/me/months/2026-07").status_code == 200
    raw = client.cookies.get(COOKIE_NAME)
    assert raw
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    employee.active = False
    db_session.flush()
    assert client.get("/api/v1/auth/session").json()["authenticated"] is False
    # The row that was PRESENTED is deleted, not merely denied downstream
    # (``session_service.find_session`` deletes the record it looked up when the
    # employee is inactive or remapped). Scoped to this session's token hash: the
    # lookup deletes that one record, it does not bulk-delete the employee's
    # sessions, and the long-lived test database retains other tests' committed
    # rows — so neither "the table is empty" nor "the employee has no sessions"
    # is the claim being tested.
    assert (
        db_session.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(raw)))
        is None
    )
    denied = client.get("/api/v1/me/months/2026-07")
    assert denied.status_code == 401


def test_identity_subject_unique_and_maps_to_single_employee(
    production_db_client_factory, db_session: Session, fake_idp: FakeIdP
) -> None:
    """Two employees can never share one idp_subject: collision is impossible."""
    from sqlalchemy.exc import IntegrityError

    duplicate = Employee(
        external_employee_id="dup-0001", legal_name="Duplicate", idp_subject="employee"
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()
    rows = db_session.scalars(select(Employee).where(Employee.idp_subject == "employee")).all()
    assert len(rows) == 1
    # a session for that subject resolves to the one canonical employee
    client = production_db_client_factory()
    assert _callback(client, db_session, fake_idp).status_code == 303
    assert client.get("/api/v1/me").json()["subject"] == "employee"


def test_opaque_session_cannot_follow_identity_subject_remap(
    production_db_client_factory, db_session: Session, fake_idp: FakeIdP
) -> None:
    """A browser session remains tied to its original employee row, not just sub."""
    client = production_db_client_factory()
    assert _callback(client, db_session, fake_idp).status_code == 303
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    other = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee-b"))
    assert employee is not None and other is not None
    employee.idp_subject = None
    db_session.flush()
    other.idp_subject = "employee"
    db_session.flush()

    assert client.get("/api/v1/me").status_code == 401
    assert client.get("/api/v1/auth/session").json()["authenticated"] is False


def test_csrf_enforced_for_every_unsafe_verb_and_allows_real_writes(
    production_db_client_factory, db_session: Session, fake_idp: FakeIdP
) -> None:
    """Synchronizer-token CSRF: every unsafe verb is denied without the token;
    a real ledger write succeeds with it (middleware reads sessions on its own
    connection, so the authenticated session is committed first)."""
    client = production_db_client_factory()
    assert _callback(client, db_session, fake_idp).status_code == 303
    db_session.commit()
    csrf = client.get("/api/v1/auth/session").json()["csrf_token"]
    assert csrf
    payload = {
        "start_local": "2026-07-06T08:00:00",
        "end_local": "2026-07-06T10:00:00",
        "work_type": "ON_SITE",
    }
    for verb in ("POST", "PUT", "PATCH", "DELETE"):
        denied = client.request(
            verb, "/api/v1/me/work-intervals", json=payload, headers={"X-CSRF-Token": "wrong"}
        )
        assert denied.status_code == 403, verb
        assert denied.json()["detail"]["code"] == "CSRF_REJECTED"
    created = client.post("/api/v1/me/work-intervals", json=payload, headers={"X-CSRF-Token": csrf})
    assert created.status_code == 201
    interval_id = created.json()["id"]
    patched = client.patch(
        f"/api/v1/me/work-intervals/{interval_id}",
        json={"version": 1, "start_local": "2026-07-06T08:15:00", "reason": "later start"},
        headers={"X-CSRF-Token": csrf},
    )
    assert patched.status_code == 200
    deleted = client.delete(
        f"/api/v1/me/work-intervals/{patched.json()['id']}?version={patched.json()['version']}",
        headers={"X-CSRF-Token": csrf},
    )
    assert deleted.status_code == 204
