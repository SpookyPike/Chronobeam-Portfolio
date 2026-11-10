"""Shift plan facts and employee-owned actual/deviation facts (BE §5.4).

Planned schedules are never rewritten into actual work.  Actual work remains
``work_interval`` rows linked by ``linked_shift_assignment_id``; a deviation
only supplies the structured plan-vs-actual context (SHIFT-001 / INV-09).
"""

import uuid
from datetime import date, datetime

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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.enums import ShiftAssignmentStatus, ShiftDeviationType


class ShiftPlan(Base):
    __tablename__ = "shift_plan"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    plan_key: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(Text)
    source_ref: Mapped[str | None] = mapped_column(Text)


class ShiftAssignment(Base):
    __tablename__ = "shift_assignment"
    __table_args__ = (
        # Idempotency key for the employee create command (BE-CMD-001). The key is
        # unique TABLE-WIDE, not per employee: a client-generated command id
        # identifies one command, so two employees cannot both own it. That is why
        # the replay lookup is global and why the write path must handle the
        # cross-employee collision on this exact constraint name (see
        # ``shift/service.py`` — the name is compared, never guessed).
        UniqueConstraint("client_request_id", name="uq_shift_assignment_client_request_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    shift_plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shift_plan.id"))
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employee.id"))
    planned_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    planned_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    shift_kind: Mapped[str | None] = mapped_column(Text)
    approved_revision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    status: Mapped[ShiftAssignmentStatus] = mapped_column(
        Enum(ShiftAssignmentStatus, name="shift_assignment_status")
    )
    client_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)


class ShiftPlanRevision(Base):
    __tablename__ = "shift_plan_revision"
    __table_args__ = (
        UniqueConstraint("shift_assignment_id", "revision_no", name="uq_shift_revision_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    shift_assignment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shift_assignment.id"))
    revision_no: Mapped[int] = mapped_column(Integer)
    planned_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    planned_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    communicated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_maker_type: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)


class ShiftDeviation(Base):
    __tablename__ = "shift_deviation"
    __table_args__ = (
        ForeignKeyConstraint(
            ["supersedes_id", "employee_id"],
            ["shift_deviation.id", "shift_deviation.employee_id"],
            name="fk_shift_deviation_supersedes_same_owner",
        ),
        # The composite FK above needs this key as its target, and
        # PostgreSQL requires a unique constraint on the referenced columns.
        UniqueConstraint("id", "employee_id", name="uq_shift_deviation_id_employee_id"),
        CheckConstraint(
            "(actual_start_at IS NULL AND actual_end_at IS NULL) OR "
            "(actual_start_at IS NOT NULL AND actual_end_at IS NOT NULL "
            "AND actual_end_at > actual_start_at)",
            name="ck_shift_deviation_actual_order",
        ),
        UniqueConstraint("supersedes_id", name="uq_shift_deviation_supersedes_id"),
        # One ROOT per shift assignment: ``save_deviation`` and ``confirm_shift``
        # both resolve "the current deviation" with a WHERE and no ORDER BY, so
        # two roots would make the deviation findings nondeterministic.
        Index(
            "uq_shift_deviation_root",
            "shift_assignment_id",
            unique=True,
            postgresql_where=text("supersedes_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employee.id"))
    shift_assignment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shift_assignment.id"))
    actual_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deviation_type: Mapped[ShiftDeviationType] = mapped_column(
        Enum(ShiftDeviationType, name="shift_deviation_type")
    )
    noticed_or_decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_maker_type: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    source_work_interval_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    version: Mapped[int] = mapped_column(Integer)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
