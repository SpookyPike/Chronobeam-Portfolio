"""Response-durability ordering guards (EINV-11, EINV-11a).

``get_db`` is a route-level dependency, so FastAPI closes it on the *inner*
dependency stack — which exits only **after** the response has been sent. A
write whose effect a *later* request is expected to observe can therefore be
observed stale, and for a revocation that is a security failure, not a cosmetic
one: an operator told "deactivated" while the target's cookie still
authenticates, a role revoked while the old capability still resolves, a
withdrawal acknowledged while the recipient can still read the package.

``EINV-11a`` closes that at the framework level: ``create_app`` declares
``commit_before_response`` app-level and ``scope="function"``, so the request's
transaction commits **before the first response byte** for every route, present
and future, and a commit failure is answered as a failure rather than as
success.

**The direct-call tests below do not measure timing.** An in-process
``TestClient`` completes the dependency teardown before the caller regains
control, so it cannot observe the race at all. Instead each test calls the ROUTE
FUNCTION DIRECTLY with a spy session that records the order of ``commit()``
against the cookie/response events. Called directly there is no teardown, so a
commit seen by the time the function returns can only be the route's own — which
is exactly the property under test. Remove a route's ``session.commit()`` and
the assertion fails; no sleep, no retry, no server needed.

**The two EINV-11a guards at the end of this file are different**: they run the
REAL app (no ``get_db`` override) and observe the database from an independent
connection at ``http.response.start``, which is the instant the first byte
leaves. That is the only shape that can see framework ordering, and it is what
makes the guarantee safe against a FastAPI upgrade moving the boundary.
"""

import os
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import pytest
from app.admin import routes as admin_routes
from app.admin.schemas import AdminUserPatch
from app.auth import routes as auth_routes
from app.auth.models import RoleAssignment
from app.auth.principal import Capability, Principal
from app.auth.roles import Role
from app.auth.session_models import AuthSession, LoginAttempt
from app.auth.session_service import COOKIE_NAME, LOGIN_COOKIE_NAME, token_hash
from app.config import get_settings
from app.db import commit_before_response, get_engine
from app.disclosure import routes as disclosure_routes
from app.disclosure.models import SubmissionPackage, SubmissionPackageStatus
from app.enums import SubmissionPurpose
from app.identity.models import Employee
from app.integrations.shift_plan import ShiftAssignmentSeed
from app.main import create_app
from app.shift.service import seed_shift_assignment
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

#: The recorded event order for one direct route call.
Log = list[str]


class _SpySession:
    """A real ``Session`` that records every ``commit()`` into a shared log.

    Everything else is delegated, so the route and its service helpers behave
    exactly as in production.
    """

    def __init__(self, session: Session, log: Log) -> None:
        self._session = session
        self._log = log

    def commit(self) -> None:
        self._log.append("commit")
        self._session.commit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


@pytest.fixture()
def events(monkeypatch: pytest.MonkeyPatch) -> Log:
    """Record cookie mutation into the same ordered log as ``commit``."""
    log: Log = []
    real_set = Response.set_cookie
    real_delete = Response.delete_cookie

    def spy_set(self: Response, key: str, value: str = "", **kwargs: Any) -> None:
        log.append(f"set_cookie:{key}")
        real_set(self, key, value, **kwargs)

    def spy_delete(self: Response, key: str, **kwargs: Any) -> None:
        log.append(f"delete_cookie:{key}")
        real_delete(self, key, **kwargs)

    monkeypatch.setattr(Response, "set_cookie", spy_set)
    monkeypatch.setattr(Response, "delete_cookie", spy_delete)
    return log


def _request_with_cookie(name: str, value: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/auth/callback",
            "query_string": b"",
            "scheme": "https",
            "server": ("testserver", 443),
            "headers": [(b"cookie", f"{name}={value}".encode())],
        }
    )


def _admin_principal() -> Principal:
    return Principal(
        subject="technical-admin",
        display_name="Technical Admin",
        capabilities=frozenset({Capability.ACCESS_ADMIN}),
    )


def _employee(session: Session, subject: str = "employee") -> Employee:
    employee = session.scalar(select(Employee).where(Employee.idp_subject == subject))
    assert employee is not None, subject
    return employee


def _session_row(session: Session, employee: Employee, token: str) -> None:
    now = datetime.now(UTC)
    session.add(
        AuthSession(
            employee_id=employee.id,
            subject=employee.idp_subject or str(employee.id),
            token_hash=token_hash(token),
            csrf_token=f"csrf-{token}",
            last_seen_at=now,
            idle_expires_at=now + timedelta(hours=1),
            absolute_expires_at=now + timedelta(hours=2),
        )
    )
    session.flush()


