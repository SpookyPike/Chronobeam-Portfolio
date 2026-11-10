"""``day_status_event`` — the canonical employee-owned day-status fact.

Shape decisions (each replaces a column of the earlier imported-record design):

- **One row per local date; no ``start_date``/``end_date`` range.** The effect
  is per-day (calendar class, schedule and the half fraction all vary within a
  range), so every consumer would have to split it anyway; a range would add a
  second representation of the same fact and make "does correcting one day fork
  the 21-day range?" ambiguous.
- **No approval ``status``.** Recording a status must not imply institutional
  approval, and there is no approval actor. Approval belongs to
  ``external_hr_record`` when an external HR interface is actually confirmed.
- **No ``valid_from``/``valid_to`` timestamptz.** The repository has exactly one
  version mechanism — ``supersedes_id`` chains — and a second one would be a
  second answer to "which is current" (EINV-04).
- **``source`` reuses ``IntervalSource``** (EMPLOYEE | IMPORT) rather than
  duplicating an identical enum; ``work_interval.source`` is the precedent.
- **``external_category`` is the BE-ENUM-002 seam**, never exposed to the
  employee (HR-side provenance the employee never authored).
- **No comment/reason column.** It is where medical detail would leak; the
  ``correction`` row already carries the lineage reason.

``portion = HALF`` on ``SICK`` is refused at the *application* layer
(422 ``POLICY_BLOCKED`` naming ``ABSENCE-008``), not by a database CHECK: the
policy decision is blocked and must not be frozen into the schema, and a CHECK
would make its eventual resolution a migration on an employee-fact table. The
schema's ability to store a half day is a storage property, never a licence to
execute an unsourced calculation.
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
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.enums import DayStatusCategory, DayStatusDayPart, DayStatusPortion, IntervalSource


class DayStatusEvent(Base):
    __tablename__ = "day_status_event"
    __table_args__ = (
        ForeignKeyConstraint(
            ["supersedes_id", "employee_id"],
            ["day_status_event.id", "day_status_event.employee_id"],
            name="fk_day_status_event_supersedes_same_owner",
        ),
        # The composite FK above needs this key as its target, and
        # PostgreSQL requires a unique constraint on the referenced columns.
        UniqueConstraint("id", "employee_id", name="uq_day_status_event_id_employee_id"),
        CheckConstraint("version >= 1", name="ck_day_status_event_version_positive"),
        # ``day_part`` is required iff the portion is HALF, expressed in the
        # database rather than in prose.
        CheckConstraint(
            "(portion = 'FULL' AND day_part = 'ALL') OR (portion = 'HALF' AND day_part <> 'ALL')",
            name="ck_day_status_event_day_part",
        ),
        # At most one successor per row: a forked chain would have two current
        # heads, which the current-version predicate cannot express (EINV-03).
        UniqueConstraint("supersedes_id", name="uq_day_status_event_supersedes_id"),
        # BE-CMD-001 idempotency: a replayed create returns the existing row.
        UniqueConstraint("client_request_id", name="uq_day_status_event_client_request_id"),
        # EINV-04 root uniqueness. Honest limit: this cannot express convergence
        # across a *corrected* slot (a root corrected MORNING -> AFTERNOON lands
        # outside the partial index while the old AFTERNOON root may remain), so
        # the service layer asserts it too — see ``absence/service.py``.
        Index(
            "uq_day_status_event_root",
            "employee_id",
            "day",
            "category",
            "day_part",
            unique=True,
            postgresql_where=text("supersedes_id IS NULL"),
        ),
        Index("ix_day_status_event_employee_day", "employee_id", "day"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employee.id"))
    day: Mapped[date] = mapped_column(Date)
    category: Mapped[DayStatusCategory] = mapped_column(
        Enum(DayStatusCategory, name="day_status_category")
    )
    portion: Mapped[DayStatusPortion] = mapped_column(
        Enum(DayStatusPortion, name="day_status_portion")
    )
    day_part: Mapped[DayStatusDayPart] = mapped_column(
        Enum(DayStatusDayPart, name="day_status_day_part")
    )
    source: Mapped[IntervalSource] = mapped_column(
        Enum(IntervalSource, name="interval_source"), default=IntervalSource.EMPLOYEE
    )
    external_category: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    client_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
