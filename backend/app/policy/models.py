"""Policy version and calculation snapshot persistence."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
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
from app.enums import PolicyVersionStatus


class PolicyVersion(Base):
    __tablename__ = "policy_version"
    __table_args__ = (
        UniqueConstraint("policy_key", "version", name="uq_policy_version_key"),
        # Edition selection is `ORDER BY effective_from DESC, version DESC`, so a
        # shared effective date would let string ordering decide which edition
        # applies (F4 D7). One edition per key per effective date.
        Index("uq_policy_version_effective_from", "policy_key", "effective_from", unique=True),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_policy_version_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    policy_key: Mapped[str] = mapped_column(Text)
    version: Mapped[str] = mapped_column(Text)
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    status: Mapped[PolicyVersionStatus] = mapped_column(
        Enum(PolicyVersionStatus, name="policy_version_status")
    )
    engine_rule_set: Mapped[str] = mapped_column(Text)
    source_manifest_hash: Mapped[str] = mapped_column(Text)


class PeriodCalculationSnapshot(Base):
    __tablename__ = "period_calculation_snapshot"
    __table_args__ = (
        UniqueConstraint("work_period_id", "snapshot_no", name="uq_period_snapshot_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    work_period_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("work_period.id"))
    snapshot_no: Mapped[int] = mapped_column(Integer)
    policy_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("policy_version.id"))
    # Ordered selected calendar-version IDs keep a frozen result reproducible
    # when a calendar changes within its period (INV-11 / ARCH §8.3).
    calendar_version_ids: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    engine_build: Mapped[str] = mapped_column(Text)
    input_fingerprint: Mapped[str] = mapped_column(Text)
    result_json: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    frozen: Mapped[bool] = mapped_column(Boolean, default=False)
