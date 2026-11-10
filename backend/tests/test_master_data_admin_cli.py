"""F5 §5.11/§7.9 — the operator master-data CLI and the EINV-02 boundary.

``app.master_data_admin`` is the supported production writer for
``employment_contract`` and an authoritative ``work_schedule_version``: a CSV in,
the existing models and their constraints out. These tests drive the real seam
(``import_file`` and ``main``) against a ``tmp_path`` file and prove the
properties the design rests on:

- the import creates/refreshes legal metadata, the contract and the schedule,
  and nothing else;
- a re-run corrects in place (version bump) instead of duplicating rows;
- an employee-maintained (``EMPLOYEE``-source) schedule on the same date is
  refused;
- a contract row that governs no day is refused, naming the shadowing row;
- the format cannot carry an identity binding (EINV-02): the row model has no
  ``idp_subject`` field, an extra column is REFUSED rather than ignored, the
  module never names the column in code, and a real import leaves an existing
  binding byte-identical;
- onboarding cannot widen access: ``active`` and role assignments are untouched
  and a new row is created inactive and unbound;
- the lock order is fixed lock -> per-employee fact lock -> month reopen;
- ``--dry-run`` writes nothing.

Two deliberate deviations from the plan text are recorded where they are
asserted (the shadowing boundary and the raw ``idp_subject`` substring check);
each is written as a tripwire for the behaviour the code actually has.
"""

import csv
import io
import re
import tokenize
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest
from app import master_data_admin
from app.auth.models import RoleAssignment
from app.auth.roles import Role
from app.enums import MasterDataSource, PayGroupBucket, PeriodStatus
from app.identity.models import Employee, EmploymentContract, WorkScheduleVersion
from app.master_data_admin import (
    CSV_COLUMNS,
    ImportRefused,
    MasterDataRow,
    assert_contract_row_governs_a_day,
    import_file,
    main,
)
from app.worktime.models import WorkPeriod
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

SOURCE_REF = "CHG-F5-MASTER-0001"
BACKEND = Path(__file__).resolve().parents[1]
MODULE_PATH = BACKEND / "app" / "master_data_admin.py"

# Seeded employees (``app/dev_seed.py``): emp-0001 already carries an ADMIN_SEED
# contract and schedule from 2026-01-01, so the tests below import on
# far-future dates and never collide with the seed.
SEEDED_EXTERNAL_ID = "emp-0001"
SEEDED_SUBJECT = "employee"


def _weekdays(minutes: int, *, weekly: int | None = None) -> dict[str, object]:
    """Monday-Friday allocation; the sum must equal the weekly target."""
    return {
        "weekly_required_minutes": str(weekly if weekly is not None else minutes * 5),
        "mon_required_minutes": str(minutes),
        "tue_required_minutes": str(minutes),
        "wed_required_minutes": str(minutes),
        "thu_required_minutes": str(minutes),
        "fri_required_minutes": str(minutes),
        "sat_required_minutes": "0",
        "sun_required_minutes": "0",
    }


def _row(external_employee_id: str, valid_from: str, **overrides: object) -> dict[str, object]:
    """One CSV row; the default weekday allocation sums to the weekly target."""
    row: dict[str, object] = {
        "external_employee_id": external_employee_id,
        "legal_name": "Imported Person (synthetic)",
        "email": "imported.person@example.invalid",
        "valid_from": valid_from,
        "valid_to": "",
        "fte": "0.8",
        "pay_group_bucket": PayGroupBucket.LE_9B.value,
        "employment_start": "2030-01-01",
        "employment_end": "",
        **_weekdays(240),
    }
    row.update(overrides)
    return row


def _write_csv(
    path: Path,
    rows: list[dict[str, object]],
    *,
    extra_column: tuple[str, str] | None = None,
) -> Path:
    """Write the exact accepted header, the rows, and optionally one extra column."""
    fieldnames: list[str] = list(CSV_COLUMNS)
    if extra_column is not None:
        fieldnames.append(extra_column[0])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            payload = {name: row.get(name, "") for name in CSV_COLUMNS}
            if extra_column is not None:
                payload[extra_column[0]] = extra_column[1]
            writer.writerow(payload)
    return path


