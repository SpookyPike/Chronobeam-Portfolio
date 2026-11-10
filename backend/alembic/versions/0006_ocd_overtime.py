"""PHASE-6 OCD/overtime facts and work-interval links."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_ocd_overtime"
down_revision: str | None = "0005_employee_working_time"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ocd_assignment",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True)),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text()),
        sa.Column(
            "status",
            sa.Enum("SCHEDULED", "CANCELLED", "SUPERSEDED", name="ocd_assignment_status"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("end_at > start_at", name="ck_ocd_assignment_end_after_start"),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employee.id"], name="fk_ocd_assignment_employee"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "ocd_call",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("ocd_assignment_id", sa.Uuid()),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("call_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("call_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mode", sa.Enum("REMOTE", "ON_SITE", name="ocd_call_mode"), nullable=False),
        sa.Column("travel_out_minutes", sa.Integer()),
        sa.Column("travel_back_minutes", sa.Integer()),
        sa.Column("comment", sa.Text()),
        sa.Column(
            "status",
            sa.Enum("RECORDED", "VOIDED", name="ocd_call_status"),
            server_default="RECORDED",
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("supersedes_id", sa.Uuid()),
        sa.Column("client_request_id", sa.Uuid()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("call_end_at > call_start_at", name="ck_ocd_call_end_after_start"),
        sa.CheckConstraint(
            "travel_out_minutes IS NULL OR travel_out_minutes >= 0",
            name="ck_ocd_call_travel_out_nonneg",
        ),
        sa.CheckConstraint(
            "travel_back_minutes IS NULL OR travel_back_minutes >= 0",
            name="ck_ocd_call_travel_back_nonneg",
        ),
        sa.CheckConstraint("version >= 1", name="ck_ocd_call_version_positive"),
        sa.ForeignKeyConstraint(
            ["ocd_assignment_id"], ["ocd_assignment.id"], name="fk_ocd_call_assignment"
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"], name="fk_ocd_call_employee"),
        sa.ForeignKeyConstraint(["supersedes_id"], ["ocd_call.id"], name="fk_ocd_call_supersedes"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_request_id", name="uq_ocd_call_client_request_id"),
    )
    op.create_table(
        "overtime_authorization",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
        sa.Column("purpose", sa.Text()),
        sa.Column(
            "status",
            sa.Enum("REQUESTED", "APPROVED", "REJECTED", "CANCELLED", name="overtime_auth_status"),
            server_default="REQUESTED",
            nullable=False,
        ),
        sa.Column("authorized_by", sa.Text()),
        sa.Column("source_ref", sa.Text()),
        sa.Column("source", sa.Text(), server_default="EMPLOYEE_RECORDED", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("supersedes_id", sa.Uuid()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from", name="ck_overtime_auth_range_order"
        ),
        sa.CheckConstraint("version >= 1", name="ck_overtime_auth_version_positive"),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"], name="fk_overtime_auth_employee"),
        sa.ForeignKeyConstraint(
            ["supersedes_id"], ["overtime_authorization.id"], name="fk_overtime_auth_supersedes"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "compensation_election",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column(
            "context_type",
            sa.Enum("OVERTIME", "SHIFT", "OCD", name="compensation_context_type"),
            nullable=False,
        ),
        sa.Column("context_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.Enum("PAY", "TIME", name="compensation_mode"), nullable=False),
        sa.Column(
            "selected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("supersedes_id", sa.Uuid()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version >= 1", name="ck_compensation_election_version_positive"),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employee.id"], name="fk_compensation_election_employee"
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["compensation_election.id"],
            name="fk_compensation_election_supersedes",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.add_column("work_interval", sa.Column("overtime_authorization_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_work_interval_overtime_authorization",
        "work_interval",
        "overtime_authorization",
        ["overtime_authorization_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_work_interval_ocd_call", "work_interval", "ocd_call", ["linked_ocd_call_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_work_interval_ocd_call", "work_interval", type_="foreignkey")
    op.drop_constraint(
        "fk_work_interval_overtime_authorization", "work_interval", type_="foreignkey"
    )
    op.drop_column("work_interval", "overtime_authorization_id")
    op.drop_table("compensation_election")
    _drop("compensation_mode")
    _drop("compensation_context_type")
    op.drop_table("overtime_authorization")
    _drop("overtime_auth_status")
    op.drop_table("ocd_call")
    _drop("ocd_call_status")
    _drop("ocd_call_mode")
    op.drop_table("ocd_assignment")
    _drop("ocd_assignment_status")


def _drop(name: str) -> None:
    postgresql.ENUM(name=name).drop(op.get_bind(), checkfirst=True)
