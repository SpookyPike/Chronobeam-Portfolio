"""Public liveness and narrowly-scoped database readiness probes."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from app.config import get_settings, production_settings_errors
from app.db import get_engine
from app.schema import expected_schema_revision
from app.version import APP_VERSION, build_sha

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    build_sha: str


class ReadyResponse(BaseModel):
    status: str


def _database_schema_status() -> tuple[bool, bool]:
    """Return categorical database/schema readiness without exposing errors."""
    expected = expected_schema_revision()
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
            revisions = connection.scalars(text("SELECT version_num FROM alembic_version")).all()
    except Exception:
        return False, False
    return True, expected is not None and len(revisions) == 1 and revisions[0] == expected


def _live_response() -> HealthResponse:
    return HealthResponse(
        status="ok", service="chronobeam-backend", version=APP_VERSION, build_sha=build_sha()
    )


@router.get("/health/live", response_model=HealthResponse, summary="Liveness probe")
def live() -> HealthResponse:
    """Liveness intentionally does no database or network work."""
    return _live_response()


@router.get("/health", response_model=HealthResponse, summary="Liveness compatibility probe")
def health() -> HealthResponse:
    return _live_response()


@router.get("/health/ready", response_model=ReadyResponse, summary="Database readiness probe")
def ready() -> ReadyResponse | JSONResponse:
    settings = get_settings()
    checks: dict[str, str] = {}
    if settings.is_production and production_settings_errors(settings):
        checks["config"] = "invalid"
    database_ok, schema_ok = _database_schema_status()
    if not database_ok:
        checks["database"] = "unreachable"
    elif not schema_ok:
        checks["schema"] = "mismatch"
    if checks:
        return JSONResponse(status_code=503, content={"status": "unavailable", "checks": checks})
    return ReadyResponse(status="ok")
