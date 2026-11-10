"""The operator identity-remap CLI and its locking protocol.

``app.identity_admin`` is the ONLY way to change an existing employee's identity
binding. These tests prove the four properties the F1 design rests on:

- the change is recorded with its previous/new subject, reason and actor;
- every session of the employee is invalidated;
- the fixed identity-administration lock is taken before the target is resolved,
  and the per-employee fact lock after it (observed lock waits, never sleeps);
- a blank/whitespace-only reason cannot be persisted by any caller.

The CLI commits, so the tests use dedicated subjects and clean up in ``finally``.
"""

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.audit.models import AuditEvent
from app.auth.session_models import AuthSession
from app.db import Base, lock_employee_facts
from app.identity.models import Employee, IdentityBindingChange
from app.identity.service import lock_identity_administration, set_identity_binding
from app.identity_admin import main
from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

ACTION = "ADMIN_IDENTITY_REMAP"


def _wait_for_another_blocked_backend(engine: Engine, timeout: float = 15.0) -> bool:
    """Wait until SOME other backend is observed waiting on a lock.

    The CLI opens its own connection, so its pid is not known in advance; with
    nothing else contended in the test, an observed blocked backend can only be
    the invocation under test.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as probe:
            blocked = probe.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
                )
            )
        if blocked:
            return True
        time.sleep(0.02)
    return False


@pytest.fixture()
def subject_fixture(db_engine: Engine):
    """Two employees with dedicated subjects; cleaned up after the test."""
    tag = uuid.uuid4().hex[:8]
    old_subject = f"cli-old-{tag}"
    new_subject = f"cli-new-{tag}"
    taken_subject = f"cli-taken-{tag}"
    with Session(db_engine) as session, session.begin():
        target = Employee(legal_name="CLI Target", idp_subject=old_subject, active=True)
        other = Employee(legal_name="CLI Other", idp_subject=taken_subject, active=True)
        session.add_all([target, other])
        session.flush()
        target_id, other_id = target.id, other.id
    try:
        yield {
            "target_id": target_id,
            "other_id": other_id,
            "old_subject": old_subject,
            "new_subject": new_subject,
            "taken_subject": taken_subject,
        }
    finally:
        with Session(db_engine) as session, session.begin():
            session.execute(
                delete(IdentityBindingChange).where(
                    IdentityBindingChange.employee_id.in_([target_id, other_id])
                )
            )
            session.execute(delete(AuthSession).where(AuthSession.employee_id == target_id))
            session.execute(delete(Employee).where(Employee.id.in_([target_id, other_id])))


def test_set_binding_records_provenance_and_invalidates_sessions(
    db_engine: Engine, subject_fixture: dict
) -> None:
    """A change records old/new/reason/actor and kills every live session."""
    target_id = subject_fixture["target_id"]
    with Session(db_engine) as session, session.begin():
        session.add(
            AuthSession(
                token_hash=uuid.uuid4().hex,
                employee_id=target_id,
                subject=subject_fixture["old_subject"],
                csrf_token=uuid.uuid4().hex,
                last_seen_at=datetime.now(UTC),
                idle_expires_at=datetime.now(UTC) + timedelta(minutes=30),
                absolute_expires_at=datetime.now(UTC) + timedelta(hours=8),
            )
        )

    exit_code = main(
        [
            "set-binding",
            "--employee-id",
            str(target_id),
            "--new-subject",
            subject_fixture["new_subject"],
            "--reason",
            "ticket CHG-123",
        ]
    )
    assert exit_code == 0

    with Session(db_engine) as session:
        employee = session.get(Employee, target_id)
        assert employee is not None
        assert employee.idp_subject == subject_fixture["new_subject"]
        change = session.scalar(
            select(IdentityBindingChange).where(IdentityBindingChange.employee_id == target_id)
        )
        assert change is not None
        assert change.previous_subject == subject_fixture["old_subject"]
        assert change.new_subject == subject_fixture["new_subject"]
        assert change.reason == "ticket CHG-123"
        assert change.actor_principal == "IDENTITY_CLI"
        assert change.changed_at is not None
        sessions = session.scalar(
            select(func.count())
            .select_from(AuthSession)
            .where(AuthSession.employee_id == target_id)
        )
        assert sessions == 0
        # One distinguishable audit event, with no metadata (the audit
        # allowlist deliberately has no subject/reason keys). Asserted through
        # the ORM: JSONB stores Python None as JSON null, not SQL NULL.
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.action == ACTION, AuditEvent.object_id == target_id)
        ).all()
        assert len(events) == 1
        assert events[0].outcome == "UPDATED"
        assert events[0].metadata_json is None
        assert events[0].actor_principal == "IDENTITY_CLI"


def test_provenance_index_is_declared_in_metadata_and_present_in_the_schema(
    db_engine: Engine,
) -> None:
    """Metadata and migration 0012 must describe the provenance index identically.

    The history read is "what happened to this employee's binding, in order". If
    the index exists only in the database, the next
    ``alembic revision --autogenerate`` sees it as undeclared and proposes
    dropping the access path the operator CLI's audit read depends on.
    """
    declared = {
        (index.name, tuple(column.name for column in index.columns))
        for index in Base.metadata.tables["identity_binding_change"].indexes
    }
    assert ("ix_identity_binding_change_employee", ("employee_id", "changed_at")) in declared
    with db_engine.connect() as connection:
        definitions = [
            row[0]
            for row in connection.execute(
                text(
                    """
                    SELECT pg_get_indexdef(x.indexrelid)
                    FROM pg_index x
                    JOIN pg_class t ON t.oid = x.indrelid
                    WHERE t.relname = 'identity_binding_change'
                    """
                )
            )
        ]
    assert any(
        "ix_identity_binding_change_employee" in definition
        and "(employee_id, changed_at)" in definition
        for definition in definitions
    ), definitions


def test_clear_is_distinguishable_from_never_mapped(
    db_engine: Engine, subject_fixture: dict
) -> None:
    """``--clear`` records the displaced subject, so history is not lost."""
    target_id = subject_fixture["target_id"]
    assert (
        main(
            [
                "set-binding",
                "--employee-id",
                str(target_id),
                "--clear",
                "--reason",
                "employee left the institute",
            ]
        )
        == 0
    )
    with Session(db_engine) as session:
        employee = session.get(Employee, target_id)
        assert employee is not None and employee.idp_subject is None
        change = session.scalar(
            select(IdentityBindingChange).where(IdentityBindingChange.employee_id == target_id)
        )
        assert change is not None
        assert change.new_subject is None
        assert change.previous_subject == subject_fixture["old_subject"]


def test_blank_reason_is_refused(db_engine: Engine, subject_fixture: dict) -> None:
    """Blank, whitespace-only and omitted reasons write nothing."""
    target_id = subject_fixture["target_id"]
    for argv in (
        ["set-binding", "--employee-id", str(target_id), "--clear", "--reason", "   "],
        ["set-binding", "--employee-id", str(target_id), "--clear", "--reason", ""],
    ):
        with pytest.raises(SystemExit) as excinfo:
            main(argv)
        assert excinfo.value.code == 2
    with Session(db_engine) as session:
        employee = session.get(Employee, target_id)
        assert employee is not None and employee.idp_subject == subject_fixture["old_subject"]
        assert (
            session.scalar(
                select(func.count())
                .select_from(IdentityBindingChange)
                .where(IdentityBindingChange.employee_id == target_id)
            )
            == 0
        )


def test_whitespace_reason_cannot_be_persisted_by_a_service_caller(
    db_engine: Engine, subject_fixture: dict
) -> None:
    """The service re-validates, so a non-CLI caller cannot store blank provenance."""
    target_id = subject_fixture["target_id"]
    with Session(db_engine) as session:
        lock_identity_administration(session)
        with pytest.raises(ValueError):
            set_identity_binding(
                session,
                actor_principal="IDENTITY_CLI",
                employee_id=target_id,
                new_subject=None,
                reason="  \t ",
            )
        session.rollback()
    with Session(db_engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(IdentityBindingChange)
                .where(IdentityBindingChange.employee_id == target_id)
            )
            == 0
        )


def test_duplicate_subject_is_refused(db_engine: Engine, subject_fixture: dict) -> None:
    """Binding to a subject another employee already holds exits 2, writes nothing."""
    target_id = subject_fixture["target_id"]
    assert (
        main(
            [
                "set-binding",
                "--employee-id",
                str(target_id),
                "--new-subject",
                subject_fixture["taken_subject"],
                "--reason",
                "should collide",
            ]
        )
        == 2
    )
    with Session(db_engine) as session:
        employee = session.get(Employee, target_id)
        assert employee is not None and employee.idp_subject == subject_fixture["old_subject"]
        assert (
            session.scalar(
                select(func.count())
                .select_from(IdentityBindingChange)
                .where(IdentityBindingChange.employee_id == target_id)
            )
            == 0
        )


def test_no_op_writes_no_provenance_and_deletes_no_session(
    db_engine: Engine, subject_fixture: dict
) -> None:
    """Re-binding the same value is a no-op."""
    target_id = subject_fixture["target_id"]
    with Session(db_engine) as session, session.begin():
        session.add(
            AuthSession(
                token_hash=uuid.uuid4().hex,
                employee_id=target_id,
                subject=subject_fixture["old_subject"],
                csrf_token=uuid.uuid4().hex,
                last_seen_at=datetime.now(UTC),
                idle_expires_at=datetime.now(UTC) + timedelta(minutes=30),
                absolute_expires_at=datetime.now(UTC) + timedelta(hours=8),
            )
        )
    assert (
        main(
            [
                "set-binding",
                "--employee-id",
                str(target_id),
                "--new-subject",
                subject_fixture["old_subject"],
                "--reason",
                "no change intended",
            ]
        )
        == 0
    )
    with Session(db_engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(IdentityBindingChange)
                .where(IdentityBindingChange.employee_id == target_id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuthSession)
                .where(AuthSession.employee_id == target_id)
            )
            == 1
        )


def test_dry_run_writes_nothing(db_engine: Engine, subject_fixture: dict) -> None:
    target_id = subject_fixture["target_id"]
    assert (
        main(
            [
                "set-binding",
                "--employee-id",
                str(target_id),
                "--clear",
                "--reason",
                "dry run",
                "--dry-run",
            ]
        )
        == 0
    )
    with Session(db_engine) as session:
        employee = session.get(Employee, target_id)
        assert employee is not None and employee.idp_subject == subject_fixture["old_subject"]
        assert (
            session.scalar(
                select(func.count())
                .select_from(IdentityBindingChange)
                .where(IdentityBindingChange.employee_id == target_id)
            )
            == 0
        )


def test_unknown_employee_and_ambiguous_selector_are_refused(
    db_engine: Engine, subject_fixture: dict
) -> None:
    assert (
        main(
            [
                "set-binding",
                "--employee-id",
                str(uuid.uuid4()),
                "--clear",
                "--reason",
                "unknown target",
            ]
        )
        == 2
    )
    assert (
        main(
            [
                "set-binding",
                "--current-subject",
                "cli-does-not-exist",
                "--clear",
                "--reason",
                "unknown subject",
            ]
        )
        == 2
    )
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "set-binding",
                "--employee-id",
                str(subject_fixture["target_id"]),
                "--current-subject",
                subject_fixture["old_subject"],
                "--clear",
                "--reason",
                "ambiguous",
            ]
        )
    assert excinfo.value.code == 2


def test_current_subject_selector_works(db_engine: Engine, subject_fixture: dict) -> None:
    assert (
        main(
            [
                "set-binding",
                "--current-subject",
                subject_fixture["old_subject"],
                "--new-subject",
                subject_fixture["new_subject"],
                "--reason",
                "select by subject",
            ]
        )
        == 0
    )
    with Session(db_engine) as session:
        employee = session.get(Employee, subject_fixture["target_id"])
        assert employee is not None and employee.idp_subject == subject_fixture["new_subject"]


def test_stale_current_subject_selector_is_refused(
    db_engine: Engine, subject_fixture: dict
) -> None:
    """A selector that no longer matches writes nothing (step 4 of the protocol)."""
    target_id = subject_fixture["target_id"]
    with Session(db_engine) as session:
        lock_identity_administration(session)
        with pytest.raises(ValueError):
            set_identity_binding(
                session,
                actor_principal="IDENTITY_CLI",
                employee_id=target_id,
                new_subject=None,
                reason="stale selector",
                matched_subject="cli-someone-else",
            )
        session.rollback()
    with Session(db_engine) as session:
        employee = session.get(Employee, target_id)
        assert employee is not None and employee.idp_subject == subject_fixture["old_subject"]


def test_fixed_lock_is_taken_before_the_target_is_resolved(
    db_engine: Engine, subject_fixture: dict
) -> None:
    """A concurrent invocation is observed blocked on the FIXED lock first.

    Nothing else is contended here, so the only lock the invocation can block on
    is the fixed identity-administration lock — and it must therefore have taken
    that lock before resolving its target.
    """
    holder = Session(db_engine)
    outcome: list[int] = []

    def _run() -> None:
        outcome.append(
            main(
                [
                    "set-binding",
                    "--employee-id",
                    str(subject_fixture["target_id"]),
                    "--clear",
                    "--reason",
                    "serialized behind the fixed lock",
                ]
            )
        )

    try:
        lock_identity_administration(holder)
        worker = threading.Thread(target=_run)
        worker.start()
        blocked = _wait_for_another_blocked_backend(db_engine)
        assert blocked, "the invocation never reached the fixed lock"
        assert outcome == [], "the invocation proceeded while the fixed lock was held"
        holder.commit()
        worker.join(timeout=30)
        assert not worker.is_alive()
        assert outcome == [0]
    finally:
        holder.close()


def test_per_employee_lock_is_taken_after_resolution(
    db_engine: Engine, subject_fixture: dict
) -> None:
    """With the employee fact lock held, the CLI blocks instead of mutating."""
    holder = Session(db_engine)
    outcome: list[int] = []

    def _run() -> None:
        outcome.append(
            main(
                [
                    "set-binding",
                    "--employee-id",
                    str(subject_fixture["target_id"]),
                    "--clear",
                    "--reason",
                    "serialized behind the employee lock",
                ]
            )
        )

    try:
        lock_employee_facts(holder, subject_fixture["target_id"])
        worker = threading.Thread(target=_run)
        worker.start()
        blocked = _wait_for_another_blocked_backend(db_engine)
        assert blocked, "the invocation never reached the employee fact lock"
        assert outcome == [], "the invocation mutated facts while the employee lock was held"
        holder.commit()
        worker.join(timeout=30)
        assert not worker.is_alive()
        assert outcome == [0]
    finally:
        holder.close()


def test_concurrent_invocations_serialize_and_chain_provenance(
    db_engine: Engine, subject_fixture: dict
) -> None:
    """Two invocations cannot lose an update: the chain lines up."""
    target_id = subject_fixture["target_id"]
    first_subject = f"cli-first-{uuid.uuid4().hex[:8]}"
    second_subject = f"cli-second-{uuid.uuid4().hex[:8]}"
    codes: list[int] = []

    def _run(new_subject: str) -> None:
        codes.append(
            main(
                [
                    "set-binding",
                    "--employee-id",
                    str(target_id),
                    "--new-subject",
                    new_subject,
                    "--reason",
                    "concurrent remap",
                ]
            )
        )

    threads = [
        threading.Thread(target=_run, args=(first_subject,)),
        threading.Thread(target=_run, args=(second_subject,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert codes == [0, 0], codes

    with Session(db_engine) as session:
        employee = session.get(Employee, target_id)
        assert employee is not None
        assert employee.idp_subject in {first_subject, second_subject}
        rows = session.scalars(
            select(IdentityBindingChange)
            .where(IdentityBindingChange.employee_id == target_id)
            .order_by(IdentityBindingChange.changed_at.asc())
        ).all()
        assert len(rows) == 2
        # The second change's previous value is the first change's new value.
        assert rows[1].previous_subject == rows[0].new_subject
        assert rows[1].new_subject == employee.idp_subject