def _employee(session: Session, external_employee_id: str) -> Employee:
    employee = session.scalar(
        select(Employee).where(Employee.external_employee_id == external_employee_id)
    )
    assert employee is not None, f"employee {external_employee_id!r} missing"
    return employee


def _contracts(session: Session, employee_id: uuid.UUID) -> list[EmploymentContract]:
    return list(
        session.scalars(
            select(EmploymentContract)
            .where(EmploymentContract.employee_id == employee_id)
            .order_by(EmploymentContract.valid_from)
        )
    )


def _schedules(session: Session, employee_id: uuid.UUID) -> list[WorkScheduleVersion]:
    return list(
        session.scalars(
            select(WorkScheduleVersion)
            .where(WorkScheduleVersion.employee_id == employee_id)
            .order_by(WorkScheduleVersion.valid_from)
        )
    )


def _advisory_locks_held(session: Session) -> int:
    """Advisory locks this backend currently holds (the fixed lock is one)."""
    held = session.scalar(
        text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()")
    )
    return int(held or 0)


# ---------------------------------------------------------------------------
# Create / re-run / refuse
# ---------------------------------------------------------------------------


def test_import_creates_employee_contract_and_authoritative_schedule(
    db_session: Session, tmp_path: Path
) -> None:
    path = _write_csv(
        tmp_path / "people.csv",
        [_row("emp-f5-new", "2034-02-01", employment_start="2034-01-15")],
    )
    notes = import_file(db_session, path, SOURCE_REF)

    employee = _employee(db_session, "emp-f5-new")
    assert notes == [
        "employee  emp-f5-new: created (inactive, unbound)",
        "contract  2034-02-01: created",
        "schedule  2034-02-01: created",
    ]
    # Legal profile metadata only — and NOT active, NOT bound (EINV-02/INV-04).
    assert employee.legal_name == "Imported Person (synthetic)"
    assert employee.email == "imported.person@example.invalid"
    assert employee.active is False
    assert employee.idp_subject is None

    contracts = _contracts(db_session, employee.id)
    assert len(contracts) == 1
    contract = contracts[0]
    assert contract.valid_from == date(2034, 2, 1)
    assert contract.valid_to is None
    assert float(contract.fte or 0) == pytest.approx(0.8)
    assert contract.weekly_required_minutes == 1200
    assert contract.pay_group_bucket is PayGroupBucket.LE_9B
    assert contract.employment_start == date(2034, 1, 15)
    assert contract.employment_end is None
    assert contract.source is MasterDataSource.MASTER
    assert contract.source_ref == SOURCE_REF
    assert contract.version == 1

    schedules = _schedules(db_session, employee.id)
    assert len(schedules) == 1
    schedule = schedules[0]
    assert schedule.valid_from == date(2034, 2, 1)
    assert schedule.valid_to is None
    assert schedule.weekly_required_minutes == 1200
    assert [
        schedule.mon_required_minutes,
        schedule.tue_required_minutes,
        schedule.wed_required_minutes,
        schedule.thu_required_minutes,
        schedule.fri_required_minutes,
        schedule.sat_required_minutes,
        schedule.sun_required_minutes,
    ] == [240, 240, 240, 240, 240, 0, 0]
    # The CLI writes no typical daily times; they stay NULL rather than guessed.
    assert schedule.mon_typical_start is None and schedule.mon_typical_end is None
    assert schedule.source is MasterDataSource.MASTER
    assert schedule.version == 1
    # The schedule row has no provenance column at all (only the contract does).
    assert "source_ref" not in WorkScheduleVersion.__table__.columns


