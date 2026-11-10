"""Operational recovery proof using two disposable PostgreSQL databases."""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from app import backup
from app.audit.models import AuditEvent
from app.backup import DOMAIN_NAMES, RETENTION_MARKER, _table_counts
from app.balance.models import BalanceLedgerEntry
from app.config import get_settings
from app.db import create_engine, ensure_database
from app.demo_seed import seed_demo
from app.dev_seed import seed_dev
from app.disclosure.models import (
    AccessRequest,
    AccessRequestStatus,
    DisclosureGrant,
    SubmissionPackage,
    SubmissionRecipient,
)
from app.enums import PeriodStatus, PolicyVersionStatus, SubmissionPurpose, WorkType
from app.identity.models import Employee
from app.ocd_overtime.models import OvertimeAuthorization, OvertimeAuthStatus
from app.policy.models import PolicyVersion
from app.schema import expected_schema_revision
from app.travel.models import (
    BusinessTrip,
    TravelSegment,
    TravelSegmentBasis,
    TravelSegmentClassification,
    TripStartOrigin,
)
from app.worktime.models import WorkInterval, WorkPeriod
from psycopg import sql
from sqlalchemy import select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from tests.conftest import _require_db_or_skip

_BACKEND_DIR = Path(__file__).resolve().parents[1]


def _scratch_url(database_name: str) -> str:
    return (
        make_url(get_settings().test_database_url)
        .set(database=database_name)
        .render_as_string(hide_password=False)
    )


@dataclass(frozen=True)
class MaintenanceConnection:
    host: str
    port: int
    user: str
    password: str


def _connection_arguments(database_url: str) -> MaintenanceConnection:
    url = make_url(database_url)
    assert url.host is not None and url.username is not None
    return MaintenanceConnection(
        host=url.host,
        port=url.port or 5432,
        user=url.username,
        password=url.password or "",
    )


def _drop_database(database_url: str) -> None:
    name = make_url(database_url).database
    assert name is not None
    connection_args = _connection_arguments(database_url)
    with psycopg.connect(
        host=connection_args.host,
        port=connection_args.port,
        user=connection_args.user,
        password=connection_args.password,
        dbname="postgres",
        autocommit=True,
    ) as connection:
        statement = sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        connection.execute(statement)


def _require_postgres_tools() -> None:
    for executable in ("pg_dump", "pg_restore"):
        path = shutil.which(executable)
        if path is None:
            _require_db_or_skip(f"required PostgreSQL tool is unavailable: {executable}")
        assert path is not None
        completed = subprocess.run(  # noqa: S603 - resolved PostgreSQL executable
            [path, "--version"], capture_output=True, text=True
        )
        if completed.returncode:
            _require_db_or_skip(f"required PostgreSQL tool is unavailable: {executable}")


def _backup_cli(*arguments: str) -> list[str]:
    executable = shutil.which("uv")
    assert executable is not None, "uv is required for the documented backup CLI"
    return [executable, "run", "python", "-m", "app.backup", *arguments]


def _upgrade_source(database_url: str) -> Engine:
    ensure_database(database_url)
    os.environ["CHRONOBEAM_DATABASE_URL"] = database_url
    get_settings.cache_clear()
    config = Config()
    config.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    return create_engine(database_url, pool_pre_ping=True)


#: Golden facts this test owns: a work period and one interval whose exact
#: instants and elapsed duration must survive the dump/restore round trip.
#: July is deliberate — it is the month the fixture's other synthetic rows
#: already use (overtime authorization, trip, balance entry, access request), so
#: the whole synthetic population agrees on one month, and the demo seed's facts
#: (2026-09-01..2026-10-08) can neither overlap these rows nor move their
#: expected instants.
GOLDEN_PERIOD_MONTH = date(2026, 7, 1)
GOLDEN_START = datetime(2026, 7, 1, 6, tzinfo=UTC)
GOLDEN_END = datetime(2026, 7, 1, 10, tzinfo=UTC)
GOLDEN_SECONDS = 14_400


