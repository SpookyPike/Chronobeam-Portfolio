"""Operational auth cleanup CLI (``python -m app.db_admin cleanup-auth``).

Runs against the database named by CHRONOBEAM_DATABASE_URL like every db_admin
command; the test points it at the isolated test database and cleans up its
own rows afterwards.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.auth.session_models import AuthSession
from app.auth.session_service import token_hash
from app.config import get_settings
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

_BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_cleanup_auth_cli_prunes_only_expired() -> None:
    settings = get_settings()
    url = settings.test_database_url
    factory = sessionmaker(bind=create_engine(url), expire_on_commit=False)
    with factory() as session:
        # This test owns auth_session for its duration (the `finally` below
        # already wipes it), so make that ownership explicit up front: the CLI
        # reports a GLOBAL prune count, and a leftover expired row from an
        # earlier run would otherwise turn "pruned 1" into a false failure.
        session.execute(text("delete from auth_session"))
        session.commit()
        employee = session.execute(
            text("select id from employee where idp_subject = 'employee' order by id limit 1")
        ).scalar_one()
        now = datetime.now(UTC)
        expired = AuthSession(
            token_hash=token_hash("expired-opaque"),
            employee_id=employee,
            subject="employee",
            csrf_token="test-csrf-value",  # noqa: S106 - test row, not a real secret
            last_seen_at=now,
            idle_expires_at=now - timedelta(minutes=1),
            absolute_expires_at=now + timedelta(hours=1),
        )
        live = AuthSession(
            token_hash=token_hash("live-opaque"),
            employee_id=employee,
            subject="employee",
            csrf_token="test-csrf-value",  # noqa: S106 - test row, not a real secret
            last_seen_at=now,
            idle_expires_at=now + timedelta(minutes=10),
            absolute_expires_at=now + timedelta(hours=1),
        )
        session.add_all([expired, live])
        session.commit()
        try:
            env = dict(os.environ)
            env["CHRONOBEAM_ENV"] = "test"
            env["CHRONOBEAM_DATABASE_URL"] = url
            result = subprocess.run(
                [sys.executable, "-m", "app.db_admin", "cleanup-auth"],
                cwd=_BACKEND_DIR,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert result.returncode == 0, result.stderr
            assert "pruned 1" in result.stdout, result.stdout
            hashes = session.scalars(text("select token_hash from auth_session")).all()
            assert token_hash("expired-opaque") not in hashes
            assert token_hash("live-opaque") in hashes
        finally:
            session.execute(text("delete from auth_session"))
            session.commit()
