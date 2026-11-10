"""Authorization state: role assignments.

The single persistent authorization table. Roles themselves are code-defined
bundles in ``app.auth.roles`` (never stored as free text); this table records
WHO holds WHICH role and the assignment history. Revocation is soft
(``revoked_at``), so deactivating or re-educating a user never destroys the
history that audit/INV-11 rely on. One ACTIVE assignment per (employee, role)
is enforced by the partial unique index in migration 0010.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RoleAssignment(Base):
    __tablename__ = "role_assignment"
    __table_args__ = (
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= granted_at",
            name="ck_role_assignment_revoke_order",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("employee.id"))
    role: Mapped[str] = mapped_column(Text)
    # The subject that granted this assignment (or BOOTSTRAP_CLI).
    granted_by_principal: Mapped[str] = mapped_column(Text)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
