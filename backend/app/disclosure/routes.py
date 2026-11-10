"""Employee-controlled disclosure routes — security core.

HR asks without seeing data, the employee opens exactly the requested
one-month view, the grantee reads the frozen snapshot projection. No generic
employee-ledger route exists for non-employees (ACCESS-002);
supervisor/technical-admin stay capability-less (ACCESS-012/013). The
submission-package surface is added on top of this core.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import (
    get_current_employee,
    get_current_grant_owner,
    get_current_principal,
    get_current_submission_owner,
)
from app.auth.principal import Capability, Principal
from app.db import get_db
from app.disclosure import service as disclosure
from app.disclosure.models import DisclosureGrant
from app.disclosure.schemas import (
    AccessRequestCreate,
    AccessRequestOut,
    DisclosureGrantOut,
    DisclosureViewOut,
    RecipientOptionOut,
    RecipientSubmissionPackageListOut,
    RecipientSubmissionPackageOut,
    SubmissionCreate,
    SubmissionPackageOut,
    SubmissionPackagePurpose,
    SubmissionRecipientOut,
)
from app.enums import SubmissionPurpose
from app.errors import forbidden
from app.identity.models import Employee

router = APIRouter(prefix="/api/v1", tags=["disclosure"])


def _require(principal: Principal, capability: Capability) -> None:
    """Capability gate for non-employee surfaces (byte-identical 403 body)."""
    if not principal.has_capability(capability):
        raise forbidden()


def _request_out(
    request: disclosure.AccessRequest, grant: DisclosureGrant | None
) -> AccessRequestOut:
    return AccessRequestOut(
        id=request.id,
        requester_principal=request.requester_principal,
        purpose=request.purpose,
        period_start=request.period_start,
        period_end=request.period_end,
        view_type=request.view_type,
        message=request.message,
        status=request.status,
        created_at=request.created_at,
        closed_at=request.closed_at,
        grant_id=grant.id if grant is not None else None,
    )


@router.post("/hr/access-requests", response_model=AccessRequestOut)
def hr_create_access_request(
    payload: AccessRequestCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[Session, Depends(get_db)],
) -> AccessRequestOut:
    """Record an HR random-check request; no ledger content is returned."""
    _require(principal, Capability.ACCESS_REQUEST_CREATE)
    request = disclosure.create_access_request(
        session,
        principal,
        employee_external_id=payload.employee_external_id,
        purpose=payload.purpose,
        month=payload.month,
        view_type=payload.view_type,
        message=payload.message,
    )
    return _request_out(request, disclosure.active_grant_for_request(session, request.id))


@router.get("/hr/access-requests", response_model=list[AccessRequestOut])
def hr_list_access_requests(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[Session, Depends(get_db)],
) -> list[AccessRequestOut]:
    """The requester's own requests only; status metadata, no ledger content."""
    _require(principal, Capability.ACCESS_REQUEST_CREATE)
    requests = disclosure.list_hr_access_requests(session, principal)
    grants = disclosure.active_grants_for_requests(session, requests)
    return [_request_out(request, grants.get(request.id)) for request in requests]


@router.get("/me/access-requests", response_model=list[AccessRequestOut])
def me_list_access_requests(
    employee: Annotated[Employee, Depends(get_current_employee)],
    session: Annotated[Session, Depends(get_db)],
) -> list[AccessRequestOut]:
    """Requests awaiting (or answered by) this employee."""
    requests = disclosure.list_employee_access_requests(session, employee)
    grants = disclosure.active_grants_for_requests(session, requests)
    return [_request_out(request, grants.get(request.id)) for request in requests]


@router.post("/me/access-requests/{request_id}/grant", response_model=DisclosureGrantOut)
def me_grant_access_request(
    request_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    employee: Annotated[Employee, Depends(get_current_grant_owner)],
    session: Annotated[Session, Depends(get_db)],
) -> DisclosureGrantOut:
    """Employee opens the requested one-month view (ACCESS-008/009)."""
    grant = disclosure.grant_access_request(session, principal, employee, request_id)
    return _grant_out(session, grant)