def _at(log: Log, *names: str) -> int:
    """Index of the first logged event whose name is in ``names``."""
    for index, entry in enumerate(log):
        if entry in names:
            return index
    raise AssertionError(f"none of {names} in {log}")


# ---------------------------------------------------------------------------
# Auth cookies: the cookie must not outrun the row it names
# ---------------------------------------------------------------------------


def test_login_commits_the_attempt_before_the_binding_cookie(
    production_db_client_factory, db_session: Session, events: Log
) -> None:
    """The callback looks the attempt up by ``state``; it must be durable."""
    production_db_client_factory()  # installs complete production settings
    session = _SpySession(db_session, events)
    response = auth_routes.login(cast(Session, session), return_to="/month")  # type: ignore[arg-type]
    assert response.status_code == 302
    assert _at(events, "commit") < _at(events, f"set_cookie:{LOGIN_COOKIE_NAME}")


def test_callback_commits_the_session_after_creating_it_and_before_the_cookie(
    production_db_client_factory,
    db_session: Session,
    events: Log,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Order matters precisely: ``create_session`` -> ``commit`` -> cookie.

    The callback already commits earlier to consume the login attempt, so the
    assertion is anchored to the CREATION marker rather than to "a commit
    happened somewhere" — otherwise the earlier commit would satisfy it falsely.
    """
    production_db_client_factory()
    employee = _employee(db_session)
    now = datetime.now(UTC)
    db_session.add(
        LoginAttempt(
            state="durability-state",
            pkce_verifier="verifier",
            nonce="nonce",
            return_to="/month",
            browser_binding_hash=token_hash("binding"),
            expires_at=now + timedelta(minutes=10),
        )
    )
    db_session.flush()

    monkeypatch.setattr(auth_routes, "discovery", lambda settings: {"issuer": "x"})
    monkeypatch.setattr(auth_routes, "exchange_code", lambda *a, **k: {"id_token": "t"})
    monkeypatch.setattr(
        auth_routes, "validate_production_token", lambda *a, **k: employee.idp_subject
    )
    real_create = auth_routes.create_session

    def spy_create(*args: Any, **kwargs: Any) -> Any:
        events.append("create_session")
        return real_create(*args, **kwargs)

    monkeypatch.setattr(auth_routes, "create_session", spy_create)

    session = _SpySession(db_session, events)
    response = auth_routes.callback(
        _request_with_cookie(LOGIN_COOKIE_NAME, "binding"),
        code="code",
        state="durability-state",
        session=cast(Session, session),
    )
    assert response.status_code == 303
    created = _at(events, "create_session")
    cookie = _at(events, f"set_cookie:{COOKIE_NAME}")
    assert any(
        entry == "commit" and created < index < cookie for index, entry in enumerate(events)
    ), events


def test_logout_commits_the_revocation_before_clearing_the_cookie(
    production_db_client_factory, db_session: Session, events: Log
) -> None:
    """A cleared cookie must not precede the revocation it advertises."""
    production_db_client_factory()
    employee = _employee(db_session)
    _session_row(db_session, employee, "durability-opaque-token")
    session = _SpySession(db_session, events)
    auth_routes.logout(
        _request_with_cookie(COOKIE_NAME, "durability-opaque-token"),
        Response(),
        cast(Session, session),
    )
    assert _at(events, "commit") < _at(events, f"delete_cookie:{COOKIE_NAME}")


# ---------------------------------------------------------------------------
# Revocations: never tell the operator "done" while it is still pending
# ---------------------------------------------------------------------------


def test_deactivation_commits_the_session_revocation_before_returning(
    db_session: Session, events: Log
) -> None:
    """A deactivation deletes the target's opaque sessions."""
    target = _employee(db_session, "employee-b")
    _session_row(db_session, target, "employee-b-token")
    session = _SpySession(db_session, events)
    admin_routes.admin_update_user(  # type: ignore[arg-type]
        target.id, AdminUserPatch(active=False), _admin_principal(), cast(Session, session)
    )
    assert events.count("commit") >= 1, events
    assert (
        db_session.scalar(
            select(AuthSession).where(AuthSession.token_hash == token_hash("employee-b-token"))
        )
        is None
    )


def test_role_revocation_commits_before_returning(db_session: Session, events: Log) -> None:
    """The revoked ``revoked_at`` is what removes the capability."""
    target = _employee(db_session, "employee-b")
    db_session.add(
        RoleAssignment(
            employee_id=target.id,
            role=Role.SUBMISSION_RECIPIENT.value,
            granted_by_principal="test",
        )
    )
    db_session.flush()
    session = _SpySession(db_session, events)
    admin_routes.admin_revoke_role(  # type: ignore[arg-type]
        target.id, Role.SUBMISSION_RECIPIENT, _admin_principal(), cast(Session, session)
    )
    assert events.count("commit") >= 1, events
    db_session.expire_all()
    assignment = db_session.scalar(
        select(RoleAssignment).where(RoleAssignment.employee_id == target.id)
    )
    assert assignment is not None and assignment.revoked_at is not None


def test_submission_withdrawal_commits_before_returning(
    db_session: Session, events: Log, production_db_client_factory
) -> None:
    """The recipient's read is gated on ``status == SUBMITTED``."""
    production_db_client_factory()
    employee = _employee(db_session)
    seed_shift_assignment(
        db_session,
        employee,
        ShiftAssignmentSeed(
            plan_key="durability-shift",
            plan_name="Durability test shift",
            planned_start_at=datetime.fromisoformat("2026-07-31T19:00:00+02:00"),
            planned_end_at=datetime.fromisoformat("2026-08-01T07:00:00+02:00"),
            shift_kind="NIGHT",
            source="TEST_SEED",
            source_ref="response-durability-test",
        ),
    )
    package = SubmissionPackage(
        employee_id=employee.id,
        purpose=SubmissionPurpose.SHIFT_REPORT,
        period_start=date(2026, 7, 31),
        period_end=date(2026, 8, 1),
        version=1,
        payload_json={"durability": True},
        employee_confirmed_at=datetime.now(UTC),
        client_request_id=uuid.uuid4(),
        status=SubmissionPackageStatus.SUBMITTED,
    )
    db_session.add(package)
    db_session.flush()
    session = _SpySession(db_session, events)
    out = disclosure_routes.me_withdraw_submission(  # type: ignore[arg-type]
        package.id,
        Principal(subject="employee", display_name="Employee"),
        employee,
        cast(Session, session),
    )
    assert out.id == package.id
    assert events.count("commit") >= 1, events
    db_session.expire_all()
    reloaded = db_session.scalar(
        select(SubmissionPackage).where(SubmissionPackage.id == package.id)
    )
    assert reloaded is not None
    assert reloaded.status == SubmissionPackageStatus.WITHDRAWN


def test_the_authorization_routes_commit_before_returning(db_session: Session, events: Log) -> None:
    """The F6 overtime-authorization writes are durable before the response.

    The response acknowledges a policy-relevant transition — a request now
    exists — so a later request must not be able to observe the pre-state.
    Removing the route's ``session.commit()`` fails this: a DIRECT call has no
    dependency teardown to hide behind.
    """
    from app.ocd_overtime import routes as ocd_routes
    from app.ocd_overtime.models import OvertimeAuthorization, OvertimeAuthStatus
    from app.ocd_overtime.schemas import OvertimeAuthorizationIn

    employee = _employee(db_session)
    session = _SpySession(db_session, events)
    created = ocd_routes.add_auth(
        OvertimeAuthorizationIn(valid_from=datetime(2032, 3, 1)),
        employee,
        cast(Session, session),
    )
    assert events.count("commit") >= 1, events
    db_session.expire_all()
    row = db_session.get(OvertimeAuthorization, created.id)
    assert row is not None
    assert row.status is OvertimeAuthStatus.REQUESTED
    assert row.source == "EMPLOYEE_RECORDED"


def test_a_pending_cancellation_is_durable_before_returning(
    db_session: Session, events: Log
) -> None:
    """The committed head is ``CANCELLED`` when the caller regains control."""
    from app.ocd_overtime import routes as ocd_routes
    from app.ocd_overtime.models import OvertimeAuthorization, OvertimeAuthStatus
    from app.ocd_overtime.schemas import OvertimeAuthorizationIn, OvertimeAuthorizationPatch
    from app.ocd_overtime.service import create_authorization

    employee = _employee(db_session)
    created = create_authorization(
        db_session, employee, OvertimeAuthorizationIn(valid_from=datetime(2032, 3, 1))
    )
    db_session.flush()
    events.clear()
    session = _SpySession(db_session, events)
    patched = ocd_routes.edit_auth(
        created.id,
        OvertimeAuthorizationPatch(version=1, status=OvertimeAuthStatus.CANCELLED),
        employee,
        cast(Session, session),
    )
    assert events.count("commit") >= 1, events
    db_session.expire_all()
    row = db_session.get(OvertimeAuthorization, patched.id)
    assert row is not None
    assert row.status is OvertimeAuthStatus.CANCELLED


# ---------------------------------------------------------------------------
# EINV-11a — the production guarantee, observed at the first response byte
#
# The direct-call tests above pin the ROUTE-level ordering (a cookie set before
# the response is built). They cannot observe framework ordering at all, and
# they say nothing about the 28 mutating routes that never commit explicitly.
# The two guards below cover that: one behavioural, one structural.
# ---------------------------------------------------------------------------


class _FirstByteProbe:
    """ASGI wrapper that queries an INDEPENDENT connection at the first byte.

    ``http.response.start`` is emitted when the response begins leaving the
    application. With the app-level ``scope="function"`` dependency the
    request's transaction is already committed at that instant; with a
    route-level dependency (or none) it is not, because FastAPI's inner
    dependency stack exits only *after* the response has been sent.
    """

    def __init__(self, app: FastAPI, url: str, count_sql: str, params: dict[str, Any]) -> None:
        self.app = app
        self.url = url
        self.count_sql = count_sql
        self.params = params
        self.rows_at_first_byte: int | None = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        async def _send(message: Any) -> None:
            if message["type"] == "http.response.start" and self.rows_at_first_byte is None:
                engine = create_engine(self.url)
                try:
                    with engine.connect() as connection:
                        self.rows_at_first_byte = connection.execute(
                            text(self.count_sql), self.params
                        ).scalar_one()
                finally:
                    engine.dispose()
            await send(message)

        await self.app(scope, receive, _send)


def test_a_mutation_is_committed_before_the_first_response_byte(db_engine: Engine) -> None:
    """A 2xx must not be observable before the row it acknowledges is durable.

    Uses the REAL ``get_db`` (no override), so the production dependency stack
    is what is under test. The probe sees the database on a separate
    connection, so only committed rows are visible.
    """
    url = get_settings().test_database_url
    previous_env = os.environ.get("CHRONOBEAM_ENV")
    previous_url = os.environ.get("CHRONOBEAM_DATABASE_URL")
    day = date(2035, 3, 15)
    count_sql = "select count(*) from day_status_event where day = :day"
    count_params = {"day": day}
    engine = create_engine(url)
    # This test uses the REAL `get_db`, so everything it writes is COMMITTED into
    # the shared test database. The cleanup below must therefore remove the
    # `correction` tombstone the 204 route writes as well as the fact rows: an
    # unscoped `select(Correction)` assertion elsewhere in the suite would
    # otherwise count this row and fail for a reason it does not own.
    created_id: str | None = None
    os.environ["CHRONOBEAM_ENV"] = "test"
    os.environ["CHRONOBEAM_DATABASE_URL"] = url
    get_settings.cache_clear()
    get_engine.cache_clear()
    try:
        with engine.begin() as connection:
            connection.execute(text("delete from day_status_event where day = :day"), {"day": day})

        app = create_app()
        probe = _FirstByteProbe(app, url, count_sql, count_params)
        headers = {"X-Chronobeam-Dev-Principal": "employee"}
        with TestClient(probe, base_url="https://testserver") as client:
            created = client.post(
                "/api/v1/me/day-status",
                json={"day": day.isoformat(), "category": "VACATION", "portion": "FULL"},
                headers=headers,
            )
            assert created.status_code == 201, created.text
            created_id = str(created.json()["id"])
            assert probe.rows_at_first_byte == 1, (
                "the 201 was sent before the day-status row was committed"
            )

            # A 204 route must carry the same guarantee: the row is gone by the
            # time the empty response starts, not after it.
            probe.rows_at_first_byte = None
            deleted = client.delete(
                f"/api/v1/me/day-status/{created_id}",
                params={"version": 1},
                headers=headers,
            )
            assert deleted.status_code == 204, deleted.text
            assert probe.rows_at_first_byte == 0, "the 204 was sent before the delete was committed"
    finally:
        with engine.begin() as connection:
            connection.execute(text("delete from day_status_event where day = :day"), {"day": day})
            if created_id is not None:
                connection.execute(
                    text("delete from correction where entity_id = :entity_id"),
                    {"entity_id": created_id},
                )
        engine.dispose()
        for name, value in (
            ("CHRONOBEAM_ENV", previous_env),
            ("CHRONOBEAM_DATABASE_URL", previous_url),
        ):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        get_settings.cache_clear()
        get_engine.cache_clear()


def test_the_app_declares_the_pre_response_commit_dependency() -> None:
    """The guarantee is app-level, so it needs no route list — only a declaration.

    This fails if the dependency is deleted, if its scope changes, or if it is
    swapped for something else; the behavioural test above fails if it stops
    working. Together they cover both "removed" and "silently neutered".
    """
    declared = [
        dependency
        for dependency in create_app().router.dependencies
        if getattr(dependency, "scope", None) == "function"
        and dependency.dependency is commit_before_response
    ]
    assert declared, (
        "create_app() must declare commit_before_response as an app-level "
        "function-scoped dependency (EINV-11a), or mutating routes can "
        "acknowledge a write whose commit then fails"
    )