def test_reimport_is_idempotent(db_session: Session, tmp_path: Path) -> None:
    """The second run corrects in place; it never creates a duplicate row.

    Deliberate correct-in-place: the same ``(employee, valid_from)`` keeps ONE
    row (stable id) and its ``version`` is bumped, so a re-run cannot accumulate
    parallel contracts or schedules. ``uq_contract_employee_valid_from`` /
    ``uq_schedule_employee_valid_from`` remain the real uniqueness guards.
    """
    path = _write_csv(tmp_path / "people.csv", [_row("emp-f5-idem", "2034-03-01")])
    first = import_file(db_session, path, SOURCE_REF)
    employee = _employee(db_session, "emp-f5-idem")
    employee_id, created_at = employee.id, employee.created_at
    contract_id = _contracts(db_session, employee_id)[0].id
    schedule_id = _schedules(db_session, employee_id)[0].id
    assert first[-2:] == ["contract  2034-03-01: created", "schedule  2034-03-01: created"]

    second = import_file(db_session, path, SOURCE_REF)

    assert second == [
        "employee  emp-f5-idem: legal metadata refreshed",
        "contract  2034-03-01: corrected to version 2",
        "schedule  2034-03-01: corrected to version 2",
    ]
    contracts = _contracts(db_session, employee_id)
    schedules = _schedules(db_session, employee_id)
    assert [row.id for row in contracts] == [contract_id]
    assert [row.id for row in schedules] == [schedule_id]
    assert contracts[0].version == 2 and schedules[0].version == 2
    assert contracts[0].source_ref == SOURCE_REF
    assert contracts[0].valid_from == date(2034, 3, 1) and contracts[0].valid_to is None
    # No row was deleted and recreated, and no second employee appeared.
    assert employee.created_at == created_at
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(Employee)
            .where(Employee.external_employee_id == "emp-f5-idem")
        )
        == 1
    )


def test_import_refuses_to_overwrite_an_employee_maintained_schedule_on_the_same_date(
    db_session: Session, tmp_path: Path
) -> None:
    """The mirror of ``identity/service.set_working_time``: EMPLOYEE wins.

    An employee's own unsubmitted configuration is not silently overwritten by a
    bulk import; the employee must change it in the application.
    """
    employee = _employee(db_session, SEEDED_EXTERNAL_ID)
    valid_from = date(2036, 2, 1)
    db_session.add(
        WorkScheduleVersion(
            employee_id=employee.id,
            valid_from=valid_from,
            valid_to=None,
            weekly_required_minutes=600,
            mon_required_minutes=120,
            tue_required_minutes=120,
            wed_required_minutes=120,
            thu_required_minutes=120,
            fri_required_minutes=120,
            sat_required_minutes=0,
            sun_required_minutes=0,
            source=MasterDataSource.EMPLOYEE,
            version=7,
        )
    )
    db_session.flush()

    path = _write_csv(tmp_path / "people.csv", [_row(SEEDED_EXTERNAL_ID, valid_from.isoformat())])
    with pytest.raises(ImportRefused, match="employee-maintained schedule"):
        import_file(db_session, path, SOURCE_REF)

    schedule = _schedules(db_session, employee.id)[-1]
    assert schedule.source is MasterDataSource.EMPLOYEE
    assert schedule.version == 7
    assert schedule.weekly_required_minutes == 600


def test_import_dry_run_writes_nothing(
    db_session: Session, db_engine: Engine, tmp_path: Path
) -> None:
    """``--dry-run`` prints the diff and writes nothing — verified on the DATABASE.

    An existing employee's legal metadata IS assigned in the session before the
    diff is printed (so a dry run can show what would change); this test
    therefore reads the committed state through a second connection, which is
    the property that matters: a dry run leaves no trace.
    """
    existing = _employee(db_session, SEEDED_EXTERNAL_ID)
    before_name = existing.legal_name
    path = _write_csv(
        tmp_path / "people.csv",
        [
            _row("emp-f5-dry", "2035-01-01"),
            _row(SEEDED_EXTERNAL_ID, "2035-06-01", legal_name="Dry Run Renamed (synthetic)"),
        ],
    )
    notes = import_file(db_session, path, SOURCE_REF, dry_run=True)

    assert any("emp-f5-dry: create" in note for note in notes)
    # A not-yet-existing employee has no effective-dated rows to diff, so the dry
    # run prints ONLY the employee line for that row (3 notes per row otherwise).
    assert not any("emp-f5-dry: legal metadata" in note for note in notes)
    assert any(f"{SEEDED_EXTERNAL_ID}: legal metadata refreshed" in note for note in notes)
    assert any("contract  2035-06-01: create (version 1)" in note for note in notes)
    assert any("schedule  2035-06-01: create (version 1)" in note for note in notes)
    assert len(notes) == 4

    with Session(db_engine) as observer:
        assert (
            observer.scalar(select(Employee).where(Employee.external_employee_id == "emp-f5-dry"))
            is None
        )
        assert (
            observer.scalar(
                select(EmploymentContract).where(EmploymentContract.valid_from == date(2035, 6, 1))
            )
            is None
        )
        assert (
            observer.scalar(
                select(WorkScheduleVersion).where(
                    WorkScheduleVersion.valid_from == date(2035, 6, 1)
                )
            )
            is None
        )
        observed = observer.scalar(
            select(Employee).where(Employee.external_employee_id == SEEDED_EXTERNAL_ID)
        )
        assert observed is not None and observed.legal_name == before_name


