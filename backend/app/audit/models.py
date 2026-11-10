"""Audit events (WT-BACKEND §5.7, DP-LOG-002).

Accountability for security/domain actions, not behavioral monitoring.
Audit rows are written for interval DELETEs (success/failure) and
disclosure events; corrections are covered by the ``correction`` lineage
table, creates by the row itself. GET reads are never audited.

Metadata is restricted to a fixed allowlist of scalar keys — never comments,
reasons, or request bodies (BE §5.7:594, DP-LOG-001).
"""

import uuid
from datetime import datetime

from app.db import Base
from sqlalchemy import DateTime, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column


class AuditEvent(Base):
    __tablename__ = "audit_event"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    actor_principal: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    object_type: Mapped[str | None] = mapped_column(Text)
    object_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    employee_subject_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    outcome: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column("metadata", JSONB)


# Fixed allowlist of scalar keys permitted in audit metadata. Values are
# pseudonymous ids / versions / stable codes only. The disclosure surface
# deliberately extends the allowlist for disclosure events: request and
# grant scope codes, package version, and view type — never payloads, comments,
# or request bodies. Reads are audited only after a successful authorization.
ALLOWED_METADATA_KEYS = frozenset(
    {
        "interval_version",
        "error_code",
        "request_purpose",
        "grant_purpose",
        "package_purpose",
        "package_version",
        "view_type",
        "month",
    }
)


def record_audit(
    session: Session,
    *,
    actor_principal: str,
    action: str,
    outcome: str,
    object_type: str | None = None,
    object_id: uuid.UUID | None = None,
    employee_subject_id: uuid.UUID | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    """Insert one audit row. ``metadata`` keys are restricted to the allowlist."""
    if metadata is not None:
        unknown = set(metadata) - ALLOWED_METADATA_KEYS
        if unknown:
            raise ValueError(f"audit metadata key not allowed: {sorted(unknown)}")
    session.add(
        AuditEvent(
            actor_principal=actor_principal,
            action=action,
            object_type=object_type,
            object_id=object_id,
            employee_subject_id=employee_subject_id,
            outcome=outcome,
            metadata_json=metadata,
        )
    )
