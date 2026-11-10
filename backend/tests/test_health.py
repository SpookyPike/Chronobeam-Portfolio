"""Public liveness/readiness probes and OpenAPI availability."""

import tomllib
from pathlib import Path
from types import SimpleNamespace

from app import version
from app.db import get_engine
from app.routers import health
from fastapi.testclient import TestClient


def test_health_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "chronobeam-backend"
    assert body["version"] == version.APP_VERSION
    assert body["build_sha"] == ""


def test_live_is_db_free_and_surfaces_build_sha(production_client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("CHRONOBEAM_BUILD_SHA", "test-build-sha")
    response = production_client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "chronobeam-backend",
        "version": version.APP_VERSION,
        "build_sha": "test-build-sha",
    }


def test_health_alias_is_liveness_compatible(client: TestClient) -> None:
    assert client.get("/health").json() == client.get("/health/live").json()


def test_readiness_happy_path(db_client: TestClient) -> None:
    get_engine.cache_clear()
    assert db_client.get("/health/ready").json() == {"status": "ok"}
    get_engine.cache_clear()


def test_readiness_reports_database_and_schema_categories(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(health, "_database_schema_status", lambda: (False, False))
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"database": "unreachable"},
    }
    monkeypatch.setattr(health, "_database_schema_status", lambda: (True, False))
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "checks": {"schema": "mismatch"}}


def test_readiness_reports_invalid_production_config(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(health, "get_settings", lambda: SimpleNamespace(is_production=True))
    monkeypatch.setattr(health, "production_settings_errors", lambda _: ["CHRONOBEAM_DATABASE_URL"])
    monkeypatch.setattr(health, "_database_schema_status", lambda: (True, True))
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "checks": {"config": "invalid"}}


def test_readiness_does_not_fetch_oidc(db_client: TestClient, fake_idp) -> None:
    get_engine.cache_clear()
    before = dict(fake_idp.fetches)
    assert db_client.get("/health/ready").status_code == 200
    assert fake_idp.fetches == before
    get_engine.cache_clear()


def test_package_version_matches_canonical_source() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    package_version = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert package_version == version.APP_VERSION


def test_health_response_contains_no_personal_data(client: TestClient) -> None:
    # trivially true today; asserted so it stays true (DP-LOG/DP-AUTH scope)
    assert "employee" not in client.get("/health").text.lower()
    assert "emp-0001" not in client.get("/health").text


def test_openapi_generated_from_code(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    assert spec["info"]["title"] == "Chronobeam API"
    assert "/health" in spec["paths"]
    assert "/api/v1/me" in spec["paths"]
    assert "/api/v1/me/work-intervals" in spec["paths"]
    assert "/api/v1/me/months/{year_month}" in spec["paths"]


def test_unknown_route_404(client: TestClient) -> None:
    assert client.get("/does-not-exist").status_code == 404
