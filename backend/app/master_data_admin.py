"""Operator CLI: the supported onboarding path for contract and schedule master data.

There was no supported production writer for ``employment_contract`` or an
authoritative ``work_schedule_version`` — the dev/demo seeds were the only ones.
This CLI is that writer, and it is deliberately small: a CSV in, the existing
models and their constraints out. No new table, no generic HR application
(Master Fix Plan §F5.10).

**Identity binding is NOT part of this seam (EINV-02).** Which institutional
identity authenticates as an employee is create-only profile data with its own
lifecycle, reason, provenance and session consequences. A bulk personnel file is
the wrong owner for it, so the CSV has **no** ``idp_subject`` column, an extra
column is refused rather than silently ignored, and this module never touches
``idp_subject``, ``active`` or role assignments — on create or on update. The
onboarding sequence is explicit and uses the existing owners:

1. ``python -m app.master_data_admin import --file people.csv --source-ref REF``
   — creates/updates the employee row (legal metadata only), the employment
   contract and the authoritative work-schedule version;
2. the identity binding is set by ``POST /api/v1/admin/users`` (when the employee
   row is created through the admin API) or by
   ``python -m app.identity_admin set-binding`` — with its provenance row, audit
   event and session invalidation unchanged.

Lock order: ``lock_master_data_administration`` (fixed, global) first, then
``lock_employee_facts`` per row — the same total order the other operator paths
use. ``--dry-run`` prints the diff and writes nothing.

The input file is synthetic/anonymised in dev and CI and is never committed.
"""

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session_factory, lock_employee_facts
from app.enums import MasterDataSource, PayGroupBucket
from app.identity.models import Employee, EmploymentContract, WorkScheduleVersion
from app.identity.service import (
    _reopen_contract_months,
    _reopen_schedule_months,
    lock_master_data_administration,
)
from app.worktime.walltime import now_berlin

WEEKDAY_FIELDS = (
    "mon_required_minutes",
    "tue_required_minutes",
    "wed_required_minutes",
    "thu_required_minutes",
    "fri_required_minutes",
    "sat_required_minutes",
    "sun_required_minutes",
)

#: The exact accepted header. An unexpected column is a refusal, never a silent
#: ignore: a personnel file that carries a field this import does not own (an
#: identity binding, an approval flag) must be visible, not quietly dropped.
CSV_COLUMNS = (
    "external_employee_id",
    "legal_name",
    "email",
    "valid_from",
    "valid_to",
    "fte",
    "weekly_required_minutes",
    "pay_group_bucket",
    "employment_start",
    "employment_end",
    *WEEKDAY_FIELDS,
)


class MasterDataRow(BaseModel):
    """One CSV line. ``extra="forbid"`` is what makes an unexpected column loud."""

    model_config = ConfigDict(extra="forbid")

    external_employee_id: str = Field(min_length=1)
    legal_name: str = Field(min_length=1)
    email: str | None = None
    valid_from: date
    valid_to: date | None = None
    fte: float | None = None
    weekly_required_minutes: int = Field(ge=0)
    pay_group_bucket: PayGroupBucket = PayGroupBucket.UNKNOWN
    employment_start: date | None = None
    employment_end: date | None = None
    mon_required_minutes: int = Field(ge=0, le=1440)
    tue_required_minutes: int = Field(ge=0, le=1440)
    wed_required_minutes: int = Field(ge=0, le=1440)
    thu_required_minutes: int = Field(ge=0, le=1440)
    fri_required_minutes: int = Field(ge=0, le=1440)
    sat_required_minutes: int = Field(ge=0, le=1440)
    sun_required_minutes: int = Field(ge=0, le=1440)


class ImportRefused(ValueError):
    """A row the import refuses; the message names the row number and the reason."""


def _blank_to_none(value: object) -> object:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def read_rows(path: Path) -> list[MasterDataRow]:
    """Parse the file; reports the failing row number and refuses extra columns."""
    rows: list[MasterDataRow] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = [name for name in (reader.fieldnames or []) if name is not None]
        required = ("external_employee_id", "legal_name", "valid_from")
        missing = [name for name in required if name not in fieldnames]
        if missing:
            raise ImportRefused(f"the file is missing required column(s): {', '.join(missing)}")
        unexpected = [name for name in fieldnames if name not in CSV_COLUMNS]
        if unexpected:
            raise ImportRefused(
                "unexpected column(s) "
                + ", ".join(unexpected)
                + ": this import owns employee legal metadata, the employment contract and "
                "the authoritative work schedule only (identity bindings belong to "
                "app.identity_admin / POST /admin/users)"
            )
        for number, raw in enumerate(reader, start=2):
            if not any(_blank_to_none(value) for value in raw.values()):
                continue  # a trailing blank line
            payload = {key: _blank_to_none(value) for key, value in raw.items() if key is not None}
            try:
                rows.append(MasterDataRow.model_validate(payload))
            except ValidationError as exc:
                first = exc.errors()[0]
                location = ".".join(str(part) for part in first.get("loc", ()))
                raise ImportRefused(
                    f"row {number}: {location}: {first.get('msg', 'invalid value')}"
                ) from None
    if not rows:
        raise ImportRefused("the file contains no data rows")
    return rows


