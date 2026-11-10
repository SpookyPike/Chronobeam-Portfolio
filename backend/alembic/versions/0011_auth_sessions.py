"""PHASE-12 opaque browser sessions and one-time OIDC login attempts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_auth_sessions"
down_revision: str | None = "0010_role_assignment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_session",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("csrf_token", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"], name="fk_auth_session_employee"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_auth_session_token_hash"),
    )
    op.create_index(
        "ix_auth_session_expiry",
        "auth_session",
        ["idle_expires_at", "absolute_expires_at"],
        unique=False,
    )
    op.create_table(
        "login_attempt",
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("pkce_verifier", sa.Text(), nullable=False),
        sa.Column("nonce", sa.Text(), nullable=False),
        sa.Column("return_to", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("state"),
    )
    op.create_index("ix_login_attempt_expires_at", "login_attempt", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_login_attempt_expires_at", table_name="login_attempt")
    op.drop_table("login_attempt")
    op.drop_index("ix_auth_session_expiry", table_name="auth_session")
    op.drop_table("auth_session")
