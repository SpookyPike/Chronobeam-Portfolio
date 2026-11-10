"""Actual work: periods, intervals, corrections (WT-BACKEND §5.3).

- ``work_period``: one row per employee/calendar month; ``CLOSED`` never
  grants HR access (BE §5.3:281).
- ``work_interval``: 0-N per day, mixed work types, cross-midnight allowed;
  optimistic ``version`` + ``supersedes_id`` chain (with a UNIQUE constraint on
  the successor link, so a chain cannot fork); ``end_at > start_at`` at the
  storage level. Overlap is DOMAIN validation (API) executed under the shared
  per-employee fact lock, not a DB exclusion constraint (BE §5.3:303): an
  ``EXCLUDE`` constraint cannot express "current only", because a correction
  leaves its predecessor in place and overlapping.
- ``correction``: lineage/audit helper (old version -> new version), not
  event sourcing (BE §5.3:337).
"""

import uuid
from datetime import date, datetime

from app.db import Base
from app.enums import BreakKind, IntervalSource, PeriodStatus, WorkType
from sqlalchemy import (
    CheckConstraint,
    Date,
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


class WorkPeriod(Base):
    __tablename__ = "work_period"
    __table_args__ = (
        # One row per employee/calendar month (BE §5.3).
        UniqueConstraint("employee_id", "period_month", name="uq_work_period_employee_month"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employee.id"))
    period_month: Mapped[date] = mapped_column(Date)
    status: Mapped[PeriodStatus] = mapped_column(
        Enum(PeriodStatus, name="work_period_status"), default=PeriodStatus.OPEN
    )
    calculation_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )


class WorkInterval(Base):
    __tablename__ = "work_interval"
    __table_args__ = (
        ForeignKeyConstraint(
            ["supersedes_id", "employee_id"],
            ["work_interval.id", "work_interval.employee_id"],
            name="fk_work_interval_supersedes_same_owner",
        ),
        # The composite FK above needs this key as its target, and
        # PostgreSQL requires a unique constraint on the referenced columns.
        UniqueConstraint("id", "employee_id", name="uq_work_interval_id_employee_id"),
        CheckConstraint("end_at > start_at", name="ck_work_interval_end_after_start"),
        CheckConstraint("version >= 1", name="ck_work_interval_version_positive"),
        # BE-CMD-001 idempotency: client_request_id is unique where present
        # (replay returns the existing interval).
        UniqueConstraint("client_request_id", name="uq_work_interval_client_request_id"),
        # At most one successor per version: a forked chain would have two
        # "current" heads, which the current-version predicate cannot express.
        UniqueConstraint("supersedes_id", name="uq_work_interval_supersedes_id"),
        Index("ix_work_interval_employee_start", "employee_id", "start_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employee.id"))
    work_period_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("work_period.id"))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    work_type: Mapped[WorkType] = mapped_column(Enum(WorkType, name="work_type"))
    comment: Mapped[str | None] = mapped_column(Text)
    linked_shift_assignment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    linked_ocd_call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # Employee-recorded overtime authorization reference (ALLOW-001/002,
    # ACCESS-006). Ownership/integrity is validated in the service layer; the FK
    # constraint itself lives in migration 0006 (fk_work_interval_overtime_authorization).
    overtime_authorization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    source: Mapped[IntervalSource] = mapped_column(
        Enum(IntervalSource, name="interval_source"), default=IntervalSource.EMPLOYEE
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    client_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # actor ref (BE §5.3)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )


class BreakInterval(Base):
    """An explicit factual break (WT-BACKEND §5.3).

    Only for a break whose semantics a gap between intervals cannot express —
    primarily a break *inside* one factual interval (a shift). Ordinary gaps
    remain inferred; employees are never asked to enter lunch twice. A
    ``PAID_SHIFT_BREAK`` is compensated presence (SHIFT-003) and is NOT deducted
    from worked time; ``UNPAID`` is. The version chain matches every other
    factual table (EINV-03/EINV-04, EINV-26): one successor per version, scoped
    to the same employee.
    """

    __tablename__ = "break_interval"
    __table_args__ = (
        ForeignKeyConstraint(
            ["supersedes_id", "employee_id"],
            ["break_interval.id", "break_interval.employee_id"],
            name="fk_break_interval_supersedes_same_owner",
        ),
        UniqueConstraint("id", "employee_id", name="uq_break_interval_id_employee_id"),
        CheckConstraint("end_at > start_at", name="ck_break_interval_end_after_start"),
        CheckConstraint("version >= 1", name="ck_break_interval_version_positive"),
        UniqueConstraint("client_request_id", name="uq_break_interval_client_request_id"),
        UniqueConstraint("supersedes_id", name="uq_break_interval_supersedes_id"),
        Index("ix_break_interval_employee_start", "employee_id", "start_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employee.id"))
    work_period_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("work_period.id"))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    kind: Mapped[BreakKind] = mapped_column(Enum(BreakKind, name="break_kind"))
    # The shift assignment this break belongs to, where one exists. Ownership
    # and existence are validated in the service layer.
    linked_shift_assignment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    version: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    client_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )


class Correction(Base):
    __tablename__ = "correction"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    old_version: Mapped[int | None] = mapped_column(Integer)
    new_version: Mapped[int | None] = mapped_column(Integer)
    actor_employee_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    actor_role: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