def assert_contract_row_governs_a_day(
    session: Session,
    employee_id: object,
    valid_from: date,
    valid_to: date | None,
    *,
    exclude_id: object | None = None,
) -> None:
    """Refuse a contract row that can never govern a day (a silent no-op).

    One comparison against the next ``valid_from`` for that employee: the row's
    governed range is ``[valid_from, min(valid_to, next_valid_from - 1)]``, and an
    empty governed range means the row would be stored but change nothing. The
    refusal names the shadowing row.

    ``uq_contract_employee_valid_from`` remains the real uniqueness guard — no
    ``EXCLUDE USING gist`` non-overlap constraint is added, because resolution is
    single-row per day (so overlapping rows cannot double count) and the
    established model writes legitimate open-ended successors, which a non-overlap
    constraint would reject.
    """
    shadowing = session.scalar(
        select(EmploymentContract)
        .where(
            EmploymentContract.employee_id == employee_id,
            EmploymentContract.valid_from >= valid_from,
        )
        .order_by(EmploymentContract.valid_from)
        .limit(1)
    )
    if shadowing is None or shadowing.id == exclude_id:
        return
    governed_to = min(
        value
        for value in (valid_to, shadowing.valid_from)  # inclusive start of the shadower
        if value is not None
    )
    if governed_to < valid_from:
        raise ImportRefused(
            "this contract row governs no day: it is entirely shadowed by the row starting "
            f"{shadowing.valid_from.isoformat()} (source_ref={shadowing.source_ref!r})"
        )


def _apply_contract(
    session: Session, employee: Employee, row: MasterDataRow, source_ref: str, *, dry_run: bool
) -> str:
    existing = session.scalar(
        select(EmploymentContract).where(
            EmploymentContract.employee_id == employee.id,
            EmploymentContract.valid_from == row.valid_from,
        )
    )
    assert_contract_row_governs_a_day(
        session,
        employee.id,
        row.valid_from,
        row.valid_to,
        exclude_id=existing.id if existing else None,
    )
    if existing is None:
        if dry_run:
            return f"contract  {row.valid_from}: create (version 1)"
        session.add(
            EmploymentContract(
                employee_id=employee.id,
                valid_from=row.valid_from,
                valid_to=row.valid_to,
                fte=row.fte,
                weekly_required_minutes=row.weekly_required_minutes,
                pay_group_bucket=row.pay_group_bucket,
                employment_start=row.employment_start,
                employment_end=row.employment_end,
                source=MasterDataSource.MASTER,
                source_ref=source_ref,
                version=1,
            )
        )
        session.flush()
        _reopen_contract_months(session, employee, row.valid_from)
        return f"contract  {row.valid_from}: created"
    if existing.source is MasterDataSource.EMPLOYEE:
        raise ImportRefused(
            f"an employee-maintained contract row already starts {row.valid_from.isoformat()}"
        )
    if dry_run:
        return (
            f"contract  {row.valid_from}: correct "
            f"(version {existing.version} -> {existing.version + 1})"
        )
    existing.valid_to = row.valid_to
    existing.fte = row.fte
    existing.weekly_required_minutes = row.weekly_required_minutes
    existing.pay_group_bucket = row.pay_group_bucket
    existing.employment_start = row.employment_start
    existing.employment_end = row.employment_end
    existing.source = MasterDataSource.MASTER
    existing.source_ref = source_ref
    existing.version += 1
    session.flush()
    _reopen_contract_months(session, employee, row.valid_from)
    return f"contract  {row.valid_from}: corrected to version {existing.version}"


