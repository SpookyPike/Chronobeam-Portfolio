"""F5 migration ``0016`` and the new restore-verification domains.

Two kinds of evidence:

- **The migration itself** (pre-flight aborts, the placeholder retirement, the
  downgrade refusal and the clean round trip) against a SCRATCH database, driven
  through alembic in-process so the upgrade can be observed from revision 0015
  with deliberately corrupted data in place.
- **The restore verifier's new domains**, exercised through the same
  ``backup._domain_checks`` function the verifier CLI calls, with corrupt rows
  inserted inside the rolled-back test transaction. That is the discriminating
  half: a restored database must no longer verify ``ok`` with a corrupt calendar,
  an uncovered frozen period or a forked day-status chain.
"""

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import cast

import pytest
from alembic import command
from alembic.config import Config
from app.absence.models import DayStatusEvent
from app.backup import DOMAIN_NAMES, _domain_checks, _metadata_tables
from app.calendar.models import CalendarDay, CalendarVersion
from app.config import get_settings
from app.db import create_engine, ensure_database, get_session_factory
from app.enums import (
    CalendarDayClass,
    DayStatusCategory,
    DayStatusDayPart,
    DayStatusPortion,
    PeriodStatus,
)
from app.identity.models import Employee
from app.ocd_overtime.models import OcdAssignment
from app.policy.models import PeriodCalculationSnapshot, PolicyVersion
from app.shift.models import ShiftAssignment
from app.travel.models import (
    BusinessTrip,
    TravelSegment,
    TravelSegmentBasis,
    TravelSegmentClassification,
    TripStartOrigin,
)
from app.worktime.models import WorkPeriod
from sqlalchemy import UniqueConstraint, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.sql.schema import Table

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BEFORE = "0015_policy_edition_identity"
HEAD = "0016_calendar_and_day_status"
F7_BEFORE = "0016_calendar_and_day_status"
F7_HEAD = "0017_version_chain_same_owner"
F8_BEFORE = "0018_break_interval"
F8_HEAD = "0019_employee_plan_entry"


def _config(url: str) -> Config:
    """Alembic config for the scratch database.

    ``alembic/env.py`` re-reads ``get_settings().database_url``, which is derived
    from ``CHRONOBEAM_DATABASE_URL``, so the environment variable is the
    authority — setting only ``sqlalchemy.url`` would silently upgrade the shared
    test database instead.
    """
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND, "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


