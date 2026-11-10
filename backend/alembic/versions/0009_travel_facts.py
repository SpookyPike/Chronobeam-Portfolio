"""PHASE-9 employee-declared business-trip and travel-segment facts.

TRAVEL-001/002 facts remain separate from canonical work intervals.  Active
travel links to a BUSINESS_TRIP_WORK interval; passive travel has no interval.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_travel_facts"
down_revision: str | None = "0008_cutover_seed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

trip_start_origin = sa.Enum("HOME", "WORKPLACE", name="trip_start_origin")
segment_classification = sa.Enum(
    "PASSIVE_TRAVEL", "ACTIVE_WORK", "DRIVING", name="travel_segment_classification"
)
segment_basis = sa.Enum("ACTUAL", "FICTITIOUS_BUSINESS_ROUTE", name="travel_segment_basis")


def upgrade() -> None:
    op.create_table(
        "business_trip",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("start_origin", trip_start_origin, nullable=False),
        sa.Column("source", sa.Text(), nullable=False, server_default="EMPLOYEE_DECLARED"),
        sa.Column("reference_label", sa.Text(), nullable=True),
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
        sa.CheckConstraint("end_at > start_at", name="ck_business_trip_end_after_start"),
        sa.CheckConstraint("version >= 1", name="ck_business_trip_version_positive"),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"]),
        sa.ForeignKeyConstraint(["supersedes_id"], ["business_trip.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_request_id", name="uq_business_trip_client_request_id"),
    )
    op.create_table(
        "travel_segment",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("trip_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("classification", segment_classification, nullable=False),
        sa.Column("basis", segment_basis, nullable=False, server_default="ACTUAL"),
        sa.Column("work_interval_id", postgresql.UUID(as_uuid=True), nullable=True),
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
        sa.CheckConstraint("end_at > start_at", name="ck_travel_segment_end_after_start"),
        sa.CheckConstraint("version >= 1", name="ck_travel_segment_version_positive"),
        sa.ForeignKeyConstraint(["trip_id"], ["business_trip.id"]),
        sa.ForeignKeyConstraint(["work_interval_id"], ["work_interval.id"]),
        sa.ForeignKeyConstraint(["supersedes_id"], ["travel_segment.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("work_interval_id", name="uq_travel_segment_work_interval"),
        sa.UniqueConstraint("client_request_id", name="uq_travel_segment_client_request_id"),
    )


def downgrade() -> None:
    op.drop_table("travel_segment")
    op.drop_table("business_trip")
    bind = op.get_bind()
    segment_basis.drop(bind, checkfirst=True)
    segment_classification.drop(bind, checkfirst=True)
    trip_start_origin.drop(bind, checkfirst=True)