def test_import_reports_the_failing_row_number(
    db_session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bad row names its FILE line number (header line 1, first data row line 2)."""
    path = _write_csv(
        tmp_path / "people.csv",
        [
            _row("emp-f5-good", "2034-05-01"),
            _row("emp-f5-bad", "2034-06-01", mon_required_minutes="2000"),
        ],
    )
    with pytest.raises(ImportRefused, match=r"row 3: mon_required_minutes"):
        import_file(db_session, path, SOURCE_REF)
    # Nothing was written for the good row either: the whole file is validated
    # before the first write.
    assert (
        db_session.scalar(select(Employee).where(Employee.external_employee_id == "emp-f5-good"))
        is None
    )

    # The CLI entrypoint reports the same refusal as exit code 2 on stderr.
    assert main(["import", "--file", str(path), "--source-ref", SOURCE_REF]) == 2
    assert "row 3" in capsys.readouterr().err


def test_import_refuses_a_blank_source_ref(db_session: Session, tmp_path: Path) -> None:
    """Provenance is mandatory: an unattributable bulk write is refused."""
    path = _write_csv(tmp_path / "people.csv", [_row("emp-f5-noref", "2034-09-01")])
    with pytest.raises(ImportRefused, match="source-ref"):
        import_file(db_session, path, "   ")
    assert (
        db_session.scalar(select(Employee).where(Employee.external_employee_id == "emp-f5-noref"))
        is None
    )


# ---------------------------------------------------------------------------
# EINV-02: the format cannot carry an identity binding
# ---------------------------------------------------------------------------


def test_the_import_format_cannot_carry_an_identity_binding(
    db_session: Session, tmp_path: Path
) -> None:
    """No binding path exists here, structurally AND behaviourally (EINV-02)."""
    # 1. structural: the row model has no such field, and cannot accept one.
    assert "idp_subject" not in MasterDataRow.model_fields
    assert MasterDataRow.model_config.get("extra") == "forbid"

    # 2. an extra column is REFUSED, never silently ignored.
    path = _write_csv(
        tmp_path / "people.csv",
        [_row(SEEDED_EXTERNAL_ID, "2035-02-01")],
        extra_column=("idp_subject", SEEDED_SUBJECT),
    )
    with pytest.raises(ImportRefused, match="unexpected column"):
        import_file(db_session, path, SOURCE_REF)
    assert (
        db_session.scalar(
            select(EmploymentContract).where(EmploymentContract.valid_from == date(2035, 2, 1))
        )
        is None
    )

    # 3. behavioural: an import for an EXISTING employee leaves the binding
    #    byte-identical (not read, not written, not cleared).
    employee = _employee(db_session, SEEDED_EXTERNAL_ID)
    assert employee.idp_subject == SEEDED_SUBJECT
    good_path = _write_csv(
        tmp_path / "good.csv",
        [_row(SEEDED_EXTERNAL_ID, "2035-03-01", legal_name="Refreshed Legal Name (synthetic)")],
    )
    import_file(db_session, good_path, SOURCE_REF)
    db_session.expire_all()
    refreshed = _employee(db_session, SEEDED_EXTERNAL_ID)
    assert refreshed.idp_subject == SEEDED_SUBJECT
    assert refreshed.legal_name == "Refreshed Legal Name (synthetic)"
    assert (
        db_session.scalar(
            select(func.count()).select_from(Employee).where(Employee.idp_subject == SEEDED_SUBJECT)
        )
        == 1
    )

    # 4. the module's CODE never names the column. DEVIATION FROM THE PLAN TEXT:
    #    the raw substring assertion ("the module never contains the string at
    #    all") cannot hold, because this module deliberately documents the
    #    exclusion in prose (its docstring and two comments name the column to
    #    say it is NOT part of this seam). The property that actually keeps a
    #    second identity mechanism out is "no code occurrence", asserted below.
    source = MODULE_PATH.read_text(encoding="utf-8")
    code_only = "".join(
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type not in {tokenize.COMMENT, tokenize.STRING}
    )
    assert "idp_subject" not in code_only
    # Even a commented-out assignment would be caught here (the F1 convention).
    assert re.search(r"idp_subject\s*=[^=]", source) is None


def test_the_import_never_activates_or_grants_roles(db_session: Session, tmp_path: Path) -> None:
    """Onboarding cannot widen access (INV-04/INV-05)."""
    employee = _employee(db_session, SEEDED_EXTERNAL_ID)
    db_session.add_all(
        [
            RoleAssignment(
                employee_id=employee.id,
                role=Role.APPLICATION_ADMIN.value,
                granted_by_principal="test-setup",
            ),
            RoleAssignment(
                employee_id=employee.id,
                role=Role.HR_REQUESTER.value,
                granted_by_principal="test-setup",
                revoked_at=None,
            ),
        ]
    )
    db_session.flush()
    before = [
        (row.id, row.role, row.revoked_at)
        for row in db_session.scalars(
            select(RoleAssignment)
            .where(RoleAssignment.employee_id == employee.id)
            .order_by(RoleAssignment.granted_at)
        )
    ]
    assert len(before) == 2
    assert employee.active is True

    path = _write_csv(
        tmp_path / "people.csv",
        [_row(SEEDED_EXTERNAL_ID, "2035-04-01"), _row("emp-f5-inactive", "2035-04-01")],
    )
    import_file(db_session, path, SOURCE_REF)

    db_session.expire_all()
    assert _employee(db_session, SEEDED_EXTERNAL_ID).active is True
    after = [
        (row.id, row.role, row.revoked_at)
        for row in db_session.scalars(
            select(RoleAssignment)
            .where(RoleAssignment.employee_id == employee.id)
            .order_by(RoleAssignment.granted_at)
        )
    ]
    assert after == before

    created = _employee(db_session, "emp-f5-inactive")
    assert created.active is False
    assert created.idp_subject is None
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(RoleAssignment)
            .where(RoleAssignment.employee_id == created.id)
        )
        == 0
    )


# ---------------------------------------------------------------------------
# Effective-range integrity and lock order
# ---------------------------------------------------------------------------


def test_a_corrected_contract_reopens_exactly_its_effective_range(
    db_session: Session, tmp_path: Path
) -> None:
    """EINV-15: only the corrected row's own months are reopened.

    The successor row bounds the range: the corrected 2034-01-01 row governs
    ``[2034-01-01, 2034-03-31]`` (the day before the 2034-04-01 row starts), so
    exactly 2034-01..2034-03 reopen. The month BEFORE the correction (2033-12,
    which still resolves to this row) and the successor's own month (2034-04)
    keep their review state: an earlier correction does not cascade forward, and
    a successor is not invalidated by its predecessor.
    """
    first = _write_csv(tmp_path / "first.csv", [_row("emp-f5-range", "2034-01-01")])
    successor = _write_csv(
        tmp_path / "successor.csv",
        [_row("emp-f5-range", "2034-04-01", **_weekdays(120))],
    )
    import_file(db_session, first, SOURCE_REF)
    import_file(db_session, successor, SOURCE_REF)
    employee = _employee(db_session, "emp-f5-range")
    assert [row.valid_from for row in _contracts(db_session, employee.id)] == [
        date(2034, 1, 1),
        date(2034, 4, 1),
    ]

    months = [date(2033, 12, 1), *[date(2034, month, 1) for month in range(1, 6)]]
    for month in months:
        db_session.add(
            WorkPeriod(employee_id=employee.id, period_month=month, status=PeriodStatus.REVIEWED)
        )
    db_session.flush()

    correction = _write_csv(
        tmp_path / "correction.csv",
        [_row("emp-f5-range", "2034-01-01", **_weekdays(360))],
    )
    import_file(db_session, correction, SOURCE_REF)

    statuses = {
        row.period_month: row.status
        for row in db_session.scalars(
            select(WorkPeriod).where(WorkPeriod.employee_id == employee.id)
        )
    }
    assert statuses[date(2033, 12, 1)] is PeriodStatus.REVIEWED
    assert statuses[date(2034, 1, 1)] is PeriodStatus.OPEN
    assert statuses[date(2034, 2, 1)] is PeriodStatus.OPEN
    assert statuses[date(2034, 3, 1)] is PeriodStatus.OPEN
    assert statuses[date(2034, 4, 1)] is PeriodStatus.REVIEWED
    assert statuses[date(2034, 5, 1)] is PeriodStatus.REVIEWED
    corrected = _contracts(db_session, employee.id)[0]
    assert corrected.version == 2
    assert corrected.weekly_required_minutes == 1800


def test_a_finite_ended_import_reopens_only_its_own_months(
    db_session: Session, tmp_path: Path
) -> None:
    """A row's OWN declared ``valid_to`` bounds the reopened range (review R30).

    ``_effective_range_end`` read only the SUCCESSOR bound, so a row imported as
    effective for one month alone — with no successor — reported an open-ended
    range, and ``open_reviewed_month_range`` then cleared the review state of
    every reviewed month from that month onward: months the row does not govern
    at all. Reopening a reviewed month is a state change, not a recomputation.

    The master-data writer already modelled the range as
    ``[valid_from, min(valid_to, next_valid_from - 1)]``
    (``assert_contract_row_governs_a_day``); this pins that the resolver agrees
    with it, for the contract AND the schedule the same CSV row carries.
    """
    first = _write_csv(
        tmp_path / "finite.csv",
        [_row("emp-finite", "2034-01-01", valid_to="2034-01-31", **_weekdays(120))],
    )
    import_file(db_session, first, SOURCE_REF)
    employee = _employee(db_session, "emp-finite")

    months = [date(2033, 12, 1), *[date(2034, month, 1) for month in range(1, 5)]]
    for month in months:
        db_session.add(
            WorkPeriod(employee_id=employee.id, period_month=month, status=PeriodStatus.REVIEWED)
        )
    db_session.flush()

    correction = _write_csv(
        tmp_path / "finite-correction.csv",
        [_row("emp-finite", "2034-01-01", valid_to="2034-01-31", **_weekdays(360))],
    )
    import_file(db_session, correction, SOURCE_REF)

    statuses = {
        row.period_month: row.status
        for row in db_session.scalars(
            select(WorkPeriod).where(WorkPeriod.employee_id == employee.id)
        )
    }
    assert statuses[date(2033, 12, 1)] is PeriodStatus.REVIEWED
    assert statuses[date(2034, 1, 1)] is PeriodStatus.OPEN, "the row governs January"
    for month in (2, 3, 4):
        assert statuses[date(2034, month, 1)] is PeriodStatus.REVIEWED, (
            "a row ending 31 January reopened a month it does not govern"
        )


def test_an_entirely_shadowed_contract_row_is_refused(db_session: Session, tmp_path: Path) -> None:
    """A row that governs no day is refused, naming the shadowing row.

    DEVIATION FROM THE PLAN TEXT (recorded, not silently encoded). §5.11
    describes "a bounded row whose whole range is covered by a later-starting
    row". Under the resolution rule (the greatest ``(valid_from, version)`` wins
    per day) a later-starting row can only ever shadow an earlier row's *tail*,
    so that input cannot exist: the guard compares against
    ``shadowing.valid_from`` (its own docstring says ``next_valid_from - 1``)
    and therefore refuses exactly the rows whose governed range is EMPTY. Both
    reachable boundaries are asserted here, so a change to that comparison
    cannot pass unnoticed.
    """
    path = _write_csv(tmp_path / "people.csv", [_row("emp-f5-shadow", "2033-03-01")])
    import_file(db_session, path, SOURCE_REF)
    employee = _employee(db_session, "emp-f5-shadow")
    shadowing_start = date(2033, 3, 1)

    # (a) an empty governed range is refused, and the message names the row that
    #     already carries that date.
    with pytest.raises(ImportRefused, match=shadowing_start.isoformat()):
        assert_contract_row_governs_a_day(
            db_session,
            employee.id,
            shadowing_start,
            shadowing_start - timedelta(days=1),
            exclude_id=None,
        )

    # (b) the same date with an open end governs that day: a duplicate
    #     ``(employee, valid_from)`` is the DB's unique-constraint case, not a
    #     silent no-op, so the guard lets it through.
    assert_contract_row_governs_a_day(
        db_session, employee.id, shadowing_start, None, exclude_id=None
    )

    # (c) a bounded row ending the day before a later row starts DOES govern
    #     days, so it is not refused.
    later = _write_csv(tmp_path / "later.csv", [_row("emp-f5-later", "2033-09-01")])
    import_file(db_session, later, SOURCE_REF)
    later_employee = _employee(db_session, "emp-f5-later")
    assert_contract_row_governs_a_day(
        db_session,
        later_employee.id,
        date(2033, 6, 1),
        date(2033, 8, 31),
        exclude_id=None,
    )


def test_a_corrected_contract_keeps_the_employee_lock_order(
    db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixed lock -> per-employee fact lock -> month reopen, on the corrected path.

    The same total order as every other operator path and as
    ``identity/service.set_working_time``: the per-employee lock is taken BEFORE
    the row's currentness is read, and the reopen happens under it.
    """
    events: list[str] = []
    real_fixed_lock = master_data_admin.lock_master_data_administration
    real_facts_lock = master_data_admin.lock_employee_facts
    real_contract_reopen = master_data_admin._reopen_contract_months
    real_schedule_reopen = master_data_admin._reopen_schedule_months

    def record_fixed_lock(session: Session) -> None:
        events.append("fixed_lock")
        real_fixed_lock(session)

    def record_facts_lock(session: Session, employee_id: uuid.UUID) -> None:
        events.append(f"facts_lock:{employee_id}")
        real_facts_lock(session, employee_id)

    def record_contract_reopen(session: Session, employee: Employee, valid_from: date) -> None:
        events.append("contract_reopen")
        real_contract_reopen(session, employee, valid_from)

    def record_schedule_reopen(session: Session, employee: Employee, valid_from: date) -> None:
        events.append("schedule_reopen")
        real_schedule_reopen(session, employee, valid_from)

    monkeypatch.setattr(master_data_admin, "lock_master_data_administration", record_fixed_lock)
    monkeypatch.setattr(master_data_admin, "lock_employee_facts", record_facts_lock)
    monkeypatch.setattr(master_data_admin, "_reopen_contract_months", record_contract_reopen)
    monkeypatch.setattr(master_data_admin, "_reopen_schedule_months", record_schedule_reopen)

    path = _write_csv(tmp_path / "people.csv", [_row("emp-f5-locks", "2034-07-01")])
    import_file(db_session, path, SOURCE_REF)
    employee = _employee(db_session, "emp-f5-locks")
    expected = [
        "fixed_lock",
        f"facts_lock:{employee.id}",
        "contract_reopen",
        "schedule_reopen",
    ]
    assert events == expected

    # The corrected path keeps the same order (and the same employee lock).
    events.clear()
    import_file(db_session, path, SOURCE_REF)
    assert events == expected


