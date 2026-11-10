"""Backend-managed OIDC authorization-code endpoints (DP-AUTH-001/002).

Login CSRF: ``GET /auth/login`` hands the initiating browser a short-lived
``__Host-`` binding cookie and stores only its hash with the attempt. The
callback requires that cookie back, so a callback URL delivered to a different
browser cannot complete there. Every terminal callback response — success and
all failure paths — clears the binding, because ``raise HTTPException`` cannot
carry a cookie and the callback therefore returns explicit ``Response`` objects.
"""

import secrets
from datetime import timedelta
from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.oidc import (
    InvalidOidcToken,
    authorization_url,
    discovery,
    exchange_code,
    validate_production_token,
)
from app.auth.session_models import AuthSession, LoginAttempt
from app.auth.session_service import (
    COOKIE_NAME,
    LOGIN_COOKIE_MAX_AGE,
    LOGIN_COOKIE_NAME,
    create_session,
    find_session,
    new_secret,
    now,
    prune_expired,
    token_hash,
)
from app.config import get_settings
from app.db import get_db
from app.errors import error_body
from app.identity.models import Employee

router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])


def _clear_login_binding(response: Response) -> Response:
    """Clear the pre-authentication binding cookie on a terminal callback.

    ``__Host-`` deletion requires Secure + Path=/ + no Domain; Starlette's
    ``delete_cookie`` defaults to ``secure=False``, which makes the deletion a
    silent no-op in a browser. Mirrors the session-cookie deletion in ``logout``.
    """
    response.delete_cookie(LOGIN_COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax")
    return response


def _callback_error(status_code: int, code: str, message: str) -> Response:
    """One terminal callback failure response, with the binding cleared."""
    return _clear_login_binding(
        JSONResponse(status_code=status_code, content={"detail": error_body(code, message)})
    )


def _return_to(value: str) -> str:
    parsed = urlparse(value)
    invalid = not value.startswith("/") or value.startswith("//") or "\\" in value
    if invalid or parsed.scheme or parsed.netloc:
        raise HTTPException(
            status_code=422,
            detail={"code": "VALIDATION_FAILED", "message": "Invalid return target."},
        )
    return value


@router.get("/login")
def login(
    session: Annotated[Session, Depends(get_db)],
    return_to: str = Query("/"),
) -> RedirectResponse:
    settings = get_settings()
    if not settings.is_production:
        raise HTTPException(status_code=404)
    target = _return_to(return_to)
    prune_expired(session)
    state, verifier, nonce = new_secret(), new_secret(), new_secret()
    binding = new_secret()
    session.add(
        LoginAttempt(
            state=state,
            pkce_verifier=verifier,
            nonce=nonce,
            return_to=target,
            browser_binding_hash=token_hash(binding),
            expires_at=now() + timedelta(minutes=10),
        )
    )
    try:
        metadata = discovery(settings)
    except (InvalidOidcToken, OSError) as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "AUTH_UNAVAILABLE", "message": "Authentication is unavailable."},
        ) from exc
    location = authorization_url(settings, metadata, state=state, nonce=nonce, verifier=verifier)
    # Durability before the response: the callback looks this attempt up by
    # ``state``, and the pre-auth cookie that names the binding leaves the process
    # with the 302. ``get_db`` commits in its teardown, which runs AFTER the
    # response is sent, so without this the redirect can outrun the row it
    # depends on and the callback would answer 401 for a legitimate login.
    session.commit()
    response = RedirectResponse(location, status_code=302)
    # The binding value is never the session token: it only proves that the
    # callback arrives in the browser that started this attempt.
    response.set_cookie(
        LOGIN_COOKIE_NAME,
        binding,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=LOGIN_COOKIE_MAX_AGE,
    )
    return response


@router.get("/callback")
def callback(
    request: Request,
    code: str,
    state: str,
    session: Annotated[Session, Depends(get_db)],
) -> Response:
    settings = get_settings()
    if not settings.is_production:
        raise HTTPException(status_code=404)
    prune_expired(session)
    # Lock and durably consume the single-use state before contacting the IdP.
    # The request dependency rolls back on an unsuccessful exchange; without
    # this separate commit, that rollback would make the state replayable.
    attempt = session.scalar(
        select(LoginAttempt).where(LoginAttempt.state == state).with_for_update()
    )
    if attempt is None or attempt.expires_at <= now():
        return _callback_error(401, "AUTH_INVALID_CALLBACK", "Invalid login callback.")
    # Browser binding is checked BEFORE the attempt is consumed, so a callback
    # delivered to the wrong browser does not burn the legitimate attempt. The
    # body is byte-identical to the unknown-state response: no new oracle.
    presented = request.cookies.get(LOGIN_COOKIE_NAME)
    if not presented or not secrets.compare_digest(
        token_hash(presented), attempt.browser_binding_hash
    ):
        return _callback_error(401, "AUTH_INVALID_CALLBACK", "Invalid login callback.")
    session.delete(attempt)
    session.commit()
    # Discovery is separated from the exchange so a recoverable IdP outage is
    # reported as unavailability (503), not as an invalid callback (401) that a
    # browser would present as "your login link is wrong".
    try:
        metadata = discovery(settings)
    except (InvalidOidcToken, OSError):
        return _callback_error(503, "AUTH_UNAVAILABLE", "Authentication is unavailable.")
    try:
        token = exchange_code(settings, metadata, code=code, verifier=attempt.pkce_verifier)
        subject = validate_production_token(token, nonce=attempt.nonce)
    except (InvalidOidcToken, OSError):
        return _callback_error(401, "AUTH_INVALID_CALLBACK", "Invalid login callback.")
    employee = session.scalar(select(Employee).where(Employee.idp_subject == subject))
    if employee is None or not employee.active:
        return _callback_error(403, "AUTH_FORBIDDEN", "Access to this resource is restricted.")
    opaque, _record = create_session(
        session, employee_id=employee.id, subject=subject, settings=settings
    )
    # Durability before the response: the session cookie must not outrun the row
    # it names. ``get_db`` commits in its teardown, which runs AFTER the response
    # is sent, so a client that follows the 303 immediately — exactly what a
    # browser does — could read ``/auth/session`` first and see
    # ``authenticated: false``. Observed intermittently in the pilot smoke; a
    # 100 ms retry always succeeded, the signature of a commit-order race rather
    # than a missing row.
    return_to = attempt.return_to
    session.commit()
    response = RedirectResponse(return_to, status_code=303)
    response.set_cookie(COOKIE_NAME, opaque, secure=True, httponly=True, samesite="lax", path="/")
    return _clear_login_binding(response)


@router.get("/session")
def session_status(
    request: Request,
    session: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    settings = get_settings()
    token = request.cookies.get(COOKIE_NAME)
    record = find_session(session, token, settings) if settings.is_production and token else None
    return {
        "authenticated": record is not None,
        "csrf_token": record.csrf_token if record else None,
        "oidc_mode": settings.is_production,
    }


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(get_db)],
) -> Response:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        record = session.scalar(
            select(AuthSession).where(AuthSession.token_hash == token_hash(token))
        )
        if record is not None:
            session.delete(record)
    # Durability before the response: the revocation must be visible before the
    # cleared cookie is, or a client that re-presents the old cookie during the
    # window is still authenticated.
    session.commit()
    response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax")
    # An injected Response has no status code until FastAPI creates one; returning
    # it directly must still preserve this endpoint's explicit 204 contract.
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
