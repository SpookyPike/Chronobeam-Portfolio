"""Policy-edition identity: seed the phantom editions and guard the table.

F4 / ``EINV-09`` (a recorded policy edition must be executable) plus the
integrity guards that make "a later policy change can never reinterpret an
already-accounted earlier month" true (F4 D7).

Three classes of change, one migration:

1. **Seed the sibling edition identities.** ``ocd_overtime/report.py`` and
   ``shift/service.py`` assert edition labels that are *not* ``policy_version``
   rows, so a frozen submission package names an edition that cannot be resolved
   or executed — ``EINV-09``'s exact failure mode. The rows are seeded here and
   the matching registry entries (``app/policy/editions.py``) ship in the SAME
   commit, which is what makes "every recorded edition is executable" hold at
   every revision (F4 D4).
2. **``UNIQUE(policy_key, effective_from)``.** Resolution is
   ``ORDER BY effective_from DESC, version DESC`` and ``version`` is Text, so
   two editions sharing an effective date would have "which one applies"
   decided by *string ordering*. The class ``EINV-04``/``EINV-18`` forbid.
3. **``CHECK(effective_to IS NULL OR effective_to >= effective_from)``** —
   the same guard the sibling effective-dated tables carry.

**Pre-flight, then DDL+DML.** A duplicate/back-dated/invalid scan runs BEFORE
anything is written and the migration ABORTS naming the offending keys; there is
deliberately no in-migration repair, because choosing a winning edition is a
policy decision a migration has no authority to make (the ``0013`` rule).

**Downgrade** drops the guards and deletes the three seeded rows. If a
``submission_package`` or ``period_calculation_snapshot`` already references one,
the foreign key fails loudly — correct: you must not delete an edition that
produced frozen results (F4 D3).

No backfill. Chronobeam is pre-production with a clean cutover and no historical
import, so already-frozen rows keep their old ``input_fingerprint`` and
``generated_at`` and their ``submission_package.policy_version_id`` stays NULL.
This is deliberate: unlike ``0014``, there is no stated invariant over rows the
old code had already written that requires upgrading them in place.
"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op

revision: str = "0015_policy_edition_identity"
down_revision: str | None = "0014_work_period_relink"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (policy_key, version label, engine_rule_set) — the rule set must exist in
# ``app/policy/editions.py::EDITIONS`` in this same commit.
SEEDED_EDITIONS: tuple[tuple[str, str, str], ...] = (
    ("ocd-compensation", "2026-ocd-v1", "phase-2-ocd-v1"),
    ("overtime", "2026-ot-v1", "phase-2-overtime-v1"),
    ("travel-credit", "2026-06-travel-v1", "phase-2-travel-v1"),
)

# Every seeded edition takes effect with the published policy-sources generation
# (docs/POLICY_SOURCES.md); none of them is back-dated.
EFFECTIVE_FROM = date(2026, 6, 1)
SOURCE_MANIFEST_HASH = "e45fb742f9f2aaa53b8e1e6ad0538ba1a631f9adb02ab717608815d5cd850b9a"

_DELETABLE = " OR ".join(
    f"(policy_key = '{key}' AND version = '{version}')" for key, version, _ in SEEDED_EDITIONS
)

# (label, SQL, offending-key columns, bind parameters) — one scan per new guard.
_PREFLIGHT_SCANS: tuple[tuple[str, str, tuple[str, ...], dict[str, object]], ...] = (
    (
        "no-back-dating",
        "SELECT policy_key, max(effective_from) AS latest FROM policy_version "
        "GROUP BY 1 HAVING max(effective_from) > :effective_from",
        ("policy_key", "latest"),
        {"effective_from": EFFECTIVE_FROM},
    ),
    (
        "uq_policy_version_effective_from",
        "SELECT policy_key, effective_from, count(*) AS n FROM policy_version "
        "GROUP BY 1, 2 HAVING count(*) > 1",
        ("policy_key", "effective_from"),
        {},
    ),
    (
        "ck_policy_version_range",
        "SELECT id, policy_key, effective_from, effective_to FROM policy_version "
        "WHERE effective_to IS NOT NULL AND effective_to < effective_from",
        ("id", "policy_key"),
        {},
    ),
    (
        "uq_policy_version_key",
        # Constant fragment built from module constants; no external input.
        f"SELECT policy_key, version FROM policy_version WHERE {_DELETABLE}",  # noqa: S608  # nosec B608
        ("policy_key", "version"),
        {},
    ),
)


def _preflight() -> None:
    """Abort before any write when the existing data contradicts a new guard."""
    connection = op.get_bind()
    for label, sql, keys, params in _PREFLIGHT_SCANS:
        statement = sa.text(sql)
        if params:
            statement = statement.bindparams(**params)
        rows = connection.execute(statement).fetchall()
        if not rows:
            continue
        detail = "; ".join(
            ", ".join(f"{key}={getattr(row, key)!r}" for key in keys)
            + (f" (x{row.n})" if "n" in row._mapping else "")
            for row in rows[:10]
        )
        raise RuntimeError(
            f"0015 pre-flight refused to proceed: {label} is already violated by "
            f"{len(rows)} row group(s): {detail}. Nothing was written. Restore from a "
            "verified backup and resolve the policy-edition conflict as a reviewed "
            "operator decision; a migration must not choose which edition applies."
        )


def upgrade() -> None:
    _preflight()

    statement = sa.text(
        """
        INSERT INTO policy_version
          (policy_key, version, effective_from, effective_to, status,
           engine_rule_set, source_manifest_hash)
        VALUES (:policy_key, :version, :effective_from, NULL, 'PROVISIONAL',
                :engine_rule_set, :source_manifest_hash)
        """
    )
    for policy_key, version, engine_rule_set in SEEDED_EDITIONS:
        op.execute(
            statement.bindparams(
                policy_key=policy_key,
                version=version,
                effective_from=EFFECTIVE_FROM,
                engine_rule_set=engine_rule_set,
                source_manifest_hash=SOURCE_MANIFEST_HASH,
            )
        )

    op.create_check_constraint(
        "ck_policy_version_range",
        "policy_version",
        "effective_to IS NULL OR effective_to >= effective_from",
    )
    op.create_index(
        "uq_policy_version_effective_from",
        "policy_version",
        ["policy_key", "effective_from"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_policy_version_effective_from", table_name="policy_version")
    op.drop_constraint("ck_policy_version_range", "policy_version", type_="check")
    # Fails loudly if a frozen artefact already references a seeded edition.
    # The predicate is assembled only from the migration's seeded edition constants.
    op.execute(sa.text(f"DELETE FROM policy_version WHERE {_DELETABLE}"))  # noqa: S608  # nosec B608
