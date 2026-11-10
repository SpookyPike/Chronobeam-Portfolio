"""F0 regression reproductions — identity and authentication boundary.

Baseline freeze for the two confirmed identity defects from the targeted
closure audit (2026-09-17, §1.1/§1.2):

- an ``APPLICATION_ADMIN`` can take over any employee's private ledger by
  remapping that employee's identity binding through the ordinary user-update
  endpoint and then authenticating as the new subject;
- an OIDC login attempt is not bound to the browser that started it, so a
  callback URL delivered to a different browser establishes a session there.

Both now assert the INVARIANT across the SUPPORTED application API, on the
production authentication path (a real browser-session login). ``EINV-02`` is
an attempt matrix rather than a single request, because "the old PATCH stopped
accepting the field" is not the invariant: every path that could plausibly move
the victim's ledger is attempted, and the load-bearing assertions are identity
separation and non-disclosure rather than a status code.
"""

import uuid
from datetime import UTC, date, datetime
from urllib.parse import parse_qs, urlparse

from app.auth.models import RoleAssignment
from app.auth.roles import Role
from app.auth.session_service import COOKIE_NAME
from app.identity.models import Employee, EmploymentContract, WorkScheduleVersion
from app.worktime.models import WorkInterval, WorkPeriod
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.conftest import production_login
from tests.oidc_fake import FakeIdP

ADMIN_USERS = "/api/v1/admin/users"
MONTHS = "/api/v1/me/months"
VICTIM_MONTH = "2026-07"
VICTIM_COMMENT = "private-victim-note"


def _employee_with_ledger(session: Session, subject: str) -> Employee:
    """A private employee with one interval in July 2026 (as the audit used)."""
    employee = Employee(
        legal_name="Victim Employee",
        external_employee_id=f"f0-{subject}",
        idp_subject=subject,
        active=True,
    )
    session.add(employee)
    session.flush()
    session.add(
        EmploymentContract(
            employee_id=employee.id,
            valid_from=date(2026, 1, 1),
            fte=1.0,
            weekly_required_minutes=2400,
            version=1,
            pay_group_bucket="LE_9B",
            source="ADMIN_SEED",
        )
    )
    session.add(
        WorkScheduleVersion(
            employee_id=employee.id,
            valid_from=date(2026, 1, 1),
            version=1,
            source="ADMIN_SEED",
            weekly_required_minutes=2400,
            mon_required_minutes=480,
            tue_required_minutes=480,
            wed_required_minutes=480,
            thu_required_minutes=480,
            fri_required_minutes=480,
            sat_required_minutes=0,
            sun_required_minutes=0,
        )
    )
    period = WorkPeriod(employee_id=employee.id, period_month=date(2026, 7, 1))
    session.add(period)
    session.flush()
    session.add(
        WorkInterval(
            employee_id=employee.id,
            work_period_id=period.id,
            start_at=datetime(2026, 7, 1, 6, 0, tzinfo=UTC),
            end_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
            work_type="ON_SITE",
            comment=VICTIM_COMMENT,
            version=1,
        )
    )
    session.flush()
    return employee


def _victim_facts_reachable(client, victim: Employee) -> tuple[bool, str]:
    """Whether the client's own session can reach any of the victim's facts."""
    response = client.get(f"{MONTHS}/{VICTIM_MONTH}")
    if response.status_code == 200 and response.json().get("interval_count", 0) > 0:
        return True, "month interval_count > 0"
    # The day timeline is where interval detail (comment) is materialized.
    detail = client.get(f"{MONTHS}/{VICTIM_MONTH}/days/{VICTIM_MONTH}-01")
    if detail.status_code == 200:
        blob = detail.text
        if VICTIM_COMMENT in blob or str(victim.id) in blob:
            return True, "victim fact echoed in the day timeline"
        if '"intervals":[]' not in blob and '"intervals": []' not in blob:
            return True, "non-empty day timeline"
    return False, f"no facts (month HTTP {response.status_code}, day HTTP {detail.status_code})"


