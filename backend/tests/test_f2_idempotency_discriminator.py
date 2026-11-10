"""F2 — only the idempotency-key violation is considered, and never guessed at.

The real recovery path is proven end-to-end by the deterministic lock-wait
interleave in ``test_f2_concurrency_foundation.py``. What this module pins down
is the DISCRIMINATION, which is the part that could silently rot into "map any
IntegrityError to a friendly 422":

- a violation of the semantic scope/version constraint must propagate;
- a violation of the successor constraint must propagate;
- a violation the driver does not name must propagate (``constraint_name`` is
  ``None``, and ``None != IDEMPOTENCY_CONSTRAINT``);
- a violation that DOES name the idempotency constraint, but whose winner cannot
  be re-read, must also propagate — the code must not invent a package.

The synthetic error is injected at the flush that the savepoint wraps, so the
branch under test is the real one.
"""

import uuid
from datetime import datetime

import pytest
from app.disclosure.service import IDEMPOTENCY_CONSTRAINT, create_submission_package
from app.identity.models import Employee
from app.ocd_overtime.service import OCD_IDEMPOTENCY_CONSTRAINT
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


class _Diag:
    def __init__(self, constraint_name: str | None) -> None:
        self.constraint_name = constraint_name


class _Orig(Exception):
    """Stands in for the driver's exception, carrying a constraint name."""

    def __init__(self, constraint_name: str | None) -> None:
        super().__init__("synthetic unique violation")
        self.diag = _Diag(constraint_name)


def _armed_flush(monkeypatch: pytest.MonkeyPatch, state: dict, constraint: str | None) -> None:
    """Make the NEXT ``Session.flush()`` raise a synthetic unique violation."""
    real_flush = Session.flush

    def _flush(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if state.get("armed"):
            state["armed"] = False
            raise IntegrityError("synthetic", {}, _Orig(constraint))
        return real_flush(self, *args, **kwargs)

    monkeypatch.setattr(Session, "flush", _flush)


@pytest.mark.parametrize(
    "constraint",
    [
        "uq_submission_package_scope_version",
        "uq_submission_package_supersedes_id",
        None,  # the driver did not name it
        IDEMPOTENCY_CONSTRAINT,  # named, but no winner exists to re-read
    ],
)
def test_submission_integrity_failures_other_than_a_known_winner_are_re_raised(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, constraint: str | None
) -> None:
    """Nothing is disguised as an idempotency collision."""
    employee = _employee(db_engine)
    key = uuid.uuid4()
    with Session(db_engine) as setup, setup.begin():
        args = _shift_package_args(setup, employee, key)

    state: dict = {}
    with Session(db_engine) as session:
        session.scalar(text("SELECT 1"))  # open the transaction
        _armed_flush(monkeypatch, state, constraint)
        state["armed"] = True
        with pytest.raises(IntegrityError):
            create_submission_package(
                session,
                _principal(employee),
                employee,
                **args,
            )
        session.rollback()
    # Nothing was created for the synthetic attempt.
    with Session(db_engine) as verify:
        assert (
            verify.scalar(
                text("SELECT count(*) FROM submission_package WHERE client_request_id = :key"),
                {"key": key},
            )
            == 0
        )
    _cleanup(db_engine, employee)


@pytest.mark.parametrize(
    "constraint",
    ["uq_ocd_call_supersedes_id", None, OCD_IDEMPOTENCY_CONSTRAINT],
)
def test_ocd_call_integrity_failures_are_re_raised(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, constraint: str | None
) -> None:
    from app.ocd_overtime.service import create_call

    employee = _employee(db_engine)
    key = uuid.uuid4()
    with Session(db_engine) as setup, setup.begin():
        assignment = _ocd_assignment(setup, employee)
        args = _ocd_args(assignment, key)

    state: dict = {}
    with Session(db_engine) as session:
        session.scalar(text("SELECT 1"))
        _armed_flush(monkeypatch, state, constraint)
        state["armed"] = True
        with pytest.raises(IntegrityError):
            create_call(session, employee, args)
        session.rollback()
    with Session(db_engine) as verify:
        assert (
            verify.scalar(
                text("SELECT count(*) FROM ocd_call WHERE client_request_id = :key"), {"key": key}
            )
            == 0
        )
        # No dependent write escaped the rolled-back savepoint either.
        assert (
            verify.scalar(
                text(
                    "SELECT count(*) FROM work_interval "
                    "WHERE employee_id = :id AND work_type = 'OCD_CALL'"
                ),
                {"id": employee.id},
            )
            == 0
        )
    _cleanup(db_engine, employee)


def test_the_discriminated_names_are_the_real_constraints(db_engine: Engine) -> None:
    """The names the recovery branch compares against must exist in the schema."""
    with db_engine.connect() as connection:
        names = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT conname FROM pg_constraint WHERE conname = ANY(:names) "
                    "UNION SELECT indexname FROM pg_indexes WHERE indexname = ANY(:names)"
                ),
                {"names": [IDEMPOTENCY_CONSTRAINT, OCD_IDEMPOTENCY_CONSTRAINT]},
            )
        }
    assert names == {IDEMPOTENCY_CONSTRAINT, OCD_IDEMPOTENCY_CONSTRAINT}


