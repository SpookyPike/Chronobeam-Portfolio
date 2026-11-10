"""PHASE-8 cutover opening-state provenance.

Adds the nullable JSONB provenance column to ``balance_ledger_entry`` and the
authoritative duplicate-seed guard: at most ONE row per employee with
``entry_type = 'CUTOVER_SEED'`` (partial unique index). No new table, no
workbook-import subsystem (INV-13, CUTOVER-T01..03).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_cutover_seed"
down_revision: str | None = "0007_disclosure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "balance_ledger_entry",
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index(
        "uq_balance_ledger_cutover_seed_employee",
        "balance_ledger_entry",
        ["employee_id"],
        unique=True,
        postgresql_where=sa.text("entry_type = 'CUTOVER_SEED'"),
    )


def downgrade() -> None:
    op.drop_index("uq_balance_ledger_cutover_seed_employee", table_name="balance_ledger_entry")
    op.drop_column("balance_ledger_entry", "metadata")
