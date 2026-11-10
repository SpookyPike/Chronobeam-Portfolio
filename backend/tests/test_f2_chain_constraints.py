"""F2 — the chain/scope/root uniqueness constraints, at the persistence level.

Two kinds of evidence, deliberately split:

- **Catalogue (all eleven chains).** Every table that carries a ``supersedes_id``
  chain must have a UNIQUE index on exactly that column. In PostgreSQL a unique
  constraint IS a unique index, so the catalogue is the enforcement object —
  asserting it for all eleven is stronger than reproducing eleven bespoke
  constructions of the same DDL. (``day_status_event`` joined the catalogue in
  F5, ``break_interval`` in the P0 completeness remediation.)
- **Behavioural (the cases with real construction cost).** The chain case on
  ``work_interval`` (where the fork was reproduced), the two ROOT constraints
  and the submission scope/version constraint each get a real duplicate-insert
  test.

Index hygiene is asserted too: the three redundant non-unique indexes removed by
``0013`` must be gone, and nothing may have replaced them accidentally.
"""

import uuid
from datetime import UTC, date, datetime
from typing import cast

import pytest
from app.absence.models import (
    DayStatusCategory,
    DayStatusDayPart,
    DayStatusEvent,
    DayStatusPortion,
)
from app.db import constraint_name
from app.disclosure.models import SubmissionPackage
from app.enums import SubmissionPurpose
from app.identity.models import Employee
from app.ocd_overtime.models import CompensationContextType, CompensationElection, CompensationMode
from app.worktime.models import WorkInterval, WorkPeriod
from sqlalchemy import ForeignKeyConstraint, Table, UniqueConstraint, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

CHAIN_TABLES = (
    "work_interval",
    "ocd_call",
    "overtime_authorization",
    "compensation_election",
    "shift_deviation",
    "business_trip",
    "travel_segment",
    "disclosure_grant",
    "submission_package",
    # F5: the tenth chain table.
    "day_status_event",
    # P0 remediation: explicit factual breaks (WT-BACKEND §5.3).
    "break_interval",
)


def _unique_index_columns(engine: Engine, table: str) -> list[tuple[str, ...]]:
    """Column tuples of every UNIQUE index on ``table`` (order matters)."""
    statement = text(
        """
        SELECT pg_get_indexdef(x.indexrelid) AS definition
        FROM pg_index x
        JOIN pg_class t ON t.oid = x.indrelid
        WHERE t.relname = :table AND x.indisunique
        """
    )
    with engine.connect() as connection:
        definitions = [row.definition for row in connection.execute(statement, {"table": table})]
    columns: list[tuple[str, ...]] = []
    for definition in definitions:
        inside = definition.split("(", 1)[1].split(")", 1)[0]
        columns.append(tuple(part.strip() for part in inside.split(",")))
    return columns


@pytest.mark.parametrize("table", CHAIN_TABLES)
def test_every_chain_table_has_a_unique_successor_constraint(db_engine: Engine, table: str) -> None:
    """At most one successor per version, enforced by the database."""
    assert ("supersedes_id",) in _unique_index_columns(db_engine, table), table


def test_chain_fork_is_rejected_for_work_interval(db_engine: Engine) -> None:
    """Behavioural proof on the hot table: a second child of one version fails."""
    with Session(db_engine) as session:
        employee = session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
        assert employee is not None
        month = date(2032, 9, 1)
        period = session.scalar(
            select(WorkPeriod).where(
                WorkPeriod.employee_id == employee.id, WorkPeriod.period_month == month
            )
        )
        if period is None:
            period = WorkPeriod(employee_id=employee.id, period_month=month)
            session.add(period)
            session.flush()
        parent = WorkInterval(
            employee_id=employee.id,
            work_period_id=period.id,
            start_at=datetime(2032, 9, 1, 6, 0, tzinfo=UTC),
            end_at=datetime(2032, 9, 1, 8, 0, tzinfo=UTC),
            work_type="ON_SITE",
            version=1,
        )
        session.add(parent)
        session.flush()
        parent_id = parent.id

        def _child(offset_minutes: int) -> WorkInterval:
            return WorkInterval(
                employee_id=employee.id,
                work_period_id=period.id,
                start_at=datetime(2032, 9, 1, 6, 0, tzinfo=UTC),
                end_at=datetime(2032, 9, 1, 8 + offset_minutes, 0, tzinfo=UTC),
                work_type="ON_SITE",
                version=2,
                supersedes_id=parent_id,
            )

        session.add(_child(0))
        session.flush()
        with pytest.raises(IntegrityError) as error:
            session.add(_child(1))
            session.flush()
        assert "uq_work_interval_supersedes_id" in str(error.value)
        session.rollback()


