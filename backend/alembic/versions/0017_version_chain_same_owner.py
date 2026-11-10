"""A version chain never crosses employees (`EINV-26`).

``uq_<table>_supersedes_id`` guarantees at most one successor per row, but says
nothing about *whose* successor it is. ``supersedes_id`` is a plain
single-column self-FK, so a row of employee B may legally supersede a row of
employee A. Every consequence of that state is a defect, and the state is
reachable without any application writer producing it (a restore, an import, a
manual fix):

- **Readers that anti-join on it fail closed but wrong.** ``NOT EXISTS (child
  WHERE child.supersedes_id = this.id)`` decides "is this row current?"; a
  foreign successor makes the employee's OWN row non-current for that consumer —
  hidden from a list, refused a link, dropped from a calculation.
- **Two disclosure readers fail OPEN**, which is worse. ``month_is_disclosed``
  returns ``False``, so ``assert_not_disclosed`` does not raise and a destructive
  DELETE of a *disclosed* month becomes permitted (`ACCESS-011`). And a prior
  grant excluded from ``priors`` is not revoked on re-grant, so the new grant
  becomes a root and the stale one never lapses.
- **The backward walks are DELETE walks.** Following ``supersedes_id`` backwards
  to prune a chain, with no owner check, deletes *another employee's rows*.

The fix is at the STORAGE layer, not in the readers: the constraint removes the
state, so all four reader shapes — anti-join, positive "is-superseded" scalar,
and both chain-walk directions — are covered at once, including readers that do
not exist yet. A reader sweep would instead have to change guard *polarity* in
the privacy and policy modules, would need four different helpers because the
shapes differ, and still could not cover ``travel_segment`` (see below).

**Nine owner-bearing tables** get ``UNIQUE(id, employee_id)`` and a composite FK
``(supersedes_id, employee_id) -> (id, employee_id)``. ``MATCH SIMPLE`` (the
PostgreSQL default) makes a NULL in any referencing column trivially satisfy the
FK, and since ``employee_id`` is NOT NULL every non-root row is fully checked;
the composite FK also **subsumes** the single-column FK, so the old one is
dropped rather than kept.

**``travel_segment`` is the special case: it has no ``employee_id`` column.** Its
chain is keyed on ``trip_id``, and ``business_trip.employee_id`` is NOT NULL, so
same-trip implies same-employee: the composite FK on ``(supersedes_id, trip_id)
-> (id, trip_id)`` is behaviour-preserving (only two sites construct a segment
and ``patch_segment`` carries ``trip_id`` forward), needs no new column and no
backfill, and is strictly stronger than the same-employee rule. Denormalizing
``employee_id`` onto the table was rejected: three more objects and a new derived
linkage to keep true.

**Pre-flight, then DDL.** A cross-owner scan runs BEFORE any DDL and the
migration ABORTS with the offending keys. There is deliberately NO in-migration
repair: re-pointing or deleting a version edge is a destructive decision about
canonical employee facts, which a migration has no authority to make. Under the
clean-cutover baseline (INV-13) there is no legacy data and no writer produces
such an edge, so a refusal means a real anomaly requiring a separately reviewed
reconciliation after a verified backup.

**Deployment: quiesced single-version transition** (`docs/operations.md`). These
are new constraints a *running* writer could violate, so no second application
version may overlap the migration.

**Downgrade is lossless and unconditional** — drop the composite FKs, re-create
the plain self-FKs under their original names, drop the unique constraints. No
refusal is needed (unlike `0016`): nothing here is insert-only, and the state the
downgrade re-opens is exactly the state the upgrade forbade.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_version_chain_same_owner"
down_revision: str | None = "0016_calendar_and_day_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (table, existing single-column self-FK name) for the nine owner-bearing
#: chains. ``id`` is the sole PK and ``employee_id`` is NOT NULL on all nine, so
#: ``(id, employee_id)`` is a valid unique key.
OWNER_CHAIN_TABLES: tuple[tuple[str, str], ...] = (
    ("work_interval", "fk_interval_supersedes"),
    ("ocd_call", "fk_ocd_call_supersedes"),
    ("overtime_authorization", "fk_overtime_auth_supersedes"),
    ("compensation_election", "fk_compensation_election_supersedes"),
    ("shift_deviation", "fk_shift_deviation_supersedes"),
    ("business_trip", "business_trip_supersedes_id_fkey"),
    ("disclosure_grant", "fk_disclosure_grant_supersedes"),
    ("submission_package", "fk_submission_package_supersedes"),
    ("day_status_event", "fk_day_status_event_supersedes"),
)

#: ``travel_segment`` carries no ``employee_id``; its chain is keyed on the trip.
TRIP_CHAIN_TABLE = "travel_segment"
TRIP_CHAIN_EXISTING_FK = "travel_segment_supersedes_id_fkey"


def _unique_name(table: str, key: str) -> str:
    return f"uq_{table}_id_{key}"


def _fk_name(table: str, key: str) -> str:
    return f"fk_{table}_supersedes_same_{'owner' if key == 'employee_id' else 'trip'}"


# Each entry is (label, SQL, offending-key columns) — one scan per new FK.
# Interpolated table names come only from the fixed module-level chain constants.
_PREFLIGHT_SCANS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    *(
        (
            _fk_name(table, "employee_id"),
            f"SELECT child.id AS child_id, child.supersedes_id AS parent_id, "  # noqa: S608  # nosec B608
            f"child.employee_id AS child_owner, parent.employee_id AS parent_owner "
            f"FROM {table} AS child JOIN {table} AS parent ON parent.id = child.supersedes_id "
            "WHERE child.employee_id <> parent.employee_id",
            ("child_id", "parent_id", "child_owner", "parent_owner"),
        )
        for table, _fk in OWNER_CHAIN_TABLES
    ),
    (
        _fk_name(TRIP_CHAIN_TABLE, "trip_id"),
        "SELECT child.id AS child_id, child.supersedes_id AS parent_id, "  # noqa: S608  # nosec B608
        "child.trip_id AS child_trip, parent.trip_id AS parent_trip "
        f"FROM {TRIP_CHAIN_TABLE} AS child "
        f"JOIN {TRIP_CHAIN_TABLE} AS parent ON parent.id = child.supersedes_id "
        "WHERE child.trip_id <> parent.trip_id",
        ("child_id", "parent_id", "child_trip", "parent_trip"),
    ),
)


def _preflight() -> None:
    """Abort before any DDL when a version chain already crosses its owner."""
    connection = op.get_bind()
    for label, sql, keys in _PREFLIGHT_SCANS:
        rows = connection.execute(sa.text(sql)).fetchall()
        if not rows:
            continue
        detail = "; ".join(
            ", ".join(f"{key}={getattr(row, key)!r}" for key in keys) for row in rows[:10]
        )
        raise RuntimeError(
            f"0017 pre-flight refused to proceed: {label} is already violated by "
            f"{len(rows)} version edge(s): {detail}. No DDL was applied. Restore from a "
            "verified backup and run a separately reviewed reconciliation/repair "
            "procedure; do not re-point or delete canonical employee version edges "
            "inside a migration."
        )


def upgrade() -> None:
    _preflight()

    for table, existing_fk in OWNER_CHAIN_TABLES:
        op.create_unique_constraint(
            _unique_name(table, "employee_id"), table, ["id", "employee_id"]
        )
        op.drop_constraint(existing_fk, table, type_="foreignkey")
        op.create_foreign_key(
            _fk_name(table, "employee_id"),
            table,
            table,
            ["supersedes_id", "employee_id"],
            ["id", "employee_id"],
        )

    op.create_unique_constraint(
        _unique_name(TRIP_CHAIN_TABLE, "trip_id"), TRIP_CHAIN_TABLE, ["id", "trip_id"]
    )
    op.drop_constraint(TRIP_CHAIN_EXISTING_FK, TRIP_CHAIN_TABLE, type_="foreignkey")
    op.create_foreign_key(
        _fk_name(TRIP_CHAIN_TABLE, "trip_id"),
        TRIP_CHAIN_TABLE,
        TRIP_CHAIN_TABLE,
        ["supersedes_id", "trip_id"],
        ["id", "trip_id"],
    )

    _assert_constraints_present()


def _assert_constraints_present() -> None:
    """Post-assert every object, so a partially applied migration fails loudly."""
    connection = op.get_bind()
    expected = [
        (name, table)
        for table, _fk in OWNER_CHAIN_TABLES
        for name in (_unique_name(table, "employee_id"), _fk_name(table, "employee_id"))
    ] + [
        (_unique_name(TRIP_CHAIN_TABLE, "trip_id"), TRIP_CHAIN_TABLE),
        (_fk_name(TRIP_CHAIN_TABLE, "trip_id"), TRIP_CHAIN_TABLE),
    ]
    missing = [
        f"{name} on {table}"
        for name, table in expected
        if not connection.execute(
            sa.text(
                "SELECT 1 FROM pg_constraint WHERE conname = :name "
                "AND conrelid = to_regclass(:table)"
            ),
            {"name": name, "table": table},
        ).scalar()
    ]
    if missing:
        raise RuntimeError(f"0017 did not create every constraint: {', '.join(missing)}")


def downgrade() -> None:
    op.drop_constraint(_fk_name(TRIP_CHAIN_TABLE, "trip_id"), TRIP_CHAIN_TABLE, type_="foreignkey")
    op.create_foreign_key(
        TRIP_CHAIN_EXISTING_FK,
        TRIP_CHAIN_TABLE,
        TRIP_CHAIN_TABLE,
        ["supersedes_id"],
        ["id"],
    )
    op.drop_constraint(_unique_name(TRIP_CHAIN_TABLE, "trip_id"), TRIP_CHAIN_TABLE, type_="unique")

    for table, existing_fk in OWNER_CHAIN_TABLES:
        op.drop_constraint(_fk_name(table, "employee_id"), table, type_="foreignkey")
        op.create_foreign_key(existing_fk, table, table, ["supersedes_id"], ["id"])
        op.drop_constraint(_unique_name(table, "employee_id"), table, type_="unique")