class _AlembicTarget:
    """Point the alembic environment at one database for the duration of a call."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.previous: str | None = None

    def __enter__(self) -> "_AlembicTarget":
        self.previous = os.environ.get("CHRONOBEAM_DATABASE_URL")
        os.environ["CHRONOBEAM_DATABASE_URL"] = self.url
        get_settings.cache_clear()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.previous is None:
            os.environ.pop("CHRONOBEAM_DATABASE_URL", None)
        else:
            os.environ["CHRONOBEAM_DATABASE_URL"] = self.previous
        get_settings.cache_clear()


def _drop(url: str) -> None:
    from sqlalchemy import create_engine as _create

    split = url.rsplit("/", 1)
    server = f"{split[0]}/postgres"
    engine = _create(server, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{split[1]}" WITH (FORCE)'))
    finally:
        engine.dispose()


class _Scratch:
    """A scratch database upgraded to ``revision``, ready to be corrupted."""

    def __init__(self, revision: str = BEFORE) -> None:
        self.name = f"chronobeam_f5mig_{uuid.uuid4().hex[:10]}"
        base = get_settings().test_database_url.rsplit("/", 1)[0]
        self.url = f"{base}/{self.name}"
        ensure_database(self.url)
        with _AlembicTarget(self.url):
            command.upgrade(_config(self.url), revision)
        self.engine = create_engine(self.url, pool_pre_ping=True)

    def close(self) -> None:
        self.engine.dispose()
        _drop(self.url)

    def revision(self) -> str:
        with self.engine.connect() as connection:
            return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    def has_constraint(self, table: str, name: str) -> bool:
        with self.engine.connect() as connection:
            return bool(
                connection.execute(
                    text(
                        "SELECT 1 FROM pg_constraint WHERE conname = :name "
                        "AND conrelid = to_regclass(:table)"
                    ),
                    {"name": name, "table": table},
                ).first()
            )

    def has_column(self, table: str, column: str) -> bool:
        with self.engine.connect() as connection:
            return bool(
                connection.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = :table AND column_name = :column"
                    ),
                    {"table": table, "column": column},
                ).first()
            )

    def has_enum_label(self, enum_name: str, label: str) -> bool:
        with self.engine.connect() as connection:
            return bool(
                connection.execute(
                    text(
                        "SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                        "WHERE t.typname = :enum_name AND e.enumlabel = :label"
                    ),
                    {"enum_name": enum_name, "label": label},
                ).first()
            )

    def has_table(self, table: str) -> bool:
        with self.engine.connect() as connection:
            return bool(
                connection.execute(
                    text(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_name = :table"
                    ),
                    {"table": table},
                ).first()
            )

    def execute(self, statement: str, **params: object) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(statement), params)


def _skip_without_db() -> None:
    if not os.environ.get("CHRONOBEAM_REQUIRE_DB") and not _db_reachable():
        pytest.skip("PostgreSQL not reachable; run 'just db-up' for migration tests")


@pytest.fixture()
def scratch() -> Iterator[_Scratch]:
    _skip_without_db()
    instance = _Scratch()
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture()
def scratch_0018() -> Iterator[_Scratch]:
    """A scratch database at the revision ``0019`` pre-flights against."""
    _skip_without_db()
    instance = _Scratch(F8_BEFORE)
    try:
        yield instance
    finally:
        instance.close()


def _upgrade(instance: _Scratch, revision: str) -> None:
    with _AlembicTarget(instance.url):
        command.upgrade(_config(instance.url), revision)


def _downgrade(instance: _Scratch, revision: str) -> None:
    with _AlembicTarget(instance.url):
        command.downgrade(_config(instance.url), revision)


def _stamp(instance: _Scratch, revision: str) -> None:
    """Move the version table without running any migration body.

    The only way to re-run an upgrade that is already applied: alembic treats
    "upgrade to the revision we are at" as a no-op, so the migration body — and
    with it the schema guard — would never execute.
    """
    with _AlembicTarget(instance.url):
        command.stamp(_config(instance.url), revision)


def _db_reachable() -> bool:
    from sqlalchemy.exc import OperationalError

    try:
        engine = create_engine(get_settings().test_database_url, pool_pre_ping=True)
        with engine.connect():
            pass
        engine.dispose()
        return True
    except OperationalError:
        return False


# ---------------------------------------------------------------------------
# Pre-flight: abort before any write
# ---------------------------------------------------------------------------


def _assert_aborted(instance: _Scratch, label: str) -> None:
    with pytest.raises(RuntimeError) as excinfo:
        _upgrade(instance, HEAD)
    assert label in str(excinfo.value)
    # NOTHING was written: the revision is unchanged and the new column absent.
    assert instance.revision() == BEFORE
    assert instance.has_column("calendar_version", "source") is False
    assert not instance.has_table("day_status_event")


def test_0016_preflight_aborts_on_an_inverted_range(scratch: _Scratch) -> None:
    scratch.execute(
        "INSERT INTO calendar_version (calendar_key, version, valid_from, valid_to) "
        "VALUES ('primary', 'inverted-v1', DATE '2027-06-01', DATE '2027-01-01')"
    )
    _assert_aborted(scratch, "ck_calendar_version_range")


def test_0016_preflight_aborts_on_a_duplicate_effective_date(scratch: _Scratch) -> None:
    scratch.execute(
        "INSERT INTO calendar_version (calendar_key, version, valid_from, valid_to) "
        "VALUES ('primary', 'dup-v1', DATE '2026-01-01', NULL)"
    )
    _assert_aborted(scratch, "uq_calendar_version_effective_from")


def test_0016_preflight_aborts_on_an_out_of_range_calendar_day(scratch: _Scratch) -> None:
    scratch.execute(
        "INSERT INTO calendar_version (calendar_key, version, valid_from, valid_to) "
        "VALUES ('primary', 'range-v1', DATE '2027-01-01', DATE '2027-12-31')"
    )
    scratch.execute(
        "INSERT INTO calendar_day (calendar_version_id, day, day_class) "
        "SELECT id, DATE '2028-01-01', 'PUBLIC_HOLIDAY' FROM calendar_version "
        "WHERE version = 'range-v1'"
    )
    _assert_aborted(scratch, "day-inside-edition-range")


def test_0016_preflight_aborts_when_the_placeholder_is_absent(scratch: _Scratch) -> None:
    """The NEGATIVE half of "exactly the empty placeholder".

    ``0003`` seeds the placeholder unconditionally and nothing before ``0016``
    deletes it, so a table without it was altered by hand. Proceeding would
    install no edition at all and turn every calculation into
    ``CALENDAR_BLOCKED`` — a silent outage instead of a named refusal.
    """
    scratch.execute("DELETE FROM calendar_version")
    _assert_aborted(scratch, "placeholder edition")


def test_0016_preflight_aborts_on_a_populated_placeholder(scratch: _Scratch) -> None:
    scratch.execute(
        "INSERT INTO calendar_day (calendar_version_id, day, day_class) "
        "SELECT id, DATE '2026-10-03', 'PUBLIC_HOLIDAY' FROM calendar_version "
        "WHERE version = 'default-v1'"
    )
    _assert_aborted(scratch, "placeholder-is-populated")


def test_0016_preflight_aborts_on_unexpected_additional_calendar_data(scratch: _Scratch) -> None:
    """The migration must not delete the placeholder underneath real editions.

    No supported writer exists before F5, so any other edition is
    operator-created; rewriting unknown institutional data is not a migration's
    call.
    """
    scratch.execute(
        "INSERT INTO calendar_version (calendar_key, version, valid_from, valid_to) "
        "VALUES ('primary', 'operator-made-v1', DATE '2027-01-01', NULL)"
    )
    _assert_aborted(scratch, "no-unexpected-calendar-data")


def test_0016_aborts_are_named_in_the_message(scratch: _Scratch) -> None:
    scratch.execute(
        "INSERT INTO calendar_version (calendar_key, version, valid_from, valid_to) "
        "VALUES ('primary', 'named-v1', DATE '2027-01-01', NULL)"
    )
    with pytest.raises(RuntimeError) as excinfo:
        _upgrade(scratch, HEAD)
    message = str(excinfo.value)
    assert "named-v1" in message
    assert "Nothing was written" in message


# ---------------------------------------------------------------------------
# 0017 pre-flight: a version chain that already crosses its owner
# ---------------------------------------------------------------------------
#
# `0017` adds a composite `(supersedes_id, <key>) -> (id, <key>)` FK to ten
# tables. The edge it forbids is writable at `0016` (the FK there is a plain
# single-column self-FK, so no bypass is needed to forge it) and `0017` must
# refuse to proceed rather than repair it: re-pointing or deleting a canonical
# employee version edge is a destructive decision a migration has no authority to
# make. Both scan SHAPES are covered, because they are separate SQL: the nine
# `employee_id`-bearing chains and the `travel_segment` chain, which is keyed on
# `trip_id` because the table carries no `employee_id`.


def _assert_0017_aborted(instance: _Scratch, table: str, existing_fk: str) -> str:
    """The upgrade refused, and NOTHING was written or dropped."""
    with pytest.raises(RuntimeError) as excinfo:
        _upgrade(instance, F7_HEAD)
    message = str(excinfo.value)
    # Still at 0016: no revision bump, so the failed run left no partial state.
    assert instance.revision() == F7_BEFORE
    # The new objects do not exist...
    assert instance.has_constraint(table, f"fk_{table}_supersedes_same_owner") is False
    assert instance.has_constraint(table, f"uq_{table}_id_employee_id") is False
    # ...and the PRE-EXISTING FK was not dropped either. This is the half that a
    # "did the new FK appear" assertion alone would miss: `upgrade()` drops the
    # old FK before creating the new one, so a partial run could leave the table
    # with NO supersedes FK at all and still look "not upgraded".
    assert instance.has_constraint(table, existing_fk) is True
    return message


def test_0017_preflight_aborts_on_a_cross_employee_chain() -> None:
    instance = _Scratch(F7_BEFORE)
    try:
        instance.execute(
            "INSERT INTO employee (legal_name, active) VALUES ('f7-owner-a', true), "
            "('f7-owner-b', true)"
        )
        instance.execute(
            "INSERT INTO overtime_authorization (id, employee_id, valid_from, version) "
            "SELECT '00000000-0000-4000-8000-0000000000a1', id, TIMESTAMPTZ '2026-07-01', 1 "
            "FROM employee WHERE legal_name = 'f7-owner-a'"
        )
        instance.execute(
            "INSERT INTO overtime_authorization (id, employee_id, valid_from, version, "
            "supersedes_id) SELECT '00000000-0000-4000-8000-0000000000a2', id, "
            "TIMESTAMPTZ '2026-07-01', 2, '00000000-0000-4000-8000-0000000000a1' "
            "FROM employee WHERE legal_name = 'f7-owner-b'"
        )
        message = _assert_0017_aborted(
            instance, "overtime_authorization", "fk_overtime_auth_supersedes"
        )
        # The refusal NAMES the offending keys, so an operator can find the rows.
        assert "fk_overtime_authorization_supersedes_same_owner" in message
        for key in ("child_id", "parent_id", "child_owner", "parent_owner"):
            assert key in message
        assert "No DDL was applied" in message
    finally:
        instance.close()


def test_0017_preflight_aborts_on_a_cross_trip_segment_chain() -> None:
    instance = _Scratch(F7_BEFORE)
    try:
        instance.execute(
            "INSERT INTO employee (legal_name, active) VALUES ('f7-trip-a', true), "
            "('f7-trip-b', true)"
        )
        instance.execute(
            "INSERT INTO business_trip (id, employee_id, start_at, end_at, start_origin) "
            "SELECT '00000000-0000-4000-8000-0000000000b1', id, TIMESTAMPTZ '2026-07-01', "
            "TIMESTAMPTZ '2026-07-02', 'HOME' FROM employee WHERE legal_name = 'f7-trip-a'"
        )
        instance.execute(
            "INSERT INTO business_trip (id, employee_id, start_at, end_at, start_origin) "
            "SELECT '00000000-0000-4000-8000-0000000000b2', id, TIMESTAMPTZ '2026-07-03', "
            "TIMESTAMPTZ '2026-07-04', 'HOME' FROM employee WHERE legal_name = 'f7-trip-b'"
        )
        instance.execute(
            "INSERT INTO travel_segment (id, trip_id, start_at, end_at, classification) VALUES "
            "('00000000-0000-4000-8000-0000000000c1', "
            "'00000000-0000-4000-8000-0000000000b1', TIMESTAMPTZ '2026-07-01', "
            "TIMESTAMPTZ '2026-07-01 02:00', 'PASSIVE_TRAVEL')"
        )
        # A segment of trip B superseding a segment of trip A: writable at 0016,
        # because the plain self-FK only checks that the row exists.
        instance.execute(
            "INSERT INTO travel_segment (id, trip_id, start_at, end_at, classification, "
            "supersedes_id) VALUES ('00000000-0000-4000-8000-0000000000c2', "
            "'00000000-0000-4000-8000-0000000000b2', TIMESTAMPTZ '2026-07-03', "
            "TIMESTAMPTZ '2026-07-03 02:00', 'PASSIVE_TRAVEL', "
            "'00000000-0000-4000-8000-0000000000c1')"
        )
        message = _assert_0017_aborted(
            instance, "travel_segment", "travel_segment_supersedes_id_fkey"
        )
        assert "fk_travel_segment_supersedes_same_trip" in message
        for key in ("child_id", "parent_id", "child_trip", "parent_trip"):
            assert key in message
    finally:
        instance.close()


# ---------------------------------------------------------------------------
# Upgrade: retire the placeholder, rewrite nothing
# ---------------------------------------------------------------------------


def test_0016_retires_the_placeholder_without_rewriting_any_snapshot(scratch: _Scratch) -> None:
    """The inverse of the previously planned repair, and the EINV-07 discriminator."""
    with Session(scratch.engine) as session:
        employee = Employee(
            external_employee_id="mig-0001", legal_name="Migration fixture", active=True
        )
        session.add(employee)
        session.flush()
        period = WorkPeriod(
            employee_id=employee.id, period_month=date(2026, 7, 1), status=PeriodStatus.OPEN
        )
        session.add(period)
        session.flush()
        policy = session.scalar(
            select(PolicyVersion).where(PolicyVersion.policy_key == "ordinary-working-time")
        )
        assert policy is not None
        placeholder_id = session.scalar(
            text("SELECT id FROM calendar_version WHERE version = 'default-v1'")
        )
        snapshot = PeriodCalculationSnapshot(
            work_period_id=period.id,
            snapshot_no=1,
            policy_version_id=policy.id,
            calendar_version_ids=[str(placeholder_id)],
            engine_build=policy.engine_rule_set,
            input_fingerprint="0" * 64,
            result_json={"daily": [], "marker": "frozen"},
        )
        session.add(snapshot)
        session.commit()
        frozen = (
            [str(value) for value in snapshot.calendar_version_ids],
            snapshot.input_fingerprint,
            snapshot.result_json,
        )
        snapshot_id = snapshot.id

    _upgrade(scratch, HEAD)
    assert scratch.revision() == HEAD

    with Session(scratch.engine) as session:
        assert (
            session.scalar(
                text("SELECT count(*) FROM calendar_version WHERE version = 'default-v1'")
            )
            == 0
        )
        blank = session.scalar(
            text("SELECT count(*) FROM calendar_version WHERE btrim(source) = ''")
        )
        assert blank == 0
        row = session.get(PeriodCalculationSnapshot, snapshot_id)
        assert row is not None
        assert (
            [str(value) for value in row.calendar_version_ids],
            row.input_fingerprint,
            row.result_json,
        ) == frozen


def test_0016_creates_the_day_status_table_and_its_constraints(scratch: _Scratch) -> None:
    _upgrade(scratch, HEAD)
    with scratch.engine.connect() as connection:
        indexes = {
            row[0]
            for row in connection.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'day_status_event'")
            )
        }
        constraints = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT conname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                    "WHERE t.relname = 'day_status_event'"
                )
            )
        }
    assert "uq_day_status_event_root" in indexes
    assert "ix_day_status_event_employee_day" in indexes
    assert {
        "ck_day_status_event_version_positive",
        "ck_day_status_event_day_part",
        "uq_day_status_event_supersedes_id",
        "fk_day_status_event_employee",
    } <= constraints


def test_0016_downgrade_refuses_while_day_status_facts_exist(scratch: _Scratch) -> None:
    _upgrade(scratch, HEAD)
    with Session(scratch.engine) as session:
        employee = Employee(
            external_employee_id="mig-0002", legal_name="Migration fixture 2", active=True
        )
        session.add(employee)
        session.flush()
        session.add(
            DayStatusEvent(
                employee_id=employee.id,
                day=date(2032, 3, 8),
                category=DayStatusCategory.VACATION,
                portion=DayStatusPortion.FULL,
                day_part=DayStatusDayPart.ALL,
                version=1,
            )
        )
        session.commit()
    with pytest.raises(RuntimeError) as excinfo:
        _downgrade(scratch, BEFORE)
    assert "downgrade refused" in str(excinfo.value)
    assert scratch.revision() == HEAD


def test_0016_downgrade_refuses_while_a_published_edition_exists(scratch: _Scratch) -> None:
    """The mirror of the upgrade pre-flight (review M3).

    A downgrade cannot un-publish an institutional edition: it would re-insert
    the retired placeholder beside it, and the next upgrade would abort on
    ``no-unexpected-calendar-data``, stranding the database at ``0015``.
    """
    _upgrade(scratch, HEAD)
    scratch.execute(
        "INSERT INTO calendar_version (calendar_key, version, valid_from, valid_to, source) "
        "VALUES ('primary', 'operator-published-v1', DATE '2027-01-01', NULL, 'TEST')"
    )
    with pytest.raises(RuntimeError) as excinfo:
        _downgrade(scratch, BEFORE)
    assert "downgrade refused" in str(excinfo.value)
    assert scratch.revision() == HEAD
    # The refusal is non-destructive: the edition and the schema both survive.
    assert scratch.has_table("day_status_event")
    with Session(scratch.engine) as session:
        assert (
            session.scalar(
                text(
                    "SELECT count(*) FROM calendar_version WHERE version = 'operator-published-v1'"
                )
            )
            == 1
        )


def test_0016_downgrade_and_re_upgrade_round_trips(scratch: _Scratch) -> None:
    _upgrade(scratch, HEAD)
    _downgrade(scratch, BEFORE)
    assert scratch.revision() == BEFORE
    assert scratch.has_column("calendar_version", "source") is False
    assert not scratch.has_table("day_status_event")
    # The downgrade re-inserts the placeholder (with a new id, documented).
    with Session(scratch.engine) as session:
        assert (
            session.scalar(
                text("SELECT count(*) FROM calendar_version WHERE version = 'default-v1'")
            )
            == 1
        )
    _upgrade(scratch, HEAD)
    assert scratch.revision() == HEAD


# ---------------------------------------------------------------------------
# Restore verification: the two new domains
# ---------------------------------------------------------------------------


def test_backup_manifest_counts_day_status_event() -> None:
    tables = {table.name for table in _metadata_tables()}
    assert {"calendar_version", "calendar_day", "day_status_event"} <= tables


def test_the_new_domains_are_registered() -> None:
    assert "calendar" in DOMAIN_NAMES
    assert "day status" in DOMAIN_NAMES


def test_the_restore_verifier_accepts_a_clean_f5_database(db_session: Session) -> None:
    failures = _domain_checks(db_session)
    assert failures["calendar"] == []
    assert failures["day status"] == []


def test_the_restore_verifier_reports_a_calendar_day_outside_its_edition(
    db_session: Session,
) -> None:
    edition = CalendarVersion(
        calendar_key="primary",
        version="verify-range-v1",
        valid_from=date(2032, 1, 1),
        valid_to=date(2032, 1, 31),
        source="TEST",
    )
    db_session.add(edition)
    db_session.flush()
    db_session.add(
        CalendarDay(
            calendar_version_id=edition.id,
            day=date(2032, 3, 1),
            day_class=CalendarDayClass.PUBLIC_HOLIDAY,
        )
    )
    db_session.flush()
    assert any(
        "outside its own edition" in failure for failure in _domain_checks(db_session)["calendar"]
    )


def test_the_restore_verifier_reports_a_duplicate_edition_effective_date(
    db_session: Session,
) -> None:
    """A restored database can carry what the writer would refuse.

    The unique index prevents this at write time, so the restored-database shape
    is constructed by dropping the index inside the test transaction (PostgreSQL
    DDL is transactional, so the rollback restores it). Two editions of one key
    sharing an effective date make resolution ambiguous — that is the failure the
    check encodes, and it must actually be exercised.
    """
    db_session.execute(text("DROP INDEX uq_calendar_version_effective_from"))
    for version in ("verify-dup-v1", "verify-dup-v2"):
        db_session.add(
            CalendarVersion(
                calendar_key="primary",
                version=version,
                valid_from=date(2032, 1, 1),
                valid_to=None,
                source="TEST",
            )
        )
    db_session.flush()
    failures = _domain_checks(db_session)["calendar"]
    assert any("share an effective date" in failure for failure in failures), failures


def test_the_restore_verifier_reports_an_uncovered_frozen_snapshot_period(
    db_session: Session,
) -> None:
    """A frozen snapshot whose month no edition covers is a real failure.

    Written as a coverage check, not a referential check on the JSONB id list —
    pre-F5 snapshots legitimately name the retired placeholder.
    """
    from sqlalchemy import delete

    db_session.execute(delete(CalendarDay))
    db_session.execute(delete(CalendarVersion))
    db_session.flush()
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    period = WorkPeriod(
        employee_id=employee.id, period_month=date(2032, 7, 1), status=PeriodStatus.OPEN
    )
    db_session.add(period)
    db_session.flush()
    policy = db_session.scalar(
        select(PolicyVersion).where(PolicyVersion.policy_key == "ordinary-working-time")
    )
    assert policy is not None
    db_session.add(
        PeriodCalculationSnapshot(
            work_period_id=period.id,
            snapshot_no=1,
            policy_version_id=policy.id,
            calendar_version_ids=["00000000-0000-0000-0000-000000000000"],
            engine_build=policy.engine_rule_set,
            input_fingerprint="0" * 64,
            result_json={"daily": []},
        )
    )
    db_session.flush()
    failures = _domain_checks(db_session)["calendar"]
    assert any("not covered by any calendar edition" in failure for failure in failures)


def test_the_restore_verifier_reports_a_forked_day_status_chain(db_session: Session) -> None:
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    root = DayStatusEvent(
        employee_id=employee.id,
        day=date(2032, 3, 8),
        category=DayStatusCategory.VACATION,
        portion=DayStatusPortion.FULL,
        day_part=DayStatusDayPart.ALL,
        version=1,
    )
    db_session.add(root)
    db_session.flush()
    # Two successors on one row is impossible through the API (the unique
    # constraint forbids it); the check is the restore-time backstop, so the
    # constraint is dropped for the duration of this transaction.
    db_session.execute(
        text("ALTER TABLE day_status_event DROP CONSTRAINT uq_day_status_event_supersedes_id")
    )
    for version, day_part in ((2, DayStatusDayPart.MORNING), (3, DayStatusDayPart.AFTERNOON)):
        db_session.add(
            DayStatusEvent(
                employee_id=employee.id,
                day=date(2032, 3, 8),
                category=DayStatusCategory.VACATION,
                portion=DayStatusPortion.HALF,
                day_part=day_part,
                version=version,
                supersedes_id=root.id,
            )
        )
    db_session.flush()
    assert any(
        "more than one successor" in failure for failure in _domain_checks(db_session)["day status"]
    )


def test_the_restore_verifier_reports_two_current_day_status_heads_for_one_slot(
    db_session: Session,
) -> None:
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    db_session.execute(text("DROP INDEX uq_day_status_event_root"))
    for version in (1, 2):
        db_session.add(
            DayStatusEvent(
                employee_id=employee.id,
                day=date(2032, 3, 8),
                category=DayStatusCategory.VACATION,
                portion=DayStatusPortion.FULL,
                day_part=DayStatusDayPart.ALL,
                version=version,
            )
        )
    db_session.flush()
    assert any(
        "one employee/day/category/day-part slot" in failure
        for failure in _domain_checks(db_session)["day status"]
    )


def test_the_restore_verifier_accepts_legal_edition_overlap(db_session: Session) -> None:
    """The NEGATIVE direction: two legal overlapping editions must NOT be reported.

    Fails if a non-overlap check is implemented instead of a coverage check.
    """
    for version, valid_from, valid_to in (
        ("verify-overlap-base", date(2032, 1, 1), date(2032, 12, 31)),
        ("verify-overlap-overlay", date(2032, 6, 1), date(2032, 6, 30)),
    ):
        db_session.add(
            CalendarVersion(
                calendar_key="primary",
                version=version,
                valid_from=valid_from,
                valid_to=valid_to,
                source="TEST",
            )
        )
    db_session.flush()
    assert _domain_checks(db_session)["calendar"] == []


def test_the_restore_verifier_reports_an_orphan_day_status_employee(db_session: Session) -> None:
    """A restored database can carry a row the FK would refuse.

    The FK prevents this at write time, so the restored shape is constructed by
    dropping the constraint inside the test transaction (transactional DDL: the
    rollback restores it) and inserting a row whose employee does not exist.
    """
    db_session.execute(
        text("ALTER TABLE day_status_event DROP CONSTRAINT fk_day_status_event_employee")
    )
    orphan_id = uuid.uuid4()
    assert db_session.scalar(select(Employee.id).where(Employee.id == orphan_id)) is None
    db_session.add(
        DayStatusEvent(
            employee_id=orphan_id,
            day=date(2032, 3, 8),
            category=DayStatusCategory.SICK,
            portion=DayStatusPortion.FULL,
            day_part=DayStatusDayPart.ALL,
            version=1,
        )
    )
    db_session.flush()
    failures = _domain_checks(db_session)["day status"]
    assert any("references a missing employee" in failure for failure in failures), failures


def test_the_restore_verifier_reports_a_half_status_without_a_day_part(db_session: Session) -> None:
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    db_session.execute(
        text("ALTER TABLE day_status_event DROP CONSTRAINT ck_day_status_event_day_part")
    )
    db_session.execute(
        text(
            "INSERT INTO day_status_event "
            "(employee_id, day, category, portion, day_part, source, version) "
            "VALUES (:employee_id, DATE '2032-03-08', 'VACATION', 'HALF', 'ALL', 'EMPLOYEE', 1)"
        ),
        {"employee_id": employee.id},
    )
    assert any(
        "does not name MORNING or AFTERNOON" in failure
        for failure in _domain_checks(db_session)["day status"]
    )


def test_the_restore_verifier_does_not_require_the_retired_placeholder(
    db_session: Session,
) -> None:
    """Pre-F5 snapshots name the retired id; coverage, not existence, is checked."""
    assert (
        db_session.scalar(select(CalendarVersion.id).where(CalendarVersion.version == "default-v1"))
        is None
    )
    assert _domain_checks(db_session)["calendar"] == []


def test_a_corrupt_calendar_is_reported_under_its_own_domain(db_session: Session) -> None:
    """Not silently attributed to the fallback ``employee`` domain."""
    edition = CalendarVersion(
        calendar_key="primary",
        version="verify-domain-v1",
        valid_from=date(2032, 1, 1),
        valid_to=date(2032, 1, 31),
        source="TEST",
    )
    db_session.add(edition)
    db_session.flush()
    db_session.add(
        CalendarDay(
            calendar_version_id=edition.id,
            day=date(2032, 5, 1),
            day_class=CalendarDayClass.PUBLIC_HOLIDAY,
        )
    )
    db_session.flush()
    failures = _domain_checks(db_session)
    assert failures["calendar"]
    assert not any("calendar" in failure for failure in failures["employee"])


def test_the_verifier_runs_on_the_migrated_test_schema(db_engine: Engine) -> None:
    """Sanity: the checks execute against the real migrated schema."""
    with get_session_factory()() as session:
        failures = _domain_checks(session)
    assert set(failures) >= {"calendar", "day status"}


def test_the_restore_verifier_reports_a_cross_trip_segment_lineage(
    db_session: Session,
) -> None:
    """``travel_segment`` is the one chain whose owner is not an employee column.

    It has no ``employee_id``; its predecessor link is scoped by the trip, and
    ``business_trip.employee_id`` is NOT NULL, so same-trip implies same-owner.
    While the lineage sweep hard-coded ``employee_id`` it silently SKIPPED this
    table, so the sweep's own claim ("a table added later cannot be forgotten")
    was false for it. The corrupt shape is forged by dropping the composite FK
    inside the test transaction (transactional DDL: the rollback restores it),
    which is also the only way it can exist — a database restored before `0017`.
    """
    from app.backup import _domain_checks

    first, second = db_session.scalars(select(Employee).order_by(Employee.id).limit(2)).all()
    trips = [
        BusinessTrip(
            employee_id=owner.id,
            start_at=datetime(2033, 5, 4, 6, tzinfo=UTC),
            end_at=datetime(2033, 5, 4, 10, tzinfo=UTC),
            start_origin=TripStartOrigin.HOME,
            source="EMPLOYEE_DECLARED",
            version=1,
        )
        for owner in (first, second)
    ]
    db_session.add_all(trips)
    db_session.flush()
    parent = TravelSegment(
        trip_id=trips[0].id,
        start_at=trips[0].start_at,
        end_at=trips[0].end_at,
        classification=TravelSegmentClassification.PASSIVE_TRAVEL,
        basis=TravelSegmentBasis.ACTUAL,
        version=1,
    )
    db_session.add(parent)
    db_session.flush()
    assert not [
        entry for entry in _domain_checks(db_session)["travel"] if "another employee" in entry
    ]

    db_session.execute(
        text("ALTER TABLE travel_segment DROP CONSTRAINT fk_travel_segment_supersedes_same_trip")
    )
    db_session.add(
        TravelSegment(
            trip_id=trips[1].id,
            start_at=trips[1].start_at,
            end_at=trips[1].end_at,
            classification=TravelSegmentClassification.PASSIVE_TRAVEL,
            basis=TravelSegmentBasis.ACTUAL,
            version=2,
            supersedes_id=parent.id,
        )
    )
    db_session.flush()

    failures = _domain_checks(db_session)["travel"]
    assert any("another employee" in entry for entry in failures), failures


# ---------------------------------------------------------------------------
# 0019 pre-flight: the schema guard and the head-cache scan
# ---------------------------------------------------------------------------
#
# `0019` adds four columns to `ocd_assignment`, one to `shift_assignment`, six
# constraints and two enum labels. Its pre-flight has TWO shapes, so there are
# two abort tests:
#
# 1. a SCHEMA GUARD that refuses when any 0019 column already exists — the
#    partially-applied-migration / hand-edited-schema / re-run case. It is what
#    proves the new columns are ABSENT, which is why no data scan needs to look
#    for them.
# 2. a DATA SCAN for a `shift_assignment` whose `planned_start_at` /
#    `planned_end_at` disagree with its approved revision. Those two columns are
#    a denormalized HEAD CACHE of the revision, and the new write path maintains
#    them in lockstep; a pre-existing disagreement must be surfaced, never
#    silently repaired (re-pointing canonical employee facts is not a migration's
#    call).
#
# Both are forged at 0018 with owner-level DML/DDL inside the scratch database —
# no superuser tricks and never `session_replication_role`.

#: Every object 0019 creates, as the assertions below need it: the 0018 state
#: must be provably intact (new object absent, pre-existing object NOT dropped).
_0019_NEW_CONSTRAINTS: tuple[tuple[str, str], ...] = (
    ("ocd_assignment", "ck_ocd_assignment_version_positive"),
    ("ocd_assignment", "fk_ocd_assignment_supersedes_same_owner"),
    ("ocd_assignment", "uq_ocd_assignment_id_employee_id"),
    ("ocd_assignment", "uq_ocd_assignment_supersedes_id"),
    ("ocd_assignment", "uq_ocd_assignment_client_request_id"),
    ("shift_assignment", "uq_shift_assignment_client_request_id"),
)

_0019_NEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("ocd_assignment", "version"),
    ("ocd_assignment", "supersedes_id"),
    ("ocd_assignment", "client_request_id"),
    ("ocd_assignment", "reported_notice_at"),
    ("shift_assignment", "client_request_id"),
)

#: Objects that exist at 0018 and must survive a refused upgrade. The upgrade
#: itself drops nothing, but a partial run that failed half-way through the DDL
#: is exactly what "the old object was NOT dropped" is meant to catch.
_0018_CONSTRAINTS: tuple[tuple[str, str], ...] = (
    ("ocd_assignment", "ck_ocd_assignment_end_after_start"),
    ("ocd_assignment", "fk_ocd_assignment_employee"),
    ("shift_assignment", "fk_shift_assignment_approved_revision"),
    ("shift_assignment", "ck_shift_assignment_order"),
)


def _assert_0019_upgrade_aborted(
    instance: _Scratch, pre_created: tuple[tuple[str, str], ...] = ()
) -> str:
    """The upgrade refused, and the database is still exactly at 0018.

    Four assertions, not one: no revision bump, the new objects absent, the
    PRE-EXISTING objects not dropped, and the operator told no DDL ran.

    ``pre_created`` names the objects a test forged itself (the schema-guard
    case): those must still be present afterwards, not "absent".
    """
    with pytest.raises(RuntimeError) as excinfo:
        _upgrade(instance, F8_HEAD)
    message = str(excinfo.value)
    assert instance.revision() == F8_BEFORE
    for table, name in _0019_NEW_CONSTRAINTS:
        assert instance.has_constraint(table, name) is False, f"{name} appeared on {table}"
    for table, column in _0019_NEW_COLUMNS:
        if (table, column) in pre_created:
            assert instance.has_column(table, column) is True, f"{table}.{column} was dropped"
            continue
        assert instance.has_column(table, column) is False, f"{table}.{column} appeared"
    assert instance.has_enum_label("shift_assignment_status", "WITHDRAWN") is False
    assert instance.has_enum_label("shift_deviation_type", "ABORTED_BY_COMPANY") is False
    for table, name in _0018_CONSTRAINTS:
        assert instance.has_constraint(table, name) is True, f"{name} was dropped from {table}"
    assert "No DDL was applied" in message
    return message


def test_0019_preflight_aborts_on_a_shift_head_cache_disagreement(
    scratch_0018: _Scratch,
) -> None:
    """The one data scan that is both valid and meaningful at 0018.

    ``shift_assignment.planned_start_at``/``planned_end_at`` are a denormalized
    cache of the approved revision. The row is written with the cache disagreeing
    by an hour — legal at 0018, because nothing there keeps the two in step.
    """
    scratch_0018.execute("INSERT INTO employee (legal_name, active) VALUES ('f8-shift', true)")
    scratch_0018.execute(
        "INSERT INTO shift_plan (plan_key, valid_from, source) "
        "VALUES ('f8-plan', DATE '2026-07-01', 'EMPLOYEE_RECORDED')"
    )
    scratch_0018.execute(
        "INSERT INTO shift_assignment (id, shift_plan_id, employee_id, planned_start_at, "
        "planned_end_at, status) SELECT :id, p.id, e.id, TIMESTAMPTZ '2026-07-06 06:00+02', "
        "TIMESTAMPTZ '2026-07-06 14:00+02', 'PLANNED' FROM employee e, shift_plan p "
        "WHERE e.legal_name = 'f8-shift' AND p.plan_key = 'f8-plan'",
        id="00000000-0000-4000-8000-0000000000f1",
    )
    scratch_0018.execute(
        "INSERT INTO shift_plan_revision (id, shift_assignment_id, revision_no, planned_start_at, "
        "planned_end_at, changed_at, source) VALUES "
        "('00000000-0000-4000-8000-0000000000f2', '00000000-0000-4000-8000-0000000000f1', 1, "
        "TIMESTAMPTZ '2026-07-06 07:00+02', TIMESTAMPTZ '2026-07-06 15:00+02', "
        "TIMESTAMPTZ '2026-07-01 09:00+02', 'EMPLOYEE_RECORDED')"
    )
    scratch_0018.execute(
        "UPDATE shift_assignment SET approved_revision_id = "
        "'00000000-0000-4000-8000-0000000000f2' "
        "WHERE id = '00000000-0000-4000-8000-0000000000f1'"
    )
    message = _assert_0019_upgrade_aborted(scratch_0018)
    # The refusal NAMES the scan and the offending row, so an operator can find it.
    assert "shift-head-cache-disagreement" in message
    assert "00000000-0000-4000-8000-0000000000f1" in message
    for key in ("id", "assignment_start", "assignment_end", "revision_start", "revision_end"):
        assert key in message


def test_0019_preflight_schema_guard_aborts_on_an_existing_new_column(
    scratch_0018: _Scratch,
) -> None:
    """The second shape: a 0019 column already present.

    Forged with owner-level DDL inside the scratch database (PostgreSQL DDL is
    transactional, and this database is ours alone). This is what a partially
    applied migration, a hand-edited schema or a re-run leaves behind, and the
    guard must refuse rather than try to add the column a second time.
    """
    scratch_0018.execute("ALTER TABLE ocd_assignment ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
    message = _assert_0019_upgrade_aborted(
        scratch_0018, pre_created=(("ocd_assignment", "version"),)
    )
    assert "ocd_assignment.version" in message
    assert "already exist(s)" in message


def test_0019_schema_guard_refuses_a_re_run_after_the_upgrade(scratch_0018: _Scratch) -> None:
    """Re-running the upgrade over an already-migrated schema refuses.

    Alembic treats "upgrade to the revision we are already at" as a no-op, so a
    literal second `upgrade 0019` would never execute the migration body at all.
    The state a re-run actually produces — the 0019 objects present while the
    version table still says 0018 — is forged by stamping the version back, which
    is also what a restore that recovered the schema but not `alembic_version`
    looks like.
    """
    _upgrade(scratch_0018, F8_HEAD)
    assert scratch_0018.revision() == F8_HEAD
    _stamp(scratch_0018, F8_BEFORE)

    with pytest.raises(RuntimeError) as excinfo:
        _upgrade(scratch_0018, F8_HEAD)
    message = str(excinfo.value)
    assert "already exist(s)" in message
    assert "No DDL was applied" in message
    # The refused run dropped nothing and bumped nothing.
    assert scratch_0018.revision() == F8_BEFORE
    for table, name in _0019_NEW_CONSTRAINTS:
        assert scratch_0018.has_constraint(table, name) is True, f"{name} was dropped"
    for table, column in _0019_NEW_COLUMNS:
        assert scratch_0018.has_column(table, column) is True, f"{table}.{column} was dropped"


# ---------------------------------------------------------------------------
# 0019 downgrade: one named refusal per 0019-only persisted value
# ---------------------------------------------------------------------------
#
# The downgrade is deliberately NOT lossless: dropping `version`/`supersedes_id`
# leaves N historical rows that revision-0018 code reads as N INDEPENDENT
# availabilities — duplicated in the employee's own list, all counted by
# `OCD-003`, each able to receive its own call, with the head no longer
# distinguishable from its history. So every 0019-only persisted value refuses,
# and nothing is dropped when any refusal fires.
#
# The two enum-label cases need care: PostgreSQL forbids USING a newly added
# enum value in the same transaction that added it. `_Scratch.execute` opens its
# own transaction, so the upgrade is already committed by the time the row is
# written — the label is usable, as it is in production from the next request.

_OCD_ROOT = "00000000-0000-4000-8000-0000000000a1"
_OCD_CHILD = "00000000-0000-4000-8000-0000000000a2"
_SHIFT_ID = "00000000-0000-4000-8000-0000000000b1"
_DEVIATION_ID = "00000000-0000-4000-8000-0000000000c1"

_OCD_ROOT_INSERT = (
    "INSERT INTO ocd_assignment (id, employee_id, start_at, end_at, source, status) "
    "SELECT :id, id, TIMESTAMPTZ '2026-07-01 20:00+02', TIMESTAMPTZ '2026-07-02 06:00+02', "
    "'EMPLOYEE_RECORDED', 'SCHEDULED' FROM employee WHERE legal_name = 'f8-owner'"
)
_OCD_CHILD_INSERT = (
    "INSERT INTO ocd_assignment (id, employee_id, start_at, end_at, source, status, supersedes_id) "
    "SELECT :id, id, TIMESTAMPTZ '2026-07-02 20:00+02', TIMESTAMPTZ '2026-07-03 06:00+02', "
    "'EMPLOYEE_RECORDED', 'SCHEDULED', :supersedes_id FROM employee WHERE legal_name = 'f8-owner'"
)

#: ``label -> (statement, params)`` forges. Each writes exactly ONE 0019-only
#: value, so the refusal it must produce is unambiguous.
_DOWNGRADE_FORGES: dict[str, tuple[tuple[str, dict[str, object]], ...]] = {
    "ocd_assignment.supersedes_id": (
        (_OCD_ROOT_INSERT, {"id": _OCD_ROOT}),
        (_OCD_CHILD_INSERT, {"id": _OCD_CHILD, "supersedes_id": _OCD_ROOT}),
    ),
    "ocd_assignment.version": (
        (_OCD_ROOT_INSERT, {"id": _OCD_ROOT}),
        ("UPDATE ocd_assignment SET version = 2 WHERE id = :id", {"id": _OCD_ROOT}),
    ),
    "ocd_assignment.client_request_id": (
        (_OCD_ROOT_INSERT, {"id": _OCD_ROOT}),
        (
            "UPDATE ocd_assignment SET client_request_id = :value WHERE id = :id",
            {"id": _OCD_ROOT, "value": "00000000-0000-4000-8000-0000000000a9"},
        ),
    ),
    "ocd_assignment.reported_notice_at": (
        (_OCD_ROOT_INSERT, {"id": _OCD_ROOT}),
        (
            "UPDATE ocd_assignment SET reported_notice_at = TIMESTAMPTZ '2026-07-01 15:00+02' "
            "WHERE id = :id",
            {"id": _OCD_ROOT},
        ),
    ),
    "shift_assignment.client_request_id": (
        (
            "UPDATE shift_assignment SET client_request_id = :value WHERE id = :id",
            {"id": _SHIFT_ID, "value": "00000000-0000-4000-8000-0000000000b9"},
        ),
    ),
    "shift_assignment.status": (
        ("UPDATE shift_assignment SET status = 'WITHDRAWN' WHERE id = :id", {"id": _SHIFT_ID}),
    ),
    "shift_deviation.deviation_type": (
        (
            "INSERT INTO shift_deviation (id, employee_id, shift_assignment_id, deviation_type, "
            "version) SELECT :id, a.employee_id, a.id, 'ABORTED_BY_COMPANY', 1 "
            "FROM shift_assignment a WHERE a.id = :assignment_id",
            {"id": _DEVIATION_ID, "assignment_id": _SHIFT_ID},
        ),
    ),
}


def _forge_plan_state(instance: _Scratch) -> None:
    """One employee, one plan, one PLANNED assignment — the minimum the
    shift-side refusals need.

    Deliberately WITHOUT an approved revision, so the head-cache disagreement
    scan is not implicated: these tests are about the downgrade.
    """
    instance.execute("INSERT INTO employee (legal_name, active) VALUES ('f8-owner', true)")
    instance.execute(
        "INSERT INTO shift_plan (plan_key, valid_from, source) "
        "VALUES ('f8-plan', DATE '2026-07-01', 'EMPLOYEE_RECORDED')"
    )
    instance.execute(
        "INSERT INTO shift_assignment (id, shift_plan_id, employee_id, planned_start_at, "
        "planned_end_at, status) SELECT :id, p.id, e.id, TIMESTAMPTZ '2026-07-06 06:00+02', "
        "TIMESTAMPTZ '2026-07-06 14:00+02', 'PLANNED' FROM employee e, shift_plan p "
        "WHERE e.legal_name = 'f8-owner' AND p.plan_key = 'f8-plan'",
        id=_SHIFT_ID,
    )


@pytest.mark.parametrize("label", sorted(_DOWNGRADE_FORGES))
def test_0019_downgrade_refuses_every_0019_only_value(scratch_0018: _Scratch, label: str) -> None:
    _upgrade(scratch_0018, F8_HEAD)
    assert scratch_0018.revision() == F8_HEAD
    _forge_plan_state(scratch_0018)
    for statement, params in _DOWNGRADE_FORGES[label]:
        scratch_0018.execute(statement, **params)

    with pytest.raises(RuntimeError) as excinfo:
        _downgrade(scratch_0018, F8_BEFORE)
    message = str(excinfo.value)
    assert "0019 downgrade refused" in message
    assert f"use {label}:" in message
    # Nothing was dropped: the checks run before any DDL, and a half-downgraded
    # database would be worse than an un-downgraded one.
    assert scratch_0018.revision() == F8_HEAD
    for table, name in _0019_NEW_CONSTRAINTS:
        assert scratch_0018.has_constraint(table, name) is True, f"{name} was dropped"
    for table, column in _0019_NEW_COLUMNS:
        assert scratch_0018.has_column(table, column) is True, f"{table}.{column} was dropped"


def test_0019_downgrade_refusal_names_the_stranded_state(scratch_0018: _Scratch) -> None:
    """The refusal must say WHY 0018 cannot represent the state, not just that it can't."""
    _upgrade(scratch_0018, F8_HEAD)
    _forge_plan_state(scratch_0018)
    scratch_0018.execute(
        "UPDATE shift_assignment SET status = 'WITHDRAWN' WHERE id = :id", id=_SHIFT_ID
    )
    with pytest.raises(RuntimeError) as excinfo:
        _downgrade(scratch_0018, F8_BEFORE)
    message = str(excinfo.value)
    assert "misread a WITHDRAWN plan as PLANNED" in message
    assert "Restore from a verified backup instead" in message


