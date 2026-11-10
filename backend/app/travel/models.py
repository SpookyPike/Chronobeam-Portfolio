"""Travel facts for TRAVEL-001/002 (employee-private by default)."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
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
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TripStartOrigin(StrEnum):
    HOME = "HOME"
    WORKPLACE = "WORKPLACE"


class TravelSegmentClassification(StrEnum):
    PASSIVE_TRAVEL = "PASSIVE_TRAVEL"
    ACTIVE_WORK = "ACTIVE_WORK"
    DRIVING = "DRIVING"


class TravelSegmentBasis(StrEnum):
    ACTUAL = "ACTUAL"
    FICTITIOUS_BUSINESS_ROUTE = "FICTITIOUS_BUSINESS_ROUTE"


class BusinessTrip(Base):
    __tablename__ = "business_trip"
    __table_args__ = (
        ForeignKeyConstraint(
            ["supersedes_id", "employee_id"],
            ["business_trip.id", "business_trip.employee_id"],
            name="fk_business_trip_supersedes_same_owner",
        ),
        # The composite FK above needs this key as its target, and
        # PostgreSQL requires a unique constraint on the referenced columns.
        UniqueConstraint("id", "employee_id", name="uq_business_trip_id_employee_id"),
        CheckConstraint("end_at > start_at", name="ck_business_trip_end_after_start"),
        CheckConstraint("version >= 1", name="ck_business_trip_version_positive"),
        UniqueConstraint("client_request_id", name="uq_business_trip_client_request_id"),
        UniqueConstraint("supersedes_id", name="uq_business_trip_supersedes_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employee.id"))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    start_origin: Mapped[TripStartOrigin] = mapped_column(
        Enum(TripStartOrigin, name="trip_start_origin")
    )
    source: Mapped[str] = mapped_column(Text, default="EMPLOYEE_DECLARED")
    reference_label: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    client_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )


class TravelSegment(Base):
    __tablename__ = "travel_segment"
    __table_args__ = (
        ForeignKeyConstraint(
            ["supersedes_id", "trip_id"],
            ["travel_segment.id", "travel_segment.trip_id"],
            name="fk_travel_segment_supersedes_same_trip",
        ),
        # The composite FK above needs this key as its target, and
        # PostgreSQL requires a unique constraint on the referenced columns.
        UniqueConstraint("id", "trip_id", name="uq_travel_segment_id_trip_id"),
        CheckConstraint("end_at > start_at", name="ck_travel_segment_end_after_start"),
        CheckConstraint("version >= 1", name="ck_travel_segment_version_positive"),
        UniqueConstraint("work_interval_id", name="uq_travel_segment_work_interval"),
        UniqueConstraint("client_request_id", name="uq_travel_segment_client_request_id"),
        UniqueConstraint("supersedes_id", name="uq_travel_segment_supersedes_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    trip_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("business_trip.id"))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    classification: Mapped[TravelSegmentClassification] = mapped_column(
        Enum(TravelSegmentClassification, name="travel_segment_classification")
    )
    basis: Mapped[TravelSegmentBasis] = mapped_column(
        Enum(TravelSegmentBasis, name="travel_segment_basis")
    )
    work_interval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("work_interval.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    client_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )
