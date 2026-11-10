"""Calendar versions and exceptional calendar-day facts."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.enums import CalendarDayClass

#: The one institutional calendar key. Defined next to the tables it keys and
#: imported by every reader/writer, so the key is not a literal in two modules.
CALENDAR_KEY = "primary"


class CalendarVersion(Base):
    """One published calendar edition: insert-only, effective-dated (EINV-21).

    Two identities with one meaning each, deliberately not collapsed:

    - ``uq_calendar_version_key (calendar_key, version)`` is the institutional
      *label* the operator recognises;
    - ``uq_calendar_version_effective_from (calendar_key, valid_from)`` is the
      *resolution* key, so "which edition applies to this day" is a data
      decision rather than a ``version``-string tiebreak (EINV-04/EINV-20).

    Overlapping ranges are legitimate and deliberate: resolution is "greatest
    ``valid_from`` covering the day wins", so a bounded *corrective overlay*
    takes precedence inside its governed range and the predecessor resumes
    outside it. A non-overlap prohibition would reject every legitimate
    year-boundary publication and destroy the only way to un-classify a day
    without mutating a published edition. ``valid_to`` is INCLUSIVE.
    """

    __tablename__ = "calendar_version"
    __table_args__ = (
        UniqueConstraint("calendar_key", "version", name="uq_calendar_version_key"),
        CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from", name="ck_calendar_version_range"
        ),
        Index("uq_calendar_version_effective_from", "calendar_key", "valid_from", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    calendar_key: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(Text)
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    # Provenance of the day set (e.g. "SH_STATUTORY_2026 + EMPLOYER_FREE_2026").
    # Required: an edition that cannot name its source is not trustworthy data.
    source: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class CalendarDay(Base):
    __tablename__ = "calendar_day"
    __table_args__ = (
        UniqueConstraint("calendar_version_id", "day", name="uq_calendar_day_version"),
        # The resolution query filters ``calendar_day.day`` across the selected
        # editions; the only pre-existing access path was the un-indexed FK.
        Index("ix_calendar_day_day", "day"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    calendar_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calendar_version.id"))
    day: Mapped[date] = mapped_column(Date)
    day_class: Mapped[CalendarDayClass] = mapped_column(
        Enum(CalendarDayClass, name="calendar_day_class")
    )
