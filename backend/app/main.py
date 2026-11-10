"""Application entrypoint.

Public health endpoints plus the /me identity/worktime routes behind the
auth boundary (dev header seam in dev/test, OIDC boundary in production).
OpenAPI is generated from the FastAPI app (no hand-maintained spec).

The /me/* routes carry byte-identical denials and an existence-oracle guard.
Production refuses to start unless the OIDC boundary is fully configured
(``validate_production_settings`` below), so there is no degraded
"start anyway, answer 501" production mode to serve.
"""

import logging
import time
import uuid
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.responses import Response

from app.absence.routes import router as day_status_router
from app.admin.routes import router as admin_router
from app.auth.deps import get_db
from app.auth.routes import router as auth_router
from app.auth.session_service import COOKIE_NAME, find_session
from app.config import get_settings, validate_production_settings
from app.cutover.routes import router as cutover_router
from app.cutover.self_service_routes import router as onboarding_router
from app.db import commit_before_response, get_session_factory
from app.disclosure.routes import router as disclosure_router
from app.errors import CalendarUnavailable, ErrorCode, PolicyEditionUnavailable
from app.identity.routes import router as identity_router
from app.logging_conf import configure_logging
from app.ocd_overtime.routes import router as ocd_overtime_router
from app.policy.routes import router as policy_router
from app.reporting.routes import router as reporting_router
from app.routers import health
from app.shift.routes import router as shift_router
from app.static import mount_frontend
from app.travel.routes import router as travel_router
from app.version import APP_VERSION
from app.worktime.routes import router as worktime_router