# ---------------------------------------------------------------------------
# Fixtures shared with the concurrency module (kept local to stay independent)
# ---------------------------------------------------------------------------


def _employee(engine: Engine) -> Employee:
    with Session(engine) as session:
        employee = session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    return employee


def _principal(employee: Employee):
    from app.auth.principal import Capability, Principal

    assert employee.idp_subject is not None
    return Principal(
        subject=employee.idp_subject,
        display_name=employee.legal_name,
        capabilities=frozenset({Capability.OWN_SUBMISSION_CREATE}),
    )


def _shift_package_args(session: Session, employee: Employee, request_id: uuid.UUID) -> dict:
    from app.disclosure.fingerprint import payload_fingerprint
    from app.disclosure.schemas import AssignmentSubmissionScope
    from app.disclosure.service import _recompute_package_preview
    from app.enums import SubmissionPurpose
    from app.integrations.shift_plan import ShiftAssignmentSeed
    from app.shift.service import seed_shift_assignment

    assignment = seed_shift_assignment(
        session,
        employee,
        ShiftAssignmentSeed(
            plan_key=f"f2-discriminator-{employee.id}",
            plan_name="F2 discriminator shift",
            planned_start_at=datetime.fromisoformat("2032-11-09T19:00:00+02:00"),
            planned_end_at=datetime.fromisoformat("2032-11-10T07:00:00+02:00"),
            shift_kind="NIGHT",
            source="TEST_SEED",
            source_ref="f2-discriminator",
        ),
    )
    scope = AssignmentSubmissionScope(assignment_id=assignment.id)
    payload, _, _ = _recompute_package_preview(
        session, employee, SubmissionPurpose.SHIFT_REPORT, scope
    )
    return {
        "purpose": SubmissionPurpose.SHIFT_REPORT,
        "scope": scope,
        "preview_fingerprint": payload_fingerprint(payload),
        "client_request_id": request_id,
        "recipients": ["recipient"],
    }


def _ocd_assignment(session: Session, employee: Employee):
    from app.ocd_overtime.service import seed_ocd_assignment

    return seed_ocd_assignment(
        session,
        employee,
        start_at=datetime.fromisoformat("2032-11-09T22:00:00+02:00"),
        end_at=datetime.fromisoformat("2032-11-10T04:00:00+02:00"),
        external_id=f"f2-discriminator-{employee.id}",
    )


def _ocd_args(assignment, key: uuid.UUID):
    from app.ocd_overtime.models import OcdCallMode
    from app.ocd_overtime.schemas import OcdCallIn

    return OcdCallIn(
        ocd_assignment_id=assignment.id,
        call_start_local=datetime(2032, 11, 9, 22, 30),
        call_end_local=datetime(2032, 11, 9, 23, 30),
        mode=OcdCallMode.ON_SITE,
        travel_out_minutes=None,
        travel_back_minutes=None,
        comment=None,
        client_request_id=key,
    )


def _cleanup(engine: Engine, employee: Employee) -> None:
    """Remove what these tests write (nothing is committed, but be explicit)."""
    from app.disclosure.models import SubmissionPackage, SubmissionRecipient
    from app.ocd_overtime.models import OcdAssignment, OcdCall
    from app.shift.models import ShiftAssignment, ShiftPlan, ShiftPlanRevision
    from app.worktime.models import WorkInterval
    from sqlalchemy import delete

    with Session(engine) as session, session.begin():
        package_ids = session.scalars(
            select(SubmissionPackage.id).where(SubmissionPackage.employee_id == employee.id)
        ).all()
        if package_ids:
            session.execute(
                delete(SubmissionRecipient).where(
                    SubmissionRecipient.submission_package_id.in_(package_ids)
                )
            )
            session.execute(delete(SubmissionPackage).where(SubmissionPackage.id.in_(package_ids)))
        session.execute(delete(OcdCall).where(OcdCall.employee_id == employee.id))
        session.execute(
            delete(WorkInterval).where(
                WorkInterval.employee_id == employee.id,
                WorkInterval.work_type == "OCD_CALL",
            )
        )
        session.execute(
            delete(OcdAssignment).where(
                OcdAssignment.external_id == f"f2-discriminator-{employee.id}"
            )
        )
        assignment_ids = session.scalars(
            select(ShiftAssignment.id).where(ShiftAssignment.employee_id == employee.id)
        ).all()
        if assignment_ids:
            from sqlalchemy import update

            session.execute(
                update(ShiftAssignment)
                .where(ShiftAssignment.id.in_(assignment_ids))
                .values(approved_revision_id=None)
            )
            session.execute(
                delete(ShiftPlanRevision).where(
                    ShiftPlanRevision.shift_assignment_id.in_(assignment_ids)
                )
            )
            session.execute(delete(ShiftAssignment).where(ShiftAssignment.id.in_(assignment_ids)))
        session.execute(
            delete(ShiftPlan).where(ShiftPlan.plan_key == f"f2-discriminator-{employee.id}")
        )
