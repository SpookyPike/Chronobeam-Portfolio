"""PHASE-5 shift plan, confirmation, deviation, and private report-preview facts.

Requirement ID: SHIFT-001..003, BE §5.4, PHASE-5.
Concrete problem solved: retain approved shift plans/revisions independently
from employee-confirmed actual work and structured deviations (INV-09).
Simpler alternative considered: add planned timestamps or a deviation flag to
``work_interval``.
Why insufficient: it would overwrite plan history, make revisions
irreproducible, and collapse plan/actual/deviation into one mutable fact.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_shift_workflow"
down_revision: str | None = "0003_calendar_policy_balance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shift_plan",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("plan_key", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "shift_assignment",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("shift_plan_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("planned_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("shift_kind", sa.Text(), nullable=True),
        sa.Column("approved_revision_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("PLANNED", "CANCELLED", "SUPERSEDED", name="shift_assignment_status"),
            nullable=False,
        ),
        sa.CheckConstraint("planned_end_at > planned_start_at", name="ck_shift_assignment_order"),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employee.id"], name="fk_shift_assignment_employee"
        ),
        sa.ForeignKeyConstraint(
            ["shift_plan_id"], ["shift_plan.id"], name="fk_shift_assignment_plan"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "shift_plan_revision",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("shift_assignment_id", sa.Uuid(), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("planned_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("communicated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_maker_type", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.CheckConstraint("planned_end_at > planned_start_at", name="ck_shift_revision_order"),
        sa.ForeignKeyConstraint(
            ["shift_assignment_id"], ["shift_assignment.id"], name="fk_shift_revision_assignment"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("shift_assignment_id", "revision_no", name="uq_shift_revision_number"),
    )
    op.create_foreign_key(
        "fk_shift_assignment_approved_revision",
        "shift_assignment",
        "shift_plan_revision",
        ["approved_revision_id"],
        ["id"],
    )
    op.create_table(
        "shift_deviation",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("shift_assignment_id", sa.Uuid(), nullable=False),
        sa.Column("actual_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "deviation_type",
            sa.Enum(
                "CANCELLED",
                "RESCHEDULED",
                "ADDITIONAL",
                "SWAPPED",
                "SHORTENED",
                "OVERRUN",
                "OVERRUN_GT_10H",
                "MINOR_CHANGE",
                "CANCELLED_SICKNESS",
                name="shift_deviation_type",
            ),
            nullable=False,
        ),
        sa.Column("noticed_or_decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_maker_type", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "source_work_interval_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "(actual_start_at IS NULL AND actual_end_at IS NULL) OR "
            "(actual_start_at IS NOT NULL AND actual_end_at IS NOT NULL "
            "AND actual_end_at > actual_start_at)",
            name="ck_shift_deviation_actual_order",
        ),
        sa.CheckConstraint("version >= 1", name="ck_shift_deviation_version_positive"),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employee.id"], name="fk_shift_deviation_employee"
        ),
        sa.ForeignKeyConstraint(
            ["shift_assignment_id"], ["shift_assignment.id"], name="fk_shift_deviation_assignment"
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"], ["shift_deviation.id"], name="fk_shift_deviation_supersedes"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_foreign_key(
        "fk_work_interval_shift_assignment",
        "work_interval",
        "shift_assignment",
        ["linked_shift_assignment_id"],
        ["id"],
    )
    op.execute(
        sa.text(
            """
            INSERT INTO policy_version
              (policy_key, version, effective_from, effective_to, status,
               engine_rule_set, source_manifest_hash)
            VALUES
              ('shift-work', '2026-06-shadow-v1', DATE '2026-06-01', NULL,
               'PROVISIONAL', 'phase-5-shift-shadow-v1',
               'e45fb742f9f2aaa53b8e1e6ad0538ba1a631f9adb02ab717608815d5cd850b9a')
            """
        )
    )


def downgrade() -> None:
    op.drop_constraint("fk_work_interval_shift_assignment", "work_interval", type_="foreignkey")
    op.drop_table("shift_deviation")
    _drop_enum("shift_deviation_type")
    op.drop_constraint(
        "fk_shift_assignment_approved_revision", "shift_assignment", type_="foreignkey"
    )
    op.drop_table("shift_plan_revision")
    op.drop_table("shift_assignment")
    _drop_enum("shift_assignment_status")
    op.drop_table("shift_plan")
    op.execute(sa.text("DELETE FROM policy_version WHERE policy_key = 'shift-work'"))


def _drop_enum(name: str) -> None:
    postgresql.ENUM(name=name).drop(op.get_bind(), checkfirst=True)
