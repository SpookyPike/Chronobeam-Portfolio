"""Small admin CLI for the just recipes.

``python -m app.db_admin migration-check`` runs the canonical migration gate
(upgrade head -> downgrade base -> upgrade head) against a **dedicated scratch
database** — never the developer's seeded dev database, and never
``chronobeam_test``.

Both exclusions are load-bearing. A downgrade to base would wipe local dev seed
data, and ``chronobeam_test`` is not clean either: it accumulates the calendar
editions and facts the suite publishes, and migration ``0016`` legitimately
REFUSES to downgrade while any institutional calendar edition survives (it
cannot un-publish one, and re-inserting the retired placeholder beside it would
strand the next upgrade at ``0015``). Running the round trip against residual
test state would make the gate fail for a reason that has nothing to do with the
migrations. A fresh scratch database is the only state on which the round trip
is actually defined.
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

from app.config import get_settings
from app.db import ensure_database, get_session_factory, quote_identifier

_BACKEND_DIR = Path(__file__).resolve().parents[1]


def _drop_database(url: str) -> None:
    """Drop the scratch database, tolerating a lingering connection."""
    from sqlalchemy import create_engine as _create
    from sqlalchemy import text

    server = url.rsplit("/", 1)[0] + "/postgres"
    name = url.rsplit("/", 1)[1]
    engine = _create(server, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(
                text(  # nosemgrep: avoid-sqlalchemy-text - DDL; name validated
                    f"DROP DATABASE IF EXISTS {quote_identifier(name)} WITH (FORCE)"
                )
            )
    finally:
        engine.dispose()


def migration_check() -> None:
    settings = get_settings()
    base = settings.test_database_url.rsplit("/", 1)[0]
    scratch_url = f"{base}/chronobeam_migcheck_{uuid.uuid4().hex[:8]}"
    ensure_database(scratch_url)
    env = {**os.environ, "CHRONOBEAM_DATABASE_URL": scratch_url}
    try:
        for args in (["upgrade", "head"], ["downgrade", "base"], ["upgrade", "head"]):
            subprocess.run(["uv", "run", "alembic", *args], cwd=_BACKEND_DIR, env=env, check=True)
    finally:
        _drop_database(scratch_url)
    print("migration apply/downgrade/re-apply OK (scratch database)")


def cleanup_auth() -> None:
    """Prune expired opaque sessions and OIDC attempts; no worker is needed."""
    from app.auth.session_service import prune_expired

    with get_session_factory()() as session:
        deleted = prune_expired(session)
        session.commit()
    print(f"pruned {deleted} expired authentication records")


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "migration-check":
        migration_check()
    elif command == "cleanup-auth":
        cleanup_auth()
    else:
        sys.exit(f"unknown command: {command!r} (expected 'migration-check' or 'cleanup-auth')")


if __name__ == "__main__":
    main()
