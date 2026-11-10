"""PHASE-10 role assignments and nullable identity mapping.

Two changes:

1. ``employee.idp_subject`` becomes nullable: a provisioned application user
   may exist before an institutional identity is mapped (no JIT provisioning;
   active + unmapped = no capabilities, canonical 403). Uniqueness keeps
   holding for rows that DO carry a subject (NULLs never collide in
   PostgreSQL unique indexes).

2. ``role_assignment``: the one persistent authorization-state table.
   Roles are code-defined convenience bundles (app.auth.roles); this table
   only stores who holds which role, so role history survives revocation
   (soft revoke via ``revoked_at``, never DELETE) and stays audit-reproducible
   (INV-11). At most one ACTIVE assignment per (employee, role) is enforced
   by a partial unique index. ``revoked_at >= granted_at`` is a CHECK; the
   role value itself is constrained to the known role codes so a typo cannot
   silently create a useless assignment.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_role_assignment"
down_revision: str | None = "0009_travel_facts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KNOWN_ROLES = (
    "APPLICATION_ADMIN",
    "HR_REQUESTER",
    "SUBMISSION_RECIPIENT",
)

_ROLE_CHECK = "role IN ({})".format(", ".join(repr(role) for role in KNOWN_ROLES))


def upgrade() -> None:
    op.alter_column("employee", "idp_subject", existing_type=sa.Text(), nullable=True)
    op.create_table(
        "role_assignment",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("granted_by_principal", sa.Text(), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_ROLE_CHECK, name="ck_role_assignment_known_role"),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= granted_at",
            name="ck_role_assignment_revoke_order",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
            name="fk_role_assignment_employee",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_role_assignment_employee",
        "role_assignment",
        ["employee_id"],
        unique=False,
    )
    # One active assignment per (employee, role); revoked rows keep history.
    op.create_index(
        "uq_role_assignment_active_employee_role",
        "role_assignment",
        ["employee_id", "role"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    # Check the documented precondition *before* removing any authorization
    # state. A refused downgrade must leave the schema and role history
    # untouched rather than stopping halfway through a destructive sequence.
    bind = op.get_bind()
    null_subjects = bind.execute(
        sa.text("SELECT count(*) FROM employee WHERE idp_subject IS NULL")
    ).scalar_one()
    if null_subjects:
        raise RuntimeError(
            "cannot downgrade: employee.idp_subject is NULL for "
            f"{null_subjects} row(s); map an identity for those employees first"
        )
    op.drop_index("uq_role_assignment_active_employee_role", table_name="role_assignment")
    op.drop_index("ix_role_assignment_employee", table_name="role_assignment")
    op.drop_table("role_assignment")
    # Documented downgrade precondition: the NOT NULL
    # constraint can only be restored while no unmapped employee exists. The
    # migration gate runs against the test DB (no NULL subjects); downgrading
    # a deployment that provisioned unmapped users is refused instead of
    # fabricating identities.
    op.alter_column("employee", "idp_subject", existing_type=sa.Text(), nullable=False)
