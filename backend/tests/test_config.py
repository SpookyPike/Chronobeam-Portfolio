"""Configuration fail-closed guarantees."""

from pathlib import Path
from typing import cast

import pytest
from app.config import Settings, validate_production_settings
from pydantic_settings import SettingsConfigDict


def _set_valid_production_environment(monkeypatch) -> None:
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


def test_settings_default_to_production(monkeypatch) -> None:
    """Unset CHRONOBEAM_ENV must deny: the dev-auth seam is off by default."""
    monkeypatch.delenv("CHRONOBEAM_ENV", raising=False)
    assert Settings().env == "production"
    assert Settings().is_production


def test_env_file_is_anchored_to_repo_root() -> None:
    """The .env file is resolved from the repository root, not from CWD:
    backend commands run with CWD=backend/."""
    env_file = cast(SettingsConfigDict, Settings().model_config).get("env_file")
    assert env_file == Path(__file__).resolve().parents[2] / ".env"


def test_env_file_values_are_loaded(tmp_path, monkeypatch) -> None:
    """An env file referenced by the config is read at instantiation
    (regardless of CWD), proving the anchored-path fix works at runtime."""
    (tmp_path / ".env").write_text("CHRONOBEAM_ENV=dev\n", encoding="utf-8")
    monkeypatch.delenv("CHRONOBEAM_ENV", raising=False)

    class TmpSettings(Settings):
        model_config = SettingsConfigDict(
            env_file=tmp_path / ".env",
            env_prefix="CHRONOBEAM_",
            extra="ignore",
        )

    assert TmpSettings().env == "dev"
    assert not TmpSettings().is_production


def test_empty_env_value_is_fail_closed(monkeypatch) -> None:
    """A blank CHRONOBEAM_ENV (e.g. from a sloppy .env) must not enable the seam."""
    monkeypatch.setenv("CHRONOBEAM_ENV", "")
    assert Settings().is_production


def test_unknown_env_value_is_fail_closed(monkeypatch) -> None:
    monkeypatch.setenv("CHRONOBEAM_ENV", "staging-mystery")
    assert Settings().is_production


def test_dev_mode_is_explicit_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("CHRONOBEAM_ENV", "dev")
    assert Settings().env == "dev"
    assert not Settings().is_production
    assert Settings().cors_origins == ["http://localhost:5173"]


def test_partial_production_oidc_configuration_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("CHRONOBEAM_ENV", "production")
    monkeypatch.setenv("CHRONOBEAM_PUBLIC_BASE_URL", "https://chronobeam.example.invalid")
    with pytest.raises(RuntimeError) as error:
        validate_production_settings(Settings())
    # Diagnostics name keys only, never supplied values or secrets.
    assert "CHRONOBEAM_OIDC_DISCOVERY_URL" in str(error.value)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("CHRONOBEAM_PUBLIC_BASE_URL", "http://chronobeam.test"),
        ("CHRONOBEAM_OIDC_DISCOVERY_URL", "http://idp.test/config"),
        ("CHRONOBEAM_OIDC_CLIENT_AUTH_METHOD", "private_key_jwt"),
        ("CHRONOBEAM_OIDC_CLIENT_AUTH_METHOD", "made-up"),
        ("CHRONOBEAM_SESSION_IDLE_TTL_SECONDS", "0"),
        ("CHRONOBEAM_SESSION_ABSOLUTE_TTL_SECONDS", "-1"),
    ],
)
def test_invalid_production_auth_settings_name_the_bad_key(
    monkeypatch, key: str, value: str
) -> None:
    monkeypatch.setenv("CHRONOBEAM_ENV", "production")
    monkeypatch.setenv("CHRONOBEAM_PUBLIC_BASE_URL", "https://chronobeam.test")
    monkeypatch.setenv("CHRONOBEAM_OIDC_DISCOVERY_URL", "https://idp.test/config")
    monkeypatch.setenv("CHRONOBEAM_OIDC_ISSUER", "https://idp.test")
    monkeypatch.setenv("CHRONOBEAM_OIDC_CLIENT_ID", "chronobeam")
    monkeypatch.setenv("CHRONOBEAM_OIDC_AUDIENCE", "chronobeam")
    monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError) as error:
        validate_production_settings(Settings())
    assert key in str(error.value)
    assert value not in str(error.value)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (
            "CHRONOBEAM_DATABASE_URL",
            "postgresql+psycopg://chronobeam:chronobeam_dev_only@db:5432/app",
        ),
        (
            "CHRONOBEAM_DATABASE_URL",
            "postgresql+psycopg://chronobeam:chronobeam%5Fdev%5Fonly@db:5432/app",
        ),
        ("CHRONOBEAM_DATABASE_URL", "postgresql+psycopg://chronobeam:pilot@db:55432/app"),
        ("CHRONOBEAM_DATABASE_URL", "mysql://pilot.example/chronobeam"),
        ("CHRONOBEAM_CORS_ORIGINS", '["*"]'),
        ("CHRONOBEAM_CORS_ORIGINS", '["http://chronobeam.test"]'),
        ("CHRONOBEAM_LOG_LEVEL", "VERBOSE"),
        ("CHRONOBEAM_FORWARDED_ALLOW_IPS", "not-an-ip"),
    ],
)
def test_invalid_production_operational_settings_name_keys_only(
    monkeypatch, key: str, value: str
) -> None:
    _set_valid_production_environment(monkeypatch)
    monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError) as error:
        validate_production_settings(Settings())
    assert key in str(error.value)
    assert value not in str(error.value)


def test_explicit_nondefault_localhost_database_is_allowed_in_production(monkeypatch) -> None:
    _set_valid_production_environment(monkeypatch)
    monkeypatch.setenv(
        "CHRONOBEAM_DATABASE_URL",
        "postgresql+psycopg://chronobeam:pilot_test@127.0.0.1:5433/chronobeam",
    )
    validate_production_settings(Settings())


def test_non_https_oidc_endpoints_fail_closed(monkeypatch) -> None:
    """Every configured OIDC URL must be HTTPS in production.

    The issuer is the one the closure audit questioned: a plain-http issuer
    string would otherwise be accepted and let a redirect/downgrade stand in
    for the institutional IdP.
    """
    for key in (
        "CHRONOBEAM_OIDC_ISSUER",
        "CHRONOBEAM_OIDC_DISCOVERY_URL",
        "CHRONOBEAM_PUBLIC_BASE_URL",
    ):
        _set_valid_production_environment(monkeypatch)
        monkeypatch.setenv(key, "http://idp.test")
        with pytest.raises(RuntimeError) as error:
            validate_production_settings(Settings())
        assert key in str(error.value), (key, str(error.value))
    # the valid baseline still passes
    _set_valid_production_environment(monkeypatch)
    validate_production_settings(Settings())
