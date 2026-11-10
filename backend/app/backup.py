"""PostgreSQL backup, restore, and recovery verification for Chronobeam.

Chronobeam produces a PostgreSQL custom-format dump and a non-personal JSON
manifest, and can restore that dump into an EMPTY database.  Scheduling,
off-host transfer, encrypted-at-rest storage, monitoring, access control, and
deletion are infrastructure (IT) responsibilities.  Backup retention is
RETENTION_REQUIRES_DPO_IT_CONFIRMATION; RPO and RTO are IT decisions.

Upgrade procedure: backup -> deploy the new artifact -> ``alembic upgrade
head`` (or ``just migrate``) -> start -> verify ``/health/ready`` returns 200
-> smoke-test one authenticated employee read.

Rollback procedure: roll back the application image/artifact FIRST.  A schema
downgrade is safe only when the new migration's downgrade path is proven and
no new data was written under the new schema.  Otherwise restore the
pre-upgrade backup into a fresh database.  Verify recovery with readiness and
``python -m app.backup verify --database-url-env TARGET_URL --manifest MANIFEST``.  No
zero-downtime promise is made; a short maintenance window is the accepted
model.

**Execution contract (the dump/restore invariants an operator can rely on):**

- **One snapshot per dump.** ``table_counts`` and the dump itself come from the
  SAME exported snapshot (``BEGIN ISOLATION LEVEL REPEATABLE READ`` →
  ``pg_export_snapshot()`` → ``pg_dump --snapshot=<id>`` on that still-open
  transaction), so a write landing mid-dump cannot make the manifest describe a
  database state the dump does not contain. The cost is real and accepted: the
  exporting connection sits **idle-in-transaction** for the whole dump and pins
  the xmin horizon, holding back vacuum. Fine at pilot scale; revisit if a dump
  ever grows to where vacuum starvation matters.
- **The manifest is not personal data.** Aggregate counts, versions and hashes
  only — never row contents.
- **A failed restore is not retried in place.** ``pg_restore`` runs with
  ``--exit-on-error`` so a partial restore cannot be recorded as a good one, and
  a failure leaves the target unusable by design: the error names the disposal
  step rather than inviting a retry that the empty-target precondition would
  refuse.
- **Client/server pairing.** ``pg_dump`` cannot dump a server whose major
  version is newer than the client's. Source server and dump-client versions
  are recorded in the manifest. Archive-reader/restore-target compatibility is
  separate and left to ``pg_restore``; a stricter retention policy is a
  deployment decision.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import Date, distinct, exists, func, inspect, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, aliased

from app.config import get_settings
from app.db import Base, create_engine, ensure_database
from app.schema import expected_schema_revision
from app.version import APP_VERSION, build_sha

RETENTION_MARKER = "RETENTION_REQUIRES_DPO_IT_CONFIRMATION"
# PostgreSQL 16 libpq environment variables:
# https://www.postgresql.org/docs/16/libpq-envars.html
_LIBPQ_SECURITY_ENV = {
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "sslcrl": "PGSSLCRL",
}


class BackupError(RuntimeError):
    """A safe-to-display backup/restore operational error."""


@dataclass(frozen=True)
class ConnectionParts:
    host: str
    port: str
    username: str
    database: str
    password: str
    security_env: dict[str, str] = field(default_factory=dict)


def _metadata_tables():
    """Load every persisted model before reading the application's metadata."""
    # The imports intentionally register tables on the shared declarative Base.
    from app.absence import models as absence_models
    from app.audit import models as audit_models
    from app.auth import models as auth_models
    from app.auth import session_models as auth_session_models
    from app.balance import models as balance_models
    from app.calendar import models as calendar_models
    from app.disclosure import models as disclosure_models
    from app.identity import models as identity_models
    from app.ocd_overtime import models as ocd_overtime_models
    from app.policy import models as policy_models
    from app.shift import models as shift_models
    from app.travel import models as travel_models
    from app.worktime import models as worktime_models

    _ = (
        audit_models,
        absence_models,
        auth_models,
        auth_session_models,
        balance_models,
        calendar_models,
        disclosure_models,
        identity_models,
        ocd_overtime_models,
        policy_models,
        shift_models,
        travel_models,
        worktime_models,
    )
    return list(Base.metadata.sorted_tables)


def _connection_parts(database_url: str) -> ConnectionParts:
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql"):
        raise BackupError("database URL must use PostgreSQL")
    if not url.host or not url.username or not url.database:
        raise BackupError("database URL must include host, username, and database name")
    security_env: dict[str, str] = {}
    # SQLAlchemy drops blank query values; retain them so none are silently ignored.
    query = parse_qs(urlsplit(database_url).query, keep_blank_values=True)
    for option, values in query.items():
        if option == "sslpassword":
            # https://www.postgresql.org/docs/16/libpq-connect.html#LIBPQ-CONNECT-SSLPASSWORD
            # libpq has no environment equivalent; never invent one or expose it in argv.
            raise BackupError(
                "sslpassword has no libpq environment variable; this backup module does not "
                "support it. Use an approved client-key configuration without this URL option."
            )
        if option not in _LIBPQ_SECURITY_ENV:
            # Unknown keys as well as their values may contain secrets.
            raise BackupError(
                "unsupported database URL query option; backup supports only "
                "sslmode, sslrootcert, sslcert, sslkey, sslcrl"
            )
        if len(values) != 1 or not values[0]:
            raise BackupError(
                "each supported database URL query option must have one value (non-empty)"
            )
        security_env[_LIBPQ_SECURITY_ENV[option]] = values[0]
    # SQLAlchemy accepts raw '#' where urlsplit sees a fragment. Refuse disagreement
    # rather than let the application and child tools use different security options.
    if {option: tuple(values) for option, values in query.items()} != dict(url.normalized_query):
        raise BackupError(
            "database URL query parsing is ambiguous; percent-encode reserved characters"
        )
    return ConnectionParts(
        host=url.host,
        port=str(url.port or 5432),
        username=url.username,
        database=url.database,
        password=url.password or "",
        security_env=security_env,
    )


