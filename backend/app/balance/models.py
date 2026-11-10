"""Balance ledger entries (WT-FLEX-002).

(BE spec §5.8): ``metadata_json`` carries the provenance of the
one-time controlled ``CUTOVER_SEED`` opening-balance row
(previous_system/previous_period/confirmed_by). It is NULL for ordinary
entries; the partial unique index on ``(employee_id) WHERE
entry_type = 'CUTOVER_SEED'`` (migration 0008) is the authoritative
duplicate-seed guard.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class BalanceLedgerEntry(Base):
    __tablename__ = "balance_ledger_entry"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employee.id"))
    work_period_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("work_period.id"))
    occurred_on: Mapped[date] = mapped_column(Date)
    entry_type: Mapped[str] = mapped_column(Text)
    minutes: Mapped[int] = mapped_column(Integer)
    source_entity_type: Mapped[str] = mapped_column(Text)
    source_entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    policy_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("policy_version.id"))
    metadata_json: Mapped[dict | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
