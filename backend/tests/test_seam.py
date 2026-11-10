"""DB-free authorization seam tests (the real-route successors of the
original placeholder proofs: test_ACCESS_T0x_* naming preserved).

These run WITHOUT a database: capability denials and auth failures happen
before any data access (ARCH-AUTH-004). Employee-touching
denials that need the employee lookup live in tests/test_authz_db.py.
"""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import (
    HR_HEADER,
    RECIPIENT_HEADER,
    SUPERVISOR_HEADER,
    TECHNICAL_ADMIN_HEADER,
)

PROTECTED_PATHS = [
    "/api/v1/me/months/2026-07",
    "/api/v1/me/months/2026-07/days/2026-07-01",
    "/api/v1/me/findings?period=2026-07",
]


def test_seam_missing_identity_is_unauthorized(client: TestClient) -> None:
    response = client.get("/api/v1/me/months/2026-07")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_REQUIRED"


def test_seam_unknown_principal_is_unauthorized(client: TestClient) -> None:
    response = client.get(
        "/api/v1/me/months/2026-07", headers={"X-Chronobeam-Dev-Principal": "ghost"}
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_UNKNOWN_PRINCIPAL"


def test_seam_production_blocks_every_protected_route_even_with_dev_header(
    production_client: TestClient,
) -> None:
    """Production configuration keeps the dev seam inert (fail-closed)."""
    for principal in ("employee", "employee-b", "hr-requester", "technical-admin"):
        for method, path in [
            ("GET", "/api/v1/me"),
            ("GET", PROTECTED_PATHS[0]),
            ("GET", PROTECTED_PATHS[2]),
            ("POST", "/api/v1/me/work-intervals"),
            ("PATCH", "/api/v1/me/work-intervals/00000000-0000-0000-0000-000000000001"),
            ("DELETE", "/api/v1/me/work-intervals/00000000-0000-0000-0000-000000000001"),
        ]:
            response = production_client.request(
                method, path, headers={"X-Chronobeam-Dev-Principal": principal}
            )
            assert response.status_code == 401, (method, path, principal)
            assert response.json()["detail"]["code"] == "AUTH_REQUIRED"


def test_seam_health_is_public_even_in_production(production_client: TestClient) -> None:
    response = production_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_seam_identity_card_works_for_any_principal(client: TestClient) -> None:
    """GET /me answers 'who am I' for every authenticated principal and
    carries ZERO employee data ."""
    for principal, name in [
        ("employee", "Employee A (synthetic)"),
        ("hr-requester", "HR requester"),
    ]:
        response = client.get("/api/v1/me", headers={"X-Chronobeam-Dev-Principal": principal})
        assert response.status_code == 200
        body = response.json()
        assert body["subject"] == principal
        assert body["display_name"] == name
        assert set(body) == {"subject", "display_name", "capabilities"}
        assert all(isinstance(capability, str) for capability in body["capabilities"])


@pytest.mark.parametrize(
    "headers",
    [HR_HEADER, SUPERVISOR_HEADER, RECIPIENT_HEADER, TECHNICAL_ADMIN_HEADER],
)
def test_seam_non_employee_denials_are_byte_identical(
    client: TestClient, headers: dict[str, str]
) -> None:
    """ACCESS-002/012/013: HR/supervisor/recipient/technical-admin get the
    exact same 403 body on every ledger route (no capability, no employee)."""
    bodies = []
    for path in PROTECTED_PATHS:
        response = client.get(path, headers=headers)
        assert response.status_code == 403
        bodies.append(response.text)
    assert all(body == bodies[0] for body in bodies)
    assert response.json() == {
        "detail": {"code": "AUTH_FORBIDDEN", "message": "Access to this resource is restricted."}
    }


def test_seam_non_employee_denied_for_writes_too(client: TestClient) -> None:
    """PATCH/DELETE/POST by non-employees share the identical denial."""
    response = client.patch(
        "/api/v1/me/work-intervals/00000000-0000-0000-0000-000000000001",
        headers=HR_HEADER,
        json={"version": 1, "start_local": "2026-07-01T08:00:00"},
    )
    assert response.status_code == 403
    assert response.text == (
        '{"detail":{"code":"AUTH_FORBIDDEN","message":"Access to this resource is restricted."}}'
    )


def test_seam_validation_envelope_no_input_echo_on_deny(client: TestClient) -> None:
    """M6 invariant, DB-free slice: even a denied request must never echo
    user-supplied values. (The employee+DB 422-envelope assertions live in
    tests/test_worktime_api.py, where the writer dependency can resolve.)"""
    response = client.post(
        "/api/v1/me/work-intervals",
        headers=HR_HEADER,
        json={"start_local": "2026-07-01T08:00:00", "end_local": "not-a-time"},
    )
    assert response.status_code == 403
    assert "not-a-time" not in response.text