# ---------------------------------------------------------------------------
# 0019 ORM <-> migration parity for the two idempotency keys
# ---------------------------------------------------------------------------
#
# The plan's step 3(b) requires BOTH unique constraints to be declared with their
# exact names in the models as well as the migration. The name is part of the
# contract, not cosmetic: the write paths compare `constraint_name(exc)` against
# exactly one module constant (`SHIFT_ASSIGNMENT_IDEMPOTENCY_CONSTRAINT`,
# `OCD_IDEMPOTENCY_CONSTRAINT`), so an auto-named or renamed constraint makes the
# recovery branch unreachable and re-raises an unrelated integrity failure. A
# `just migration-check` round trip CANNOT see this — it never consults the
# models — so the parity is asserted here, against the migration's own list.

#: `(model, table, constraint name)` for every 0019 idempotency key.
_0019_IDEMPOTENCY_KEYS = (
    (ShiftAssignment, "shift_assignment", "uq_shift_assignment_client_request_id"),
    (OcdAssignment, "ocd_assignment", "uq_ocd_assignment_client_request_id"),
)


def test_0019_idempotency_constraint_names_match_the_models() -> None:
    for model, table_name, name in _0019_IDEMPOTENCY_KEYS:
        # The name the migration creates and the name the model declares are the
        # same single name, so a drift on either side fails here.
        assert (table_name, name) in _0019_NEW_CONSTRAINTS
        table = cast(Table, model.__table__)
        assert table.name == table_name
        declared = sorted(
            str(constraint.name)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
            and [column.name for column in constraint.columns] == ["client_request_id"]
        )
        assert declared == [name], (table_name, declared)