def _add_representative_rows(session: Session) -> None:
    employee = session.scalar(select(Employee).where(Employee.external_employee_id == "emp-0101"))
    assert employee is not None
    # The recovery proof owns its own golden facts. The period and interval used
    # to be borrowed from the demo seed, so moving the demo to September
    # (`14ff610`) silently broke this fixture; creating them here, in the month
    # the fixture's other synthetic rows already use, keeps the proof independent
    # of the seed's shape.
    period = WorkPeriod(
        employee_id=employee.id,
        period_month=GOLDEN_PERIOD_MONTH,
        status=PeriodStatus.OPEN,
    )
    session.add(period)
    session.flush()
    interval = WorkInterval(
        employee_id=employee.id,
        work_period_id=period.id,
        start_at=GOLDEN_START,
        end_at=GOLDEN_END,
        # ``OVERTIME_PAID`` because the row below links it to an overtime
        # authorization, and the write path accepts that link only on
        # ``OVERTIME_*`` facts (``worktime.service._validate_overtime_link``).
        # The fixture must not assert a state the supported writer refuses.
        work_type=WorkType.OVERTIME_PAID,
        version=1,
    )
    session.add(interval)
    session.flush()
    policy = PolicyVersion(
        policy_key="restore-test",
        version="1",
        effective_from=date(2026, 1, 1),
        effective_to=None,
        status=PolicyVersionStatus.PROVISIONAL,
        engine_rule_set="restore-test",
        source_manifest_hash="synthetic-restore-test",
    )
    session.add(policy)
    session.flush()
    overtime = OvertimeAuthorization(
        employee_id=employee.id,
        valid_from=datetime(2026, 7, 1, tzinfo=UTC),
        valid_to=datetime(2026, 7, 31, tzinfo=UTC),
        purpose="synthetic recovery proof",
        status=OvertimeAuthStatus.REQUESTED,
        source="EMPLOYEE_RECORDED",
        version=1,
    )
    session.add(overtime)
    session.flush()
    interval.overtime_authorization_id = overtime.id
    trip = BusinessTrip(
        employee_id=employee.id,
        start_at=datetime(2026, 7, 22, 6, tzinfo=UTC),
        end_at=datetime(2026, 7, 22, 10, tzinfo=UTC),
        start_origin=TripStartOrigin.HOME,
        source="EMPLOYEE_DECLARED",
        reference_label="synthetic recovery trip",
        version=1,
    )
    session.add(trip)
    session.flush()
    session.add(
        TravelSegment(
            trip_id=trip.id,
            start_at=trip.start_at,
            end_at=trip.end_at,
            classification=TravelSegmentClassification.PASSIVE_TRAVEL,
            basis=TravelSegmentBasis.ACTUAL,
            version=1,
        )
    )
    session.add(
        # The work period proves a balance row is tied to the same employee/month.
        BalanceLedgerEntry(
            employee_id=employee.id,
            work_period_id=period.id,
            occurred_on=date(2026, 7, 31),
            entry_type="RESTORE_TEST",
            minutes=1,
            source_entity_type="restore_test",
            source_entity_id=None,
            policy_version_id=policy.id,
        )
    )
    request = AccessRequest(
        employee_id=employee.id,
        requester_principal="demo-hr",
        purpose=SubmissionPurpose.RANDOM_CHECK,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        view_type="MONTH",
        status=AccessRequestStatus.PENDING,
    )
    session.add(request)
    session.flush()
    session.add(
        DisclosureGrant(
            access_request_id=request.id,
            employee_id=employee.id,
            grantee_principal="demo-hr",
            purpose=SubmissionPurpose.RANDOM_CHECK,
            period_start=request.period_start,
            period_end=request.period_end,
            view_type=request.view_type,
            granted_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
    )
    package = SubmissionPackage(
        employee_id=employee.id,
        purpose=SubmissionPurpose.OVERTIME_PAID,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        version=1,
        payload_json={"projection": "synthetic-recovery-proof"},
        employee_confirmed_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    session.add(package)
    session.flush()
    session.add(
        SubmissionRecipient(
            submission_package_id=package.id,
            recipient_type="HR",
            recipient_principal="demo-hr",
        )
    )
    session.add(
        AuditEvent(
            actor_principal="restore-test-operator",
            action="RESTORE_TEST",
            object_type="work_period",
            object_id=period.id,
            employee_subject_id=employee.id,
            outcome="SUCCESS",
            metadata_json={"month": "2026-07"},
        )
    )
    session.commit()


@pytest.fixture(scope="module")
def restored_backup(tmp_path_factory: pytest.TempPathFactory):
    _require_postgres_tools()
    source_url = _scratch_url("chronobeam_restore_src")
    target_url = _scratch_url("chronobeam_restore_dst")
    try:
        _drop_database(source_url)
        _drop_database(target_url)
    except psycopg.OperationalError as exc:
        _require_db_or_skip(str(exc).splitlines()[0] if str(exc) else repr(exc))
        raise AssertionError("unreachable") from None  # pragma: no cover
    previous_url = os.environ.get("CHRONOBEAM_DATABASE_URL")
    source_engine: Engine | None = None
    try:
        source_engine = _upgrade_source(source_url)
        with Session(source_engine) as session:
            # ``seed_demo`` alone, deliberately: the demo seed owns 2026-01-01 for
            # its statutory calendar, and ``seed_dev`` publishes its own
            # exception-free coverage edition on that same effective date, which
            # ``uq_calendar_version_effective_from`` permits only once. Running
            # both in one database is refused rather than silently merged (see
            # ``app.demo_seed.publish_demo_calendar``). ``seed_demo`` is also the
            # richer population for a restore proof: 13 employees, the calendar,
            # trips, shifts, OCD, overtime, a ledger entry and a submission package.
            seed_demo(session)
            _add_representative_rows(session)
        source_counts = _table_counts(source_engine)
        output_dir = tmp_path_factory.mktemp("chronobeam-backup")
        env = {
            **os.environ,
            "CHRONOBEAM_ENV": "test",
            "CHRONOBEAM_DATABASE_URL": source_url,
        }
        dump = subprocess.run(  # noqa: S603 - documented local CLI
            _backup_cli("dump", "--out", str(output_dir)),
            cwd=_BACKEND_DIR,
            env=env,
            capture_output=True,
            text=True,
        )
        assert dump.returncode == 0, dump.stderr
        dump_path = next(output_dir.glob("*.dump"))
        manifest_path = dump_path.with_suffix(".manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        created_at = datetime.fromisoformat(manifest["created_at"])
        assert created_at.tzinfo == UTC
        assert "PostgreSQL" in manifest["postgres_version"]
        assert manifest["schema_revision"] == expected_schema_revision()
        assert isinstance(manifest["app_version"], str) and manifest["app_version"]
        assert isinstance(manifest["build_sha"], str)
        assert len(manifest["dump_sha256"]) == 64
        assert manifest["retention"] == RETENTION_MARKER
        assert manifest["table_counts"] == source_counts
        assert manifest["table_counts"] and all(
            isinstance(count, int) and count >= 0 for count in manifest["table_counts"].values()
        )
        restore = subprocess.run(  # noqa: S603 - documented local CLI
            _backup_cli(
                "restore",
                str(dump_path),
                "--database-url",
                target_url,
            ),
            cwd=_BACKEND_DIR,
            env=env,
            capture_output=True,
            text=True,
        )
        if restore.returncode != 0:
            # The CLI keeps child output out of its message, so the diagnostic
            # file beside the dump is the only place the cause is recorded. The
            # fixture's own data is synthetic, so printing it is safe here.
            diagnostic = dump_path.with_name(dump_path.name + ".restore.log")
            tail = (
                diagnostic.read_text(encoding="utf-8", errors="replace")
                if diagnostic.is_file()
                else "<no diagnostic file>"
            )
            pytest.fail(f"restore failed: {restore.stderr}{tail}")
        yield source_url, target_url, dump_path, manifest_path, env
    finally:
        if source_engine is not None:
            source_engine.dispose()
        _drop_database(source_url)
        _drop_database(target_url)
        if previous_url is None:
            os.environ.pop("CHRONOBEAM_DATABASE_URL", None)
        else:
            os.environ["CHRONOBEAM_DATABASE_URL"] = previous_url
        get_settings.cache_clear()


def test_restored_database_verifies_all_required_domains(restored_backup) -> None:
    _, target_url, _, manifest_path, env = restored_backup
    verify = subprocess.run(  # noqa: S603 - documented local CLI
        _backup_cli(
            "verify",
            target_url,
            "--manifest",
            str(manifest_path),
        ),
        cwd=_BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    assert verify.returncode == 0, verify.stderr + verify.stdout
    for domain in DOMAIN_NAMES:
        assert f"{domain}: ok" in verify.stdout
    # What a table-count comparison cannot see: the restored interval must keep
    # its owner, its instants, its elapsed duration and its work-period link.
    # Matching on the owner as well as on the instants is what stops another
    # employee's row from satisfying the assertion.
    target_engine = create_engine(target_url)
    try:
        with Session(target_engine) as session:
            owner = session.scalar(
                select(Employee).where(Employee.external_employee_id == "emp-0101")
            )
            assert owner is not None, "the restored database lost the fixture employee"
            restored = session.scalar(
                select(WorkInterval).where(
                    WorkInterval.employee_id == owner.id,
                    WorkInterval.start_at == GOLDEN_START,
                    WorkInterval.end_at == GOLDEN_END,
                )
            )
            assert restored is not None, "the golden interval did not survive the restore"
            elapsed = int((restored.end_at - restored.start_at).total_seconds())
            assert elapsed == GOLDEN_SECONDS
            restored_period = session.scalar(
                select(WorkPeriod).where(WorkPeriod.id == restored.work_period_id)
            )
            assert restored_period is not None, "the golden interval lost its work period"
            assert restored_period.period_month == GOLDEN_PERIOD_MONTH
    finally:
        target_engine.dispose()


def test_restore_refuses_nonempty_target(restored_backup) -> None:
    _, target_url, dump_path, _, env = restored_backup
    restore = subprocess.run(  # noqa: S603 - documented local CLI
        _backup_cli(
            "restore",
            str(dump_path),
            "--database-url",
            target_url,
        ),
        cwd=_BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    assert restore.returncode != 0
    assert "EMPTY database" in restore.stderr


def test_verify_detects_restored_work_interval_loss(restored_backup) -> None:
    _, target_url, _, manifest_path, env = restored_backup
    target_engine = create_engine(target_url)
    try:
        with target_engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM work_interval WHERE id = "
                    "(SELECT id FROM work_interval WHERE overtime_authorization_id IS NULL LIMIT 1)"
                )
            )
    finally:
        target_engine.dispose()
    verify = subprocess.run(  # noqa: S603 - documented local CLI
        _backup_cli(
            "verify",
            target_url,
            "--manifest",
            str(manifest_path),
        ),
        cwd=_BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    assert verify.returncode != 0
    assert "work interval: MISSING" in verify.stdout


def test_backup_cli_hides_unexpected_exception_details(monkeypatch, capsys, tmp_path) -> None:
    """An operational CLI error must not turn a configured URL into a traceback."""
    from types import SimpleNamespace

    secret_url = "postgresql+psycopg://operator:" + "secret-value@db.example/chronobeam"
    monkeypatch.setattr(backup, "get_settings", lambda: SimpleNamespace(database_url=secret_url))

    def unexpected(*_args, **_kwargs):
        raise RuntimeError(f"failed against {secret_url}")

    monkeypatch.setattr(backup, "dump_database", unexpected)
    with pytest.raises(SystemExit) as error:
        backup.main(["dump", "--out", str(tmp_path)])
    captured = capsys.readouterr()
    assert error.value.code == 1
    assert captured.err == "backup: unexpected operational error\n"
    assert secret_url not in captured.err


def test_the_manifest_counts_come_from_the_dump_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write landing DURING ``pg_dump`` must not enter the manifest.

    The counts used to be read after the dump, on a fresh READ COMMITTED
    connection, so a concurrent write made the manifest describe a state the
    dump does not contain — and ``verify_database`` compares the restored counts
    against that same manifest, so a good dump could fail verification.

    The side effect is injected at the moment ``pg_dump`` is invoked, which is
    the exact window; no threads and no timing are involved.
    """
    _require_postgres_tools()
    source_url = _scratch_url("chronobeam_snapshot_src")
    try:
        _drop_database(source_url)
    except psycopg.OperationalError as exc:
        _require_db_or_skip(str(exc).splitlines()[0] if str(exc) else repr(exc))
        raise AssertionError("unreachable") from None  # pragma: no cover

    previous_url = os.environ.get("CHRONOBEAM_DATABASE_URL")
    source_engine: Engine | None = None
    try:
        source_engine = _upgrade_source(source_url)
        with Session(source_engine) as session:
            seed_dev(session)
        baseline = _table_counts(source_engine)["audit_event"]

        real = backup._run_postgres_tool

        def with_concurrent_write(  # type: ignore[no-untyped-def]
            args, parts, operation, **kwargs
        ):
            if operation == "pg_dump":
                # A DIFFERENT connection, committed: exactly the write the
                # snapshot must exclude.
                writer = create_engine(source_url, pool_pre_ping=True)
                try:
                    with writer.begin() as connection:
                        connection.execute(
                            text(
                                "INSERT INTO audit_event "
                                "(actor_principal, action, outcome) "
                                "VALUES ('snapshot-probe', 'PROBE', 'SUCCESS')"
                            )
                        )
                finally:
                    writer.dispose()
            real(args, parts, operation, **kwargs)

        monkeypatch.setattr(backup, "_run_postgres_tool", with_concurrent_write)

        output_dir = tmp_path / "dump"
        dump_path, manifest_path = backup.dump_database(source_url, output_dir)
        assert dump_path.is_file()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        # The probe row is committed and visible on a fresh connection...
        assert _table_counts(source_engine)["audit_event"] == baseline + 1
        # ...and is NOT in the manifest, because the manifest describes the
        # snapshot the dump was taken from.
        assert manifest["table_counts"]["audit_event"] == baseline
        assert manifest["pg_client_version"]
    finally:
        if source_engine is not None:
            source_engine.dispose()
        _drop_database(source_url)
        if previous_url is None:
            os.environ.pop("CHRONOBEAM_DATABASE_URL", None)
        else:
            os.environ["CHRONOBEAM_DATABASE_URL"] = previous_url


def test_a_dump_client_older_than_the_server_is_refused() -> None:
    """pg_dump cannot dump a server newer than its own major version."""
    server = "PostgreSQL 16.15 on x86_64-pc-linux-musl, compiled by gcc (Alpine 15.2.0)"
    with pytest.raises(backup.BackupError, match="older than the server") as raised:
        backup._require_compatible_client("pg_dump", "15.9", server)
    assert "Install pg_dump major >=16" in str(raised.value)
    assert backup._major(server) == 16
    assert backup._major("16.4") == 16


@pytest.mark.parametrize("client,server", [("16.4", "16.15"), ("16.4", "15.9"), ("17.1", "16.15")])
def test_supported_dump_version_pairings(client: str, server: str) -> None:
    """Same-major and newer-client dumps remain accepted."""
    backup._require_compatible_client("pg_dump", client, server)


@pytest.mark.parametrize(
    "client,server", [("15.9", "16.15"), ("16.4", "16.15"), ("17.1", "16.15"), ("16.4", "17.1")]
)
def test_the_dump_rule_does_not_gate_pg_restore(client: str, server: str) -> None:
    """The pg_restore binary's own major is not the pairing input."""
    backup._require_compatible_client("pg_restore", client, server)


def test_a_dump_from_a_newer_client_is_refused_by_an_older_target() -> None:
    """A PostgreSQL 17 dump carries SET transaction_timeout, which 16 rejects.

    This is the exact failure that broke CI: the runner's client was a newer
    major than the compose server, pg_restore performed no comparison of its
    own, and --exit-on-error left the target partially populated.
    """
    server = "PostgreSQL 16.15 on x86_64-pc-linux-musl, compiled by gcc (Alpine 15.2.0)"
    with pytest.raises(backup.BackupError, match="written by a PostgreSQL 17 client") as raised:
        backup._require_restore_target("17.11", server)
    message = str(raised.value)
    assert "PostgreSQL 16 server" in message
    assert "re-dump with a client whose major matches the target server (16)" in message


@pytest.mark.parametrize(
    "dump_client,server", [("16.4", "16.15"), ("15.9", "16.15"), ("16.4", "17.1"), ("17.1", "17.4")]
)
def test_a_dump_from_the_same_or_older_client_is_accepted(dump_client: str, server: str) -> None:
    backup._require_restore_target(dump_client, server)


def test_a_manifest_without_a_dump_client_version_is_refused() -> None:
    with pytest.raises(backup.BackupError, match="does not record the dump client version"):
        backup._require_restore_target("", "PostgreSQL 16.15")


@pytest.mark.parametrize("tool", ["pg_dump", "pg_restore"])
@pytest.mark.parametrize("sslmode", ["verify-full", "disable"])
def test_backup_tls_options_reach_the_child_environment(monkeypatch, tool, sslmode) -> None:
    monkeypatch.setenv("PGSSLMODE", "prefer")
    monkeypatch.setenv("PGSSLROOTCERT", "/old/root.crt")
    parts = backup._connection_parts(
        "postgresql+psycopg://operator:synthetic-password@db.example/chronobeam"
        f"?sslmode={sslmode}&sslrootcert=/etc/ssl/xf.crt"
        "&sslcert=/etc/ssl/client.crt&sslkey=/etc/ssl/client.key&sslcrl=/etc/ssl/root.crl"
    )
    captured = []

    def completed(args, *, env, capture_output, text):
        captured.append((args, env))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(backup.subprocess, "run", completed)
    backup._run_postgres_tool([tool], parts, tool)
    assert len(captured) == 1
    args, env = captured[0]
    assert env["PGSSLMODE"] == sslmode
    assert env["PGSSLROOTCERT"] == "/etc/ssl/xf.crt"
    assert env["PGSSLCERT"] == "/etc/ssl/client.crt"
    assert env["PGSSLKEY"] == "/etc/ssl/client.key"
    assert env["PGSSLCRL"] == "/etc/ssl/root.crl"
    assert env["PGPASSWORD"] == "synthetic-password"
    assert args == [tool]


@pytest.mark.parametrize("value", ["QUERY_VALUE_SENTINEL", ""])
def test_backup_refuses_unsupported_url_query_options(value: str) -> None:
    with pytest.raises(backup.BackupError, match="unsupported database URL query option") as raised:
        backup._connection_parts(
            "postgresql://operator:synthetic-password@db.example/chronobeam"
            f"?connect_timeout={value}&QUERY_KEY_SENTINEL=hidden"
        )
    assert "QUERY_VALUE_SENTINEL" not in str(raised.value)
    assert "QUERY_KEY_SENTINEL" not in str(raised.value)


def test_backup_refuses_sslpassword_without_a_libpq_environment_equivalent() -> None:
    with pytest.raises(backup.BackupError, match="sslpassword has no libpq environment variable"):
        backup._connection_parts(
            "postgresql://operator:synthetic-password@db.example/chronobeam?sslpassword=hidden"
        )


@pytest.mark.parametrize(
    "query", ["sslmode=verify-full&sslmode=disable", "sslmode=&sslmode=disable"]
)
def test_backup_refuses_repeated_tls_options(query: str) -> None:
    with pytest.raises(backup.BackupError, match="one value"):
        backup._connection_parts(
            f"postgresql://operator:synthetic-password@db.example/chronobeam?{query}"
        )


def test_backup_refuses_empty_tls_options() -> None:
    with pytest.raises(backup.BackupError, match="non-empty"):
        backup._connection_parts("postgresql://operator:synthetic-password@db.example/db?sslmode=")


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://operator:raw#password@db.example/db?sslmode=verify-full",
        "postgresql://operator:synthetic-password@db.example/db"
        "?sslrootcert=/foo#bar&sslmode=verify-full",
        "postgresql://operator:synthetic-password@db.example/db?sslmode=verify-full#fragment",
    ],
)
def test_backup_refuses_disagreeing_url_query_parsers(url: str) -> None:
    """Ambiguous '#' syntax cannot silently drop or change application TLS options."""
    with pytest.raises(backup.BackupError, match="percent-encode reserved characters") as raised:
        backup._connection_parts(url)
    assert url not in str(raised.value)


@pytest.mark.parametrize("password", ["raw%23password", "raw%3Fpassword"])
def test_backup_preserves_tls_with_percent_encoded_credentials(password: str) -> None:
    parts = backup._connection_parts(
        f"postgresql://operator:{password}@db.example/db?sslmode=verify-full&sslrootcert=/foo%23bar"
    )
    assert parts.security_env == {"PGSSLMODE": "verify-full", "PGSSLROOTCERT": "/foo#bar"}


def test_backup_url_without_tls_options_adds_no_tls_environment(monkeypatch) -> None:
    for name in ("PGSSLMODE", "PGSSLROOTCERT", "PGSSLCERT", "PGSSLKEY", "PGSSLCRL"):
        monkeypatch.delenv(name, raising=False)
    parts = backup._connection_parts(
        "postgresql://operator:synthetic-password@db.example/chronobeam"
    )
    assert backup._pg_env(parts) == {**os.environ, "PGPASSWORD": "synthetic-password"}


def test_backup_url_without_tls_options_preserves_inherited_environment(monkeypatch) -> None:
    monkeypatch.setenv("PGSSLMODE", "disable")
    parts = backup._connection_parts(
        "postgresql://operator:synthetic-password@db.example/chronobeam"
    )
    assert backup._pg_env(parts) == {**os.environ, "PGPASSWORD": "synthetic-password"}


@pytest.mark.parametrize("command", ["restore", "verify"])
@pytest.mark.parametrize("target_form", ["env", "argv"])
def test_backup_cli_accepts_target_credentials(monkeypatch, capsys, command, target_form) -> None:
    target_url = "postgresql+psycopg://operator:synthetic-password@db.example/restore_target"
    monkeypatch.setenv("CHRONOBEAM_RESTORE_URL", target_url)
    captured = []

    def accepted(*args):
        captured.append(args)
        return True

    monkeypatch.setattr(backup, "restore_database", accepted)
    monkeypatch.setattr(backup, "verify_database", accepted)
    argv = [command, "backup.dump"] if command == "restore" else [command]
    if target_form == "env":
        argv += ["--database-url-env", "CHRONOBEAM_RESTORE_URL"]
    else:
        argv += ["--database-url", target_url] if command == "restore" else [target_url]
    backup.main(argv)
    expected = [(Path("backup.dump"), target_url)] if command == "restore" else [(target_url, None)]
    assert captured == expected
    output = capsys.readouterr()
    assert target_url not in output.out + output.err


@pytest.mark.parametrize("command", ["restore", "verify"])
def test_backup_cli_refuses_both_target_forms(monkeypatch, capsys, command) -> None:
    target_url = "postgresql://operator:ARGV_PASSWORD_SENTINEL@db.example/restore_target"
    monkeypatch.setenv("CHRONOBEAM_RESTORE_URL", target_url)
    argv = (
        ["restore", "backup.dump", "--database-url", target_url]
        if command == "restore"
        else ["verify", target_url]
    )
    with pytest.raises(SystemExit) as raised:
        backup.main([*argv, "--database-url-env", "CHRONOBEAM_RESTORE_URL"])
    output = capsys.readouterr()
    assert raised.value.code != 0
    assert "not allowed" in output.err or "supply only one" in output.err
    assert "ARGV_PASSWORD_SENTINEL" not in output.out + output.err


@pytest.mark.parametrize("command", ["restore", "verify"])
@pytest.mark.parametrize("value", [None, ""])
def test_backup_cli_refuses_missing_or_empty_target_environment(
    monkeypatch, capsys, command, value
):
    if value is None:
        monkeypatch.delenv("CHRONOBEAM_RESTORE_URL", raising=False)
    else:
        monkeypatch.setenv("CHRONOBEAM_RESTORE_URL", value)
    argv = [command, "backup.dump"] if command == "restore" else [command]
    with pytest.raises(SystemExit) as raised:
        backup.main([*argv, "--database-url-env", "CHRONOBEAM_RESTORE_URL"])
    assert raised.value.code == 1
    assert "target database URL environment variable is unset or empty" in capsys.readouterr().err


@pytest.mark.parametrize("tool", ["pg_dump", "pg_restore"])
def test_backup_tool_failures_withhold_child_stderr(monkeypatch, tool) -> None:
    sentinel = "SYNTHETIC_EMPLOYEE_ROW_PAYLOAD_9a3f"

    def failed(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr=f'COPY error: "{sentinel}"')

    monkeypatch.setattr(backup.subprocess, "run", failed)
    parts = backup._connection_parts(
        "postgresql://operator:synthetic-password@db.example/chronobeam"
    )
    with pytest.raises(backup.BackupError) as raised:
        backup._run_postgres_tool([tool], parts, tool)
    assert sentinel not in str(raised.value)
    message = str(raised.value)
    assert message.startswith(f"{tool} failed (exit 1)")
    assert "PostgreSQL writes this to the child's stderr, not to a log file." in message


@pytest.mark.parametrize("tool", ["pg_dump", "pg_restore"])
def test_backup_client_version_failures_withhold_child_stderr(monkeypatch, tool) -> None:
    sentinel = "SYNTHETIC_CONNECTION_SECRET_9a3f"

    def failed(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr=sentinel)

    monkeypatch.setattr(backup.subprocess, "run", failed)
    with pytest.raises(backup.BackupError) as raised:
        backup._client_version(tool)
    assert sentinel not in str(raised.value)
    assert (
        str(raised.value) == f"{tool} --version failed (exit 1); see the PostgreSQL log for details"
    )


@pytest.mark.parametrize("tool", ["pg_dump", "pg_restore"])
def test_backup_unparseable_client_version_withholds_child_stdout(monkeypatch, tool) -> None:
    """A *successful* `--version` whose output cannot be parsed must not echo it.

    The child's stdout is external output and this error reaches the CLI, so a
    wrapper script or an unexpected build that prints anything else must not put
    that text into the operator's error message.
    """
    sentinel = "SYNTHETIC_EMPLOYEE_ROW_PAYLOAD_9a3f"

    def succeeded(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 0, stdout=f"unexpected wrapper {sentinel}\n", stderr=""
        )

    monkeypatch.setattr(backup.subprocess, "run", succeeded)
    version = backup._client_version(tool)
    # The sentinel must really reach the failing function, or this test could
    # pass simply because it never got there.
    assert sentinel in version
    with pytest.raises(backup.BackupError) as raised:
        backup._major(version)
    assert sentinel not in str(raised.value)


def test_backup_cli_restore_failure_withholds_row_payload_and_keeps_disposal_guidance(
    monkeypatch, capsys, tmp_path
) -> None:
    sentinel = "SYNTHETIC_EMPLOYEE_ROW_PAYLOAD_9a3f"
    dump_path = tmp_path / "probe.dump"
    dump_path.write_bytes(b"synthetic archive")
    dump_path.with_suffix(".manifest.json").write_text(
        json.dumps(
            {
                "table_counts": {},
                "dump_sha256": backup._sha256(dump_path),
                "pg_client_version": "16.4",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(backup, "_empty_database_or_raise", lambda _url: None)
    monkeypatch.setattr(backup, "_postgres_version", lambda _engine: "16.15")
    monkeypatch.setattr(backup, "_client_version", lambda _tool: "16.4")
    captured = []

    def failed(args, **kwargs):
        captured.append(args)
        return subprocess.CompletedProcess(args, 1, stdout="", stderr=f'COPY error: "{sentinel}"')

    monkeypatch.setattr(backup.subprocess, "run", failed)
    monkeypatch.setenv(
        "CHRONOBEAM_RESTORE_URL",
        "postgresql+psycopg://operator:synthetic-password@db.example/restore",
    )
    with pytest.raises(SystemExit) as raised:
        backup.main(["restore", str(dump_path), "--database-url-env", "CHRONOBEAM_RESTORE_URL"])
    output = capsys.readouterr()
    assert raised.value.code == 1
    assert captured and "--exit-on-error" in captured[0]
    assert sentinel not in output.out + output.err
    assert "synthetic-password" not in output.out + output.err
    assert "pg_restore failed (exit 1)" in output.err
    assert "PARTIALLY POPULATED" in output.err
    assert "fresh empty database" in output.err


def test_backup_cli_verification_failure_withholds_child_output_and_keeps_disposal_guidance(
    monkeypatch, capsys, tmp_path
) -> None:
    sentinel = "SYNTHETIC_CHILD_OUTPUT_4f21"
    dump_path = tmp_path / "probe.dump"
    dump_path.write_bytes(b"synthetic archive")
    dump_path.with_suffix(".manifest.json").write_text(
        json.dumps(
            {
                "table_counts": {},
                "dump_sha256": backup._sha256(dump_path),
                "pg_client_version": "16.4",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(backup, "_empty_database_or_raise", lambda _url: None)
    monkeypatch.setattr(backup, "_postgres_version", lambda _engine: "16.15")
    monkeypatch.setattr(backup, "_client_version", lambda _tool: "16.4")
    monkeypatch.setattr(backup, "verify_database", lambda _url, _manifest: False)
    captured = []

    def succeeded(args, **kwargs):
        captured.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=sentinel, stderr=sentinel)

    monkeypatch.setattr(backup.subprocess, "run", succeeded)
    monkeypatch.setenv(
        "CHRONOBEAM_RESTORE_URL",
        "postgresql+psycopg://operator:synthetic-password@db.example/restore",
    )
    with pytest.raises(SystemExit) as raised:
        backup.main(["restore", str(dump_path), "--database-url-env", "CHRONOBEAM_RESTORE_URL"])
    output = capsys.readouterr()
    assert raised.value.code == 1
    assert captured and "--exit-on-error" in captured[0]
    assert sentinel not in output.out + output.err
    assert "synthetic-password" not in output.out + output.err
    assert "restore verification failed" in output.err
    assert "PARTIALLY POPULATED" in output.err
    assert "fresh empty database" in output.err


def test_a_failed_restore_names_the_disposal_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--exit-on-error`` is passed, and a failure is not an invitation to retry."""
    _require_postgres_tools()
    target_url = _scratch_url("chronobeam_disposal_probe")
    try:
        _drop_database(target_url)
    except psycopg.OperationalError as exc:
        _require_db_or_skip(str(exc).splitlines()[0] if str(exc) else repr(exc))
        raise AssertionError("unreachable") from None  # pragma: no cover

    dump_path = tmp_path / "probe.dump"
    dump_path.write_bytes(b"not a real dump")
    manifest_path = dump_path.with_suffix(".manifest.json")
    manifest_path.write_text(
        json.dumps(
            {
                "table_counts": {},
                "dump_sha256": backup._sha256(dump_path),
                "pg_client_version": "16.4",
            }
        ),
        encoding="utf-8",
    )

    captured: list[list[str]] = []

    def failing(args, parts, operation, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(list(args))
        if operation == "pg_restore":
            raise backup.BackupError("pg_restore failed (exit 1): simulated")
        real_run_postgres_tool(args, parts, operation, **kwargs)

    real_run_postgres_tool = backup._run_postgres_tool
    monkeypatch.setattr(backup, "_run_postgres_tool", failing)
    try:
        with pytest.raises(backup.BackupError) as raised:
            backup.restore_database(dump_path, target_url)
    finally:
        _drop_database(target_url)

    assert captured, "pg_restore was never invoked"
    assert "--exit-on-error" in captured[0], captured[0]
    message = str(raised.value)
    assert "PARTIALLY POPULATED" in message
    assert "fresh empty database" in message