def test_EINV_02_no_application_api_path_transfers_an_employee_ledger(
    production_db_client_factory, db_session: Session, fake_idp: FakeIdP
) -> None:
    """The canonical property: an APPLICATION_ADMIN alone cannot become another
    employee through any supported application-API path.

    Every attempt is made; after each one the victim's binding and the victim's
    own access are re-checked, and the attacker-authenticated principal must
    never resolve to the victim's employee row or reach a victim fact.
    """
    tag = uuid.uuid4().hex[:8]
    victim_subject = f"f0-victim-{tag}"
    attacker_subject = f"f0-attacker-{tag}"
    victim = _employee_with_ledger(db_session, victim_subject)
    victim_id = victim.id
    victim_external_id = victim.external_employee_id

    admin = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert admin is not None
    db_session.add(
        RoleAssignment(
            employee_id=admin.id,
            role=Role.APPLICATION_ADMIN.value,
            granted_by_principal="test-setup",
        )
    )
    db_session.flush()

    admin_client = production_db_client_factory()
    admin_headers = production_login(admin_client, db_session, fake_idp, subject="employee")
    attacker_client = production_db_client_factory()

    def _victim_binding() -> str | None:
        return db_session.scalar(select(Employee.idp_subject).where(Employee.id == victim_id))

    def _assert_no_takeover(label: str) -> None:
        assert _victim_binding() == victim_subject, f"{label}: victim binding changed"
        leaked, why = _victim_facts_reachable(attacker_client, victim)
        assert not leaked, f"{label}: the attacker reached the victim's facts ({why})"

    # (a) the ordinary update endpoint cannot express a binding change
    remap = admin_client.patch(
        f"{ADMIN_USERS}/{victim_id}", headers=admin_headers, json={"idp_subject": attacker_subject}
    )
    assert remap.status_code == 422
    assert remap.json()["detail"]["code"] == "VALIDATION_FAILED"
    _assert_no_takeover("PATCH with idp_subject")

    # (b) deactivate + reactivate the victim: an availability impact, not a
    #     confidentiality one — the binding must not move.
    assert (
        admin_client.patch(
            f"{ADMIN_USERS}/{victim_id}", headers=admin_headers, json={"active": False}
        ).status_code
        == 200
    )
    assert (
        admin_client.patch(
            f"{ADMIN_USERS}/{victim_id}", headers=admin_headers, json={"active": True}
        ).status_code
        == 200
    )
    _assert_no_takeover("deactivate + reactivate")

    # (c) provisioning an account for the attacker's subject is allowed, and
    #     yields a DIFFERENT employee row with its own (empty) ledger.
    created = admin_client.post(
        f"{ADMIN_USERS}",
        headers=admin_headers,
        json={
            "legal_name": "Attacker (synthetic)",
            "idp_subject": attacker_subject,
            "email": None,
        },
    )
    assert created.status_code == 201, created.text
    attacker_employee_id = created.json()["id"]
    assert attacker_employee_id != str(victim_id)
    assert created.json()["active"] is False  # activation is explicit
    assert (
        admin_client.patch(
            f"{ADMIN_USERS}/{attacker_employee_id}", headers=admin_headers, json={"active": True}
        ).status_code
        == 200
    )
    production_login(attacker_client, db_session, fake_idp, subject=attacker_subject)
    me = attacker_client.get("/api/v1/me/profile")
    assert me.status_code == 200
    assert me.json()["employee"]["id"] == attacker_employee_id
    assert me.json()["employee"]["id"] != str(victim_id)
    _assert_no_takeover("provision attacker account + activate")

    # (d) provisioning cannot point a NEW row at the victim's subject or id
    duplicate_subject = admin_client.post(
        f"{ADMIN_USERS}",
        headers=admin_headers,
        json={"legal_name": "Duplicate (synthetic)", "idp_subject": victim_subject},
    )
    assert duplicate_subject.status_code == 409
    assert duplicate_subject.json()["detail"]["code"] == "CONFLICT_DUPLICATE_IDENTITY"
    duplicate_external = admin_client.post(
        f"{ADMIN_USERS}",
        headers=admin_headers,
        json={"legal_name": "Duplicate (synthetic)", "external_employee_id": victim_external_id},
    )
    assert duplicate_external.status_code == 409
    _assert_no_takeover("provision with the victim's subject/external id")

    # (e) every role is assignable to the victim, and no role yields another
    #     employee's ledger to its holder.
    for role in Role:
        assigned = admin_client.put(
            f"{ADMIN_USERS}/{victim_id}/roles/{role.value}", headers=admin_headers
        )
        assert assigned.status_code == 200, (role, assigned.text)
        _assert_no_takeover(f"assign {role.value}")

    # (f) no remap-shaped operation exists at all
    for method, suffix in (
        ("put", "/identity-binding"),
        ("post", "/identity-binding"),
        ("patch", "/identity-binding"),
        ("put", "/remap"),
        ("post", "/rebind"),
    ):
        response = getattr(admin_client, method)(
            f"{ADMIN_USERS}/{victim_id}{suffix}", headers=admin_headers, json={"idp_subject": "x"}
        )
        assert response.status_code in (404, 405), (method, suffix, response.status_code)
    _assert_no_takeover("remap-shaped paths")

    # the victim's own access is intact throughout
    victim_client = production_db_client_factory()
    production_login(victim_client, db_session, fake_idp, subject=victim_subject)
    own = victim_client.get(f"{MONTHS}/{VICTIM_MONTH}")
    assert own.status_code == 200, own.text
    assert own.json()["interval_count"] == 1
    own_detail = victim_client.get(f"{MONTHS}/{VICTIM_MONTH}/days/{VICTIM_MONTH}-01")
    assert own_detail.status_code == 200, own_detail.text
    assert VICTIM_COMMENT in own_detail.text