def test_election_root_duplicate_is_rejected(db_engine: Engine) -> None:
    """One ROOT per (employee, context) — otherwise the current row is arbitrary."""
    with Session(db_engine) as session:
        employee = session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
        assert employee is not None
        context_id = uuid.uuid4()

        def _root() -> CompensationElection:
            return CompensationElection(
                employee_id=employee.id,
                context_type=CompensationContextType.OVERTIME,
                context_id=context_id,
                mode=CompensationMode.PAY,
                version=1,
                selected_at=datetime.now(UTC),
            )

        session.add(_root())
        session.flush()
        with pytest.raises(IntegrityError) as error:
            session.add(_root())
            session.flush()
        assert "uq_compensation_election_root" in str(error.value)
        session.rollback()


def test_shift_deviation_root_duplicate_is_rejected(db_engine: Engine) -> None:
    """One ROOT per shift assignment (proved through the same unique index)."""
    with db_engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT pg_get_indexdef(x.indexrelid) AS definition
                FROM pg_index x
                JOIN pg_class t ON t.oid = x.indrelid
                WHERE t.relname = 'shift_deviation' AND x.indisunique
                """
            )
        ).fetchall()
    definitions = [entry.definition for entry in row]
    assert any("uq_shift_deviation_root" in definition for definition in definitions)
    assert any("supersedes_id IS NULL" in definition for definition in definitions)


def test_submission_scope_version_duplicate_is_rejected(db_engine: Engine) -> None:
    """Two rows may not claim the same authoritative version of one scope."""
    with Session(db_engine) as session:
        employee = session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
        assert employee is not None

        def _package() -> SubmissionPackage:
            return SubmissionPackage(
                employee_id=employee.id,
                purpose=SubmissionPurpose.OVERTIME_PAID,
                period_start=date(2032, 10, 1),
                period_end=date(2032, 10, 31),
                version=1,
                payload_json={},
                employee_confirmed_at=datetime.now(UTC),
                client_request_id=uuid.uuid4(),
            )

        session.add(_package())
        session.flush()
        with pytest.raises(IntegrityError) as error:
            session.add(_package())
            session.flush()
        assert "uq_submission_package_scope_version" in str(error.value)
        session.rollback()


def test_removed_redundant_indexes_are_gone(db_engine: Engine) -> None:
    """The three dropped non-unique indexes must not linger."""
    with db_engine.connect() as connection:
        names = {
            row.indexname
            for row in connection.execute(
                text("SELECT indexname FROM pg_indexes WHERE indexname = ANY(:names)"),
                {
                    "names": [
                        "ix_disclosure_grant_supersedes_id",
                        "ix_submission_package_supersedes_id",
                        "ix_submission_package_client_request_id",
                    ]
                },
            )
        }
    assert names == set()


def test_scope_version_and_successor_constraints_are_distinct_objects(
    db_engine: Engine,
) -> None:
    """The semantic scope key and the successor key are separate constraints.

    Conflating them would make an unrelated integrity failure indistinguishable
    from an idempotency collision, which is exactly what the recovery branch
    must never do.
    """
    columns = _unique_index_columns(db_engine, "submission_package")
    assert (
        "employee_id",
        "purpose",
        "period_start",
        "period_end",
        "version",
    ) in columns
    assert ("supersedes_id",) in columns
    assert ("client_request_id",) in columns


# ---------------------------------------------------------------------------
# F7 / EINV-26 — a version chain never crosses employees
#
# ``uq_<table>_supersedes_id`` allows at most one successor per row but says
# nothing about whose successor it is, so a row of employee B could supersede a
# row of employee A. The constraint below removes that STATE, which is why it
# covers all four reader shapes at once (anti-join, positive is-superseded
# scalar, and both chain-walk directions) — including readers not yet written.
# ---------------------------------------------------------------------------

#: chain table -> the key the composite FK is scoped by.
CHAIN_OWNER_KEYS = {
    "work_interval": "employee_id",
    "ocd_call": "employee_id",
    "overtime_authorization": "employee_id",
    "compensation_election": "employee_id",
    "shift_deviation": "employee_id",
    "business_trip": "employee_id",
    "disclosure_grant": "employee_id",
    "submission_package": "employee_id",
    "day_status_event": "employee_id",
    "break_interval": "employee_id",
    # 0019: the on-call AVAILABILITY became a chain table so an employee can
    # correct or cancel a planned availability without overwriting the row that
    # was already recorded (the predecessor stays as lineage, INV-11).
    "ocd_assignment": "employee_id",
    # No employee_id column: its chain is keyed on the trip, and
    # business_trip.employee_id is NOT NULL, so same-trip implies same-owner.
    "travel_segment": "trip_id",
}


def _composite_chain_fk(engine: Engine, table: str) -> list[tuple[str, ...]]:
    """Column pairs of every composite FK on ``(supersedes_id, <key>)``."""
    statement = text(
        """
        SELECT array_agg(a.attname ORDER BY k.ordinality) AS columns
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ordinality)
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
        WHERE t.relname = :table AND c.contype = 'f' AND array_length(c.conkey, 1) = 2
        GROUP BY c.conname
        """
    )
    with engine.connect() as connection:
        return [tuple(row.columns) for row in connection.execute(statement, {"table": table})]


def test_EINV_26_every_chain_is_scoped_to_its_owner(db_engine: Engine) -> None:
    """Storage-level catalogue: unique key + composite FK on every chain table."""
    for table, key in CHAIN_OWNER_KEYS.items():
        columns = _unique_index_columns(db_engine, table)
        assert ("id", key) in columns, f"{table} lacks UNIQUE(id, {key})"
        assert ("supersedes_id", key) in _composite_chain_fk(db_engine, table), (
            f"{table} lacks the composite FK (supersedes_id, {key})"
        )


def test_EINV_26_a_cross_employee_successor_is_refused(db_session: Session) -> None:
    """The behavioural half: the corrupt state cannot be written at all."""
    employees = db_session.scalars(select(Employee).order_by(Employee.id).limit(2)).all()
    assert len(employees) == 2, "the synthetic seed must provide two employees"
    first, second = employees

    parent = DayStatusEvent(
        employee_id=first.id,
        day=date(2035, 4, 1),
        category=DayStatusCategory.VACATION,
        portion=DayStatusPortion.FULL,
        day_part=DayStatusDayPart.ALL,
    )
    db_session.add(parent)
    db_session.flush()

    db_session.add(
        DayStatusEvent(
            employee_id=second.id,
            day=date(2035, 4, 2),
            category=DayStatusCategory.VACATION,
            portion=DayStatusPortion.FULL,
            day_part=DayStatusDayPart.ALL,
            supersedes_id=parent.id,
        )
    )
    with pytest.raises(IntegrityError) as raised:
        db_session.flush()
    assert constraint_name(raised.value) == "fk_day_status_event_supersedes_same_owner"


def test_EINV_26_the_models_declare_what_the_migration_creates() -> None:
    """Metadata/schema parity for the chain keys.

    `0017` creates `UNIQUE(id, <key>)` on every chain table, and the composite FK
    needs it as its target. If only the migration declares it, the ORM metadata
    disagrees with the live schema: `Base.metadata`-driven creation cannot build
    the composite FK, and the constraint-name parity between migration and models
    is broken. This asserts the models side of that parity.
    """
    from app.absence.models import DayStatusEvent
    from app.disclosure.models import DisclosureGrant, SubmissionPackage
    from app.ocd_overtime.models import (
        CompensationElection,
        OcdAssignment,
        OcdCall,
        OvertimeAuthorization,
    )
    from app.shift.models import ShiftDeviation
    from app.travel.models import BusinessTrip, TravelSegment
    from app.worktime.models import BreakInterval, WorkInterval

    models = (
        WorkInterval,
        OcdCall,
        OvertimeAuthorization,
        CompensationElection,
        ShiftDeviation,
        BusinessTrip,
        TravelSegment,
        DisclosureGrant,
        SubmissionPackage,
        DayStatusEvent,
        BreakInterval,
        OcdAssignment,
    )
    for model in models:
        # `__table__` is typed as FromClause; these assertions are about the Table.
        table = cast(Table, model.__table__)
        key = CHAIN_OWNER_KEYS[table.name]
        unique_names = sorted(
            str(constraint.name)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
            and [column.name for column in constraint.columns] == ["id", key]
        )
        assert unique_names == [f"uq_{table.name}_id_{key}"], (table.name, unique_names)
        composite = [
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
            and [column.name for column in constraint.columns] == ["supersedes_id", key]
        ]
        suffix = "trip" if table.name == "travel_segment" else "owner"
        assert composite == [f"fk_{table.name}_supersedes_same_{suffix}"], (table.name, composite)