@router.post("/me/access-requests/{request_id}/decline", response_model=AccessRequestOut)
def me_decline_access_request(
    request_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    employee: Annotated[Employee, Depends(get_current_grant_owner)],
    session: Annotated[Session, Depends(get_db)],
) -> AccessRequestOut:
    """Record the employee's answer without exposing any data."""
    request = disclosure.decline_access_request(session, principal, employee, request_id)
    return _request_out(request, disclosure.active_grant_for_request(session, request.id))


@router.get("/disclosures/{grant_id}", response_model=DisclosureViewOut)
def read_disclosure(
    grant_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[Session, Depends(get_db)],
) -> DisclosureViewOut:
    """Grantee reads ONLY the frozen projection of their active grant."""
    _require(principal, Capability.DISCLOSURE_READ_GRANTED)
    grant, projection = disclosure.read_grant_projection(session, principal, grant_id)
    return DisclosureViewOut(
        grant_id=grant.id,
        purpose=grant.purpose,
        period_start=grant.period_start,
        period_end=grant.period_end,
        view_type=grant.view_type,
        granted_at=grant.granted_at,
        snapshot=projection,
    )


def _grant_out(session: Session, grant: DisclosureGrant) -> DisclosureGrantOut:
    """Map a grant row to its response shape, deriving ``active``."""
    superseded = session.scalar(
        select(DisclosureGrant.id).where(DisclosureGrant.supersedes_id == grant.id)
    )
    employee = session.get(Employee, grant.employee_id)
    return DisclosureGrantOut(
        id=grant.id,
        access_request_id=grant.access_request_id,
        employee_identifier=(
            (employee.external_employee_id if employee is not None else None)
            or str(grant.employee_id)
        ),
        grantee_principal=grant.grantee_principal,
        purpose=grant.purpose,
        period_start=grant.period_start,
        period_end=grant.period_end,
        view_type=grant.view_type,
        granted_at=grant.granted_at,
        expires_at=grant.expires_at,
        supersedes_id=grant.supersedes_id,
        active=(
            grant.revoked_at is None
            and superseded is None
            and (grant.expires_at is None or grant.expires_at > datetime.now(UTC))
        ),
    )


# ---------------------------------------------------------------------------
# Immutable purpose-specific submission packages (ACCESS-004/005/006)
# ---------------------------------------------------------------------------


def _package_out(
    session: Session,
    package: disclosure.SubmissionPackage,
    recipients: list[disclosure.SubmissionRecipient] | None = None,
) -> SubmissionPackageOut:
    if recipients is None:
        recipients = disclosure.submission_recipients(session, package.id)
    return SubmissionPackageOut(
        id=package.id,
        purpose=package.purpose,
        period_start=package.period_start,
        period_end=package.period_end,
        version=package.version,
        status=package.status.value,
        supersedes_id=package.supersedes_id,
        created_at=package.created_at,
        employee_confirmed_at=package.employee_confirmed_at,
        recipients=[
            SubmissionRecipientOut(
                recipient_principal=recipient.recipient_principal,
                recipient_type=recipient.recipient_type,
                read_at=recipient.read_at,
            )
            for recipient in recipients
        ],
        payload=dict(package.payload_json),
    )


@router.get("/me/submission-recipients", response_model=list[RecipientOptionOut])
def me_submission_recipients(
    employee: Annotated[Employee, Depends(get_current_submission_owner)],
    session: Annotated[Session, Depends(get_db)],
) -> list[RecipientOptionOut]:
    """The server-controlled recipient registry, for the submit form.

    The frontend used to hard-code a demo recipient, so the selectable list was a
    client constant rather than a server fact. ``readable`` is false when the
    configured subject holds no currently-effective recipient capability — shown
    disabled at the point of use, so an inconsistent configuration is detectable
    rather than producing a package nobody can read.
    """
    return [
        RecipientOptionOut(subject=option.subject, readable=option.readable)
        for option in disclosure.readable_recipient_subjects(session)
    ]


