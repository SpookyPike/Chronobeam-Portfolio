"""PHASE-5A employee-maintained working time (weekly target + weekday pattern).

Requirement ID: PHASE-5A, MVP seam.
Concrete problem solved: in the MVP there is no supported master-data
integration, so employees must be able to maintain Chronobeam's weekly
required-time target and weekday distribution themselves, effective-dated.
Simpler alternative considered: a new ``employee_working_time`` table.
Why simpler alternative is insufficient: ``work_schedule_version`` already
carries exactly the required effective-dated weekday semantics (INV-07/INV-11);
a parallel table would split the resolved schedule between two sources and
complicate effective resolution.

Storage invariants added (DATA-011..014):
- ``weekly_required_minutes`` backfilled from the weekday sum (seeded rows sum
  to the contract weekly value) and then NOT NULL;
- ``weekly_required_minutes = mon+...+sun`` (the weekday allocation must sum
  exactly to the weekly target);
- every weekday value in ``0..1440``; weekly target non-negative.

Downgrade limitation: PostgreSQL cannot remove enum values, so the ``EMPLOYEE``
value added to the ``schedule_source`` type remains after downgrade. Re-upgrade
is safe because ADD VALUE uses IF NOT EXISTS; the migration-check gate
(upgrade -> downgrade base -> upgrade) covers the round trip.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_employee_working_time"
down_revision: str | None = "0004_shift_workflow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DAY_SUM = (
    "mon_required_minutes + tue_required_minutes + wed_required_minutes"
    " + thu_required_minutes + fri_required_minutes + sat_required_minutes"
    " + sun_required_minutes"
)
_DAY_RANGE = (
    "mon_required_minutes BETWEEN 0 AND 1440"
    " AND tue_required_minutes BETWEEN 0 AND 1440"
    " AND wed_required_minutes BETWEEN 0 AND 1440"
    " AND thu_required_minutes BETWEEN 0 AND 1440"
    " AND fri_required_minutes BETWEEN 0 AND 1440"
    " AND sat_required_minutes BETWEEN 0 AND 1440"
    " AND sun_required_minutes BETWEEN 0 AND 1440"
)


def upgrade() -> None:
    op.execute("ALTER TYPE schedule_source ADD VALUE IF NOT EXISTS 'EMPLOYEE'")
    op.add_column(
        "work_schedule_version",
        sa.Column("weekly_required_minutes", sa.Integer(), nullable=True),
    )
    ats = _DAY_SUM  # fixed column list, never input
    # Only the migration's fixed weekday-column expression is appended here.
    op.execute(
        "UPDATE work_schedule_version SET weekly_required_minutes = " + ats  # noqa: S608  # nosec B608
    )
    op.alter_column("work_schedule_version", "weekly_required_minutes", nullable=False)
    op.create_check_constraint(
        "ck_schedule_weekly_nonneg",
        "work_schedule_version",
        "weekly_required_minutes >= 0",
    )
    op.create_check_constraint(
        "ck_schedule_weekly_sum",
        "work_schedule_version",
        "weekly_required_minutes = " + ats,
    )
    op.create_check_constraint(
        "ck_schedule_day_minutes_range",
        "work_schedule_version",
        _DAY_RANGE,
    )


def downgrade() -> None:
    op.drop_constraint("ck_schedule_day_minutes_range", "work_schedule_version")
    op.drop_constraint("ck_schedule_weekly_sum", "work_schedule_version")
    op.drop_constraint("ck_schedule_weekly_nonneg", "work_schedule_version")
    op.drop_column("work_schedule_version", "weekly_required_minutes")