def test_EINV_11_oidc_callback_is_not_bound_to_the_initiating_browser(
    production_db_client_factory, db_session: Session, fake_idp: FakeIdP
) -> None:
    """A callback must not establish a session in a browser that did not start it."""
    from app.auth.session_models import LoginAttempt

    # The attacker's subject is a real, active employee row: the takeover needs
    # a provisioned identity, so the callback must be able to reach the point at
    # which it issues a session.
    attacker_subject = f"f0-attacker-{uuid.uuid4().hex[:8]}"
    db_session.add(
        Employee(
            legal_name="Attacker Employee",
            external_employee_id=f"f0-{attacker_subject}",
            idp_subject=attacker_subject,
            active=True,
        )
    )
    db_session.flush()

    initiating = production_db_client_factory()
    login = initiating.get("/api/v1/auth/login?return_to=/", follow_redirects=False)
    assert login.status_code == 302, login.text
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]

    attempt = db_session.scalar(select(LoginAttempt).where(LoginAttempt.state == state))
    assert attempt is not None
    code = fake_idp.issue_code(
        subject=attacker_subject,
        verifier=attempt.pkce_verifier,
        nonce=attempt.nonce,
    )

    # A second browser that never saw the login attempt.
    other_browser = production_db_client_factory()
    callback = other_browser.get(
        f"/api/v1/auth/callback?code={code}&state={state}", follow_redirects=False
    )

    assert callback.status_code == 401
    assert other_browser.cookies.get(COOKIE_NAME) is None, (
        "a callback delivered to a different browser established a session "
        f"(HTTP {callback.status_code})"
    )
    # The legitimate attempt survives: the wrong browser must not burn it.
    assert db_session.scalar(select(LoginAttempt).where(LoginAttempt.state == state)) is not None
