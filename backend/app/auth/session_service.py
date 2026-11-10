"""Small session/login-attempt operations; all tokens are opaque and hashed."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.auth.session_models import AuthSession, LoginAttempt
from app.config import Settings
from app.identity.models import Employee

COOKIE_NAME = "__Host-Chronobeam-Session"
# Pre-authentication cookie binding an OIDC attempt to the browser that started
# it. Same ``__Host-`` requirements as the session cookie (Secure, Path=/).
LOGIN_COOKIE_NAME = "__Host-Chronobeam-Login"
# Lifetime of the pre-auth binding, matching the 10-minute attempt TTL.
LOGIN_COOKIE_MAX_AGE = 600


def now() -> datetime:
    return datetime.now(UTC)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def prune_expired(session: Session, at: datetime | None = None) -> int:
    current = at or now()
    attempts = session.execute(delete(LoginAttempt).where(LoginAttempt.expires_at <= current))
    sessions = session.execute(
        delete(AuthSession).where(
            (AuthSession.idle_expires_at <= current) | (AuthSession.absolute_expires_at <= current)
        )
    )
    return int(getattr(attempts, "rowcount", 0) or 0) + int(getattr(sessions, "rowcount", 0) or 0)


def find_session(
    session: Session, token: str, settings: Settings, *, touch: bool = True
) -> AuthSession | None:
    current = now()
    # Token validation is scoped to the presented session. Global expiry
    # cleanup belongs to login/callback and the explicit cleanup-auth command.
    record = session.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(token)))
    if record is None or record.idle_expires_at <= current or record.absolute_expires_at <= current:
        return None
    # The opaque cookie is bound to the employee row which existed when OIDC
    # completed.  Do not let a later subject remap turn an old session into a
    # session for another employee.  Delete the stale record so reactivation or
    # a future remap cannot revive it.
    employee = session.get(Employee, record.employee_id)
    if employee is None or not employee.active or employee.idp_subject != record.subject:
        session.delete(record)
        return None
    if not touch:
        return record
    record.last_seen_at = current
    record.idle_expires_at = min(
        record.absolute_expires_at, current + timedelta(seconds=settings.session_idle_ttl_seconds)
    )
    return record


def create_session(
    session: Session, *, employee_id: object, subject: str, settings: Settings
) -> tuple[str, AuthSession]:
    current = now()
    opaque = new_secret()
    absolute = current + timedelta(seconds=settings.session_absolute_ttl_seconds)
    record = AuthSession(
        token_hash=token_hash(opaque),
        employee_id=employee_id,  # type: ignore[arg-type]
        subject=subject,
        csrf_token=new_secret(),
        last_seen_at=current,
        idle_expires_at=min(
            absolute, current + timedelta(seconds=settings.session_idle_ttl_seconds)
        ),
        absolute_expires_at=absolute,
    )
    session.add(record)
    session.flush()
    return opaque, record
