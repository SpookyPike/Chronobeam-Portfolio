"""Disclosure API schemas (security core).

Only employee-mediated visibility surfaces: access requests (metadata only —
no ledger content ever), scoped grants, and the frozen granted view. The
submission-package schemas live in the vertical slice on top of this core.
"""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.disclosure.models import AccessRequestStatus
from app.enums import SubmissionPurpose

SubmissionPackagePurpose = Literal[
    "SHIFT_REPORT", "OCD_COMPENSATION", "OVERTIME_PAID", "OVERTIME_TIME_COMP"
]


class AccessRequestCreate(BaseModel):
    """HR asks for an employee-opened period/view (ACCESS-007).

    ``month`` is one calendar month (yyyy-mm, D7). The request itself is
    metadata only; no work-time content is accepted or returned.
    """

    employee_external_id: str = Field(min_length=1, max_length=128)
    purpose: SubmissionPurpose
    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    view_type: str = Field(default="work-time-record", min_length=1, max_length=64)
    message: str | None = Field(default=None, max_length=500)


class AccessRequestOut(BaseModel):
    id: uuid.UUID
    requester_principal: str
    purpose: SubmissionPurpose
    period_start: date
    period_end: date
    view_type: str
    message: str | None
    status: AccessRequestStatus
    created_at: datetime
    closed_at: datetime | None
    # Metadata pulse for the REQUESTER (who is always the grantee): the id of
    # the grant that currently serves this request, if any. Never ledger data;
    # the grant itself is only readable through the grantee-scoped route.
    grant_id: uuid.UUID | None = None


class DisclosureGrantOut(BaseModel):
    id: uuid.UUID
    access_request_id: uuid.UUID | None
    employee_identifier: str
    grantee_principal: str
    purpose: SubmissionPurpose
    period_start: date
    period_end: date
    view_type: str
    granted_at: datetime
    expires_at: datetime | None
    supersedes_id: uuid.UUID | None
    active: bool


class DisclosureViewOut(BaseModel):
    """The grantee-visible frozen view: scope envelope + snapshot projection."""

    grant_id: uuid.UUID
    purpose: SubmissionPurpose
    period_start: date
    period_end: date
    view_type: str
    granted_at: datetime
    snapshot: dict[str, object]


# ---------------------------------------------------------------------------
# Purpose-specific submission packages (ACCESS-004/005/006)
# ---------------------------------------------------------------------------


class AssignmentSubmissionScope(BaseModel):
    """Scope for a single shift or OCD assignment."""

    assignment_id: uuid.UUID


class MonthSubmissionScope(BaseModel):
    """Inclusive Berlin-local calendar month scope for overtime."""

    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")


SubmissionScope = AssignmentSubmissionScope | MonthSubmissionScope


class SubmissionCreate(BaseModel):
    """Employee-confirmed immutable package command.

    The route purpose determines which of the two scope shapes is valid; this
    keeps the HTTP body compact while preserving a typed union at the boundary.
    """

    scope: SubmissionScope
    preview_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    client_request_id: uuid.UUID
    recipients: list[str] = Field(default_factory=list)


class SubmissionRecipientOut(BaseModel):
    recipient_principal: str
    recipient_type: str
    read_at: datetime | None


class RecipientOptionOut(BaseModel):
    """A selectable submission recipient from the server-controlled registry.

    Only the subject and whether some principal can currently read a package
    addressed to it. No display names, no employee ids, no capability lists.
    """

    subject: str
    readable: bool


class SubmissionPackageOut(BaseModel):
    """Employee's full package view, including its frozen recipient payload."""

    id: uuid.UUID
    purpose: SubmissionPurpose
    period_start: date
    period_end: date
    version: int
    status: str
    supersedes_id: uuid.UUID | None
    created_at: datetime
    employee_confirmed_at: datetime | None
    recipients: list[SubmissionRecipientOut]
    payload: dict[str, object]


class RecipientSubmissionPackageListOut(BaseModel):
    """Recipient queue metadata only; deliberately no payload."""

    id: uuid.UUID
    purpose: SubmissionPurpose
    period_start: date
    period_end: date
    version: int
    status: str
    employee_identifier: str
    created_at: datetime
    read_at: datetime | None
    supersedes_id: uuid.UUID | None


class RecipientSubmissionPackageOut(RecipientSubmissionPackageListOut):
    """Assigned recipient's exact frozen package projection, nothing else."""

    payload: dict[str, object]
