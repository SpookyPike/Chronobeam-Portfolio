"""Relink ``work_interval.work_period_id`` to the interval's own start month.

F3 / EINV-14. ``work_period_id`` is a **derived** linkage: the owning period is
the one whose ``period_month`` equals the Berlin-local calendar month of the
row's own ``start_at``. A correction used to copy its predecessor's
``work_period_id`` instead of re-deriving it, so a correction that moved an
interval across a month boundary left the new version pointing at the old
month's period (a persisted linkage contradicting the fact it derives from).

The writers are fixed in the same change. This migration repairs the rows the
old writers produced.

**Why a repair is legitimate here (unlike migration 0013).** The correct value
is a pure function of the row itself — ``start_at`` is immutable per version
and the mapping to a period month is deterministic — so nothing is *chosen*,
no canonical employee fact is rewritten, and no information is lost. 0013 had
to abort on a dirty database precisely because resolving a forked chain means
picking a winner, which a migration has no authority to do. This one cannot
encounter that situation.

Pre-flight count -> repair -> post-repair assertion, so the migration either
leaves the invariant holding or aborts loudly rather than half-repairing.

Deliberately DML only: no column, constraint or index changes. ``updated_at``
is intentionally not touched — a linkage repair is not a fact change, and
stamping the row would misrepresent when the employee's data last changed.

``downgrade`` is a documented no-op: the pre-repair values were stale
derivations, not facts, and the repaired value is recomputable from the row.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_work_period_relink"
down_revision: str | None = "0013_chain_and_scope_uniqueness"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The repeated SQL expression is intentionally literal: these migration
# statements contain no interpolated values or identifiers.
_MISMATCH_SQL = """
SELECT wi.id, wi.work_period_id, wp.period_month,
       date_trunc('month', wi.start_at AT TIME ZONE 'Europe/Berlin')::date AS expected_month
FROM work_interval wi
JOIN work_period wp ON wp.id = wi.work_period_id
WHERE wp.period_month <> date_trunc('month', wi.start_at AT TIME ZONE 'Europe/Berlin')::date
"""

_MISSING_PERIOD_SQL = """
SELECT wi.employee_id,
       date_trunc('month', wi.start_at AT TIME ZONE 'Europe/Berlin')::date AS expected_month,
       count(*) AS n
FROM work_interval wi
LEFT JOIN work_period wp
  ON wp.employee_id = wi.employee_id
 AND wp.period_month = date_trunc('month', wi.start_at AT TIME ZONE 'Europe/Berlin')::date
WHERE wp.id IS NULL
GROUP BY 1, 2
"""

_INSERT_MISSING_PERIODS_SQL = """
INSERT INTO work_period (employee_id, period_month, status)
SELECT DISTINCT wi.employee_id,
       date_trunc('month', wi.start_at AT TIME ZONE 'Europe/Berlin')::date,
       'OPEN'::work_period_status
FROM work_interval wi
ON CONFLICT (employee_id, period_month) DO NOTHING
"""

_RELINK_SQL = """
UPDATE work_interval wi
SET work_period_id = wp.id
FROM work_period wp
WHERE wp.employee_id = wi.employee_id
  AND wp.period_month = date_trunc('month', wi.start_at AT TIME ZONE 'Europe/Berlin')::date
  AND wi.work_period_id <> wp.id
"""


def _report() -> tuple[int, int]:
    """(mismatching rows, intervals whose start month has no period row)."""
    connection = op.get_bind()
    mismatched = connection.execute(sa.text(_MISMATCH_SQL)).fetchall()
    missing = connection.execute(sa.text(_MISSING_PERIOD_SQL)).fetchall()
    for row in missing[:10]:
        print(
            f"0014: creating missing work_period for employee={row.employee_id} "
            f"month={row.expected_month} ({row.n} interval(s))"
        )
    return len(mismatched), len(missing)


def upgrade() -> None:
    before, missing_groups = _report()
    if before == 0 and missing_groups == 0:
        return

    # ``status`` has no server default (migration 0002 declares it NOT NULL
    # without one), so it is supplied explicitly. ON CONFLICT DO NOTHING keeps
    # an existing row's status — including REVIEWED — untouched.
    op.execute(sa.text(_INSERT_MISSING_PERIODS_SQL))
    result = op.execute(sa.text(_RELINK_SQL))

    after, remaining_groups = _report()
    if after or remaining_groups:
        raise RuntimeError(
            f"0014 post-repair assertion failed: {after} interval(s) still disagree "
            f"with their start month and {remaining_groups} start month(s) still have "
            "no work_period row. The database is in a partially repaired state; "
            "restore from a verified backup before retrying."
        )
    relinked = result.rowcount if result.rowcount and result.rowcount > 0 else before
    print(f"0014: relinked {relinked} work_interval row(s) to their own start month")


def downgrade() -> None:
    """No-op by design (see the module docstring)."""