def _pg_env(parts: ConnectionParts) -> dict[str, str]:
    # PGPASSWORD is supplied only to the PostgreSQL child process, never argv
    # or stdout/stderr generated by this module.
    return {**os.environ, "PGPASSWORD": parts.password, **parts.security_env}


def _run_postgres_tool(args: list[str], parts: ConnectionParts, operation: str) -> None:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed PostgreSQL executable arguments
            args, env=_pg_env(parts), capture_output=True, text=True
        )
    except FileNotFoundError as exc:
        raise BackupError(f"{operation} failed: PostgreSQL tool is not installed") from exc
    if completed.returncode:
        # PostgreSQL stderr can quote COPY row contents or connection secrets.
        raise BackupError(
            f"{operation} failed (exit {completed.returncode}); see the PostgreSQL log for details"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _table_counts(engine: Engine) -> dict[str, int]:
    counts: dict[str, int] = {}
    with engine.connect() as connection:
        for table in _metadata_tables():
            counts[table.name] = int(
                connection.execute(select(func.count()).select_from(table)).scalar_one()
            )
    return counts


def _table_counts_on(cursor: Any) -> dict[str, int]:
    """Counts read on an EXISTING cursor, inside the caller's transaction.

    The dump and its manifest counts must come from ONE snapshot, so the counts
    are read on the connection that exported that snapshot. Using
    ``_table_counts(engine)`` here instead would open a fresh READ COMMITTED
    connection and silently restore the mismatch this exists to remove — the
    counts would then be taken after ``pg_dump`` finished.
    """
    counts: dict[str, int] = {}
    for table in _metadata_tables():
        # Names are drawn only from the application's SQLAlchemy metadata.
        cursor.execute(f'SELECT count(*) FROM "{table.name}"')  # noqa: S608  # nosec B608
        counts[table.name] = int(cursor.fetchone()[0])
    return counts


def _client_version(tool: str) -> str:
    """``pg_dump``/``pg_restore`` client version, e.g. ``16.4``."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed PostgreSQL executable arguments
            [tool, "--version"], capture_output=True, text=True
        )
    except FileNotFoundError as exc:
        raise BackupError(f"{tool} is not installed") from exc
    if completed.returncode:
        raise BackupError(
            f"{tool} --version failed (exit {completed.returncode}); "
            "see the PostgreSQL log for details"
        )
    return completed.stdout.strip().rsplit(" ", 1)[-1]


def _major(version: str) -> int:
    """Major version from either form: ``16.4`` or ``SELECT version()`` output."""
    match = re.search(r"PostgreSQL\s+(\d+)", version) or re.match(r"(\d+)", version.strip())
    if match is None:
        # Never echo the child's own output: `--version` is external output, and
        # this error is printed by the CLI (see `_run_postgres_tool`).
        raise BackupError(
            "unrecognised PostgreSQL version output; expected 'PostgreSQL <major>' "
            "or '<major>.<minor>'"
        )
    return int(match.group(1))


def _require_compatible_client(tool: str, client: str, server: str) -> None:
    """Enforce the dump direction, without inventing a restore pairing policy.

    PostgreSQL 16 pg_dump Notes: older servers are supported, but a server
    newer than the client's major version is refused.
    https://www.postgresql.org/docs/16/app-pgdump.html#APP-PGDUMP-NOTES

    Archive-reader and target-server compatibility are separate concerns:
    output is not guaranteed to load into an older server. Leave these checks
    to pg_restore and retain source server/dump-client versions in the manifest.
    A stricter restore pairing remains OPEN-17.
    """
    client_major, server_major = _major(client), _major(server)
    if tool == "pg_dump" and client_major < server_major:
        raise BackupError(
            f"pg_dump client major {client_major} is older than the server major {server_major}; "
            f"pg_dump cannot dump a newer server. Install pg_dump major >={server_major}."
        )


def _postgres_version(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.exec_driver_sql("SELECT version()").scalar_one())


def _manifest_path(dump_path: Path) -> Path:
    return dump_path.with_suffix(".manifest.json")


def _write_json_exclusive(path: Path, value: dict[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except Exception:
        path.unlink(missing_ok=True)
        raise


def dump_database(database_url: str, output_directory: Path) -> tuple[Path, Path]:
    """Create a custom-format dump plus an aggregate-only manifest."""
    parts = _connection_parts(database_url)
    output_directory.mkdir(parents=True, exist_ok=True)
    if not output_directory.is_dir():
        raise BackupError("dump output path is not a directory")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    dump_path = output_directory / f"chronobeam-{stamp}.dump"
    manifest_path = _manifest_path(dump_path)
    if dump_path.exists() or manifest_path.exists():
        raise BackupError("refusing to overwrite an existing dump or manifest")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".chronobeam-", suffix=".partial", dir=output_directory
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    published_dump = False
    try:
        os.chmod(temporary_path, 0o600)
        source_engine = create_engine(database_url, pool_pre_ping=True)
        try:
            server_version = _postgres_version(source_engine)
            dump_client = _client_version("pg_dump")
            _require_compatible_client("pg_dump", dump_client, server_version)

            # ONE snapshot for both the counts and the dump.
            #
            # Counts used to be taken AFTER pg_dump, on a fresh READ COMMITTED
            # connection, so a write that landed between the two made the
            # manifest disagree with the dump it describes — and `verify_database`
            # compares the restored counts against that same manifest, so a
            # perfectly good dump could fail verification.
            #
            # `pg_dump --snapshot=<id>` is designed to import a snapshot exported
            # by ANOTHER session, and the exporting transaction must stay open for
            # the whole dump (the snapshot is unlinked when it ends). That is the
            # cost: this connection sits idle-in-transaction for the dump's
            # duration and pins the xmin horizon, holding back vacuum. Acceptable
            # at pilot scale under the accepted maintenance window
            # (`docs/backup_restore.md`), and deliberately written down.
            raw = source_engine.raw_connection()
            try:
                cursor = raw.cursor()
                # `pg_export_snapshot()` raises in READ COMMITTED, so the level is
                # set explicitly rather than inherited.
                cursor.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
                counts = _table_counts_on(cursor)
                cursor.execute("SELECT pg_export_snapshot()")
                exported = cursor.fetchone()
                if exported is None:
                    raise BackupError("pg_export_snapshot() returned no snapshot id")
                snapshot = str(exported[0])

                _run_postgres_tool(
                    [
                        "pg_dump",
                        "--format=custom",
                        "--no-owner",
                        "--no-acl",
                        "--snapshot",
                        snapshot,
                        "--host",
                        parts.host,
                        "--port",
                        parts.port,
                        "--username",
                        parts.username,
                        "--file",
                        str(temporary_path),
                        parts.database,
                    ],
                    parts,
                    "pg_dump",
                )
                raw.commit()
            finally:
                raw.close()

            manifest: dict[str, Any] = {
                "created_at": datetime.now(UTC).isoformat(),
                "postgres_version": server_version,
                "pg_client_version": dump_client,
                "schema_revision": expected_schema_revision(),
                "app_version": APP_VERSION,
                "build_sha": build_sha(),
                "dump_sha256": _sha256(temporary_path),
                "table_counts": counts,
                "retention": RETENTION_MARKER,
            }
        finally:
            source_engine.dispose()

        # link() refuses an existing destination atomically.  The source and
        # destination are in one output directory, so the link is same-filesystem.
        try:
            os.link(temporary_path, dump_path)
        except FileExistsError as exc:
            raise BackupError("refusing to overwrite an existing dump file") from exc
        published_dump = True
        temporary_path.unlink()
        _write_json_exclusive(manifest_path, manifest)
        return dump_path, manifest_path
    except FileExistsError as exc:
        temporary_path.unlink(missing_ok=True)
        if published_dump:
            dump_path.unlink(missing_ok=True)
        raise BackupError("refusing to overwrite an existing dump or manifest") from exc
    except Exception:
        temporary_path.unlink(missing_ok=True)
        # Do not leave an orphaned dump if manifest generation fails.
        if published_dump:
            dump_path.unlink(missing_ok=True)
        raise


def _empty_database_or_raise(database_url: str) -> None:
    ensure_database(database_url)
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        tables = inspect(engine).get_table_names()
    finally:
        engine.dispose()
    if tables:
        raise BackupError("restore target must be an EMPTY database (it already contains tables)")


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError("manifest is unreadable or invalid JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("table_counts"), dict):
        raise BackupError("manifest is missing aggregate table_counts")
    return value


def restore_database(dump_path: Path, database_url: str) -> bool:
    """Restore a Chronobeam dump into a new database and always verify it.

    A failed restore is deliberately NOT retried in place: ``pg_restore`` leaves
    a partially populated target, and the next attempt is then refused by the
    empty-target precondition — so the operator is told exactly what to do
    instead of being left with a database that looks restorable and is not.
    """
    if not dump_path.is_file():
        raise BackupError("dump file does not exist")
    manifest_path = _manifest_path(dump_path)
    if not manifest_path.is_file():
        raise BackupError("restore requires the adjacent non-personal manifest file")
    manifest = _load_manifest(manifest_path)
    if manifest.get("dump_sha256") != _sha256(dump_path):
        raise BackupError("dump SHA-256 does not match the adjacent manifest")
    parts = _connection_parts(database_url)
    _empty_database_or_raise(database_url)

    version_engine = create_engine(database_url, pool_pre_ping=True)
    try:
        server_version = _postgres_version(version_engine)
    finally:
        version_engine.dispose()
    restore_client = _client_version("pg_restore")
    _require_compatible_client("pg_restore", restore_client, server_version)

    try:
        _run_postgres_tool(
            [
                "pg_restore",
                "--no-owner",
                "--no-acl",
                # Without this pg_restore reports success while individual objects
                # failed, so a partial restore would be recorded as a good one.
                "--exit-on-error",
                "--host",
                parts.host,
                "--port",
                parts.port,
                "--username",
                parts.username,
                "--dbname",
                parts.database,
                str(dump_path),
            ],
            parts,
            "pg_restore",
        )
    except BackupError as exc:
        raise BackupError(f"{exc}\n{_restore_disposal_guidance(parts.database)}") from exc
    return verify_database(database_url, manifest_path)


def _restore_disposal_guidance(database: str) -> str:
    return (
        f"The restore target '{database}' is now PARTIALLY POPULATED and must not be reused: "
        "drop it and create a fresh empty database before retrying "
        "(see docs/operations.md, 'partial restore disposal'). Do not attempt an "
        "in-place retry against the same database."
    )


def _missing_count(session: Session, statement) -> int:
    return int(session.execute(select(func.count()).select_from(statement.subquery())).scalar_one())


def _domain_checks(session: Session) -> dict[str, list[str]]:
    """Read meaningful cross-domain facts through the application's ORM models."""
    from app.absence.models import DayStatusEvent
    from app.audit.models import AuditEvent
    from app.auth.models import RoleAssignment
    from app.auth.session_models import AuthSession
    from app.balance.models import BalanceLedgerEntry
    from app.calendar.models import CALENDAR_KEY, CalendarDay, CalendarVersion
    from app.disclosure.models import (
        AccessRequest,
        DisclosureGrant,
        SubmissionPackage,
        SubmissionRecipient,
    )
    from app.identity.models import Employee, EmploymentContract, WorkScheduleVersion
    from app.ocd_overtime.models import (
        CompensationElection,
        OcdAssignment,
        OcdCall,
        OvertimeAuthorization,
    )
    from app.policy.models import PeriodCalculationSnapshot, PolicyVersion
    from app.shift.models import ShiftAssignment, ShiftDeviation, ShiftPlan, ShiftPlanRevision
    from app.travel.models import BusinessTrip, TravelSegment
    from app.worktime.models import WorkInterval, WorkPeriod

    failures: dict[str, list[str]] = {name: [] for name in DOMAIN_NAMES}
    mapped = session.scalar(
        select(func.count()).select_from(Employee).where(Employee.idp_subject.is_not(None))
    )
    distinct_mapped = session.scalar(
        select(func.count(distinct(Employee.idp_subject))).where(Employee.idp_subject.is_not(None))
    )
    if mapped != distinct_mapped:
        failures["identity/roles"].append("mapped identity subjects are not unique")
    if _missing_count(
        session,
        select(RoleAssignment.id)
        .outerjoin(Employee, RoleAssignment.employee_id == Employee.id)
        .where(Employee.id.is_(None)),
    ):
        failures["identity/roles"].append("role assignment references a missing employee")
    if _missing_count(
        session,
        select(AuthSession.id)
        .outerjoin(Employee, AuthSession.employee_id == Employee.id)
        .where(Employee.id.is_(None)),
    ):
        failures["identity/roles"].append("authentication session references a missing employee")
    for model, domain in ((EmploymentContract, "schedule"), (WorkScheduleVersion, "schedule")):
        if _missing_count(
            session,
            select(model.id)
            .outerjoin(Employee, model.employee_id == Employee.id)
            .where(Employee.id.is_(None)),
        ):
            failures[domain].append(f"{model.__tablename__} references a missing employee")
    if _missing_count(
        session,
        select(WorkPeriod.id)
        .outerjoin(Employee, WorkPeriod.employee_id == Employee.id)
        .where(Employee.id.is_(None)),
    ):
        failures["work interval"].append("work period references a missing employee")
    if _missing_count(
        session,
        select(WorkInterval.id)
        .outerjoin(WorkPeriod, WorkInterval.work_period_id == WorkPeriod.id)
        .where((WorkPeriod.id.is_(None)) | (WorkInterval.employee_id != WorkPeriod.employee_id)),
    ):
        failures["work interval"].append("interval does not match its employee work period")
    # EINV-14: ``work_period_id`` is a DERIVED linkage — the Berlin-local start
    # month of the row itself, never the predecessor's month (which a
    # cross-month correction used to copy). Verified per row, superseded
    # versions included: a row's own ``start_at`` never changes, so the
    # invariant is meaningful for every version, not only the chain head.
    berlin_start_month = func.cast(
        func.date_trunc("month", func.timezone("Europe/Berlin", WorkInterval.start_at)),
        Date,
    )
    if _missing_count(
        session,
        select(WorkInterval.id)
        .outerjoin(WorkPeriod, WorkInterval.work_period_id == WorkPeriod.id)
        .where(WorkPeriod.period_month != berlin_start_month),
    ):
        failures["work interval"].append("interval work period does not match its own start month")
    if _missing_count(
        session,
        select(ShiftAssignment.id)
        .outerjoin(Employee, ShiftAssignment.employee_id == Employee.id)
        .outerjoin(ShiftPlan, ShiftAssignment.shift_plan_id == ShiftPlan.id)
        .where((Employee.id.is_(None)) | (ShiftPlan.id.is_(None))),
    ):
        failures["shift"].append("shift assignment has missing employee or plan")
    if _missing_count(
        session,
        select(ShiftPlanRevision.id)
        .outerjoin(
            ShiftAssignment,
            ShiftPlanRevision.shift_assignment_id == ShiftAssignment.id,
        )
        .where(ShiftAssignment.id.is_(None)),
    ):
        failures["shift"].append("shift plan revision references a missing assignment")
    if _missing_count(
        session,
        select(ShiftDeviation.id)
        .outerjoin(Employee, ShiftDeviation.employee_id == Employee.id)
        .outerjoin(
            ShiftAssignment,
            ShiftDeviation.shift_assignment_id == ShiftAssignment.id,
        )
        .where(
            (Employee.id.is_(None))
            | (ShiftAssignment.id.is_(None))
            | (ShiftDeviation.employee_id != ShiftAssignment.employee_id)
        ),
    ):
        failures["shift"].append("shift deviation has an inconsistent employee or assignment")
    if _missing_count(
        session,
        select(OcdAssignment.id)
        .outerjoin(Employee, OcdAssignment.employee_id == Employee.id)
        .where(Employee.id.is_(None)),
    ):
        failures["ocd"].append("OCD assignment references a missing employee")
    if _missing_count(
        session,
        select(OcdCall.id)
        .outerjoin(Employee, OcdCall.employee_id == Employee.id)
        .outerjoin(OcdAssignment, OcdCall.ocd_assignment_id == OcdAssignment.id)
        .where(
            (Employee.id.is_(None))
            | ((OcdCall.ocd_assignment_id.is_not(None)) & (OcdAssignment.id.is_(None)))
            | (
                (OcdCall.ocd_assignment_id.is_not(None))
                & (OcdCall.employee_id != OcdAssignment.employee_id)
            )
        ),
    ):
        failures["ocd"].append("OCD call has inconsistent assignment or employee")
    if _missing_count(
        session,
        select(OvertimeAuthorization.id)
        .outerjoin(Employee, OvertimeAuthorization.employee_id == Employee.id)
        .where(Employee.id.is_(None)),
    ):
        failures["overtime"].append("overtime authorization references a missing employee")
    if _missing_count(
        session,
        select(CompensationElection.id)
        .outerjoin(Employee, CompensationElection.employee_id == Employee.id)
        .where(Employee.id.is_(None)),
    ):
        failures["overtime"].append("compensation election references a missing employee")
    if _missing_count(
        session,
        select(WorkInterval.id)
        .outerjoin(
            OvertimeAuthorization,
            WorkInterval.overtime_authorization_id == OvertimeAuthorization.id,
        )
        .where(
            (WorkInterval.overtime_authorization_id.is_not(None))
            & (OvertimeAuthorization.id.is_(None))
        ),
    ):
        failures["overtime"].append("work interval references a missing overtime authorization")
    if _missing_count(
        session,
        select(BusinessTrip.id)
        .outerjoin(Employee, BusinessTrip.employee_id == Employee.id)
        .where(Employee.id.is_(None)),
    ) or _missing_count(
        session,
        select(TravelSegment.id)
        .outerjoin(BusinessTrip, TravelSegment.trip_id == BusinessTrip.id)
        .outerjoin(WorkInterval, TravelSegment.work_interval_id == WorkInterval.id)
        .where(
            (BusinessTrip.id.is_(None))
            | (
                (TravelSegment.work_interval_id.is_not(None))
                & (
                    (WorkInterval.id.is_(None))
                    | (WorkInterval.employee_id != BusinessTrip.employee_id)
                )
            )
        ),
    ):
        failures["travel"].append("travel row has missing trip or employee")
    if _missing_count(
        session,
        select(BalanceLedgerEntry.id)
        .outerjoin(Employee, BalanceLedgerEntry.employee_id == Employee.id)
        .outerjoin(PolicyVersion, BalanceLedgerEntry.policy_version_id == PolicyVersion.id)
        .outerjoin(WorkPeriod, BalanceLedgerEntry.work_period_id == WorkPeriod.id)
        .where(
            (Employee.id.is_(None))
            | (PolicyVersion.id.is_(None))
            | (
                (BalanceLedgerEntry.work_period_id.is_not(None))
                & (
                    (WorkPeriod.id.is_(None))
                    | (BalanceLedgerEntry.employee_id != WorkPeriod.employee_id)
                )
            )
        ),
    ):
        failures["balance"].append("balance ledger row has inconsistent references")
    packages_without_recipient = select(SubmissionPackage.id).where(
        ~SubmissionPackage.id.in_(select(SubmissionRecipient.submission_package_id))
    )
    if _missing_count(session, packages_without_recipient):
        failures["submission"].append("submission package has no recipient")
    if _missing_count(
        session,
        select(SubmissionPackage.id)
        .outerjoin(Employee, SubmissionPackage.employee_id == Employee.id)
        .where(Employee.id.is_(None)),
    ):
        failures["submission"].append("submission package references a missing employee")
    if _missing_count(
        session,
        select(SubmissionRecipient.submission_package_id)
        .outerjoin(
            SubmissionPackage,
            SubmissionRecipient.submission_package_id == SubmissionPackage.id,
        )
        .where(SubmissionPackage.id.is_(None)),
    ):
        failures["submission"].append("submission recipient references a missing package")
    if _missing_count(
        session,
        select(AccessRequest.id)
        .outerjoin(Employee, AccessRequest.employee_id == Employee.id)
        .where(Employee.id.is_(None)),
    ):
        failures["disclosure"].append("access request references a missing employee")
    if _missing_count(
        session,
        select(DisclosureGrant.id)
        .outerjoin(Employee, DisclosureGrant.employee_id == Employee.id)
        .outerjoin(AccessRequest, DisclosureGrant.access_request_id == AccessRequest.id)
        .where(
            (Employee.id.is_(None))
            | ((DisclosureGrant.access_request_id.is_not(None)) & (AccessRequest.id.is_(None)))
            | (
                (DisclosureGrant.access_request_id.is_not(None))
                & (DisclosureGrant.employee_id != AccessRequest.employee_id)
            )
        ),
    ):
        failures["disclosure"].append("disclosure grant has inconsistent request or employee")
    if _missing_count(
        session,
        select(AuditEvent.id).where(
            (AuditEvent.actor_principal.is_(None)) | (AuditEvent.actor_principal == "")
        ),
    ):
        failures["audit"].append("audit event has an empty actor principal")

    # F4 policy editions (EINV-09 / D7). Three state invariants, all of which a
    # restore must preserve: the range is ordered, no two editions of one key
    # overlap (otherwise "which edition applies" is ambiguous), and no frozen
    # artefact was produced under an edition that did not cover its own period.
    # Whether every recorded edition is EXECUTABLE is enforced where the plan
    # puts it — the migration+registry CI test and the adapter's fail-closed
    # resolution — not against a code registry from a database-only verifier.
    if _missing_count(
        session,
        select(PolicyVersion.id).where(
            (PolicyVersion.effective_to.is_not(None))
            & (PolicyVersion.effective_to < PolicyVersion.effective_from)
        ),
    ):
        failures["policy"].append("policy edition range ends before it starts")
    newer = aliased(PolicyVersion)
    if _missing_count(
        session,
        select(PolicyVersion.id)
        .join(
            newer,
            (newer.policy_key == PolicyVersion.policy_key) & (newer.id != PolicyVersion.id),
        )
        .where(
            # Inclusive ranges [effective_from, effective_to] overlap iff BOTH
            # orderings hold. One direction alone is wrong: against an
            # open-ended edition (`effective_to IS NULL`) it would flag a
            # perfectly valid closed-predecessor/open-successor pair.
            (newer.effective_from <= func.coalesce(PolicyVersion.effective_to, date.max))
            & (PolicyVersion.effective_from <= func.coalesce(newer.effective_to, date.max))
        ),
    ):
        failures["policy"].append("policy editions of one key have overlapping effective ranges")
    # ``period_month`` is a real DATE column, so the edition's effective range
    # can be compared against it directly.
    period_month = WorkPeriod.period_month
    if _missing_count(
        session,
        select(PeriodCalculationSnapshot.id)
        .join(WorkPeriod, PeriodCalculationSnapshot.work_period_id == WorkPeriod.id)
        .join(PolicyVersion, PeriodCalculationSnapshot.policy_version_id == PolicyVersion.id)
        .where(
            (PolicyVersion.effective_from > period_month)
            | (
                PolicyVersion.effective_to.is_not(None)
                & (PolicyVersion.effective_to < period_month)
            )
        ),
    ):
        failures["policy"].append("frozen snapshot references an edition that did not apply")
    if _missing_count(
        session,
        select(SubmissionPackage.id)
        .join(PolicyVersion, SubmissionPackage.policy_version_id == PolicyVersion.id)
        .where(
            (PolicyVersion.effective_from > SubmissionPackage.period_start)
            | (
                PolicyVersion.effective_to.is_not(None)
                & (PolicyVersion.effective_to < SubmissionPackage.period_start)
            )
        ),
    ):
        failures["policy"].append("frozen package references an edition that did not apply")

    # --- calendar: COVERAGE, not non-overlap --------------------------------
    # Overlapping governed ranges are a deliberate property of the model (a
    # bounded corrective overlay is the only way to un-classify a day without
    # mutating a published edition), so an overlap check would contradict the
    # design and produce false failures on every legitimate year-boundary
    # publication. The property that IS load-bearing is coverage.
    if _missing_count(
        session,
        select(CalendarVersion.id).where(
            CalendarVersion.valid_to.is_not(None),
            CalendarVersion.valid_to < CalendarVersion.valid_from,
        ),
    ):
        failures["calendar"].append("a calendar edition's range ends before it starts")
    duplicate_from = (
        select(CalendarVersion.calendar_key, CalendarVersion.valid_from)
        .group_by(CalendarVersion.calendar_key, CalendarVersion.valid_from)
        .having(func.count() > 1)
    )
    if _missing_count(session, select(duplicate_from.subquery().c.calendar_key)):
        failures["calendar"].append(
            "two calendar editions of one key share an effective date (ambiguous resolution)"
        )
    if _missing_count(
        session,
        select(CalendarDay.id)
        .join(CalendarVersion, CalendarDay.calendar_version_id == CalendarVersion.id)
        .where(
            (CalendarDay.day < CalendarVersion.valid_from)
            | (CalendarVersion.valid_to.is_not(None) & (CalendarDay.day > CalendarVersion.valid_to))
        ),
    ):
        failures["calendar"].append("a calendar day row falls outside its own edition's range")
    if _missing_count(
        session,
        select(CalendarDay.id)
        .outerjoin(CalendarVersion, CalendarDay.calendar_version_id == CalendarVersion.id)
        .where(CalendarVersion.id.is_(None)),
    ):
        failures["calendar"].append("a calendar day row references a missing edition")
    # Governed-range coverage of every frozen snapshot's own month. Written as a
    # coverage query over the union of governed ranges, NOT as a referential
    # check on ``calendar_version_ids``: that column is JSONB rather than an FK and
    # pre-F5 snapshots legitimately name the retired placeholder, so referential
    # existence would manufacture failures for truthful historical rows.
    uncovered = session.execute(
        text(
            """
            SELECT s.id, day_value::date AS uncovered_day
            FROM period_calculation_snapshot s
            JOIN work_period p ON p.id = s.work_period_id
            CROSS JOIN LATERAL generate_series(
                p.period_month,
                (p.period_month + interval '1 month' - interval '1 day')::date,
                interval '1 day'
            ) AS day_value
            WHERE NOT EXISTS (
                SELECT 1 FROM calendar_version cv
                WHERE cv.calendar_key = :calendar_key
                  AND cv.valid_from <= day_value::date
                  AND (cv.valid_to IS NULL OR cv.valid_to >= day_value::date)
            )
            LIMIT 1
            """
        ),
        {"calendar_key": CALENDAR_KEY},
    ).first()
    if uncovered is not None:
        failures["calendar"].append(
            "a frozen snapshot's month is not covered by any calendar edition "
            f"(first uncovered day {uncovered[1]})"
        )

    # --- day status: lineage and current-chain integrity ---------------------
    if _missing_count(
        session,
        select(DayStatusEvent.id)
        .outerjoin(Employee, DayStatusEvent.employee_id == Employee.id)
        .where(Employee.id.is_(None)),
    ):
        failures["day status"].append("a day-status row references a missing employee")
    if _missing_count(
        session,
        select(DayStatusEvent.id).where(
            (DayStatusEvent.portion == "FULL") & (DayStatusEvent.day_part != "ALL")
        ),
    ):
        failures["day status"].append("a FULL day status does not cover the whole day")
    if _missing_count(
        session,
        select(DayStatusEvent.id).where(
            (DayStatusEvent.portion == "HALF") & (DayStatusEvent.day_part == "ALL")
        ),
    ):
        failures["day status"].append("a HALF day status does not name MORNING or AFTERNOON")
    forked = (
        select(DayStatusEvent.supersedes_id)
        .where(DayStatusEvent.supersedes_id.is_not(None))
        .group_by(DayStatusEvent.supersedes_id)
        .having(func.count() > 1)
    )
    if _missing_count(session, select(forked.subquery().c.supersedes_id)):
        failures["day status"].append("a day-status row has more than one successor (forked chain)")
    converged = (
        select(
            DayStatusEvent.employee_id,
            DayStatusEvent.day,
            DayStatusEvent.category,
            DayStatusEvent.day_part,
        )
        .where(DayStatusEvent.supersedes_id.is_(None))
        .group_by(
            DayStatusEvent.employee_id,
            DayStatusEvent.day,
            DayStatusEvent.category,
            DayStatusEvent.day_part,
        )
        .having(func.count() > 1)
    )
    if _missing_count(session, select(converged.subquery().c.employee_id)):
        failures["day status"].append(
            "two current day-status heads occupy one employee/day/category/day-part slot"
        )
    parent = aliased(DayStatusEvent)
    if _missing_count(
        session,
        select(DayStatusEvent.id)
        .outerjoin(parent, DayStatusEvent.supersedes_id == parent.id)
        .where(DayStatusEvent.supersedes_id.is_not(None), parent.id.is_(None)),
    ):
        failures["day status"].append("a day-status row's superseded predecessor is missing")

    # --- versioned-fact lineage: no supersedes chain may cross employees -----
    # The DATABASE now refuses this edge (`EINV-26`, migration `0017`), but a
    # database restored from a backup taken BEFORE `0017` can still carry it, so
    # the verifier keeps its own check — and that check is the only thing that
    # sees the state when the constraint is absent.
    #
    # Every "current version" anti-join in the application would hide this
    # employee's own row (list, link validation, calculation inputs) and let the
    # foreign row decide what is current. The whole class is caught here, once,
    # for every versioned table — derived from the metadata, so a table added
    # later cannot be forgotten.
    for table in Base.metadata.sorted_tables:
        if "supersedes_id" not in table.c:
            continue
        # Ownership is normally the employee column, but NOT uniformly:
        # ``travel_segment`` has no ``employee_id`` — its chain is keyed on the
        # trip, and ``business_trip.employee_id`` is NOT NULL, so same-trip
        # implies same-owner. Deriving the key from this map is what keeps the
        # sweep honest for that table; while it was hard-coded to ``employee_id``
        # the table was silently SKIPPED, so "a table added later cannot be
        # forgotten" was false for it.
        owner_key = _LINEAGE_OWNER_KEY.get(table.name, "employee_id")
        if owner_key not in table.c:
            continue
        child = table.alias()
        if _missing_count(
            session,
            select(table.c.id).where(
                exists(
                    select(child.c.id).where(
                        child.c.supersedes_id == table.c.id,
                        child.c[owner_key] != table.c[owner_key],
                    )
                )
            ),
        ):
            failures[_TABLE_DOMAIN.get(table.name, "employee")].append(
                f"a {table.name} row is superseded by a row of another employee"
            )

    # A "known golden interval" check used to live here: it hard-coded one demo
    # seed interval (`emp-0101`, July) and failed a healthy restore as soon as
    # the seed moved to September, so a fixture assertion was deciding an
    # operational recovery verdict. The proof it carried — restored instants and
    # elapsed duration, which a table-count comparison cannot see — now lives in
    # `tests/test_backup_restore.py` with the fixture that owns the row, and is
    # asserted against the restored database there.
    return failures


DOMAIN_NAMES = (
    "employee",
    "identity/roles",
    "schedule",
    "work interval",
    "shift",
    "ocd",
    "overtime",
    "travel",
    "balance",
    "submission",
    "disclosure",
    "audit",
    "policy",
    "calendar",
    "day status",
)

#: Per-table override for the column that scopes a ``supersedes_id`` chain.
#: ``travel_segment`` is the one chain whose owner is not an employee column:
#: it has no ``employee_id``, and its predecessor link is scoped by the trip
#: (``business_trip.employee_id`` is NOT NULL, so same-trip implies same-owner).
#: Without this override the lineage sweep skipped the table entirely.
_LINEAGE_OWNER_KEY = {
    "travel_segment": "trip_id",
}

_TABLE_DOMAIN = {
    "employee": "employee",
    "employee_preference": "employee",
    "role_assignment": "identity/roles",
    "auth_session": "identity/roles",
    "login_attempt": "identity/roles",
    "employment_contract": "schedule",
    "work_schedule_version": "schedule",
    "work_period": "work interval",
    "work_interval": "work interval",
    "correction": "work interval",
    "shift_plan": "shift",
    "shift_assignment": "shift",
    "shift_plan_revision": "shift",
    "shift_deviation": "shift",
    "ocd_assignment": "ocd",
    "ocd_call": "ocd",
    "overtime_authorization": "overtime",
    "compensation_election": "overtime",
    "business_trip": "travel",
    "travel_segment": "travel",
    "balance_ledger_entry": "balance",
    "submission_package": "submission",
    "submission_recipient": "submission",
    "access_request": "disclosure",
    "disclosure_grant": "disclosure",
    "audit_event": "audit",
    "policy_version": "policy",
    "period_calculation_snapshot": "policy",
    "calendar_version": "calendar",
    "calendar_day": "calendar",
    "day_status_event": "day status",
}


def verify_database(database_url: str, manifest_path: Path | None = None) -> bool:
    """Print a compact application-level recovery report and return its status."""
    manifest = _load_manifest(manifest_path) if manifest_path is not None else None
    engine = create_engine(database_url, pool_pre_ping=True)
    failures: list[str] = []
    try:
        expected = expected_schema_revision()
        with engine.connect() as connection:
            inspector = inspect(connection)
            actual_tables = set(inspector.get_table_names())
            required_tables = {table.name for table in _metadata_tables()}
            missing_tables = sorted(required_tables - actual_tables)
            if missing_tables:
                failures.append("missing application tables: " + ", ".join(missing_tables))
            if "alembic_version" not in actual_tables:
                failures.append("alembic_version is missing")
            else:
                revisions = (
                    connection.exec_driver_sql("SELECT version_num FROM alembic_version")
                    .scalars()
                    .all()
                )
                if len(revisions) != 1 or revisions[0] != expected:
                    failures.append("alembic_version is not exactly the expected schema revision")
        counts = _table_counts(engine)
        count_failures: dict[str, list[str]] = {name: [] for name in DOMAIN_NAMES}
        if manifest is not None:
            expected_counts = manifest["table_counts"]
            for table_name, expected_count in expected_counts.items():
                actual_count = counts.get(table_name)
                if actual_count != expected_count:
                    detail = (
                        f"count mismatch for {table_name}: expected {expected_count}, "
                        f"got {actual_count}"
                    )
                    failures.append(detail)
                    domain = _TABLE_DOMAIN.get(table_name)
                    if domain is not None:
                        count_failures[domain].append(detail)
            if manifest.get("schema_revision") != expected:
                failures.append("manifest schema revision does not match this application")
        with Session(engine) as session:
            domain_failures = _domain_checks(session)
        for table_name in missing_tables:
            domain = _TABLE_DOMAIN.get(table_name, "employee")
            domain_failures[domain].append(f"required table {table_name} is missing")
        for domain in DOMAIN_NAMES:
            details = [*domain_failures[domain], *count_failures[domain]]
            if details:
                print(f"{domain}: MISSING - {'; '.join(details)}")
                failures.extend(f"{domain}: {detail}" for detail in details)
            else:
                print(f"{domain}: ok")
        if failures:
            print("verification: FAILED")
            for failure in failures:
                print(f"- {failure}")
            return False
        print("verification: ok")
        return True
    except SQLAlchemyError as exc:
        print(f"verification: FAILED\n- database verification error: {type(exc).__name__}")
        return False
    finally:
        engine.dispose()


_HELP_EPILOG = """\
Responsibility split: Chronobeam produces the dump + non-personal manifest and restores only
into an EMPTY database. Scheduling, off-host transfer, encrypted-at-rest storage, monitoring,
access control, and deletion are IT responsibilities. RETENTION_REQUIRES_DPO_IT_CONFIRMATION.

UPGRADE: backup -> deploy new artifact -> alembic upgrade head (or just migrate) -> start ->
/health/ready 200 -> one authenticated employee-read smoke test.
ROLLBACK: restore previous application artifact first. Downgrade a schema only with a proven
downgrade and no new-schema writes; otherwise restore the pre-upgrade backup into a fresh DB.
Then verify readiness and run backup verify. A short maintenance window is accepted.

TARGET CREDENTIALS: restore/verify --database-url-env NAME reads the URL from that environment
variable. Do not combine it with an argv URL. Existing argv forms remain available.
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.backup",
        description="Chronobeam PostgreSQL backup, empty-target restore, and verification.",
        epilog=_HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    dump = commands.add_parser("dump", help="create custom PostgreSQL dump and manifest")
    dump.add_argument("--out", required=True, type=Path, metavar="DIR")
    restore = commands.add_parser("restore", help="restore dump into an EMPTY database")
    restore.add_argument("dumpfile", type=Path, metavar="DUMPFILE")
    target = restore.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--database-url", metavar="URL", help="target URL (visible in process argv)"
    )
    target.add_argument(
        "--database-url-env", metavar="NAME", help="environment variable with target URL"
    )
    verify = commands.add_parser("verify", help="verify application connectivity and restored data")
    verify.add_argument(
        "database_url", nargs="?", metavar="URL", help="target URL (visible in process argv)"
    )
    verify.add_argument(
        "--database-url-env", metavar="NAME", help="environment variable with target URL"
    )
    verify.add_argument("--manifest", type=Path, metavar="FILE")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        if args.command != "dump":
            if args.database_url is not None and args.database_url_env is not None:
                raise BackupError("supply only one target URL form: argv URL or --database-url-env")
            if args.database_url_env is not None:
                args.database_url = os.environ.get(args.database_url_env)
                if not args.database_url:
                    raise BackupError("target database URL environment variable is unset or empty")
            elif args.database_url is None:
                raise BackupError("supply a target URL or --database-url-env NAME")
        if args.command == "dump":
            dump_path, manifest_path = dump_database(get_settings().database_url, args.out)
            print(f"dump: {dump_path.name}")
            print(f"manifest: {manifest_path.name}")
        elif args.command == "restore":
            if not restore_database(args.dumpfile, args.database_url):
                target_database = _connection_parts(args.database_url).database
                raise BackupError(
                    f"restore verification failed\n{_restore_disposal_guidance(target_database)}"
                )
        else:
            if not verify_database(args.database_url, args.manifest):
                raise SystemExit(1)
    except BackupError as exc:
        print(f"backup: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except Exception:
        # This CLI commonly runs with a database URL in the environment.  An
        # unexpected traceback can include implementation details or a URL in
        # its exception context, so keep its public failure contract generic.
        print("backup: unexpected operational error", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
