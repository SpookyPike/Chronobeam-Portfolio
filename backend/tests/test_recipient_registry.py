"""F5 §5.12/§7.9 — the server-controlled recipient registry.

The selectable submission recipients are a SERVER fact, not a client constant:
the configured registry (``CHRONOBEAM_RECIPIENT_SUBJECTS``; dev/test = the
fixture principals) intersected with the ONE live capability derivation
(``app.auth.deps.subject_capabilities``, i.e. ``Role.SUBMISSION_RECIPIENT`` over
non-revoked ``RoleAssignment`` rows). These tests prove:

- ``GET /me/submission-recipients`` is a bare list of ``{subject, readable}``
  served from the registry, with no names, ids or capability lists;
- a configured subject without the recipient capability is reported
  ``readable: false`` (a role rename cannot silently pass);
- audit A-L4: a submission addressed to a configured-but-unreadable subject is
  refused 422 instead of creating a package nobody can read, and the two failure
  modes stay DISTINCT (``unknown_recipients`` = configuration error vs
  ``unreadable_recipients`` = role-assignment error);
- revoking the role makes a configured subject unreadable (live, not cached);
- the production registry source keeps its fail-closed rule;
- ``GET /admin/system`` reports ``recipient_registry`` and ``calendar`` state,
  including ``INCONSISTENT``;
- the frontend has no hard-coded recipient list.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from app.admin import service as admin_service
from app.auth.models import RoleAssignment
from app.auth.roles import Role
from app.config import get_settings
from app.disclosure import service as disclosure
from app.disclosure.models import SubmissionPackage
from app.identity.models import Employee
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.conftest import (
    DEVELOPER_PRINCIPAL_HEADER as ME,
)
from tests.conftest import TECHNICAL_ADMIN_HEADER

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"

RECIPIENTS_PATH = "/api/v1/me/submission-recipients"
SUBMISSIONS_PATH = "/api/v1/me/submissions/SHIFT_REPORT"
ADMIN_SYSTEM_PATH = "/api/v1/admin/system"

#: The dev/test registry fallback: the fixture principals that carry
#: ``SUBMISSION_READ_ASSIGNED`` (``app/auth/dev_principals.py``).
FIXTURE_RECIPIENTS = {"recipient", "demo-recipient"}
#: A configured subject with NO principal behind it (no employee row, no role).
UNREADABLE_SUBJECT = "registry-probe-unreadable"


@contextmanager
def _registry(
    monkeypatch: pytest.MonkeyPatch, subjects: str, *, env: str = "test"
) -> Iterator[None]:
    """Set the server-controlled registry (and env) for one block, then restore."""
    monkeypatch.setenv("CHRONOBEAM_ENV", env)
    monkeypatch.setenv("CHRONOBEAM_RECIPIENT_SUBJECTS", subjects)
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def _probe_employee(session: Session, subject: str) -> Employee:
    """An ACTIVE employee mapped to ``subject`` (no role assignment)."""
    employee = Employee(legal_name="Registry Probe (synthetic)", idp_subject=subject, active=True)
    session.add(employee)
    session.flush()
    return employee


def _grant(session: Session, employee: Employee, role: Role) -> RoleAssignment:
    assignment = RoleAssignment(
        employee_id=employee.id, role=role.value, granted_by_principal="test-setup"
    )
    session.add(assignment)
    session.flush()
    return assignment


def _options(client: TestClient, headers: dict[str, str] | None = None) -> list[dict[str, object]]:
    response = client.get(RECIPIENTS_PATH, headers=headers or ME)
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, list), body
    return body


def _submit(client: TestClient, recipients: list[str]) -> Any:
    """Submit with a scope/fingerprint that never reaches the preview check.

    The recipient gate runs before the payload is recomputed, so an addressed
    subject is refused without any shift, month or ledger fixture existing.
    """
    return client.post(
        SUBMISSIONS_PATH,
        headers=ME,
        json={
            "scope": {"month": "2035-01"},
            "preview_fingerprint": "0" * 64,
            "client_request_id": str(uuid.uuid4()),
            "recipients": recipients,
        },
    )


def _package_count(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(SubmissionPackage)) or 0)


# ---------------------------------------------------------------------------
# The list is a server fact
# ---------------------------------------------------------------------------


def test_the_recipient_list_is_served_from_the_registry_not_a_frontend_constant(
    db_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare ``[{subject, readable}]`` list whose contents follow the SERVER."""
    with _registry(monkeypatch, "recipient"):
        assert _options(db_client) == [{"subject": "recipient", "readable": True}]

    # Unconfigured in dev/test the fixture principals fill the registry.
    with _registry(monkeypatch, ""):
        options = _options(db_client)
    assert {row["subject"] for row in options} == FIXTURE_RECIPIENTS
    assert all(row["readable"] is True for row in options)
    # No names, no ids, no capability lists — only the two documented keys.
    assert all(set(row) == {"subject", "readable"} for row in options)


