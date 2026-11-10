"""Disclosure tables (WT-BACKEND §5.7).

Employee-controlled visibility boundary: ``access_request`` (HR asks, sees
nothing), ``disclosure_grant`` (employee opens ONE scoped frozen month view,
append-only snapshots, supersede lineage), ``submission_package`` (immutable
purpose-specific minimized projection; version chain; recipients),
``submission_recipient`` (exact principal subjects from the server-side
registry only, D6).

Central storage is not central visibility (INV-05/ACCESS-002): grants and
packages are the ONLY read paths for non-employees.
"""

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.enums import SubmissionPurpose


class AccessRequestStatus(StrEnum):
    """access_request.status (BE §5.7)."""

    PENDING = "PENDING"
    GRANTED = "GRANTED"
    DECLINED = "DECLINED"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"


class SubmissionPackageStatus(StrEnum):
    """submission_package.status (BE §5.7)."""

    SUBMITTED = "SUBMITTED"
    SUPERSEDED = "SUPERSEDED"
    WITHDRAWN = "WITHDRAWN"


class AccessRequest(Base):
    __tablename__ = "access_request"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employee.id"))
    requester_principal: Mapped[str] = mapped_column(Text)
    purpose: Mapped[SubmissionPurpose] = mapped_column(
        Enum(SubmissionPurpose, name="submission_purpose")
    )
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    view_type: Mapped[str] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)
    status: Mapped[AccessRequestStatus] = mapped_column(
        Enum(AccessRequestStatus, name="access_request_status"),
        default=AccessRequestStatus.PENDING,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("period_end >= period_start", name="ck_access_request_period_order"),
    )


class DisclosureGrant(Base):
    __tablename__ = "disclosure_grant"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    access_request_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("access_request.id"))
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employee.id"))
    grantee_principal: Mapped[str] = mapped_column(Text)
    purpose: Mapped[SubmissionPurpose] = mapped_column(
        Enum(SubmissionPurpose, name="submission_purpose")
    )
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    view_type: Mapped[str] = mapped_column(Text)
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("period_calculation_snapshot.id")
    )
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    # ACCESS-014 PROVISIONAL: no guessed expiry/retention — stays NULL until a
    # confirmed retention interpretation exists.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["supersedes_id", "employee_id"],
            ["disclosure_grant.id", "disclosure_grant.employee_id"],
            name="fk_disclosure_grant_supersedes_same_owner",
        ),
        # The composite FK above needs this key as its target, and
        # PostgreSQL requires a unique constraint on the referenced columns.
        UniqueConstraint("id", "employee_id", name="uq_disclosure_grant_id_employee_id"),
        CheckConstraint("period_end >= period_start", name="ck_disclosure_grant_period_order"),
        UniqueConstraint("supersedes_id", name="uq_disclosure_grant_supersedes_id"),
    )


class SubmissionPackage(Base):
    __tablename__ = "submission_package"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employee.id"))
    purpose: Mapped[SubmissionPurpose] = mapped_column(
        Enum(SubmissionPurpose, name="submission_purpose")
    )
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    version: Mapped[int] = mapped_column(Integer)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    policy_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("policy_version.id"))
    # The package payload IS the frozen snapshot of the disclosed projection
    # (D9): immutable JSONB, never mutated. Recipient-visible data lives here.
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("period_calculation_snapshot.id")
    )
    payload_json: Mapped[dict] = mapped_column(JSONB)
    employee_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    client_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, unique=True
    )  # idempotent replay (BE-CMD-001 pattern); globally unique
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    status: Mapped[SubmissionPackageStatus] = mapped_column(
        Enum(SubmissionPackageStatus, name="submission_package_status"),
        default=SubmissionPackageStatus.SUBMITTED,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["supersedes_id", "employee_id"],
            ["submission_package.id", "submission_package.employee_id"],
            name="fk_submission_package_supersedes_same_owner",
        ),
        # The composite FK above needs this key as its target, and
        # PostgreSQL requires a unique constraint on the referenced columns.
        UniqueConstraint("id", "employee_id", name="uq_submission_package_id_employee_id"),
        CheckConstraint("version >= 1", name="ck_submission_package_version_positive"),
        CheckConstraint("period_end >= period_start", name="ck_submission_package_period_order"),
        UniqueConstraint("supersedes_id", name="uq_submission_package_supersedes_id"),
        # SEMANTIC scope uniqueness (distinct from request idempotency above):
        # two rows may not claim the same authoritative version of one scope.
        # ``status`` is excluded deliberately — it is mutable.
        UniqueConstraint(
            "employee_id",
            "purpose",
            "period_start",
            "period_end",
            "version",
            name="uq_submission_package_scope_version",
        ),
    )


class SubmissionRecipient(Base):
    __tablename__ = "submission_recipient"

    submission_package_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("submission_package.id"), primary_key=True
    )
    recipient_type: Mapped[str] = mapped_column(Text)
    recipient_principal: Mapped[str] = mapped_column(Text, primary_key=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
