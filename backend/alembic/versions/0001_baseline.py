"""PHASE-0 baseline: establish the Alembic revision head.

No tables are created in PHASE-0: the production schema arrives from PHASE-1
onward. This empty revision proves the migration toolchain upgrades and
downgrades a clean PostgreSQL database end to end.
"""

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