def test_a_registry_subject_without_the_recipient_capability_is_marked_unreadable(
    db_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Membership in the registry is NOT readability; the capability decides.

    The capability is resolved through the shared derivation, so a role rename
    (``Role.SUBMISSION_RECIPIENT`` -> ``SUBMISSION_READ_ASSIGNED``) cannot
    silently pass: the assignment below is what flips the answer.
    """
    employee = _probe_employee(db_session, UNREADABLE_SUBJECT)

    with _registry(monkeypatch, UNREADABLE_SUBJECT):
        assert _options(db_client) == [{"subject": UNREADABLE_SUBJECT, "readable": False}]
        assert disclosure.readable_recipient_subjects(db_session) == (
            disclosure.RecipientOption(subject=UNREADABLE_SUBJECT, readable=False),
        )

        _grant(db_session, employee, Role.SUBMISSION_RECIPIENT)
        assert _options(db_client) == [{"subject": UNREADABLE_SUBJECT, "readable": True}]


def test_revoking_the_recipient_role_makes_a_configured_subject_unreadable(
    db_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pairing is live, not cached: a soft revoke is enough."""
    employee = _probe_employee(db_session, "registry-probe-revoked")
    assignment = _grant(db_session, employee, Role.SUBMISSION_RECIPIENT)

    with _registry(monkeypatch, "registry-probe-revoked"):
        assert _options(db_client) == [{"subject": "registry-probe-revoked", "readable": True}]

        assignment.revoked_at = datetime.now(UTC)
        db_session.flush()

        assert _options(db_client) == [{"subject": "registry-probe-revoked", "readable": False}]


def test_dev_and_production_registry_sources_remain_distinct(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production is server-config only, and empty means fail closed (D6)."""
    with _registry(monkeypatch, "", env="production"):
        assert disclosure.valid_recipient_subjects() == ()
        # Fail closed: no configured recipient means NO selectable recipient.
        assert disclosure.readable_recipient_subjects(db_session) == ()

    with _registry(monkeypatch, "payroll@example.invalid, woco@example.invalid", env="production"):
        assert disclosure.valid_recipient_subjects() == (
            "payroll@example.invalid",
            "woco@example.invalid",
        )

    with _registry(monkeypatch, "", env="test"):
        assert set(disclosure.valid_recipient_subjects()) == FIXTURE_RECIPIENTS


# ---------------------------------------------------------------------------
# Audit A-L4: an unreadable recipient is refused, distinctly
# ---------------------------------------------------------------------------


def test_submission_to_an_unreadable_registry_subject_is_refused(
    db_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A package nobody can read must not be created (audit A-L4)."""
    _probe_employee(db_session, UNREADABLE_SUBJECT)
    before = _package_count(db_session)

    with _registry(monkeypatch, UNREADABLE_SUBJECT):
        response = _submit(db_client, [UNREADABLE_SUBJECT])

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "VALIDATION_FAILED"
    assert detail["details"]["unreadable_recipients"] == [UNREADABLE_SUBJECT]
    assert detail["details"]["unknown_recipients"] == []
    assert _package_count(db_session) == before


def test_the_refusal_distinguishes_unknown_from_unreadable(
    db_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two failure modes, two ``details`` keys — their fixes differ."""
    _probe_employee(db_session, UNREADABLE_SUBJECT)
    unknown = "not-configured-anywhere"

    with _registry(monkeypatch, UNREADABLE_SUBJECT):
        both = _submit(db_client, [unknown, UNREADABLE_SUBJECT])
        only_unknown = _submit(db_client, [unknown])

    assert both.status_code == 422, both.text
    details = both.json()["detail"]["details"]
    assert details["unknown_recipients"] == [unknown]
    assert details["unreadable_recipients"] == [UNREADABLE_SUBJECT]

    assert only_unknown.status_code == 422, only_unknown.text
    only = only_unknown.json()["detail"]["details"]
    assert only["unknown_recipients"] == [unknown]
    assert only["unreadable_recipients"] == []


# ---------------------------------------------------------------------------
# Operator detection
# ---------------------------------------------------------------------------


def test_system_status_reports_registry_and_calendar_state(
    db_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /admin/system`` exposes the codes, and only the codes.

    ``TECHNICAL_ADMIN_HEADER`` selects the fixture principal that carries NO
    fixture capability, so the setup grants the real ``APPLICATION_ADMIN`` role
    (SYSTEM_STATUS_READ) to a mapped, active employee — the same derivation
    production uses.
    """
    admin = _probe_employee(db_session, "technical-admin")
    _grant(db_session, admin, Role.APPLICATION_ADMIN)

    body = db_client.get(ADMIN_SYSTEM_PATH, headers=TECHNICAL_ADMIN_HEADER)
    assert body.status_code == 200, body.text
    integrations = body.json()["integrations"]
    assert integrations["calendar"] == "LOADED"
    assert integrations["recipient_registry"] in {
        "CONFIGURED",
        "EMPTY_FAIL_CLOSED",
        "INCONSISTENT",
    }
    assert integrations["recipient_registry"] == "CONFIGURED"

    # INCONSISTENT is exactly "the registry names a subject with no
    # currently-capable principal" — the state that used to be silent.
    with _registry(monkeypatch, UNREADABLE_SUBJECT):
        inconsistent = db_client.get(ADMIN_SYSTEM_PATH, headers=TECHNICAL_ADMIN_HEADER)
    assert inconsistent.status_code == 200, inconsistent.text
    assert inconsistent.json()["integrations"]["recipient_registry"] == "INCONSISTENT"
    assert inconsistent.json()["integrations"]["calendar"] == "LOADED"

    # EMPTY_FAIL_CLOSED is the production-empty state (the endpoint itself is
    # unreachable under production auth in this suite, so the code is read).
    with _registry(monkeypatch, "", env="production"):
        assert admin_service._recipient_registry_state(db_session) == "EMPTY_FAIL_CLOSED"


def test_frontend_has_no_hard_coded_recipient_list() -> None:
    """Structural guard in the ``test_identity_surface.py`` style.

    Scoped to PRODUCTION sources: ``frontend/src/pages/disclosure.test.tsx``
    legitimately names a demo subject, because that is the mocked SERVER
    response — the property under test is that no shipped file carries the
    recipient list as a client constant.
    """
    sources = sorted(FRONTEND_SRC.rglob("*.ts*"))
    assert sources, "frontend sources not found"
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in sources
        if not path.name.endswith((".test.ts", ".test.tsx"))
        and "demo-recipient" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
    # The guard is scoped, not vacuous: the frontend test's mock does name one.
    assert "demo-recipient" in (FRONTEND_SRC / "pages" / "disclosure.test.tsx").read_text(
        encoding="utf-8"
    )
    # ... and the picker is fed by the registry query, not by a constant.
    page = (FRONTEND_SRC / "pages" / "submissions.tsx").read_text(encoding="utf-8")
    assert "submission-recipients" in page
    assert "/api/v1/me/submission-recipients" in (FRONTEND_SRC / "lib" / "api.ts").read_text(
        encoding="utf-8"
    )
