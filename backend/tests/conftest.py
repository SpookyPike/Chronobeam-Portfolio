"""Shared pytest fixtures.

Two fixture families:

- DB-free ``client`` / ``production_client``: the dev-seam
  TestClient used by unit tests (health, config, seam, walltime, oidc,
  logging). No database is touched (the engine is lazy).
- DB-backed ``db_engine`` / ``db_session`` / ``db_client``: run against the
  TEST database (chronobeam_test) on the local/CI PostgreSQL. The schema is
  applied with the canonical migration (alembic upgrade head), synthetic
  employees are seeded, and every test runs in its own transaction that is
  rolled back afterwards (tests never persist rows). When the database is
  unreachable the suite SKIPS with a clear reason — unless
  CHRONOBEAM_REQUIRE_DB=1 (CI), where it FAILS instead (docs/TESTING.md).
"""

import os
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

# F8.1 — pin the process timezone for the whole suite. Berlin is the product's
# business timezone (docs/ARCHITECTURE.md) while CI runs UTC, so without a pin a
# host-timezone-dependent assertion is green on one machine and red on another —
# and a regression that silently read the HOST zone would hide on a Berlin dev
# box. The suite therefore deliberately runs under a NON-Berlin zone.
os.environ["TZ"] = "UTC"
if hasattr(time, "tzset"):  # POSIX; the app's runtime targets are Linux
    time.tzset()

# The module-level ASGI application is built at import time.  The suite's
# default is the documented test seam; individual production fixtures set a
# complete fake-IdP configuration before explicitly calling ``create_app``.
os.environ.setdefault("CHRONOBEAM_ENV", "test")
from alembic import command
from alembic.config import Config
from app.auth.deps import get_db
from app.config import get_settings
from app.db import create_engine, ensure_database, get_engine, get_session_factory
from app.dev_seed import seed_dev
from app.identity.models import Employee
from app.main import create_app
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from tests.oidc_fake import FakeIdP

_BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_REQUIRED = os.environ.get("CHRONOBEAM_REQUIRE_DB") == "1"


def _reset_settings_caches() -> None:
    """Clear the settings cache AND every cache derived from it.

    ``get_engine`` is ``lru_cache``d on ``get_settings().database_url``
    (``app/db.py``), and the app lifespan builds one at startup
    (``main.py``). A fixture that changes ``CHRONOBEAM_DATABASE_URL`` and clears
    only the settings cache therefore leaves an engine still bound to the
    PREVIOUS url — so ``db_engine`` could seed the wrong database whenever a
    "DB-free" production client had run first. Both caches must move together.
    """
    get_settings.cache_clear()
    get_engine.cache_clear()


def _require_db_or_skip(message: str) -> None:
    if DB_REQUIRED:
        raise RuntimeError(f"database required but unavailable: {message}")
    pytest.skip(f"PostgreSQL not reachable ({message}); run 'just db-up' for DB-backed tests")


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """DB-free TestClient in test environment (dev-auth seam active)."""
    monkeypatch.setenv("CHRONOBEAM_ENV", "test")
    _reset_settings_caches()
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
    _reset_settings_caches()


@pytest.fixture()
def production_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """DB-free TestClient in production configuration with fake IdP metadata."""
    monkeypatch.setenv("CHRONOBEAM_ENV", "production")
    monkeypatch.setenv("CHRONOBEAM_PUBLIC_BASE_URL", "https://chronobeam.test")
    monkeypatch.setenv(
        "CHRONOBEAM_OIDC_DISCOVERY_URL",
        FakeIdP.discovery_url,
    )
    monkeypatch.setenv("CHRONOBEAM_OIDC_ISSUER", FakeIdP.issuer)
    monkeypatch.setenv("CHRONOBEAM_OIDC_CLIENT_ID", "chronobeam")
    monkeypatch.setenv("CHRONOBEAM_OIDC_AUDIENCE", "chronobeam")
    monkeypatch.setenv(
        "CHRONOBEAM_DATABASE_URL",
        "postgresql+psycopg://chronobeam:pilot_test@127.0.0.1:5433/chronobeam",
    )
    _reset_settings_caches()
    app = create_app()
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client
    _reset_settings_caches()


