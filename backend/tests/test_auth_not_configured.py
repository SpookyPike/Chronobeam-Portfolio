"""The retired 501 ``AUTH_NOT_CONFIGURED`` promise (audit finding m3).

``main.py``/``config.py`` used to claim protected routes "return 501 in
production until the OIDC boundary is configured". No such path exists or can
exist: production refuses to start unless the boundary is fully configured, and
the only callers of the request-path raise (``validate_production_token``) sit
behind production-only routes (``/auth/login``, ``/auth/callback``).

These tests pin the coupling that makes that true. If anyone ever decouples
``oidc_configured`` from startup validation — i.e. lets a production process
run with an unconfigured boundary — they fail, which is exactly the condition
that would make a 501 handler necessary again.
"""

import pytest
from app.config import Settings, validate_production_settings
from app.errors import ErrorCode

OIDC_BOUNDARY_KEYS = (
    "CHRONOBEAM_OIDC_DISCOVERY_URL",
    "CHRONOBEAM_OIDC_ISSUER",
    "CHRONOBEAM_OIDC_CLIENT_ID",
    "CHRONOBEAM_OIDC_AUDIENCE",
)


def _valid_production_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRONOBEAM_ENV", "production")
    monkeypatch.setenv("CHRONOBEAM_PUBLIC_BASE_URL", "https://chronobeam.test")
    monkeypatch.setenv("CHRONOBEAM_OIDC_DISCOVERY_URL", "https://idp.test/config")
    monkeypatch.setenv("CHRONOBEAM_OIDC_ISSUER", "https://idp.test")
    monkeypatch.setenv("CHRONOBEAM_OIDC_CLIENT_ID", "chronobeam")
    monkeypatch.setenv("CHRONOBEAM_OIDC_AUDIENCE", "chronobeam")
    monkeypatch.setenv(
        "CHRONOBEAM_DATABASE_URL",
        "postgresql+psycopg://chronobeam:pilot_test@127.0.0.1:5433/chronobeam",
    )
    monkeypatch.setenv("CHRONOBEAM_CORS_ORIGINS", "[]")


def test_started_production_always_has_a_configured_oidc_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A production app that exists at all has ``oidc_configured`` True."""
    _valid_production_environment(monkeypatch)
    settings = Settings()
    validate_production_settings(settings)
    assert settings.oidc_configured


@pytest.mark.parametrize("missing", OIDC_BOUNDARY_KEYS)
def test_missing_boundary_key_aborts_startup_not_the_request(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """Dropping any boundary key is a startup failure, never a per-request 501."""
    _valid_production_environment(monkeypatch)
    monkeypatch.delenv(missing, raising=False)
    settings = Settings()
    # The request path (``validate_production_token``) would consider this
    # boundary unconfigured ...
    assert not settings.oidc_configured
    # ... but no such process is ever started to serve a request.
    with pytest.raises(RuntimeError):
        validate_production_settings(settings)


def test_retired_501_error_code_is_gone() -> None:
    """``AUTH_NOT_CONFIGURED`` had no producer and no consumer; re-adding it
    (even wired) is a deliberate contract decision that must update this test."""
    assert "AUTH_NOT_CONFIGURED" not in {code.value for code in ErrorCode}