@router.get("/me/submissions", response_model=list[SubmissionPackageOut])
def me_list_submissions(
    employee: Annotated[Employee, Depends(get_current_submission_owner)],
    session: Annotated[Session, Depends(get_db)],
) -> list[SubmissionPackageOut]:
    """Employee-only full view of their own frozen submission packages."""
    packages = disclosure.list_submission_packages(session, employee)
    recipients = disclosure.submission_recipients_for_packages(session, employee, packages)
    return [_package_out(session, package, recipients.get(package.id, [])) for package in packages]


@router.post("/me/submissions/{purpose}", response_model=SubmissionPackageOut)
def me_create_submission(
    purpose: SubmissionPackagePurpose,
    payload: SubmissionCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    employee: Annotated[Employee, Depends(get_current_submission_owner)],
    session: Annotated[Session, Depends(get_db)],
) -> SubmissionPackageOut:
    """Confirm a preview-sealed report without broadening ledger visibility."""
    package = disclosure.create_submission_package(
        session,
        principal,
        employee,
        purpose=SubmissionPurpose(purpose),
        scope=payload.scope,
        preview_fingerprint=payload.preview_fingerprint,
        client_request_id=payload.client_request_id,
        recipients=payload.recipients,
    )
    return _package_out(session, package)


@router.post("/me/submissions/{package_id}/withdraw", response_model=SubmissionPackageOut)
def me_withdraw_submission(
    package_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    employee: Annotated[Employee, Depends(get_current_submission_owner)],
    session: Annotated[Session, Depends(get_db)],
) -> SubmissionPackageOut:
    package = disclosure.withdraw_submission_package(session, principal, employee, package_id)
    out = _package_out(session, package)
    # Durability before the response: the recipient's access to the package is
    # gated on ``status == SUBMITTED``, so a withdrawal that is still only
    # flushed lets the recipient read the package after the employee has been
    # told it was withdrawn. ``get_db`` commits in its teardown, which runs
    # AFTER the response is sent. Materialize the response first, then commit.
    session.commit()
    return out


def _recipient_list_out(
    package: disclosure.SubmissionPackage,
    recipient: disclosure.SubmissionRecipient,
    employee: Employee,
) -> RecipientSubmissionPackageListOut:
    return RecipientSubmissionPackageListOut(
        id=package.id,
        purpose=package.purpose,
        period_start=package.period_start,
        period_end=package.period_end,
        version=package.version,
        status=package.status.value,
        employee_identifier=employee.external_employee_id or str(employee.id),
        created_at=package.created_at,
        read_at=recipient.read_at,
        supersedes_id=package.supersedes_id,
    )


@router.get("/recipient/submissions", response_model=list[RecipientSubmissionPackageListOut])
def recipient_list_submissions(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[Session, Depends(get_db)],
) -> list[RecipientSubmissionPackageListOut]:
    """Assigned-recipient queue; metadata only and no ledger materialization."""
    _require(principal, Capability.SUBMISSION_READ_ASSIGNED)
    return [
        _recipient_list_out(package, recipient, employee)
        for package, recipient, employee in disclosure.list_recipient_submission_packages(
            session, principal
        )
    ]


@router.get("/recipient/submissions/{package_id}", response_model=RecipientSubmissionPackageOut)
def recipient_read_submission(
    package_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[Session, Depends(get_db)],
) -> RecipientSubmissionPackageOut:
    """Read only the frozen package assigned to this principal, never a ledger."""
    _require(principal, Capability.SUBMISSION_READ_ASSIGNED)
    package, recipient = disclosure.read_recipient_submission_package(
        session, principal, package_id
    )
    employee = session.get(Employee, package.employee_id)
    # FK-backed employee ownership means this is only a defensive fallback.
    if employee is None:  # pragma: no cover
        raise disclosure._nondisclosing_not_found()
    return RecipientSubmissionPackageOut(
        **_recipient_list_out(package, recipient, employee).model_dump(),
        payload=dict(package.payload_json),
    )