def test_import_requires_the_operator_lock(
    db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fixed master-data lock is the FIRST statement and is really held."""
    events: list[str] = []
    baseline = _advisory_locks_held(db_session)
    real_fixed_lock = master_data_admin.lock_master_data_administration
    real_facts_lock = master_data_admin.lock_employee_facts

    def record_fixed_lock(session: Session) -> None:
        real_fixed_lock(session)
        events.append("fixed_lock")
        # HELD, not merely called: the advisory lock is visible in pg_locks for
        # this backend while the import runs. It is transaction-scoped, so it is
        # released by the caller's commit/rollback (the CLI's own session, or
        # the request transaction) — never by this module.
        assert _advisory_locks_held(session) > baseline

    def record_facts_lock(session: Session, employee_id: uuid.UUID) -> None:
        events.append("facts_lock")
        real_facts_lock(session, employee_id)

    monkeypatch.setattr(master_data_admin, "lock_master_data_administration", record_fixed_lock)
    monkeypatch.setattr(master_data_admin, "lock_employee_facts", record_facts_lock)

    path = _write_csv(tmp_path / "people.csv", [_row("emp-f5-lock", "2034-08-01")])
    import_file(db_session, path, SOURCE_REF)

    # One fixed lock, taken before the per-employee lock, once for the whole file.
    assert events == ["fixed_lock", "facts_lock"]
