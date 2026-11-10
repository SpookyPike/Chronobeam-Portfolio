"""PHASE-7 disclosure tables.

FK order: access_request -> disclosure_grant -> submission_package ->
submission_recipient. All timestamps timestamptz (docs/ARCHITECTURE.md). The
submission_purpose enum is shared by request/grant/package (BE-ENUM-004).
Grants reference a frozen period_calculation_snapshot (D8); packages carry
the immutable payload_json and a unique client_request_id for idempotent
replay (D9).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_disclosure"
down_revision: str | None = "0006_ocd_overtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "access_request",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("requester_principal", sa.Text(), nullable=False),
        sa.Column(
            "purpose",
            sa.Enum(
                "SHIFT_REPORT",
                "OVERTIME_PAID",
                "OVERTIME_TIME_COMP",
                "OCD_COMPENSATION",
                "WORK_TIME_RECORD",
                "RANDOM_CHECK",
                name="submission_purpose",
            ),
            nullable=False,
        ),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("view_type", sa.Text(), nullable=False),
        sa.Column("message", sa.Text()),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING", "GRANTED", "DECLINED", "CLOSED", "EXPIRED", name="access_request_status"
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("period_end >= period_start", name="ck_access_request_period_order"),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employee.id"], name="fk_access_request_employee"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "disclosure_grant",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("access_request_id", sa.Uuid()),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("grantee_principal", sa.Text(), nullable=False),
        sa.Column(
            "purpose",
            sa.Enum(
                "SHIFT_REPORT",
                "OVERTIME_PAID",
                "OVERTIME_TIME_COMP",
                "OCD_COMPENSATION",
                "WORK_TIME_RECORD",
                "RANDOM_CHECK",
                name="submission_purpose",
            ),
            nullable=False,
        ),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("view_type", sa.Text(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid()),
        sa.Column("supersedes_id", sa.Uuid()),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # ACCESS-014 PROVISIONAL: expires_at stays NULL until a confirmed
        # retention interpretation exists (no guessed expiry).
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("period_end >= period_start", name="ck_disclosure_grant_period_order"),
        sa.ForeignKeyConstraint(
            ["access_request_id"], ["access_request.id"], name="fk_disclosure_grant_access_request"
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employee.id"], name="fk_disclosure_grant_employee"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["period_calculation_snapshot.id"],
            name="fk_disclosure_grant_snapshot",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"], ["disclosure_grant.id"], name="fk_disclosure_grant_supersedes"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_disclosure_grant_supersedes_id", "disclosure_grant", ["supersedes_id"])
    op.create_table(
        "submission_package",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column(
            "purpose",
            sa.Enum(
                "SHIFT_REPORT",
                "OVERTIME_PAID",
                "OVERTIME_TIME_COMP",
                "OCD_COMPENSATION",
                "WORK_TIME_RECORD",
                "RANDOM_CHECK",
                name="submission_purpose",
            ),
            nullable=False,
        ),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("supersedes_id", sa.Uuid()),
        sa.Column("policy_version_id", sa.Uuid()),
        sa.Column("snapshot_id", sa.Uuid()),
        # Immutable minimized projection; the payload IS the frozen snapshot
        # the recipient reads (D9) — never mutated after creation.
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("employee_confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("client_request_id", sa.Uuid()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("SUBMITTED", "SUPERSEDED", "WITHDRAWN", name="submission_package_status"),
            nullable=False,
        ),
        sa.CheckConstraint("version >= 1", name="ck_submission_package_version_positive"),
        sa.CheckConstraint("period_end >= period_start", name="ck_submission_package_period_order"),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employee.id"], name="fk_submission_package_employee"
        ),
        sa.ForeignKeyConstraint(
            ["policy_version_id"],
            ["policy_version.id"],
            name="fk_submission_package_policy_version",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["period_calculation_snapshot.id"],
            name="fk_submission_package_snapshot",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"], ["submission_package.id"], name="fk_submission_package_supersedes"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_request_id", name="uq_submission_package_client_request_id"),
    )
    op.create_index(
        "ix_submission_package_client_request_id", "submission_package", ["client_request_id"]
    )
    op.create_index("ix_submission_package_supersedes_id", "submission_package", ["supersedes_id"])
    op.create_table(
        "submission_recipient",
        sa.Column("submission_package_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_type", sa.Text(), nullable=False),
        sa.Column("recipient_principal", sa.Text(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["submission_package_id"],
            ["submission_package.id"],
            name="fk_submission_recipient_package",
        ),
        sa.PrimaryKeyConstraint("submission_package_id", "recipient_principal"),
    )


def downgrade() -> None:
    op.drop_table("submission_recipient")
    op.drop_index("ix_submission_package_supersedes_id", table_name="submission_package")
    op.drop_index("ix_submission_package_client_request_id", table_name="submission_package")
    op.drop_table("submission_package")
    op.drop_index("ix_disclosure_grant_supersedes_id", table_name="disclosure_grant")
    op.drop_table("disclosure_grant")
    op.drop_table("access_request")
    for name in ("submission_purpose", "access_request_status", "submission_package_status"):
        postgresql.ENUM(name=name).drop(op.get_bind(), checkfirst=True)
