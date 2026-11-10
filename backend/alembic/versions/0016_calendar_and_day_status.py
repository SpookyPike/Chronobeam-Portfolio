"""Calendar editions become institutional data; the day-status fact arrives.

F5 / ``EINV-10`` (calendar classification is effective-dated institutional data,
and an uncovered window FAILS CLOSED), ``EINV-21`` (a published edition is
insert-only), ``EINV-22`` (day-status facts are employee-owned versioned facts).

Three classes of change, one migration:

1. **Give ``calendar_version`` provenance and a resolution key.** The table had
   no ``source`` column at all, and ``version`` is ``Text`` — so "which edition
   applies to this day" would have been decided by *string ordering*. The new
   ``UNIQUE (calendar_key, valid_from)`` makes it a data decision (``EINV-04``/
   ``EINV-20``'s class); ``CHECK (valid_to >= valid_from)`` is the same guard the
   sibling effective-dated tables carry.
2. **Retire the seeded placeholder edition.** ``('primary','default-v1',
   2026-01-01, NULL)`` classified no day but covered every date, so "coverage is
   missing" could never be detected and a holiday silently added required time
   (audit B-2). It is deleted — and **nothing else is rewritten**: in particular
   ``period_calculation_snapshot.calendar_version_ids`` keeps naming it, which is
   the honest record of which edition applied when the snapshot was frozen. That
   column has no readers and a frozen row must not be mutated for a cosmetic
   reason (``EINV-07``).
3. **Create ``day_status_event``** — the canonical employee-owned day-status
   fact (per-day, versioned by ``supersedes_id``, employee-private, no
   institutional approval state).

**Pre-flight, then DDL+DML.** Every scan runs BEFORE anything is written and the
migration ABORTS naming the offending keys. The migration's only destructive act
is retiring one *empty scaffolding row*, and it refuses to proceed whenever the
table holds anything it did not expect — no in-migration repair, because choosing
what to do with operator-created calendar data is a decision a migration has no
authority to make (the ``0013`` rule).

**Downgrade refuses while the database holds post-0016 facts.** Two independent
refusals: while ``day_status_event`` has rows (dropping the table would destroy
employee-owned facts), and while any ``calendar_version``/``calendar_day`` row
survives (a downgrade cannot un-publish an institutional edition, and
re-inserting the retired placeholder beside it would strand the database at
``0015`` because the next upgrade aborts on ``no-unexpected-calendar-data``). The
operator restores from a verified backup instead. The refusals are what make the
migration reversible without being destructive — ``just migration-check`` runs
the round trip on a clean schema, where neither fires.

**Documented asymmetry:** the downgrade re-inserts the placeholder with a NEW
``id``. Nothing is lost by that — no column was rewritten on the way up, so
there is no repaired state to un-repair; historical ``calendar_version_ids``
entries naming the retired id are simply dangling provenance, exactly as they
were before the upgrade (``0014``'s no-op-by-design style).
"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_calendar_and_day_status"
down_revision: str | None = "0015_policy_edition_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PLACEHOLDER_KEY = "primary"
PLACEHOLDER_VERSION = "default-v1"
LEGACY_SOURCE = "UNKNOWN_LEGACY"

_NEW_ENUMS = {
    "day_status_category": (
        "VACATION",
        "SICK",
        "FLEX_COMPENSATION",
        "SHIFT_COMPENSATION",
    ),
    "day_status_portion": ("FULL", "HALF"),
    "day_status_day_part": ("ALL", "MORNING", "AFTERNOON"),
}

# (label, SQL, offending-key columns) — one scan per new guard, plus the two
# "unexpected data" scans that make the retirement safe.
_PREFLIGHT_SCANS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "ck_calendar_version_range",
        "SELECT id, calendar_key, version FROM calendar_version "
        "WHERE valid_to IS NOT NULL AND valid_to < valid_from",
        ("id", "calendar_key", "version"),
    ),
    (
        "uq_calendar_version_effective_from",
        "SELECT calendar_key, valid_from, count(*) AS n FROM calendar_version "
        "GROUP BY 1, 2 HAVING count(*) > 1",
        ("calendar_key", "valid_from"),
    ),
    (
        "day-inside-edition-range",
        "SELECT cd.id, cv.calendar_key, cv.version, cd.day FROM calendar_day cd "
        "JOIN calendar_version cv ON cv.id = cd.calendar_version_id "
        "WHERE cd.day < cv.valid_from OR (cv.valid_to IS NOT NULL AND cd.day > cv.valid_to)",
        ("id", "calendar_key", "version", "day"),
    ),
    (
        "placeholder-is-populated",
        # Module constants only; no external input reaches this SQL.
        "SELECT cd.id, cd.day FROM calendar_day cd "  # noqa: S608  # nosec B608
        "JOIN calendar_version cv ON cv.id = cd.calendar_version_id "
        f"WHERE cv.calendar_key = '{PLACEHOLDER_KEY}' "
        f"AND cv.version = '{PLACEHOLDER_VERSION}'",
        ("id", "day"),
    ),
    (
        "no-unexpected-calendar-data",
        "SELECT id, calendar_key, version, valid_from, valid_to FROM calendar_version "  # noqa: S608  # nosec B608
        f"WHERE NOT (calendar_key = '{PLACEHOLDER_KEY}' "
        f"AND version = '{PLACEHOLDER_VERSION}')",
        ("id", "calendar_key", "version", "valid_from", "valid_to"),
    ),
)


def _preflight():
    """Abort before any write when the existing data contradicts the change.

    Returns the placeholder's id, which the caller retires.
    """
    connection = op.get_bind()
    for label, sql, keys in _PREFLIGHT_SCANS:
        rows = connection.execute(sa.text(sql)).fetchall()
        if not rows:
            continue
        detail = "; ".join(
            ", ".join(f"{key}={getattr(row, key)!r}" for key in keys)
            + (f" (x{row.n})" if "n" in row._mapping else "")
            for row in rows[:10]
        )
        raise RuntimeError(
            f"0016 pre-flight refused to proceed: {label} is already violated by "
            f"{len(rows)} row(s): {detail}. Nothing was written. A migration must not "
            "silently rewrite unknown institutional calendar data: remove or re-publish "
            "the offending editions deliberately (with the calendar loader) after "
            "upgrading, then re-run. Restore from a verified backup if the table holds "
            "data you cannot explain."
        )
    placeholder = connection.execute(
        sa.text("SELECT id FROM calendar_version WHERE calendar_key = :key AND version = :version"),
        {"key": PLACEHOLDER_KEY, "version": PLACEHOLDER_VERSION},
    ).scalar()
    if placeholder is None:
        # The negative half of the "exactly the empty placeholder" precondition.
        # Migration ``0003`` seeds this row unconditionally and nothing before
        # ``0016`` deletes it, so its absence means the table was altered by hand.
        # Proceeding would install no edition at all and turn every calculation
        # into ``CALENDAR_BLOCKED`` — a silent outage instead of a named refusal.
        raise RuntimeError(
            f"0016 pre-flight refused to proceed: the expected placeholder edition "
            f"('{PLACEHOLDER_KEY}', '{PLACEHOLDER_VERSION}') is absent from "
            "calendar_version, so the pre-0016 calendar state is not the one this "
            "migration was written against. Nothing was written. Restore from a "
            "verified backup, or re-create the placeholder deliberately, then re-run."
        )
    return placeholder


def upgrade() -> None:
    placeholder_id = _preflight()

    # --- calendar_version: provenance + the resolution key -------------------
    op.add_column("calendar_version", sa.Column("source", sa.Text(), nullable=True))
    # Reachable only for the single seeded placeholder: the pre-flight guarantees
    # no other row exists.
    op.execute(
        sa.text("UPDATE calendar_version SET source = :source WHERE source IS NULL").bindparams(
            source=LEGACY_SOURCE
        )
    )
    op.alter_column("calendar_version", "source", nullable=False)
    op.create_check_constraint(
        "ck_calendar_version_range",
        "calendar_version",
        "valid_to IS NULL OR valid_to >= valid_from",
    )
    op.create_index(
        "uq_calendar_version_effective_from",
        "calendar_version",
        ["calendar_key", "valid_from"],
        unique=True,
    )
    # The resolution query filters calendar_day.day across the selected editions;
    # the only pre-existing access path was the un-indexed FK column.
    op.create_index("ix_calendar_day_day", "calendar_day", ["day"])

    # Retire the empty placeholder. NOTHING else is rewritten — in particular
    # period_calculation_snapshot.calendar_version_ids keeps its historical value.
    op.execute(sa.text("DELETE FROM calendar_version WHERE id = :id").bindparams(id=placeholder_id))

    # --- day_status_event ----------------------------------------------------
    for name, values in _NEW_ENUMS.items():
        postgresql.ENUM(*values, name=name).create(op.get_bind(), checkfirst=True)
    op.create_table(
        "day_status_event",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column(
            "category",
            postgresql.ENUM(
                *_NEW_ENUMS["day_status_category"], name="day_status_category", create_type=False
            ),
            nullable=False,
        ),
        sa.Column(
            "portion",
            postgresql.ENUM(
                *_NEW_ENUMS["day_status_portion"], name="day_status_portion", create_type=False
            ),
            nullable=False,
        ),
        sa.Column(
            "day_part",
            postgresql.ENUM(
                *_NEW_ENUMS["day_status_day_part"], name="day_status_day_part", create_type=False
            ),
            nullable=False,
        ),
        sa.Column(
            "source",
            postgresql.ENUM("EMPLOYEE", "IMPORT", name="interval_source", create_type=False),
            nullable=False,
        ),
        sa.Column("external_category", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("client_request_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version >= 1", name="ck_day_status_event_version_positive"),
        sa.CheckConstraint(
            "(portion = 'FULL' AND day_part = 'ALL') OR (portion = 'HALF' AND day_part <> 'ALL')",
            name="ck_day_status_event_day_part",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employee.id"], name="fk_day_status_event_employee"
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"], ["day_status_event.id"], name="fk_day_status_event_supersedes"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("supersedes_id", name="uq_day_status_event_supersedes_id"),
        sa.UniqueConstraint("client_request_id", name="uq_day_status_event_client_request_id"),
    )
    op.create_index(
        "uq_day_status_event_root",
        "day_status_event",
        ["employee_id", "day", "category", "day_part"],
        unique=True,
        postgresql_where=sa.text("supersedes_id IS NULL"),
    )
    op.create_index("ix_day_status_event_employee_day", "day_status_event", ["employee_id", "day"])

    # --- post-assert (same transaction: a partial retirement cannot commit) ---
    connection = op.get_bind()
    leftover = connection.execute(
        sa.text(
            "SELECT count(*) FROM calendar_version WHERE calendar_key = :key AND version = :version"
        ),
        {"key": PLACEHOLDER_KEY, "version": PLACEHOLDER_VERSION},
    ).scalar()
    if leftover:
        raise RuntimeError("0016 post-assert failed: the placeholder edition survived")
    blank = connection.execute(
        sa.text("SELECT count(*) FROM calendar_version WHERE btrim(source) = ''")
    ).scalar()
    if blank:
        raise RuntimeError("0016 post-assert failed: a calendar edition has a blank source")


def downgrade() -> None:
    connection = op.get_bind()
    remaining = connection.execute(sa.text("SELECT count(*) FROM day_status_event")).scalar()
    if remaining:
        raise RuntimeError(
            f"0016 downgrade refused: {remaining} day_status_event row(s) exist. Dropping the "
            "table would destroy employee-owned facts; restore from a verified backup instead."
        )
    # The mirror of the upgrade pre-flight. At 0016 the placeholder is retired, so
    # ANY surviving edition (or day row) is post-0016 institutional data that this
    # downgrade cannot un-publish: it would re-insert the placeholder beside it and
    # the next upgrade would abort on ``no-unexpected-calendar-data``, stranding the
    # database at 0015. Refusing keeps the round trip honest in both directions.
    editions = connection.execute(sa.text("SELECT count(*) FROM calendar_version")).scalar()
    days = connection.execute(sa.text("SELECT count(*) FROM calendar_day")).scalar()
    if editions or days:
        raise RuntimeError(
            f"0016 downgrade refused: {editions} calendar_version row(s) and {days} "
            "calendar_day row(s) exist. A downgrade cannot un-publish an institutional "
            "edition, and re-inserting the retired placeholder beside it would leave the "
            "next upgrade unable to proceed. Restore from a verified backup instead."
        )
    op.drop_index("ix_day_status_event_employee_day", table_name="day_status_event")
    op.drop_index("uq_day_status_event_root", table_name="day_status_event")
    op.drop_table("day_status_event")
    for name, values in _NEW_ENUMS.items():
        postgresql.ENUM(*values, name=name).drop(connection, checkfirst=True)

    op.drop_index("ix_calendar_day_day", table_name="calendar_day")
    op.drop_index("uq_calendar_version_effective_from", table_name="calendar_version")
    op.drop_constraint("ck_calendar_version_range", "calendar_version", type_="check")
    op.drop_column("calendar_version", "source")
    # A downgraded system behaves as revision 0015 did: silent all-working days
    # rather than 409s. The re-inserted row has a NEW id; see the docstring.
    op.execute(
        sa.text(
            "INSERT INTO calendar_version (calendar_key, version, valid_from, valid_to) "
            "VALUES (:key, :version, :valid_from, NULL)"
        ).bindparams(key=PLACEHOLDER_KEY, version=PLACEHOLDER_VERSION, valid_from=date(2026, 1, 1))
    )
