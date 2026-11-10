"""Minimal administration routes.

Surface: users + access (ACCESS_ADMIN) and the
read-only system status card (SYSTEM_STATUS_READ). Every endpoint is a
capability gate (roles never bypass server-side checks); none of them can
read, list or project employee working-time data.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.admin import service
from app.admin.schemas import (
    AdminRoleAssignmentOut,
    AdminSystemOut,
    AdminUserCreate,
    AdminUserDetail,
    AdminUserPatch,
    AdminUserSummary,
    SortDirection,
    UserSortKey,
)
from app.auth.deps import get_current_principal
from app.auth.principal import Capability, Principal
from app.auth.roles import Role
from app.db import get_db
from app.errors import forbidden

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def _require(principal: Principal, capability: Capability) -> None:
    """Capability gate (byte-identical 403 body, same as disclosure)."""
    if not principal.has_capability(capability):
        raise forbidden()


@router.get("/users", response_model=list[AdminUserSummary])
def admin_list_users(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[Session, Depends(get_db)],
    search: str | None = None,
    active: bool | None = None,
    sort: UserSortKey = "provisioned",
    direction: SortDirection = "asc",
    limit: int = Query(default=25, ge=0, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[AdminUserSummary]:
    """List/search application users (bounded pagination).

    ``sort``/``direction`` order in SQL rather than in the client, so the order is
    correct for the WHOLE matching set and not merely for the page that happened to
    be fetched. ``limit=0`` means "every matching user" — an explicit act by an
    administrator, not a default.
    """
    _require(principal, Capability.ACCESS_ADMIN)
    return service.list_users(
        session,
        search=search,
        active=active,
        sort=sort,
        direction=direction,
        limit=limit,
        offset=offset,
    )


@router.post("/users", response_model=AdminUserDetail, status_code=201)
def admin_provision_user(
    payload: AdminUserCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[Session, Depends(get_db)],
) -> AdminUserDetail:
    """Provision an application user (inactive by default; activation explicit)."""
    _require(principal, Capability.ACCESS_ADMIN)
    return service.provision_user(session, principal, payload)


@router.get("/users/{employee_id}", response_model=AdminUserDetail)
def admin_get_user(
    employee_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[Session, Depends(get_db)],
) -> AdminUserDetail:
    """Inspect one user: identity, provisioning state, roles, effective caps."""
    _require(principal, Capability.ACCESS_ADMIN)
    return service.user_detail(session, service.employee_or_404(session, employee_id))


@router.patch("/users/{employee_id}", response_model=AdminUserDetail)
def admin_update_user(
    employee_id: uuid.UUID,
    payload: AdminUserPatch,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[Session, Depends(get_db)],
) -> AdminUserDetail:
    """Activate/deactivate or correct ordinary identity fields.

    The identity binding is not editable here: it is set only when a row is
    created, and changing an existing binding is an operator CLI operation.
    """
    _require(principal, Capability.ACCESS_ADMIN)
    detail = service.update_user(session, principal, employee_id, payload)
    # Durability before the response. A deactivation deletes the target's opaque
    # sessions (``admin/service.py::update_user``), and ``get_db`` commits in its
    # teardown — which runs AFTER the response is sent. Without this the admin's
    # 200 could arrive while the target's existing cookie still authenticates.
    # A revocation must be visible before the operator is told it happened.
    session.commit()
    return detail


@router.put("/users/{employee_id}/roles/{role}", response_model=AdminRoleAssignmentOut)
def admin_assign_role(
    employee_id: uuid.UUID,
    role: Role,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[Session, Depends(get_db)],
) -> AdminRoleAssignmentOut:
    """Assign a code-defined role (idempotent; audited)."""
    _require(principal, Capability.ACCESS_ADMIN)
    return service.assign_role(session, principal, employee_id, role)


@router.delete("/users/{employee_id}/roles/{role}", status_code=204)
def admin_revoke_role(
    employee_id: uuid.UUID,
    role: Role,
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[Session, Depends(get_db)],
) -> None:
    """Revoke a role (soft revoke; history preserved; audited)."""
    _require(principal, Capability.ACCESS_ADMIN)
    service.revoke_role(session, principal, employee_id, role)
    # Durability before the response: the revoked ``revoked_at`` is what removes
    # the capability, and the target must not be able to use the old one after
    # the operator has been told the revocation succeeded (see the PATCH route).
    session.commit()


@router.get("/system", response_model=AdminSystemOut)
def admin_system_status(
    principal: Annotated[Principal, Depends(get_current_principal)],
    session: Annotated[Session, Depends(get_db)],
) -> AdminSystemOut:
    """Read-only operational status card (no secrets, no employee data)."""
    _require(principal, Capability.SYSTEM_STATUS_READ)
    return service.system_status(session)