def create_app() -> FastAPI:
    settings = get_settings()
    validate_production_settings(settings)
    configure_logging(settings.log_level)
    app = FastAPI(
        title="Chronobeam API",
        version=APP_VERSION,
        description=(
            "Chronobeam — employee-private working-time ledger with an explicit "
            "authorization boundary. See docs/ARCHITECTURE.md and docs/SECURITY.md."
        ),
        openapi_url=None if settings.is_production else "/openapi.json",
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        # `EINV-11a`: one app-level, function-scoped dependency so EVERY route
        # commits before the response is sent. Declared here rather than per
        # route so no route list has to be maintained and a new route cannot
        # silently regress. Function scope is load-bearing: it is closed on
        # FastAPI's function stack, which exits before the response is sent,
        # whereas a route-level dependency exits after. The behavioural guard
        # in tests/test_response_durability.py pins the ordering.
        dependencies=[Depends(commit_before_response, scope="function")],
    )

    logger = logging.getLogger("app.request")

    def safe_route_name(request: Request) -> str:
        """Return the matched route template, never an incoming path value."""
        route = request.scope.get("route")
        path = getattr(route, "path", None)
        return path if isinstance(path, str) else "UNMATCHED_ROUTE"

    def _with_boundary_headers(response: Response, request: Request, request_id: str) -> Response:
        """Apply the shared response headers to every response this app returns.

        A denial that short-circuits before the route is still a response to an
        API caller, so it carries the same request ID and security headers.
        """
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        # Every API response can carry per-employee working-time facts, so no
        # shared or browser cache may retain one. Scoped to `/api/` on purpose:
        # the built frontend assets are not private and stay cacheable.
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        # Strict CSP only in production: the dev/docs Swagger UI loads assets
        # from a CDN and the Vite dev server is a separate origin, so a strict
        # production CSP would break local development surfaces.
        if settings.is_production:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; object-src 'none'; "
                "script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
                "font-src 'self'"
            )
        return response

    @app.middleware("http")
    async def _security_boundary(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.monotonic()
        try:
            if (
                settings.is_production
                and request.method in {"POST", "PUT", "PATCH", "DELETE"}
                and request.url.path.startswith("/api/")
                and request.url.path != "/api/v1/auth/callback"
            ):
                origin = request.headers.get("origin")
                if origin is not None and origin.rstrip("/") != settings.public_base_url.rstrip(
                    "/"
                ):
                    return _with_boundary_headers(
                        JSONResponse(
                            status_code=403,
                            content={
                                "detail": {
                                    "code": "CSRF_REJECTED",
                                    "message": "Request origin is not allowed.",
                                }
                            },
                        ),
                        request,
                        request_id,
                    )
                opaque = request.cookies.get(COOKIE_NAME)
                if opaque:
                    # The CSRF check must see the SAME session the request
                    # authenticates with. In production that is the app's own
                    # session factory; test clients override get_db with a
                    # shared transaction session, and the middleware honors
                    # that override so the token check is exercised for real.
                    owned = False
                    provider = request.app.dependency_overrides.get(get_db)
                    if provider is not None:
                        db_session = provider()
                    else:
                        db_session = get_session_factory()()
                        owned = True
                    try:
                        # This lookup validates expiry/mapping for CSRF but
                        # must not compete with the request's authenticated
                        # dependency for the sliding idle-window update.
                        auth_session = find_session(db_session, opaque, settings, touch=False)
                        if (
                            auth_session is not None
                            and request.headers.get("X-CSRF-Token") != auth_session.csrf_token
                        ):
                            return _with_boundary_headers(
                                JSONResponse(
                                    status_code=403,
                                    content={
                                        "detail": {
                                            "code": "CSRF_REJECTED",
                                            "message": "CSRF validation failed.",
                                        }
                                    },
                                ),
                                request,
                                request_id,
                            )
                        db_session.commit()
                    finally:
                        if owned:
                            db_session.close()
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request failed request_id=%s route=%s", request_id, safe_route_name(request)
            )
            response = JSONResponse(
                status_code=500,
                content={
                    "detail": {
                        "code": "INTERNAL_ERROR",
                        "message": "An unexpected error occurred.",
                        "request_id": request_id,
                    }
                },
            )
        response = _with_boundary_headers(response, request, request_id)
        # Deliberately log only technical route/status/latency metadata.
        logger.info(
            "request request_id=%s route=%s status=%s latency_ms=%d",
            request_id,
            safe_route_name(request),
            response.status_code,
            int((time.monotonic() - started) * 1000),
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Unified error envelope.

        Strips ``input``/``ctx`` so invalid values (comments, reasons, tokens)
        are never reflected back to clients or into proxy logs (DP-LOG-001,
        DP-DATA-002). Keeps ``loc`` + message for debuggability.
        """
        details: list[dict[str, Any]] = []
        for error in exc.errors():
            details.append(
                {
                    "loc": [str(part) for part in error.get("loc", [])],
                    "msg": str(error.get("msg", "")),
                }
            )
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "VALIDATION_FAILED",
                    "message": "Request validation failed.",
                    "details": details,
                }
            },
        )

    @app.exception_handler(PolicyEditionUnavailable)
    async def _policy_edition_unavailable_handler(
        request: Request, exc: PolicyEditionUnavailable
    ) -> JSONResponse:
        """A month with no installed policy edition is a domain condition (EINV-08).

        Answering 409 ``POLICY_BLOCKED`` keeps it out of the unexpected-failure
        path, which logs a traceback and returns 500 for a state the operator can
        actually fix by installing the missing edition.
        """
        return JSONResponse(
            status_code=409,
            content={
                "detail": {
                    "code": ErrorCode.POLICY_BLOCKED.value,
                    "message": "No working-time policy edition is installed for this period.",
                    "details": exc.details(),
                }
            },
        )

    @app.exception_handler(CalendarUnavailable)
    async def _calendar_unavailable_handler(
        request: Request, exc: CalendarUnavailable
    ) -> JSONResponse:
        """A window with no covering calendar edition is a domain condition (EINV-10).

        Answering 409 ``CALENDAR_BLOCKED`` keeps an operator-fixable data gap out
        of the unexpected-failure path — and, crucially, out of the silent
        "every day is an ordinary working day" fallback that made a holiday add
        required time and invalidated the balance without a trace.
        """
        return JSONResponse(
            status_code=409,
            content={
                "detail": {
                    "code": ErrorCode.CALENDAR_BLOCKED.value,
                    "message": "No institutional calendar edition covers this period.",
                    "details": exc.details(),
                }
            },
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(auth_router)
    app.include_router(identity_router)
    app.include_router(day_status_router)
    app.include_router(worktime_router)
    app.include_router(policy_router)
    app.include_router(shift_router)
    app.include_router(ocd_overtime_router)
    app.include_router(disclosure_router)
    app.include_router(cutover_router)
    app.include_router(onboarding_router)
    app.include_router(reporting_router)
    app.include_router(travel_router)
    app.include_router(admin_router)
    mount_frontend(app, settings.frontend_dist)
    return app


app = create_app()