@pytest.fixture()
def fake_idp(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeIdP]:
    """The sole OIDC network seam: ``build_opener`` in the OIDC module."""
    from app.auth import oidc

    provider = FakeIdP()
    monkeypatch.setattr(oidc, "build_opener", lambda *_handlers: provider)
    oidc._jwks_cache.clear()
    yield provider
    oidc._jwks_cache.clear()


@pytest.fixture()
def production_db_client_factory(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, fake_idp: FakeIdP
) -> Iterator[Callable[..., TestClient]]:
    """Build a DB-backed HTTPS production client after settings are complete.

    The settings cache is cleared on teardown like every other client fixture
    here. Without it the production settings set by ``make`` stay cached past the
    test, and ``get_settings().is_production`` leaks into whichever module runs
    next — which silently changes behaviour that reads settings (e.g. the
    recipient registry falls back to the fail-closed production branch).
    """

    def make(**settings: str) -> TestClient:
        monkeypatch.setenv("CHRONOBEAM_ENV", "production")
        monkeypatch.setenv("CHRONOBEAM_PUBLIC_BASE_URL", "https://chronobeam.test")
        monkeypatch.setenv("CHRONOBEAM_OIDC_DISCOVERY_URL", FakeIdP.discovery_url)
        monkeypatch.setenv("CHRONOBEAM_OIDC_ISSUER", FakeIdP.issuer)
        monkeypatch.setenv("CHRONOBEAM_OIDC_CLIENT_ID", FakeIdP.client_id)
        monkeypatch.setenv("CHRONOBEAM_OIDC_AUDIENCE", FakeIdP.audience)
        monkeypatch.setenv(
            "CHRONOBEAM_DATABASE_URL",
            "postgresql+psycopg://chronobeam:pilot_test@127.0.0.1:5433/chronobeam",
        )
        for key, value in settings.items():
            monkeypatch.setenv(key, value)
        _reset_settings_caches()
        app = create_app()
        app.dependency_overrides[get_db] = lambda: db_session
        return TestClient(app, base_url="https://testserver", follow_redirects=False)

    yield make
    _reset_settings_caches()


# ---------------------------------------------------------------------------
# DB-backed fixtures (test database chronobeam_test)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def db_url() -> str:
    return get_settings().test_database_url


@pytest.fixture(scope="session")
def db_engine(db_url: str) -> Iterator[Engine]:
    """Session-scoped engine with migrated schema + synthetic seed.

    Creates the test database on demand and applies the canonical migration.
    """
    previous_url = os.environ.get("CHRONOBEAM_DATABASE_URL")
    try:
        ensure_database(db_url)
        os.environ["CHRONOBEAM_DATABASE_URL"] = db_url
        _reset_settings_caches()
        cfg = Config()
        cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
        cfg.set_main_option("sqlalchemy.url", db_url)
        command.upgrade(cfg, "head")
        engine = create_engine(db_url, pool_pre_ping=True)
        with engine.connect():
            pass  # verify reachability
    except OperationalError as exc:
        _require_db_or_skip(str(exc).splitlines()[0] if str(exc) else repr(exc))
        raise AssertionError("unreachable") from None  # pragma: no cover - skip raises above
    with get_session_factory()() as session:
        seed_dev(session)  # synthetic employees (idempotent)
    yield engine
    engine.dispose()
    if previous_url is None:
        os.environ.pop("CHRONOBEAM_DATABASE_URL", None)
    else:
        os.environ["CHRONOBEAM_DATABASE_URL"] = previous_url
    _reset_settings_caches()


@pytest.fixture()
def db_session(db_engine: Engine) -> Iterator[Session]:
    """Per-test transaction (rolled back after the test)."""
    connection = db_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    yield session
    session.close()
    if transaction.is_active:  # a failed flush may already have rolled back
        transaction.rollback()
    connection.close()


@pytest.fixture()
def db_client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """TestClient (dev seam) whose get_db dependency serves the test session.

    A plain callable override (not a generator): the transaction lives in the
    fixture; nothing is committed, everything is rolled back.
    """
    monkeypatch.setenv("CHRONOBEAM_ENV", "test")
    _reset_settings_caches()
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    _reset_settings_caches()


