"""Persistent, opaque browser-authentication state (DP-AUTH-002)."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AuthSession(Base):
    __tablename__ = "auth_session"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employee.id"))
    subject: Mapped[str] = mapped_column(Text)
    csrf_token: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginAttempt(Base):
    """One-time pre-authentication state for an OIDC authorization-code flow.

    ``browser_binding_hash`` is the SHA-256 of a per-attempt secret handed to
    the initiating browser as a ``__Host-`` cookie. The callback requires the
    same cookie back, so a callback URL delivered to a different browser cannot
    complete there (login CSRF). The hash is stored, never the secret.
    """

    __tablename__ = "login_attempt"

    state: Mapped[str] = mapped_column(Text, primary_key=True)
    pkce_verifier: Mapped[str] = mapped_column(Text)
    nonce: Mapped[str] = mapped_column(Text)
    return_to: Mapped[str] = mapped_column(Text)
    browser_binding_hash: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
