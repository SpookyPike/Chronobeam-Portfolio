"""Explicit factual breaks (WT-BACKEND §5.3 ``break_interval``).

An explicit break is for the cases a gap between intervals cannot express: a
break *inside* one factual interval (typically a shift), which the span would
otherwise count as worked time. Ordinary gaps remain inferred, so employees are
never asked to enter lunch twice.

The table is the ELEVENTH version-chain table, so it carries the same storage
invariants as the other ten (EINV-03/EINV-04/EINV-26): ``UNIQUE(supersedes_id)``
so a chain cannot fork, ``UNIQUE(id, employee_id)`` as the composite-FK target,
and the composite ``(supersedes_id, employee_id)`` FK so a chain never crosses
employees. No pre-flight is needed: the table is new, so there is no existing
state that could already violate those invariants.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_break_interval"
down_revision: str | None = "0017_version_chain_same_owner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

break_kind = sa.Enum("UNPAID", "PAID_SHIFT_BREAK", "OTHER", name="break_kind")


def upgrade() -> None:
    op.create_table(
        "break_interval",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_period_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", break_kind, nullable=False),
        sa.Column("linked_shift_assignment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("supersedes_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("client_request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("end_at > start_at", name="ck_break_interval_end_after_start"),
        sa.CheckConstraint("version >= 1", name="ck_break_interval_version_positive"),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"]),
        sa.ForeignKeyConstraint(["work_period_id"], ["work_period.id"]),
        # EINV-26: the chain is scoped to its owner, not merely to the row.
        sa.ForeignKeyConstraint(
            ["supersedes_id", "employee_id"],
            ["break_interval.id", "break_interval.employee_id"],
            name="fk_break_interval_supersedes_same_owner",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "employee_id", name="uq_break_interval_id_employee_id"),
        sa.UniqueConstraint("client_request_id", name="uq_break_interval_client_request_id"),
        sa.UniqueConstraint("supersedes_id", name="uq_break_interval_supersedes_id"),
    )
    op.create_index(
        "ix_break_interval_employee_start", "break_interval", ["employee_id", "start_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_break_interval_employee_start", table_name="break_interval")
    op.drop_table("break_interval")
    break_kind.drop(op.get_bind(), checkfirst=True)
