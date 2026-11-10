"""Alembic migration environment.

The database URL always comes from application settings
(`CHRONOBEAM_DATABASE_URL` or the non-secret dev default), never from a
hand-edited ini copy — one source of truth for the local connection.
"""

from logging.config import fileConfig

from alembic import context
from app.audit import models as audit_models  # noqa: F401  (register tables)
from app.balance import models as balance_models  # noqa: F401  (register tables)
from app.calendar import models as calendar_models  # noqa: F401  (register tables)
from app.config import get_settings
from app.db import Base
from app.identity import models as identity_models  # noqa: F401  (register tables)
from app.policy import models as policy_models  # noqa: F401  (register tables)
from app.worktime import models as worktime_models  # noqa: F401  (register tables)
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)

# PHASE-2: all persisted domain models register on Base.metadata via the
# imports above, including calendar, policy, and balance tables.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a live connection)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against the configured database."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
