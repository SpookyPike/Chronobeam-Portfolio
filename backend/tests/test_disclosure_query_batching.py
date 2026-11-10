"""ACCESS-007/009: bounded disclosure list queries retain exact SQL scopes."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

import pytest
from app.auth.principal import Capability, Principal
from app.disclosure import routes, service
from app.disclosure.models import (
    AccessRequest,
    AccessRequestStatus,
    DisclosureGrant,
    SubmissionPackage,
    SubmissionPackageStatus,
    SubmissionRecipient,
)
from app.enums import SubmissionPurpose
from app.identity.models import Employee
from sqlalchemy import event, inspect, select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Session


@contextmanager
def _selects(session: Session) -> Iterator[list[str]]:
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    bind = session.get_bind()
    event.listen(bind, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(bind, "before_cursor_execute", capture)


def _employee(session: Session, subject: str = "employee") -> Employee:
    employee = session.scalar(select(Employee).where(Employee.idp_subject == subject))
    assert employee is not None
    return employee


def _principal(subject: str) -> Principal:
    return Principal(
        subject=subject,
        display_name=subject,
        capabilities=frozenset(
            {Capability.ACCESS_REQUEST_CREATE, Capability.SUBMISSION_READ_ASSIGNED}
        ),
    )


def _request(
    session: Session,
    employee: Employee,
    requester: str = "hr",
    month: int = 7,
    purpose: SubmissionPurpose = SubmissionPurpose.RANDOM_CHECK,
) -> AccessRequest:
    request = AccessRequest(
        id=uuid.uuid4(),
        employee_id=employee.id,
        requester_principal=requester,
        purpose=purpose,
        period_start=date(2026, month, 1),
        period_end=date(2026, month, 28),
        view_type="work-time-record",
        status=AccessRequestStatus.GRANTED,
    )
    session.add(request)
    session.flush()
    return request


def _grant(session: Session, request: AccessRequest, **changes) -> DisclosureGrant:
    values = {
        "id": uuid.uuid4(),
        "access_request_id": request.id,
        "employee_id": request.employee_id,
        "grantee_principal": request.requester_principal,
        "purpose": request.purpose,
        "period_start": request.period_start,
        "period_end": request.period_end,
        "view_type": request.view_type,
        "granted_at": datetime.now(UTC) - timedelta(days=1),
    }
    values.update(changes)
    grant = DisclosureGrant(**values)
    session.add(grant)
    session.flush()
    return grant


def _package(
    session: Session,
    employee: Employee,
    version: int,
    recipients: tuple[str, ...] = ("recipient", "hr"),
    status: SubmissionPackageStatus = SubmissionPackageStatus.SUBMITTED,
) -> SubmissionPackage:
    package = SubmissionPackage(
        id=uuid.uuid4(),
        employee_id=employee.id,
        purpose=SubmissionPurpose.SHIFT_REPORT,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        version=version,
        payload_json={"frozen": version, "data": "x" * 4096},
        status=status,
    )
    session.add(package)
    session.flush()
    session.add_all(
        SubmissionRecipient(
            submission_package_id=package.id,
            recipient_principal=subject,
            recipient_type="RECIPIENT",
        )
        for subject in recipients
    )
    session.flush()
    return package


def test_employee_package_list_batches_recipients_and_keeps_payload(db_session: Session) -> None:
    employee = _employee(db_session)
    other = _employee(db_session, "employee-b")
    packages = [_package(db_session, employee, i + 1) for i in range(36)]
    foreign = _package(db_session, other, 1)
    with _selects(db_session) as statements:
        result = routes.me_list_submissions(employee, db_session)
    assert len(statements) == 2
    assert {row.id for row in result} == {package.id for package in packages}
    assert foreign.id not in {row.id for row in result}
    assert all(
        [r.recipient_principal for r in row.recipients] == ["hr", "recipient"] for row in result
    )
    assert {row.payload["frozen"] for row in result} == set(range(1, 37))
    # Even a mixed caller-supplied id set cannot materialize another owner's recipients.
    recipients = service.submission_recipients_for_packages(
        db_session, employee, [*packages, foreign]
    )
    assert foreign.id not in recipients


@pytest.mark.parametrize("employee_view", [False, True])
def test_request_lists_batch_latest_grants_without_resurrecting_inactive(
    db_session: Session, employee_view: bool
) -> None:
    employee = _employee(db_session)
    other = _employee(db_session, "employee-b")
    requests = [
        _request(
            db_session,
            employee,
            month=7 + i % 3,
            purpose=(
                SubmissionPurpose.WORK_TIME_RECORD if i % 2 else SubmissionPurpose.RANDOM_CHECK
            ),
        )
        for i in range(36)
    ]
    grants = [_grant(db_session, request) for request in requests[:32]]
    now = datetime.now(UTC)
    # A newer inactive row must never expose its older still-unrevoked predecessor.
    _grant(db_session, requests[0], granted_at=now, revoked_at=now)
    _grant(db_session, requests[1], granted_at=now, expires_at=now - timedelta(seconds=1))
    # Successor detection is global to lineage, including another request/view.
    successor_request = _request(db_session, employee, requester="another-hr", month=8)
    _grant(db_session, successor_request, supersedes_id=grants[2].id)
    other_request = _request(db_session, other, requester="another-hr", month=9)
    _grant(db_session, other_request)
    db_session.flush()
    with _selects(db_session) as statements:
        result = (
            routes.me_list_access_requests(employee, db_session)
            if employee_view
            else routes.hr_list_access_requests(_principal("hr"), db_session)
        )
    assert len(statements) == 2
    by_id = {row.id: row for row in result}
    assert other_request.id not in by_id
    assert (successor_request.id in by_id) is employee_view
    assert all(by_id[request.id].grant_id is None for request in requests[:3])
    assert all(
        by_id[request.id].grant_id == grant.id
        for request, grant in zip(requests[3:32], grants[3:], strict=True)
    )
    assert all(by_id[request.id].grant_id is None for request in requests[32:])
    assert all("snapshot" not in row.model_dump() for row in result)


def test_recipient_queue_projects_metadata_before_materialization(db_session: Session) -> None:
    employee = _employee(db_session)
    other = _employee(db_session, "employee-b")
    packages = [_package(db_session, employee, i + 1) for i in range(36)]
    other_assigned = _package(db_session, other, 1, recipients=("recipient",))
    _package(db_session, employee, 37, recipients=("another-recipient",))
    _package(db_session, employee, 38, status=SubmissionPackageStatus.WITHDRAWN)
    _package(db_session, employee, 39, status=SubmissionPackageStatus.SUPERSEDED)
    expected = {package.id for package in packages} | {other_assigned.id}
    # A fresh identity map proves payload omission rather than relying on fixture-loaded values.
    db_session.expunge_all()
    with _selects(db_session) as statements:
        result = routes.recipient_list_submissions(_principal("recipient"), db_session)
    assert len(statements) == 1
    assert {row.id for row in result} == expected
    projection = statements[0].split("FROM", 1)[0]
    assert "payload_json" not in projection
    assert "employee.legal_name" not in projection
    assert "employee.email" not in projection
    assert "employee.idp_subject" not in projection
    assert all("payload" not in row.model_dump() for row in result)
    rows = service.list_recipient_submission_packages(db_session, _principal("recipient"))
    package, _recipient, loaded_employee = rows[0]
    assert "payload_json" in inspect(package).unloaded
    assert "idp_subject" in inspect(loaded_employee).unloaded
    with pytest.raises(InvalidRequestError, match="raiseload"):
        _ = package.payload_json
    with pytest.raises(InvalidRequestError, match="raiseload"):
        _ = loaded_employee.idp_subject


def test_empty_lists_do_not_issue_unneeded_batch_queries(db_session: Session) -> None:
    with _selects(db_session) as statements:
        assert service.active_grants_for_requests(db_session, []) == {}
        assert (
            service.submission_recipients_for_packages(db_session, _employee(db_session), []) == {}
        )
    assert len(statements) == 1  # fixture employee lookup only
