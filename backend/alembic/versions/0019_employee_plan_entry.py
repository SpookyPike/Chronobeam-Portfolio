"""Employee-entered planned shifts and OCD availability (pilot slice).

While no external shift-plan or OCD-schedule source exists
(``OPEN-06``), the employee records the shift/availability planned for
themselves, so the shipped confirm / deviation / shift-report / OCD-call /
compensation workflows are usable without an external planner or admin preload.
That is a pilot-mode substitute for the missing import path, not a throwaway: the
SAME assignment objects are used, so authority can later move to a supervisor, a
planning module or an adapter without migrating the accounting model.

This revision supplies the storage those write paths need, and nothing else.

**A. ``ocd_assignment`` becomes the twelfth version-chain table.**
``version`` / ``supersedes_id`` plus ``UNIQUE(supersedes_id)``,
``UNIQUE(id, employee_id)`` and the composite
``(supersedes_id, employee_id) -> (id, employee_id)`` FK — the same storage
invariants as the other eleven (``EINV-03``/``EINV-04``/``EINV-26``). Before
this, ``SUPERSEDED`` was unreachable and an edit had nowhere to go, so a
correction would have had to overwrite the availability the employee had
already recorded.

There is deliberately **NO partial root index**. ``uq_shift_deviation_root`` and
``uq_compensation_election_root`` exist because those tables carry a scope key;
an availability has none, and two root assignments are two legitimately distinct
facts (two different nights). A root index here would forbid a second legitimate
assignment.

**B. ``client_request_id`` on both create paths** (``BE-CMD-001``), each with a
NAMED unique constraint. The name is part of the contract, not cosmetic: the
write path compares ``constraint_name(exc)`` against exactly one name so an
unrelated integrity failure is re-raised instead of being disguised as an
idempotency collision. An auto-named constraint would make that branch
unreachable. The key is unique TABLE-WIDE, so the replay lookup is global.

**C. Two enum labels, added idempotently.** ``shift_assignment_status`` gains
``WITHDRAWN`` — the employee retracting their OWN plan record because it was
entered in error. It is **not** a cancellation and carries no entitlement: a real
operational cancellation is an act of the Company and is recorded as a
``shift_deviation``, leaving the plan ``PLANNED`` so the scheduled shift survives
as the compensation basis. ``shift_deviation_type`` gains
``ABORTED_BY_COMPANY`` for a shift that had already STARTED and was cut short:
representing that as ``CANCELLED`` is actively harmful, because
``save_deviation`` DELETES the linked actual intervals for a ``CANCELLED``
deviation, erasing the partial work that is itself the entitlement input.

**D. Pre-flight, then DDL — every check valid against revision 0018.**
A migration that assumes clean data will corrupt a restored, imported or
hand-edited database, so the checks run BEFORE any DDL and abort with the
offending rows named. Two parts, and the order matters:

1. a **schema guard** — no conditional data query is issued until the schema is
   known to be the pre-0019 shape. A partially applied migration or a hand-edited
   schema is refused rather than silently re-applied. This is also what *proves*
   the absence of the new columns, which is why there is no "duplicate
   ``client_request_id``" or "``supersedes_id`` chain" scan: those columns do not
   exist yet, and a scan for a not-yet-created column is not executable.
2. a **data scan** that names only columns which exist at 0018, with the column
   set asserted first so a later edit cannot silently reintroduce a query against
   a missing column.

The one scan that is both valid and meaningful at 0018 is the **shift head-cache
disagreement**: ``shift_assignment.planned_start_at``/``planned_end_at`` are a
denormalized cache of the approved revision, and the new write path maintains
them in lockstep. A pre-existing disagreement must be surfaced, not silently
repaired — re-pointing canonical employee facts is a destructive decision a
migration has no authority to make.

**E. Downgrade is a NAMED REFUSAL, not lossless.** ``0017``'s downgrade is
lossless because it only swaps a constraint for an equivalent one. This one is
not: dropping ``version``/``supersedes_id`` leaves N historical rows that
revision-0018 code reads as N independent assignments — duplicated in
``GET /me/ocd``, all counted by ``OCD-003``, each able to receive its own calls,
with the current head no longer distinguishable from its history. Every
0019-only persisted value therefore refuses (see ``_DOWNGRADE_REFUSALS``), and
nothing is dropped when it does.

The two enum labels are **retained** on downgrade: PostgreSQL has no
``ALTER TYPE ... DROP VALUE``. They are harmless there — no 0018 code produces
them and the refusal above guarantees no row uses them — and retaining them is
what makes re-upgrade idempotent instead of failing on a duplicate value.

**Deployment: quiesced single-version transition** (``docs/operations.md``).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_employee_plan_entry"
down_revision: str | None = "0018_break_interval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every column this revision adds, per table. The schema guard uses it to decide
#: whether the database is still at 0018; the data scans assert against it so a
#: scan can never reference a column that does not exist yet.
_0019_COLUMNS: dict[str, tuple[str, ...]] = {
    "ocd_assignment": ("version", "supersedes_id", "client_request_id", "reported_notice_at"),
    "shift_assignment": ("client_request_id",),
}

#: Columns each data scan is allowed to reference, checked before it runs.
_SCAN_COLUMNS: dict[str, tuple[str, ...]] = {
    "shift_assignment": ("planned_start_at", "planned_end_at", "approved_revision_id"),
}

#: ``shift_assignment`` is a denormalized HEAD CACHE of its approved revision: the
#: row carries the planned window so ``list_shifts`` can filter on it while the
#: revision columns are reported. A disagreement means the cache is already stale
#: and the new write path would preserve the disagreement rather than fix it.
_PREFLIGHT_SCANS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "shift_assignment",
        "shift-head-cache-disagreement",
        "SELECT a.id AS id, "
        "a.planned_start_at AS assignment_start, a.planned_end_at AS assignment_end, "
        "r.planned_start_at AS revision_start, r.planned_end_at AS revision_end "
        "FROM shift_assignment AS a "
        "JOIN shift_plan_revision AS r ON r.id = a.approved_revision_id "
        "WHERE a.planned_start_at <> r.planned_start_at "
        "   OR a.planned_end_at <> r.planned_end_at",
        ("id", "assignment_start", "assignment_end", "revision_start", "revision_end"),
    ),
)

#: The two labels this revision adds, as ``(enum type name, label)``. Module
#: CONSTANTS, never input, so the interpolated ``ALTER TYPE`` carries no injection
#: surface — the same argument ``0016``'s ``_NEW_ENUMS`` relies on.
_NEW_ENUM_VALUES: tuple[tuple[str, str], ...] = (
    ("shift_assignment_status", "WITHDRAWN"),
    ("shift_deviation_type", "ABORTED_BY_COMPANY"),
)

#: Every constraint this revision creates, as ``(table, name)``. Post-asserted so a
#: partially applied migration fails loudly instead of looking successful.
_NEW_CONSTRAINTS: tuple[tuple[str, str], ...] = (
    ("ocd_assignment", "ck_ocd_assignment_version_positive"),
    ("ocd_assignment", "fk_ocd_assignment_supersedes_same_owner"),
    ("ocd_assignment", "uq_ocd_assignment_id_employee_id"),
    ("ocd_assignment", "uq_ocd_assignment_supersedes_id"),
    ("ocd_assignment", "uq_ocd_assignment_client_request_id"),
    ("shift_assignment", "uq_shift_assignment_client_request_id"),
)

#: ``(label, SQL, offending columns)`` for the downgrade refusals — one per
#: 0019-only persisted value, so a future column cannot be forgotten without
#: editing this list. Each states what the re-opened state would strand.
_DOWNGRADE_REFUSALS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "ocd_assignment.supersedes_id",
        "SELECT id, supersedes_id FROM ocd_assignment WHERE supersedes_id IS NOT NULL",
        ("id", "supersedes_id"),
    ),
    (
        "ocd_assignment.version",
        "SELECT id, version FROM ocd_assignment WHERE version <> 1",
        ("id", "version"),
    ),
    (
        "ocd_assignment.client_request_id",
        "SELECT id, client_request_id FROM ocd_assignment WHERE client_request_id IS NOT NULL",
        ("id", "client_request_id"),
    ),
    (
        "ocd_assignment.reported_notice_at",
        "SELECT id, reported_notice_at FROM ocd_assignment WHERE reported_notice_at IS NOT NULL",
        ("id", "reported_notice_at"),
    ),
    (
        "shift_assignment.client_request_id",
        "SELECT id, client_request_id FROM shift_assignment WHERE client_request_id IS NOT NULL",
        ("id", "client_request_id"),
    ),
    (
        "shift_assignment.status",
        "SELECT id, status FROM shift_assignment WHERE status = 'WITHDRAWN'",
        ("id", "status"),
    ),
    (
        "shift_deviation.deviation_type",
        "SELECT id, deviation_type FROM shift_deviation "
        "WHERE deviation_type = 'ABORTED_BY_COMPANY'",
        ("id", "deviation_type"),
    ),
)

#: What each refusal strands, in the operator's words. Kept beside the scans so the
#: message and the check cannot drift apart.
_REFUSAL_REASONS: dict[str, str] = {
    "ocd_assignment.supersedes_id": (
        "every historical availability would be read by revision-0018 code as an "
        "INDEPENDENT assignment — duplicated in the employee's own list, all counted "
        "toward the OCD-003 monthly limit, each able to receive its own call, with the "
        "current head indistinguishable from its history"
    ),
    "ocd_assignment.version": (
        "the chain head would be indistinguishable from its history, so revision-0018 "
        "code could neither list nor correct the right availability"
    ),
    "ocd_assignment.client_request_id": (
        "revision 0018 has no column to honour the create-command idempotency key, so a "
        "client retry after the downgrade would double-create the availability"
    ),
    "ocd_assignment.reported_notice_at": (
        "an employee-reported notice fact would be silently destroyed — there is no "
        "column in revision 0018 to hold it"
    ),
    "shift_assignment.client_request_id": (
        "revision 0018 cannot honour the idempotency key, so a client retry after the "
        "downgrade would double-create the shift"
    ),
    "shift_assignment.status": (
        "the label leaves the model's view of the enum, so revision-0018 code would "
        "misread a WITHDRAWN plan as PLANNED — an active plan the employee had retracted"
    ),
    "shift_deviation.deviation_type": (
        "revision 0018's enum has no member for it, so code would misread a Company "
        "abortion, and the partial actual work the entitlement is calculated from would "
        "be interpreted under the wrong deviation"
    ),
}


def _preflight() -> None:
    """Abort before any DDL unless the database is genuinely at revision 0018."""
    connection = op.get_bind()
    inspector = sa.inspect(connection)

    # (1) SCHEMA GUARD. No conditional data query runs until the schema is known to
    #     be the pre-0019 shape. A partially applied migration or a hand-edited
    #     schema is refused rather than silently re-applied, and the guard is what
    #     PROVES the new columns are absent (so no scan needs to look for them).
    existing = {
        table: {column["name"] for column in inspector.get_columns(table)}
        for table in _0019_COLUMNS
    }
    already = [
        f"{table}.{column}"
        for table, columns in _0019_COLUMNS.items()
        for column in columns
        if column in existing[table]
    ]
    if already:
        raise RuntimeError(
            f"0019 pre-flight refused to proceed: {', '.join(already)} already exist(s), so this "
            "database is not at revision 0018 (a partially applied migration, a hand-edited "
            "schema, or a re-run). No DDL was applied. Restore from a verified backup or resolve "
            "the schema by hand before retrying."
        )

    # (2) DATA SCAN. Each scan names only columns that EXIST at 0018, and the
    #     column set is asserted first so a future edit cannot silently
    #     reintroduce a query against a not-yet-created column.
    for table, label, sql, keys in _PREFLIGHT_SCANS:
        missing = {column for column in _SCAN_COLUMNS[table] if column not in existing[table]}
        if missing:
            raise RuntimeError(
                f"0019 pre-flight scan for {table} needs columns absent at 0018: "
                f"{sorted(missing)}. Fix the scan, not the schema."
            )
        rows = connection.execute(sa.text(sql)).fetchall()
        if not rows:
            continue
        detail = "; ".join(
            ", ".join(f"{key}={getattr(row, key)!r}" for key in keys) for row in rows[:10]
        )
        raise RuntimeError(
            f"0019 pre-flight refused to proceed: {label} is already violated by "
            f"{len(rows)} row(s): {detail}. No DDL was applied. Restore from a verified backup "
            "and run a separately reviewed reconciliation/repair procedure; do not re-point "
            "canonical employee facts inside a migration."
        )


def _add_enum_value(enum_name: str, value: str) -> None:
    """Idempotently add one label to an EXISTING enum type.

    PostgreSQL has no ``ALTER TYPE ... ADD VALUE IF NOT EXISTS``, so the label is
    probed in ``pg_enum`` first. This is the "existing type" analogue of the
    ``checkfirst=True`` that ``0016`` uses for new types, and it is what makes
    downgrade -> re-upgrade safe: after a downgrade the labels are RETAINED
    (PostgreSQL cannot drop an enum value), so a blind re-add would fail with a
    duplicate-value error.
    """
    connection = op.get_bind()
    present = connection.execute(
        sa.text(
            "SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = :type_name AND e.enumlabel = :value"
        ),
        {"type_name": enum_name, "value": value},
    ).scalar()
    if present:
        return
    connection.execute(sa.text(f"ALTER TYPE \"{enum_name}\" ADD VALUE '{value}'"))


def upgrade() -> None:
    _preflight()

    # --- A/B: ocd_assignment becomes a version-chain table -------------------
    # ``server_default="1"`` is what makes every EXISTING row a version-1 root
    # without a backfill: no data is rewritten, and the column is then NOT NULL.
    op.add_column(
        "ocd_assignment",
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column("ocd_assignment", sa.Column("supersedes_id", sa.Uuid(), nullable=True))
    op.add_column("ocd_assignment", sa.Column("client_request_id", sa.Uuid(), nullable=True))
    op.add_column(
        "ocd_assignment", sa.Column("reported_notice_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "ck_ocd_assignment_version_positive", "ocd_assignment", "version >= 1"
    )
    op.create_unique_constraint(
        "uq_ocd_assignment_id_employee_id", "ocd_assignment", ["id", "employee_id"]
    )
    op.create_unique_constraint(
        "uq_ocd_assignment_supersedes_id", "ocd_assignment", ["supersedes_id"]
    )
    op.create_unique_constraint(
        "uq_ocd_assignment_client_request_id", "ocd_assignment", ["client_request_id"]
    )
    # The composite FK needs explicit remote columns: SQLAlchemy cannot infer them
    # for a self-reference. It SUBSUMES the single-column self-FK, so no plain
    # ``supersedes_id -> id`` FK is created alongside it.
    op.create_foreign_key(
        "fk_ocd_assignment_supersedes_same_owner",
        "ocd_assignment",
        "ocd_assignment",
        ["supersedes_id", "employee_id"],
        ["id", "employee_id"],
    )

    # --- B: shift_assignment idempotency key ---------------------------------
    op.add_column("shift_assignment", sa.Column("client_request_id", sa.Uuid(), nullable=True))
    op.create_unique_constraint(
        "uq_shift_assignment_client_request_id", "shift_assignment", ["client_request_id"]
    )

    # --- C: the two labels ---------------------------------------------------
    # Added LAST, and never USED in this transaction: PostgreSQL permits
    # ``ADD VALUE`` inside a transaction but forbids using the new value in the
    # same one. Nothing here writes a row carrying either label.
    for enum_name, value in _NEW_ENUM_VALUES:
        _add_enum_value(enum_name, value)

    _assert_present()


def _assert_present() -> None:
    """Post-assert every object, so a partially applied migration fails loudly."""
    connection = op.get_bind()
    inspector = sa.inspect(connection)

    missing_columns = [
        f"{table}.{column}"
        for table, columns in _0019_COLUMNS.items()
        for column in columns
        if column not in {col["name"] for col in inspector.get_columns(table)}
    ]
    missing_constraints = [
        f"{name} on {table}"
        for table, name in _NEW_CONSTRAINTS
        if not connection.execute(
            sa.text(
                "SELECT 1 FROM pg_constraint WHERE conname = :name "
                "AND conrelid = to_regclass(:table)"
            ),
            {"name": name, "table": table},
        ).scalar()
    ]
    missing_labels = [
        f"{enum_name}.{value}"
        for enum_name, value in _NEW_ENUM_VALUES
        if not connection.execute(
            sa.text(
                "SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = :type_name AND e.enumlabel = :value"
            ),
            {"type_name": enum_name, "value": value},
        ).scalar()
    ]
    problems = missing_columns + missing_constraints + missing_labels
    if problems:
        raise RuntimeError(f"0019 did not create every object: {', '.join(problems)}")


def downgrade() -> None:
    """Refuse when the re-opened state would be semantically corrupt.

    Not a lossless downgrade. Each check names what revision 0018 could not
    represent, and nothing is dropped when any of them fires — a partially
    downgraded database would be worse than an un-downgraded one.
    """
    connection = op.get_bind()
    for label, sql, keys in _DOWNGRADE_REFUSALS:
        rows = connection.execute(sa.text(sql)).fetchall()
        if not rows:
            continue
        detail = "; ".join(
            ", ".join(f"{key}={getattr(row, key)!r}" for key in keys) for row in rows[:10]
        )
        raise RuntimeError(
            f"0019 downgrade refused: {len(rows)} row(s) use {label}: {detail}. "
            f"Revision 0018 cannot represent this state: {_REFUSAL_REASONS[label]}. "
            "Nothing was dropped. Restore from a verified backup instead."
        )

    # Safe: no row uses any 0019-only value. The two enum labels are deliberately
    # NOT dropped — PostgreSQL cannot remove an enum value, and leaving them is
    # what makes a later re-upgrade idempotent (``_add_enum_value`` probes first).
    op.drop_constraint("uq_shift_assignment_client_request_id", "shift_assignment", type_="unique")
    op.drop_column("shift_assignment", "client_request_id")

    op.drop_constraint(
        "fk_ocd_assignment_supersedes_same_owner", "ocd_assignment", type_="foreignkey"
    )
    op.drop_constraint("uq_ocd_assignment_client_request_id", "ocd_assignment", type_="unique")
    op.drop_constraint("uq_ocd_assignment_supersedes_id", "ocd_assignment", type_="unique")
    op.drop_constraint("uq_ocd_assignment_id_employee_id", "ocd_assignment", type_="unique")
    op.drop_constraint("ck_ocd_assignment_version_positive", "ocd_assignment", type_="check")
    op.drop_column("ocd_assignment", "reported_notice_at")
    op.drop_column("ocd_assignment", "client_request_id")
    op.drop_column("ocd_assignment", "supersedes_id")
    op.drop_column("ocd_assignment", "version")