@pytest.fixture()
def entry_client_factory(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., TestClient]]:
    """Build a dev-seam client with each pilot plan-entry switch set explicitly.

    ``effective_capabilities`` reads the settings, and the settings are CACHED when
    ``create_app()`` builds the app — so the environment must be set BEFORE the app
    is created. Setting it inside a test body would be too late, and leaking a
    cached Settings object into the next test is exactly the trap ``db_client``
    warns about, so the caches are cleared on teardown either way.
    """

    created: list[TestClient] = []

    def make(*, shift: bool = True, ocd: bool = True) -> TestClient:
        monkeypatch.setenv("CHRONOBEAM_ENV", "test")
        monkeypatch.setenv("CHRONOBEAM_ENABLE_SHIFT_PLAN_ENTRY", "true" if shift else "false")
        monkeypatch.setenv("CHRONOBEAM_ENABLE_OCD_PLAN_ENTRY", "true" if ocd else "false")
        _reset_settings_caches()
        app = create_app()
        app.dependency_overrides[get_db] = lambda: db_session
        client = TestClient(app)
        created.append(client)
        return client

    yield make
    for client in created:
        client.close()
    _reset_settings_caches()


@pytest.fixture()
def entry_client(entry_client_factory: Callable[..., TestClient]) -> TestClient:
    """Both pilot plan-entry switches ON — the demo/pilot posture."""
    return entry_client_factory(shift=True, ocd=True)


DEVELOPER_PRINCIPAL_HEADER = {"X-Chronobeam-Dev-Principal": "employee"}
OTHER_EMPLOYEE_HEADER = {"X-Chronobeam-Dev-Principal": "employee-b"}
HR_HEADER = {"X-Chronobeam-Dev-Principal": "hr-requester"}
SUPERVISOR_HEADER = {"X-Chronobeam-Dev-Principal": "supervisor"}
RECIPIENT_HEADER = {"X-Chronobeam-Dev-Principal": "recipient"}
TECHNICAL_ADMIN_HEADER = {"X-Chronobeam-Dev-Principal": "technical-admin"}


def decide_authorization(
    session: Session,
    employee: Employee,
    authorization_id: uuid.UUID,
    *,
    status: str = "APPROVED",
    authorized_by: str = "woco",
    actor: str = "OVERTIME_CLI",
    **window: Any,
) -> Any:
    """Record an INSTITUTIONAL overtime decision through the ONE writer.

    Tests must reach an authoritative approval through ``decide_authorization``
    (``ACCESS-017``): the employee write path cannot produce one, so a test that
    needs one has to use the same seam the operator CLI and a future external HR
    absence-system adapter use.
    """
    from app.ocd_overtime.models import OvertimeAuthStatus
    from app.ocd_overtime.schemas import OvertimeAuthorizationDecisionIn
    from app.ocd_overtime.service import decide_authorization as _decide

    return _decide(
        session,
        employee.id,
        authorization_id,
        OvertimeAuthorizationDecisionIn(
            status=OvertimeAuthStatus(status), authorized_by=authorized_by, **window
        ),
        actor,
    )


def production_login(
    client: TestClient,
    db: Session,
    provider: FakeIdP,
    *,
    subject: str = "employee",
) -> dict[str, str]:
    """Complete a REAL production OIDC login on ``client`` and return the
    headers an authenticated caller needs.

    Production authentication is the opaque browser session cookie, so a test
    that needs an authenticated production request drives ``/auth/login`` and
    ``/auth/callback`` on one client (the pre-auth binding cookie ties the two
    together). The returned dict carries the session's CSRF token, which every
    unsafe method requires once the cookie is present.
    """
    from app.auth.session_models import LoginAttempt

    login = client.get("/api/v1/auth/login?return_to=/", follow_redirects=False)
    assert login.status_code == 302, login.text
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    attempt = db.scalar(select(LoginAttempt).where(LoginAttempt.state == state))
    assert attempt is not None
    code = provider.issue_code(subject=subject, verifier=attempt.pkce_verifier, nonce=attempt.nonce)
    callback = client.get(
        f"/api/v1/auth/callback?code={code}&state={state}", follow_redirects=False
    )
    assert callback.status_code == 303, callback.text
    status = client.get("/api/v1/auth/session")
    assert status.status_code == 200
    body = status.json()
    assert body["authenticated"] is True, body
    return {"X-CSRF-Token": body["csrf_token"]}
