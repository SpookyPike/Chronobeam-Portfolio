"""Identity/profile tables (WT-BACKEND §5.1).

employee, employment_contract, work_schedule_version, employee_preference,
identity_binding_change. Effective-dated master data (INV-07); only the dev seed
writes it so far (no master-data endpoints yet).
"""

import uuid
from datetime import date, datetime, time

from app.db import Base
from app.enums import MasterDataSource, PayGroupBucket, WorkType
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    Time,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class Employee(Base):
    __tablename__ = "employee"
    __table_args__ = (CheckConstraint("legal_name <> ''", name="ck_employee_legal_name_not_empty"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    external_employee_id: Mapped[str | None] = mapped_column(Text, unique=True)
    # Nullable until an institutional identity is mapped (no JIT
    # provisioning). Unique for rows that carry a subject; NULLs never
    # collide. active + unmapped = no capabilities (canonical 403).
    idp_subject: Mapped[str | None] = mapped_column(Text, unique=True)
    legal_name: Mapped[str] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )


class EmploymentContract(Base):
    __tablename__ = "employment_contract"
    __table_args__ = (
        CheckConstraint("weekly_required_minutes >= 0", name="ck_contract_weekly_minutes_nonneg"),
        CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from", name="ck_contract_range_order"
        ),
        CheckConstraint("version >= 1", name="ck_contract_version_positive"),
        # Same employee cannot start two contracts on one date.
        # Full range non-overlap (EXCLUDE USING gist + btree_gist) is deferred
        # to future MASTER_DATA_WRITE endpoints; the only writer today (dev seed)
        # guards overlap in code (assert_no_range_overlap).
        UniqueConstraint("employee_id", "valid_from", name="uq_contract_employee_valid_from"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employee.id"))
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    fte: Mapped[float | None] = mapped_column(Numeric(5, 4))
    weekly_required_minutes: Mapped[int] = mapped_column(Integer)
    pay_group_bucket: Mapped[PayGroupBucket] = mapped_column(
        Enum(PayGroupBucket, name="pay_group_bucket")
    )
    employment_start: Mapped[date | None] = mapped_column(Date)
    employment_end: Mapped[date | None] = mapped_column(Date)
    source: Mapped[MasterDataSource] = mapped_column(Enum(MasterDataSource, name="contract_source"))
    source_ref: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)


class WorkScheduleVersion(Base):
    __tablename__ = "work_schedule_version"
    __table_args__ = (
        CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from", name="ck_schedule_range_order"
        ),
        CheckConstraint("version >= 1", name="ck_schedule_version_positive"),
        # Employee-maintained weekly target and weekday pattern. The weekday
        # allocation must sum exactly to the
        # weekly target; each day is at most 24h (DATA-012).
        CheckConstraint("weekly_required_minutes >= 0", name="ck_schedule_weekly_nonneg"),
        CheckConstraint(
            "weekly_required_minutes = mon_required_minutes + tue_required_minutes"
            " + wed_required_minutes + thu_required_minutes + fri_required_minutes"
            " + sat_required_minutes + sun_required_minutes",
            name="ck_schedule_weekly_sum",
        ),
        CheckConstraint(
            "mon_required_minutes BETWEEN 0 AND 1440"
            " AND tue_required_minutes BETWEEN 0 AND 1440"
            " AND wed_required_minutes BETWEEN 0 AND 1440"
            " AND thu_required_minutes BETWEEN 0 AND 1440"
            " AND fri_required_minutes BETWEEN 0 AND 1440"
            " AND sat_required_minutes BETWEEN 0 AND 1440"
            " AND sun_required_minutes BETWEEN 0 AND 1440",
            name="ck_schedule_day_minutes_range",
        ),
        # Same rationale as employment_contract: one version per start date.
        UniqueConstraint("employee_id", "valid_from", name="uq_schedule_employee_valid_from"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employee.id"))
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    weekly_required_minutes: Mapped[int] = mapped_column(Integer)
    mon_required_minutes: Mapped[int] = mapped_column(Integer)
    tue_required_minutes: Mapped[int] = mapped_column(Integer)
    wed_required_minutes: Mapped[int] = mapped_column(Integer)
    thu_required_minutes: Mapped[int] = mapped_column(Integer)
    fri_required_minutes: Mapped[int] = mapped_column(Integer)
    sat_required_minutes: Mapped[int] = mapped_column(Integer, default=0)
    sun_required_minutes: Mapped[int] = mapped_column(Integer, default=0)
    mon_typical_start: Mapped[time | None] = mapped_column(Time)
    mon_typical_end: Mapped[time | None] = mapped_column(Time)
    tue_typical_start: Mapped[time | None] = mapped_column(Time)
    tue_typical_end: Mapped[time | None] = mapped_column(Time)
    wed_typical_start: Mapped[time | None] = mapped_column(Time)
    wed_typical_end: Mapped[time | None] = mapped_column(Time)
    thu_typical_start: Mapped[time | None] = mapped_column(Time)
    thu_typical_end: Mapped[time | None] = mapped_column(Time)
    fri_typical_start: Mapped[time | None] = mapped_column(Time)
    fri_typical_end: Mapped[time | None] = mapped_column(Time)
    sat_typical_start: Mapped[time | None] = mapped_column(Time)
    sat_typical_end: Mapped[time | None] = mapped_column(Time)
    sun_typical_start: Mapped[time | None] = mapped_column(Time)
    sun_typical_end: Mapped[time | None] = mapped_column(Time)
    source: Mapped[MasterDataSource] = mapped_column(Enum(MasterDataSource, name="schedule_source"))
    version: Mapped[int] = mapped_column(Integer)


class IdentityBindingChange(Base):
    """Provenance for an operator identity-binding change.

    The application API can set a binding only when creating an employee row;
    changing an existing binding is an operator CLI operation
    (``app.identity_admin``). ``employee.idp_subject`` alone cannot record what
    the binding was before, so a remap would otherwise be indistinguishable from
    an ordinary profile edit and the displaced subject unrecoverable.

    The reason lives here rather than in ``audit_event.metadata``: that table's
    own contract forbids comments/reasons in metadata, and every other reason in
    this codebase is stored with the change it justifies.

    ``btrim(reason) <> ''`` is deliberately stricter than
    ``ck_employee_legal_name_not_empty`` (``legal_name <> ''``): a whitespace-only
    value must not be storable as provenance.
    """

    __tablename__ = "identity_binding_change"
    __table_args__ = (
        CheckConstraint("btrim(reason) <> ''", name="ck_identity_binding_change_reason_not_blank"),
        # The provenance read path is "what happened to this employee's binding,
        # in order". Declared here so metadata and migration 0012 agree: a
        # metadata-only difference makes the next --autogenerate emit a drop.
        Index("ix_identity_binding_change_employee", "employee_id", "changed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employee.id"))
    # NULL = the employee was unmapped before this change.
    previous_subject: Mapped[str | None] = mapped_column(Text)
    # NULL = the binding was cleared by this change.
    new_subject: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    actor_principal: Mapped[str] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class EmployeePreference(Base):
    __tablename__ = "employee_preference"

    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employee.id"), primary_key=True)
    usual_work_type: Mapped[WorkType | None] = mapped_column(Enum(WorkType, name="work_type"))
    preferred_calendar_view: Mapped[str | None] = mapped_column(Text)
    # Employee convenience only. The command validates each use and
    # creates factual intervals only after an explicit action.
    normal_day_template_json: Mapped[object | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )
