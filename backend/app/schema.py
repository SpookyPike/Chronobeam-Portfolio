"""Alembic schema-head lookup shared by administration and readiness."""

from pathlib import Path

_ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def expected_schema_revision() -> str | None:
    """Resolve the migration head independent of the server working directory."""
    try:
        from alembic.config import Config as AlembicConfig
        from alembic.script import ScriptDirectory

        config = AlembicConfig(str(_ALEMBIC_INI))
        location = config.get_main_option("script_location") or "alembic"
        config.set_main_option("script_location", str(_ALEMBIC_INI.parent / location))
        return ScriptDirectory.from_config(config).get_current_head()
    except Exception:
        return None