# ---------------------------------------------------------------------------
# 0019 round trip: the labels are RETAINED, so the re-upgrade is idempotent
# ---------------------------------------------------------------------------


def test_0019_round_trips_without_a_duplicate_enum_value(scratch_0018: _Scratch) -> None:
    """`upgrade -> downgrade (safe, no data) -> upgrade again`.

    PostgreSQL cannot drop an enum value (`ALTER TYPE ... DROP VALUE` does not
    exist), so the downgrade RETAINS both labels and the re-upgrade must not
    blindly `ADD VALUE` again — that would fail with a duplicate-value error.
    """
    _upgrade(scratch_0018, F8_HEAD)
    assert scratch_0018.has_enum_label("shift_assignment_status", "WITHDRAWN") is True
    assert scratch_0018.has_enum_label("shift_deviation_type", "ABORTED_BY_COMPANY") is True

    _downgrade(scratch_0018, F8_BEFORE)
    assert scratch_0018.revision() == F8_BEFORE
    # The 0019 columns and constraints are gone...
    for table, column in _0019_NEW_COLUMNS:
        assert scratch_0018.has_column(table, column) is False, f"{table}.{column} survived"
    for table, name in _0019_NEW_CONSTRAINTS:
        assert scratch_0018.has_constraint(table, name) is False, f"{name} survived"
    # ...but the labels are deliberately RETAINED. That retention is what makes
    # the re-upgrade idempotent, so it is asserted, not tolerated.
    assert scratch_0018.has_enum_label("shift_assignment_status", "WITHDRAWN") is True
    assert scratch_0018.has_enum_label("shift_deviation_type", "ABORTED_BY_COMPANY") is True

    # No duplicate-enum-value error: `_add_enum_value` probes `pg_enum` first.
    _upgrade(scratch_0018, F8_HEAD)
    assert scratch_0018.revision() == F8_HEAD
    assert scratch_0018.has_enum_label("shift_assignment_status", "WITHDRAWN") is True
    assert scratch_0018.has_enum_label("shift_deviation_type", "ABORTED_BY_COMPANY") is True
    for table, name in _0019_NEW_CONSTRAINTS:
        assert scratch_0018.has_constraint(table, name) is True, f"{name} is missing"
    for table, column in _0019_NEW_COLUMNS:
        assert scratch_0018.has_column(table, column) is True, f"{table}.{column} is missing"