def _apply_schedule(
    session: Session, employee: Employee, row: MasterDataRow, source_ref: str, *, dry_run: bool
) -> str:
    allocated = sum(getattr(row, field) for field in WEEKDAY_FIELDS)
    if allocated != row.weekly_required_minutes:
        raise ImportRefused(
            f"weekday allocation sums to {allocated} but the weekly target is "
            f"{row.weekly_required_minutes}"
        )
    existing = session.scalar(
        select(WorkScheduleVersion).where(
            WorkScheduleVersion.employee_id == employee.id,
            WorkScheduleVersion.valid_from == row.valid_from,
        )
    )
    if existing is not None and existing.source is MasterDataSource.EMPLOYEE:
        # The mirror of ``identity/service.set_working_time``: the employee's own
        # unsubmitted configuration is not silently overwritten by a bulk import.
        raise ImportRefused(
            f"an employee-maintained schedule already starts {row.valid_from.isoformat()}; "
            "the employee must change it in the application"
        )
    if existing is None:
        if dry_run:
            return f"schedule  {row.valid_from}: create (version 1)"
        max_version = session.scalar(
            select(func.max(WorkScheduleVersion.version)).where(
                WorkScheduleVersion.employee_id == employee.id
            )
        )
        session.add(
            WorkScheduleVersion(
                employee_id=employee.id,
                valid_from=row.valid_from,
                valid_to=row.valid_to,
                weekly_required_minutes=row.weekly_required_minutes,
                **{field: getattr(row, field) for field in WEEKDAY_FIELDS},
                source=MasterDataSource.MASTER,
                version=(max_version or 0) + 1,
            )
        )
        session.flush()
        _reopen_schedule_months(session, employee, row.valid_from)
        return f"schedule  {row.valid_from}: created"
    if dry_run:
        return (
            f"schedule  {row.valid_from}: correct "
            f"(version {existing.version} -> {existing.version + 1})"
        )
    existing.valid_to = row.valid_to
    existing.weekly_required_minutes = row.weekly_required_minutes
    for field in WEEKDAY_FIELDS:
        setattr(existing, field, getattr(row, field))
    existing.source = MasterDataSource.MASTER
    existing.version += 1
    session.flush()
    _reopen_schedule_months(session, employee, row.valid_from)
    return f"schedule  {row.valid_from}: corrected to version {existing.version}"


def _apply_row(
    session: Session, row: MasterDataRow, source_ref: str, *, dry_run: bool
) -> list[str]:
    employee = session.scalar(
        select(Employee).where(Employee.external_employee_id == row.external_employee_id)
    )
    created = employee is None
    if employee is None:
        if dry_run:
            return [
                f"employee  {row.external_employee_id}: create "
                "(legal metadata only; idp_subject/active/roles untouched)"
            ]
        # Onboarding cannot widen access: a new row is created INACTIVE, and the
        # identity binding is left NULL — this module never names the column at
        # all (EINV-02), which the mutation-surface tripwire enforces.
        employee = Employee(
            external_employee_id=row.external_employee_id,
            legal_name=row.legal_name,
            email=row.email,
            active=False,
        )
        session.add(employee)
        session.flush()
    # Per-employee fact lock BEFORE any read of that employee's currentness.
    lock_employee_facts(session, employee.id)
    if not created:
        # Legal profile metadata only. ``idp_subject``, ``active`` and role
        # assignments are never touched (EINV-02 / INV-04).
        employee.legal_name = row.legal_name
        employee.email = row.email
        session.flush()
    notes = [
        f"employee  {row.external_employee_id}: "
        + ("created (inactive, unbound)" if created else "legal metadata refreshed")
    ]
    notes.append(_apply_contract(session, employee, row, source_ref, dry_run=dry_run))
    notes.append(_apply_schedule(session, employee, row, source_ref, dry_run=dry_run))
    return notes


def import_file(
    session: Session, path: Path, source_ref: str, *, dry_run: bool = False
) -> list[str]:
    if not source_ref.strip():
        raise ImportRefused("--source-ref must not be blank")
    rows = read_rows(path)
    lock_master_data_administration(session)
    notes: list[str] = []
    for row in rows:
        notes.extend(_apply_row(session, row, source_ref.strip(), dry_run=dry_run))
    if not dry_run:
        session.commit()
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import employee legal metadata, contracts and authoritative schedules.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("import", help="Import a CSV of people/contracts/schedules")
    command.add_argument("--file", required=True, help="CSV file (see CSV_COLUMNS)")
    command.add_argument(
        "--source-ref", required=True, help="Provenance reference (recorded on every row)"
    )
    command.add_argument("--dry-run", action="store_true", help="Print the diff, write nothing")
    args = parser.parse_args(argv)

    path = Path(args.file)
    if not path.is_file():
        parser.error(f"no such file: {path}")
    with get_session_factory()() as session:
        try:
            notes = import_file(session, path, args.source_ref, dry_run=args.dry_run)
        except ImportRefused as exc:
            session.rollback()
            print(f"import refused: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # pragma: no cover - IntegrityError and friends
            session.rollback()
            print(f"import failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    for note in notes:
        print(f"  {note}")
    if args.dry_run:
        print(f"dry-run: nothing written (env={get_settings().env})")
    else:
        print(
            f"import complete: {len(notes) // 3} row(s) at "
            f"{now_berlin().isoformat()} (env={get_settings().env})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
