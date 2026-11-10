"""EINV-02: no supported application API path can change an existing binding.

The behavioural attempt matrix lives in ``test_f0_identity_regressions.py``.
This module is the STRUCTURAL guard that keeps the invariant holding as the
code evolves: it inspects the generated OpenAPI document (request bodies and
paths, not response schemas — ``AdminUserDetail`` legitimately exposes the
binding read-only), the backend mutation surface, and the frontend sources.

The residual is honest and documented: an operator who can run
``python -m app.identity_admin`` can remap, because that needs host/DB access
and is not reachable by any application capability.
"""

import inspect
import re
from pathlib import Path

from app.admin.schemas import AdminUserCreate, AdminUserPatch
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"

FORBIDDEN_PATH_HINTS = ("identity-binding", "identity_binding", "/remap", "/rebind")
# Response schemas MAY expose the binding: an administrator must be able to see
# which subject is mapped.
READ_ONLY_OK = {"AdminUserDetail", "AdminUserSummary"}
# Assignments / constructor kwargs only, never reads: matches ``idp_subject=x``
# but not ``Employee.idp_subject == x`` or ``.is_not(None)``.
WRITE_PATTERN = re.compile(r"idp_subject\s*=[^=]")
FRONTEND_REMAP_PATTERN = re.compile(
    r"(updateIdentity|identity-binding|identity_binding|/remap|/rebind"
    r"|Update identity mapping|Unmap identity)",
    re.IGNORECASE,
)


def _request_body_schemas(schema: dict) -> set[str]:
    """Component schema names used as REQUEST bodies, per operation."""
    used: set[str] = set()
    for operations in schema["paths"].values():
        for operation in operations.values():
            for media in (operation.get("requestBody") or {}).get("content", {}).values():
                ref = media.get("schema", {}).get("$ref")
                if ref:
                    used.add(ref.rsplit("/", 1)[-1])
    return used


def test_application_api_cannot_change_an_existing_identity_binding(
    client: TestClient,
) -> None:
    schema = client.get("/openapi.json").json()
    # 1. no remap/rebind/identity-binding operation exists at all
    assert not [
        path for path in schema["paths"] if any(h in path.lower() for h in FORBIDDEN_PATH_HINTS)
    ]
    # 2. among schemas actually used as REQUEST bodies, only CREATE accepts it
    bodies = _request_body_schemas(schema)
    accepting = {
        name
        for name in bodies
        if "idp_subject" in schema["components"]["schemas"][name].get("properties", {})
    }
    assert accepting == {"AdminUserCreate"}, accepting
    # 3. the ordinary update schema cannot express a binding change, and an
    #    attempt to send it is rejected rather than ignored
    assert "idp_subject" not in AdminUserPatch.model_fields
    assert AdminUserPatch.model_config.get("extra") == "forbid"
    assert "idp_subject" in AdminUserCreate.model_fields
    # 4. read-only exposure is explicitly allowed, not banned
    assert set(schema["components"]["schemas"]) >= READ_ONLY_OK


def test_only_approved_code_paths_write_employee_idp_subject() -> None:
    """Narrow backend mutation-surface tripwire (reads are not flagged).

    Honest limit: this is a formatting-sensitive tripwire, not a proof — a
    multi-line ``Employee(...)`` constructor could evade the regex. Its job is
    to force a reviewer to look when a new writer appears; the OpenAPI checks
    above are the load-bearing ones.
    """
    allowed = {
        "app/identity/models.py",  # the column definition
        "app/admin/schemas.py",  # the CREATE payload that carries the field
        "app/admin/service.py",  # provision_user (approved provisioning)
        "app/identity/service.py",  # set_identity_binding (operator-only)
        "app/identity_admin.py",  # the operator CLI
        "app/admin_bootstrap.py",  # operator CLI (first administrator)
        "app/dev_seed.py",
        "app/demo_seed.py",  # dev/test seeds
    }
    offenders = sorted(
        str(path.relative_to(BACKEND))
        for path in (BACKEND / "app").rglob("*.py")
        if WRITE_PATTERN.search(path.read_text(encoding="utf-8"))
        and str(path.relative_to(BACKEND)) not in allowed
    )
    assert offenders == []
    # the operator-only writer must not be reachable from any route module
    for route_file in (BACKEND / "app").rglob("routes.py"):
        assert "set_identity_binding" not in route_file.read_text(encoding="utf-8")
    # the generic admin update must not mention the binding at all
    from app.admin import service as admin_service

    assert "idp_subject" not in inspect.getsource(admin_service.update_user)


def test_frontend_has_no_identity_remap_control() -> None:
    sources = REPO_ROOT / "frontend" / "src"
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in sorted(sources.rglob("*.ts*"))
        if FRONTEND_REMAP_PATTERN.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
