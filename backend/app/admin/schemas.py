"""Administration API schemas.

Deliberately minimal: identity/access fields
only — no contract/schedule editing, no payroll/master-data fields, and
absolutely no work-time payloads. "No /admin/all-timesheets" is enforced by
the absence of any ledger-shaped field here.
"""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: The sortable columns of the users table, named as the table's headers read.
#: ``role`` orders by the FIRST active role name — the same value the column
#: renders — so sorting by a column cannot disagree with what the reader sees in
#: it. ``Literal`` rather than an enum: this is an API query vocabulary, not
#: domain state (the repo's convention for such vocabularies, cf. ``FindingStatus``).
UserSortKey = Literal["name", "employee_id", "email", "status", "role", "provisioned"]

SortDirection = Literal["asc", "desc"]


def _stripped(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _non_blank(value: str | None) -> str | None:
    """Strip identity fields and reject whitespace-only values."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be empty or whitespace-only")
    return stripped


class AdminUserCreate(BaseModel):
    legal_name: str = Field(min_length=1, max_length=500)
    external_employee_id: str | None = Field(default=None, max_length=500)
    idp_subject: str | None = Field(default=None, max_length=500)
    email: str | None = Field(default=None, max_length=500)

    _subject = field_validator("idp_subject", "external_employee_id")(_non_blank)

    def normalized(self) -> "AdminUserCreate":
        return AdminUserCreate(
            legal_name=self.legal_name.strip(),
            external_employee_id=_stripped(self.external_employee_id),
            idp_subject=_stripped(self.idp_subject),
            email=_stripped(self.email),
        )


class AdminUserPatch(BaseModel):
    """Ordinary profile/activation edit.

    The identity binding (``idp_subject``) is deliberately NOT editable here.
    An administrator who could rebind an existing row could make a subject it
    controls resolve to another employee and inherit that employee's ledger, so
    the binding is settable through the application API only when the row is
    created (``AdminUserCreate``); changing an existing binding is an operator
    CLI operation (``python -m app.identity_admin``) that needs host/DB access.
    ``extra="forbid"`` makes an attempt to send the field fail loudly instead
    of being silently ignored.
    """

    model_config = ConfigDict(extra="forbid")

    legal_name: str | None = Field(default=None, min_length=1, max_length=500)
    external_employee_id: str | None = Field(default=None, max_length=500)
    email: str | None = Field(default=None, max_length=500)
    active: bool | None = None

    _subject = field_validator("external_employee_id")(_non_blank)

    def normalized(self) -> "AdminUserPatch":
        # ``None`` is meaningful for nullable identity fields: it explicitly
        # unmaps/clears them. Preserve Pydantic's supplied-field set so the
        # service can distinguish that from an omitted PATCH field.
        updates: dict[str, object] = {}
        if "legal_name" in self.model_fields_set:
            updates["legal_name"] = self.legal_name.strip() if self.legal_name is not None else None
        if "external_employee_id" in self.model_fields_set:
            updates["external_employee_id"] = _stripped(self.external_employee_id)
        if "email" in self.model_fields_set:
            updates["email"] = _stripped(self.email)
        return self.model_copy(update=updates)


class AdminUserSummary(BaseModel):
    id: uuid.UUID
    external_employee_id: str | None
    legal_name: str
    email: str | None
    active: bool
    #: ACTIVE role names only (``revoked_at IS NULL``), sorted. The list shows the
    #: access a user holds NOW; the full grant/revoke history stays in
    #: ``AdminUserDetail``, which is where an audit question belongs. Carried on the
    #: summary because a "who may do what" table is unreadable without it.
    roles: list[str]
    created_at: datetime
    #: Readiness: presence facts about PROVISIONING, never work records. The
    #: binding is reported as a boolean rather than the subject itself — the list
    #: needs to say "not mapped", not to republish the credential identifier. The
    #: two master-data flags are the same ones ``AdminUserDetail`` already showed;
    #: they live here now so the list can answer "is this account usable?" without
    #: opening every row.
    has_idp_subject: bool
    has_effective_contract: bool
    has_effective_schedule: bool


class AdminRoleAssignmentOut(BaseModel):
    id: uuid.UUID
    role: str
    granted_by_principal: str
    granted_at: datetime
    revoked_at: datetime | None


class AdminUserDetail(AdminUserSummary):
    """Inspection view: identity + provisioning state + access state.

    Role history comes from the immutable assignment rows (never rewritten);
    ``effective_capabilities`` reflects the CURRENT derivation (baseline +
    active roles, only for active+mapped employees). Readiness flags expose
    whether HR master-data exists without exposing any work records — they are
    declared on the summary, which this view extends.
    """

    idp_subject: str | None
    role_assignments: list[AdminRoleAssignmentOut]
    effective_capabilities: list[str]


class AdminCalendarEditionOut(BaseModel):
    """One published calendar edition, read-only (institutional data only).

    ``valid_to`` is INCLUSIVE and ``None`` means open-ended. Overlaps between
    editions are legitimate and deliberate — a bounded *corrective overlay* takes
    precedence inside its governed range — so this is a record of what was
    published, not a contradiction for this surface to resolve.
    """

    version: str
    valid_from: date
    valid_to: date | None
    source: str


class AdminCalendarOut(BaseModel):
    """Calendar coverage: which editions exist and where coverage runs out.

    ``editions`` ascends by ``valid_from`` (a timeline). ``first_uncovered_month``
    is DERIVED from those editions, never stored, and is the first month at or
    after the current Berlin month whose month view answers 409
    ``CALENDAR_BLOCKED``; ``None`` means there is no gap from here on.
    """

    editions: list[AdminCalendarEditionOut]
    first_uncovered_month: str | None


class AdminSystemOut(BaseModel):
    """Operational status card. No secrets, no raw configuration, no data."""

    application: dict[str, str]
    database: dict[str, object]
    auth: dict[str, object]
    integrations: dict[str, str]
    #: The calendar gets a structured field rather than a sixth integration code
    #: because the operator question is "what is covered, and when does it run
    #: out?" — a boolean cannot answer it.
    calendar: AdminCalendarOut
