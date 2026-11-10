"""Versioned OCD/overtime facts; compensation decisions never alter work facts."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.enums import (
    AUTHORITATIVE_SOURCES,
    EMPLOYEE_SOURCE,
    INSTITUTIONAL_SOURCE,
    OcdAssignmentStatus,
    is_employee_writable_source,
)

# Re-exported for the existing importers (``ocd_overtime/service.py``,
# ``travel/context.py`` and the F6/travel test suites): ``app/enums.py`` is the
# single canonical home for the provenance vocabulary and the status enums, and
# these names stay importable from here so no caller has to move.
__all__ = [
    "AUTHORITATIVE_SOURCES",
    "EMPLOYEE_SOURCE",
    "INSTITUTIONAL_SOURCE",
    "CompensationContextType",
    "CompensationElection",
    "CompensationMode",
    "OcdAssignment",
    "OcdAssignmentStatus",
    "OcdCall",
    "OcdCallMode",
    "OcdCallStatus",
    "OvertimeAuthStatus",
    "OvertimeAuthorization",
    "is_authoritative_approval",
    "is_employee_writable",
]


class OcdCallMode(StrEnum):
    REMOTE = "REMOTE"
    ON_SITE = "ON_SITE"


class OcdCallStatus(StrEnum):
    RECORDED = "RECORDED"
    VOIDED = "VOIDED"


class OvertimeAuthStatus(StrEnum):
    REQUESTED = "REQUESTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class CompensationContextType(StrEnum):
    OVERTIME = "OVERTIME"
    SHIFT = "SHIFT"
    OCD = "OCD"


class CompensationMode(StrEnum):
    PAY = "PAY"
    TIME = "TIME"


class OcdAssignment(Base):
    """An on-call AVAILABILITY window (``INV-10``), version-chained like the rest.

    The availability is a plan fact, never effective work: an actual call is a
    separate ``ocd_call`` (plus its linked interval). Making the assignment
    versioned is what lets an employee correct or cancel a planned availability
    without destroying what was previously recorded — the predecessor row stays as
    lineage (``INV-11``) and the chain head is the current truth.
    """

    __tablename__ = "ocd_assignment"
    __table_args__ = (
        CheckConstraint("end_at > start_at", name="ck_ocd_assignment_end_after_start"),
        CheckConstraint("version >= 1", name="ck_ocd_assignment_version_positive"),
        ForeignKeyConstraint(
            ["supersedes_id", "employee_id"],
            ["ocd_assignment.id", "ocd_assignment.employee_id"],
            name="fk_ocd_assignment_supersedes_same_owner",
        ),
        # The composite FK above needs this key as its target, and PostgreSQL
        # requires a unique constraint on the referenced columns.
        UniqueConstraint("id", "employee_id", name="uq_ocd_assignment_id_employee_id"),
        # At most one successor per row, so a chain cannot fork.
        UniqueConstraint("supersedes_id", name="uq_ocd_assignment_supersedes_id"),
        # Idempotency key for the employee create command (BE-CMD-001), unique
        # TABLE-WIDE: a client-generated command id identifies one command, so the
        # replay lookup is global and the write path must handle a cross-employee
        # collision on this exact constraint name.
        UniqueConstraint("client_request_id", name="uq_ocd_assignment_client_request_id"),
        # NO partial root index here, deliberately. ``uq_shift_deviation_root`` and
        # ``uq_compensation_election_root`` exist because those tables have a SCOPE
        # key. An availability has none: two root assignments are two legitimately
        # distinct facts (two different nights), so a root index would forbid a
        # second legitimate assignment.
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employee.id"))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: The INSTITUTIONAL notice/communication instant — when the Company scheduled
    #: it. This is the sole input to ``classify_duty``'s ``short_notice`` and the
    #: OCD-002 notice finding, both of which are remuneration-relevant, so it is
    #: written ONLY by institutional sources (seeds, a future adapter) and is
    #: server-forced NULL on every employee-authored row.
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The instant the EMPLOYEE reports they were told. A different fact from
    #: ``scheduled_at``: it preserves the employee's information without making it
    #: authoritative, so no classifier reads it. See ``enums``/the OCD write path.
    reported_notice_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[OcdAssignmentStatus] = mapped_column(
        Enum(OcdAssignmentStatus, name="ocd_assignment_status")
    )
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    client_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )


class OcdCall(Base):
    __tablename__ = "ocd_call"
    __table_args__ = (
        ForeignKeyConstraint(
            ["supersedes_id", "employee_id"],
            ["ocd_call.id", "ocd_call.employee_id"],
            name="fk_ocd_call_supersedes_same_owner",
        ),
        # The composite FK above needs this key as its target, and
        # PostgreSQL requires a unique constraint on the referenced columns.
        UniqueConstraint("id", "employee_id", name="uq_ocd_call_id_employee_id"),
        CheckConstraint("call_end_at > call_start_at", name="ck_ocd_call_end_after_start"),
        CheckConstraint(
            "travel_out_minutes IS NULL OR travel_out_minutes >= 0",
            name="ck_ocd_call_travel_out_nonneg",
        ),
        CheckConstraint(
            "travel_back_minutes IS NULL OR travel_back_minutes >= 0",
            name="ck_ocd_call_travel_back_nonneg",
        ),
        CheckConstraint("version >= 1", name="ck_ocd_call_version_positive"),
        UniqueConstraint("client_request_id", name="uq_ocd_call_client_request_id"),
        UniqueConstraint("supersedes_id", name="uq_ocd_call_supersedes_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    ocd_assignment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ocd_assignment.id"))
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employee.id"))
    call_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    call_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    mode: Mapped[OcdCallMode] = mapped_column(Enum(OcdCallMode, name="ocd_call_mode"))
    travel_out_minutes: Mapped[int | None] = mapped_column(Integer)
    travel_back_minutes: Mapped[int | None] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text)
    status: Mapped[OcdCallStatus] = mapped_column(
        Enum(OcdCallStatus, name="ocd_call_status"), default=OcdCallStatus.RECORDED
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    client_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )


class OvertimeAuthorization(Base):
    __tablename__ = "overtime_authorization"
    __table_args__ = (
        ForeignKeyConstraint(
            ["supersedes_id", "employee_id"],
            ["overtime_authorization.id", "overtime_authorization.employee_id"],
            name="fk_overtime_authorization_supersedes_same_owner",
        ),
        # The composite FK above needs this key as its target, and
        # PostgreSQL requires a unique constraint on the referenced columns.
        UniqueConstraint("id", "employee_id", name="uq_overtime_authorization_id_employee_id"),
        CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from", name="ck_overtime_auth_range_order"
        ),
        CheckConstraint("version >= 1", name="ck_overtime_auth_version_positive"),
        UniqueConstraint("supersedes_id", name="uq_overtime_authorization_supersedes_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employee.id"))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purpose: Mapped[str | None] = mapped_column(Text)
    status: Mapped[OvertimeAuthStatus] = mapped_column(
        Enum(OvertimeAuthStatus, name="overtime_auth_status"), default=OvertimeAuthStatus.REQUESTED
    )
    authorized_by: Mapped[str | None] = mapped_column(Text)
    source_ref: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, default="EMPLOYEE_RECORDED")
    version: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )


def is_authoritative_approval(row: OvertimeAuthorization) -> bool:
    """``EINV-23``/``ACCESS-017``: authority is a property of the WRITER.

    The single predicate every consumer that treats a row as an authorization
    must use — the travel calculation, the review-invalidation reopen and the
    submission preview. A legacy employee-self-declared ``APPROVED`` row is not
    authoritative anywhere: it neither seals a submission nor suppresses a
    compliance finding.
    """
    return row.status is OvertimeAuthStatus.APPROVED and row.source in AUTHORITATIVE_SOURCES


def is_employee_writable(row: OvertimeAuthorization) -> bool:
    """May the employee write path version this chain head?

    Deny by default: only a head the employee authored is employee-versionable,
    so an institutional decision cannot be corrected, withdrawn or cancelled
    through ``/api/v1/me/*``. Delegates to the one shared provenance predicate so
    the overtime, shift and OCD write paths cannot drift apart.
    """
    return is_employee_writable_source(row.source)


class CompensationElection(Base):
    __tablename__ = "compensation_election"
    __table_args__ = (
        ForeignKeyConstraint(
            ["supersedes_id", "employee_id"],
            ["compensation_election.id", "compensation_election.employee_id"],
            name="fk_compensation_election_supersedes_same_owner",
        ),
        # The composite FK above needs this key as its target, and
        # PostgreSQL requires a unique constraint on the referenced columns.
        UniqueConstraint("id", "employee_id", name="uq_compensation_election_id_employee_id"),
        CheckConstraint("version >= 1", name="ck_compensation_election_version_positive"),
        UniqueConstraint("supersedes_id", name="uq_compensation_election_supersedes_id"),
        # One ROOT per scope. ``_election`` resolves "the current row" with a
        # WHERE and no ORDER BY, so two roots would make the employee's
        # compensation election nondeterministic. A partial unique INDEX is
        # required because a UniqueConstraint cannot carry a predicate.
        Index(
            "uq_compensation_election_root",
            "employee_id",
            "context_type",
            "context_id",
            unique=True,
            postgresql_where=text("supersedes_id IS NULL"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employee.id"))
    context_type: Mapped[CompensationContextType] = mapped_column(
        Enum(CompensationContextType, name="compensation_context_type")
    )
    context_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    mode: Mapped[CompensationMode] = mapped_column(Enum(CompensationMode, name="compensation_mode"))
    selected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
