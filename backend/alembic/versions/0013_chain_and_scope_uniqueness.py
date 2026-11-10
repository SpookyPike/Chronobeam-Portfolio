"""Version-chain, scope and root uniqueness.

Three classes of constraint, all of which make a state the domain model says
cannot exist unrepresentable:

1. **At most one successor per version** (nine tables) — a version chain must
   be a chain, not a fork. Two children of one predecessor means two "current"
   rows for one logical fact, which the current-version predicate
   (``NOT EXISTS (child.supersedes_id = this.id)``) cannot express.
2. **Semantic scope/version uniqueness** on ``submission_package`` — distinct
   from request idempotency (``client_request_id``, which is globally unique).
   ``status`` is deliberately excluded: it is mutable.
3. **One ROOT per scope** on ``compensation_election`` and ``shift_deviation``.
   Both readers resolve "the current row" with a ``WHERE`` and no ``ORDER BY``,
   so a second root makes the employee's compensation election and the shift
   deviation findings nondeterministic.

The nine successor constraints are declared as ``UniqueConstraint`` in the ORM
metadata and created with ``op.create_unique_constraint`` here; the two root
constraints must be partial unique INDEXES because a ``UniqueConstraint``
cannot carry a predicate. One canonical representation per object kind.

**Pre-flight, then DDL.** A duplicate scan runs BEFORE any DDL and the
migration ABORTS with the offending keys. There is deliberately NO in-migration
repair: choosing a winning version is a destructive decision about canonical
employee facts, which a migration has no authority to make. A dirty database
requires a separately reviewed reconciliation performed after a verified
backup, with the offending rows exported first. A forked chain cannot occur in
a clean installation (pre-production, clean cutover, no historical import), so
an operator who hits one has a real anomaly.

Index hygiene in the same migration: three redundant non-unique indexes are
dropped (two are superseded by the new unique constraints; the third duplicated
the unique index behind ``uq_submission_package_client_request_id``).

Downgrade drops the constraints/indexes and re-creates the three dropped
indexes; the invariants reopen.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_chain_and_scope_uniqueness"
down_revision: str | None = "0012_identity_binding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The nine tables carrying a self-referencing supersedes_id chain.
CHAIN_TABLES: tuple[str, ...] = (
    "work_interval",
    "ocd_call",
    "overtime_authorization",
    "compensation_election",
    "shift_deviation",
    "business_trip",
    "travel_segment",
    "disclosure_grant",
    "submission_package",
)

DROPPED_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_disclosure_grant_supersedes_id", "disclosure_grant"),
    ("ix_submission_package_supersedes_id", "submission_package"),
    ("ix_submission_package_client_request_id", "submission_package"),
)

# Each entry is (label, SQL, offending-key columns) — one scan per constraint.
# Interpolated table names come only from this fixed module-level list.
_PREFLIGHT_SCANS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    *(
        (
            f"uq_{table}_supersedes_id",
            f"SELECT supersedes_id, count(*) AS n FROM {table} "  # noqa: S608  # nosec B608
            "WHERE supersedes_id IS NOT NULL GROUP BY 1 HAVING count(*) > 1",
            ("supersedes_id",),
        )
        for table in CHAIN_TABLES
    ),
    (
        "uq_submission_package_scope_version",
        "SELECT employee_id, purpose, period_start, period_end, version, count(*) AS n "
        "FROM submission_package GROUP BY 1,2,3,4,5 HAVING count(*) > 1",
        ("employee_id", "purpose", "period_start", "period_end", "version"),
    ),
    (
        "uq_compensation_election_root",
        "SELECT employee_id, context_type, context_id, count(*) AS n "
        "FROM compensation_election WHERE supersedes_id IS NULL "
        "GROUP BY 1,2,3 HAVING count(*) > 1",
        ("employee_id", "context_type", "context_id"),
    ),
    (
        "uq_shift_deviation_root",
        "SELECT shift_assignment_id, count(*) AS n FROM shift_deviation "
        "WHERE supersedes_id IS NULL GROUP BY 1 HAVING count(*) > 1",
        ("shift_assignment_id",),
    ),
)


def _preflight() -> None:
    """Abort before any DDL when the existing data violates a new constraint."""
    connection = op.get_bind()
    for label, sql, keys in _PREFLIGHT_SCANS:
        rows = connection.execute(sa.text(sql)).fetchall()
        if not rows:
            continue
        detail = "; ".join(
            ", ".join(f"{key}={getattr(row, key)!r}" for key in keys) + f" (x{row.n})"
            for row in rows[:10]
        )
        raise RuntimeError(
            f"0013 pre-flight refused to proceed: {label} is already violated by "
            f"{len(rows)} key group(s): {detail}. No DDL was applied. Restore from a "
            "verified backup and run a separately reviewed reconciliation/repair "
            "procedure; do not repair canonical employee facts inside a migration."
        )


def upgrade() -> None:
    _preflight()

    for index_name, table in DROPPED_INDEXES:
        op.drop_index(index_name, table_name=table)

    for table in CHAIN_TABLES:
        op.create_unique_constraint(f"uq_{table}_supersedes_id", table, ["supersedes_id"])

    op.create_unique_constraint(
        "uq_submission_package_scope_version",
        "submission_package",
        ["employee_id", "purpose", "period_start", "period_end", "version"],
    )

    op.create_index(
        "uq_compensation_election_root",
        "compensation_election",
        ["employee_id", "context_type", "context_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_id IS NULL"),
    )
    op.create_index(
        "uq_shift_deviation_root",
        "shift_deviation",
        ["shift_assignment_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_shift_deviation_root", table_name="shift_deviation")
    op.drop_index("uq_compensation_election_root", table_name="compensation_election")
    op.drop_constraint("uq_submission_package_scope_version", "submission_package", type_="unique")
    for table in CHAIN_TABLES:
        op.drop_constraint(f"uq_{table}_supersedes_id", table, type_="unique")

    op.create_index(
        "ix_submission_package_client_request_id",
        "submission_package",
        ["client_request_id"],
        unique=False,
    )
    op.create_index(
        "ix_submission_package_supersedes_id",
        "submission_package",
        ["supersedes_id"],
        unique=False,
    )
    op.create_index(
        "ix_disclosure_grant_supersedes_id",
        "disclosure_grant",
        ["supersedes_id"],
        unique=False,
    )
