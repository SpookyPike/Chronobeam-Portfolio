"""Identity-binding provenance and browser-bound login attempts.

Two independent F1 objects:

1. ``identity_binding_change`` — provenance for an operator identity-binding
   change (previous/new subject, reason, actor). The application API can set a
   binding only when creating an employee row, so without this table a remap
   would leave no record of the displaced subject.

2. ``login_attempt.browser_binding_hash`` — binds an OIDC authorization-code
   attempt to the browser that started it, closing login CSRF.

**The single destructive statement in this migration** is the ``DELETE FROM
login_attempt`` below. Those rows are single-use pre-authentication state with a
10-minute lifetime and no audit value, and the new column is NOT NULL, so
existing rows cannot be carried over; deleting them is equivalent to a restart
(the affected users simply start a new login). Nothing else is deleted.

Downgrade drops the column and the table. Dropping the table DESTROYS the only
record of past identity remaps — it is not reversible without a backup.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_identity_binding"
down_revision: str | None = "0011_auth_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "identity_binding_change",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("previous_subject", sa.Text(), nullable=True),
        sa.Column("new_subject", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_principal", sa.Text(), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(reason) <> ''", name="ck_identity_binding_change_reason_not_blank"
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employee.id"], name="fk_identity_binding_change_employee"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_identity_binding_change_employee",
        "identity_binding_change",
        ["employee_id", "changed_at"],
        unique=False,
    )

    # NOT NULL cannot be added to existing rows; see the module docstring.
    op.execute("DELETE FROM login_attempt")
    op.add_column("login_attempt", sa.Column("browser_binding_hash", sa.Text(), nullable=False))


def downgrade() -> None:
    op.drop_column("login_attempt", "browser_binding_hash")
    op.drop_index("ix_identity_binding_change_employee", table_name="identity_binding_change")
    op.drop_table("identity_binding_change")
