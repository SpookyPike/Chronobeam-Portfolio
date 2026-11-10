"""PHASE-2 calendar, code-backed policy, balance, and snapshot tables.

Requirement ID: WT-FLEX-001/002, BE-POL-001/002, ARCH §8.3.
Concrete problem solved: persist version references and authoritative opening/
settlement ledger facts without storing editable derived totals.
Simpler alternative considered: calculate from ad-hoc constants and a manual
previous-month field.
Why insufficient: it cannot reproduce the selected calendar/policy state or
provide ledger-derived continuity (INV-07/08/11).
"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_calendar_policy_balance"
down_revision: str | None = "0002_identity_worktime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENUM_TYPES = {
    "calendar_day_class": ("PUBLIC_HOLIDAY", "EMPLOYER_FREE"),
    "policy_version_status": ("ACTIVE", "PROVISIONAL", "RETIRED", "BLOCKED"),
}


def upgrade() -> None:
    op.create_table(
        "calendar_version",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("calendar_key", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("calendar_key", "version", name="uq_calendar_version_key"),
    )
    op.create_table(
        "calendar_day",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("calendar_version_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column(
            "day_class",
            sa.Enum("PUBLIC_HOLIDAY", "EMPLOYER_FREE", name="calendar_day_class"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["calendar_version_id"], ["calendar_version.id"], name="fk_calendar_day_version"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("calendar_version_id", "day", name="uq_calendar_day_version"),
    )
    op.create_table(
        "policy_version",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("policy_key", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "PROVISIONAL", "RETIRED", "BLOCKED", name="policy_version_status"),
            nullable=False,
        ),
        sa.Column("engine_rule_set", sa.Text(), nullable=False),
        sa.Column("source_manifest_hash", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("policy_key", "version", name="uq_policy_version_key"),
    )
    op.create_table(
        "balance_ledger_entry",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("work_period_id", sa.Uuid(), nullable=True),
        sa.Column("occurred_on", sa.Date(), nullable=False),
        sa.Column("entry_type", sa.Text(), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.Column("source_entity_type", sa.Text(), nullable=False),
        sa.Column("source_entity_id", sa.Uuid(), nullable=True),
        sa.Column("policy_version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"], name="fk_ledger_employee"),
        sa.ForeignKeyConstraint(
            ["policy_version_id"], ["policy_version.id"], name="fk_ledger_policy_version"
        ),
        sa.ForeignKeyConstraint(
            ["work_period_id"], ["work_period.id"], name="fk_ledger_work_period"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "period_calculation_snapshot",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("work_period_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_no", sa.Integer(), nullable=False),
        sa.Column("policy_version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "calendar_version_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("engine_build", sa.Text(), nullable=False),
        sa.Column("input_fingerprint", sa.Text(), nullable=False),
        sa.Column("result_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("frozen", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["policy_version_id"], ["policy_version.id"], name="fk_snapshot_policy_version"
        ),
        sa.ForeignKeyConstraint(
            ["work_period_id"], ["work_period.id"], name="fk_snapshot_work_period"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("work_period_id", "snapshot_no", name="uq_period_snapshot_number"),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO policy_version
              (policy_key, version, effective_from, effective_to, status,
               engine_rule_set, source_manifest_hash)
            VALUES
              ('ordinary-working-time', '2026-06-ordinary-v1', :effective_from, NULL,
               'PROVISIONAL', 'phase-2-ordinary-v1', :source_hash)
            """
        ).bindparams(
            effective_from=date(2026, 6, 1),
            source_hash="e45fb742f9f2aaa53b8e1e6ad0538ba1a631f9adb02ab717608815d5cd850b9a",
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO calendar_version (calendar_key, version, valid_from, valid_to)
            VALUES ('primary', 'default-v1', :effective_from, NULL)
            """
        ).bindparams(effective_from=date(2026, 1, 1))
    )


def downgrade() -> None:
    op.drop_table("period_calculation_snapshot")
    op.drop_table("balance_ledger_entry")
    op.drop_table("policy_version")
    _drop_enum("policy_version_status")
    op.drop_table("calendar_day")
    _drop_enum("calendar_day_class")
    op.drop_table("calendar_version")


def _drop_enum(name: str) -> None:
    postgresql.ENUM(*_ENUM_TYPES[name], name=name).drop(op.get_bind(), checkfirst=True)
