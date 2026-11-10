"""Runtime settings.

All values are non-secret (a public key PEM is NOT a secret; it is the
verification counterpart of the IdP's private signing key). Development
defaults match `compose.yaml` and `.env.example`; production configuration is
deployment-specific and is not part of this file.

Fail-closed by design: the environment defaults to
``production``, where the dev-auth seam is inert and the app refuses to start
unless the OIDC boundary is configured — ``validate_production_settings``
requires CHRONOBEAM_OIDC_DISCOVERY_URL + _ISSUER + _CLIENT_ID + _AUDIENCE
(and _CLIENT_SECRET for a secret-based client). There is no degraded
"start anyway" mode: an unconfigured boundary is a startup failure, never a
per-request 501.

The `.env` file is anchored to the repository root: backend commands run
with CWD=backend/, but the documented `.env`
lives next to `.env.example` at the repo root.
"""

import ipaddress
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urlparse

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_prefix="CHRONOBEAM_",
        extra="ignore",
    )

    # dev | test | production. Defaults to production: unset must deny,
    # never allow (dev-auth header is honored only outside production).
    env: str = "production"

    # Local development PostgreSQL (compose.yaml). Purely local default;
    # the container binds 127.0.0.1 only.
    database_url: str = (
        "postgresql+psycopg://chronobeam:chronobeam_dev_only@127.0.0.1:55432/chronobeam"
    )

    # Separate database in the same local cluster for tests/migration-check:
    # the migration gate downgrades to base, which must never
    # run against the developer's seeded dev database.
    test_database_url: str = (
        "postgresql+psycopg://chronobeam:chronobeam_dev_only@127.0.0.1:55432/chronobeam_test"
    )

    # Production is same-origin by default; .env.example supplies the local
    # Vite origin for development.
    cors_origins: list[str] = []

    # Production uses one browser origin. An empty CORS list is therefore the
    # safe default.
    log_level: str = "INFO"
    forwarded_allow_ips: str = ""
    frontend_dist: Path = _REPO_ROOT / "frontend" / "dist"

    # The static verification key is retained solely for deterministic dev/test
    # token tests. Production uses discovery and JWKS below.
    oidc_issuer: str = ""
    oidc_audience: str = ""
    # Static public-key PEM (RS256) of the institutional IdP. A public key is
    # not a secret, but it must come from approved deployment configuration,
    # not from source control.
    oidc_public_key: str = ""
    public_base_url: str = ""
    oidc_discovery_url: str = ""
    oidc_client_id: str = ""
    oidc_client_auth_method: str = "none"  # basic | post | none
    oidc_client_secret: str = ""
    session_idle_ttl_seconds: int = 30 * 60
    session_absolute_ttl_seconds: int = 8 * 60 * 60
    jwks_cache_ttl_seconds: int = 5 * 60
    oidc_clock_skew_seconds: int = 30

    # SHIFT-010/013 stay disabled because the sample shift policy does not fix
    # their exact semantics or decision authority. These narrow gates are
    # deliberately not a generic flag platform.
    enable_shift_2026_authoritative_compensation: bool = False
    enable_shift_cancellation_3d_auto_entitlement: bool = False

    # Employee plan entry. While no external shift-plan / OCD schedule source exists
    # (OPEN-06), the employee records their OWN planned shifts
    # and OCD availability so the shipped confirm / deviation / reporting /
    # compensation workflows are usable without an external planner or admin
    # preload. Both default False, consistent with the narrow gates above and the
    # repository's gated-feature posture.
    #
    # They are TWO settings, not one, because the authority can move per domain:
    # when a shift-planning source becomes authoritative the operator disables
    # ``enable_shift_plan_entry`` while OCD stays employee-entered (or the
    # reverse). Neither may gate the other domain's routes or controls.
    enable_shift_plan_entry: bool = False
    enable_ocd_plan_entry: bool = False

    # Server-side recipient registry: exact principal
    # subjects allowed as package recipients / request grantees. In
    # development and tests the fixture principals fill the registry; in
    # production ONLY this server-controlled list applies (empty = no
    # recipients configured, submissions stay blocked — fail closed).
    recipient_subjects: str = ""

    # Cutover operator allowlist: exact OIDC subjects
    # permitted to write the one-time CUTOVER_SEED opening balance. Production
    # assignment must come from approved deployment config; empty = nobody
    # may seed (fail closed).
    cutover_operator_subjects: list[str] = []

    # Institutional rollout boundary: the earliest month (`YYYY-MM`) in which
    # Chronobeam is usable at all. It is a DEPLOYMENT decision and is separate
    # from an employee's own cutover month M — selecting M never selects or
    # alters this. UNSET means "no additional lower bound beyond the installed
    # policy/calendar coverage", which is the honest default while the
    # production rollout month is still undecided (docs/INVARIANTS.md). It is
    # never invented here, and onboarding never publishes a policy edition.
    rollout_boundary_month: str = ""

    @model_validator(mode="after")
    def _development_cors_default(self) -> "Settings":
        """Keep Vite's local origin for dev/test without weakening production."""
        if not self.is_production and "cors_origins" not in self.model_fields_set:
            self.cors_origins = ["http://localhost:5173"]
        return self

    @property
    def is_production(self) -> bool:
        # Fail-closed: ONLY explicit dev/test enable the dev-auth seam.
        # An unset or empty CHRONOBEAM_ENV, or any unknown value, is
        # treated as production (deny).
        return self.env not in {"dev", "test"}

    @property
    def oidc_configured(self) -> bool:
        if self.is_production:
            return not production_settings_errors(self)
        return bool(self.oidc_issuer and self.oidc_audience and self.oidc_public_key)


def production_settings_errors(settings: Settings) -> list[str]:
    """Return missing/unsafe production configuration keys, never values."""
    if not settings.is_production:
        return []
    errors: list[str] = []
    if settings.env != "production" or "env" not in settings.model_fields_set:
        errors.append("CHRONOBEAM_ENV")
    required = {
        "CHRONOBEAM_PUBLIC_BASE_URL": settings.public_base_url,
        "CHRONOBEAM_OIDC_DISCOVERY_URL": settings.oidc_discovery_url,
        "CHRONOBEAM_OIDC_ISSUER": settings.oidc_issuer,
        "CHRONOBEAM_OIDC_CLIENT_ID": settings.oidc_client_id,
        "CHRONOBEAM_OIDC_AUDIENCE": settings.oidc_audience,
    }
    errors.extend(key for key, value in required.items() if not value)
    method = settings.oidc_client_auth_method
    if method not in {"basic", "post", "none"}:
        # private_key_jwt is deliberately NOT implemented yet (no approved
        # assertion profile): accepting it in config would start a production
        # whose login always fails. Fail closed at startup instead.
        errors.append("CHRONOBEAM_OIDC_CLIENT_AUTH_METHOD")
    if method in {"basic", "post"} and not settings.oidc_client_secret:
        errors.append("CHRONOBEAM_OIDC_CLIENT_SECRET")
    if settings.session_idle_ttl_seconds <= 0:
        errors.append("CHRONOBEAM_SESSION_IDLE_TTL_SECONDS")
    if settings.session_absolute_ttl_seconds <= 0:
        errors.append("CHRONOBEAM_SESSION_ABSOLUTE_TTL_SECONDS")
    if settings.jwks_cache_ttl_seconds <= 0:
        errors.append("CHRONOBEAM_JWKS_CACHE_TTL_SECONDS")
    if settings.oidc_clock_skew_seconds < 0:
        errors.append("CHRONOBEAM_OIDC_CLOCK_SKEW_SECONDS")
    for key, value in (
        ("CHRONOBEAM_PUBLIC_BASE_URL", settings.public_base_url),
        ("CHRONOBEAM_OIDC_DISCOVERY_URL", settings.oidc_discovery_url),
        ("CHRONOBEAM_OIDC_ISSUER", settings.oidc_issuer),
    ):
        parsed = urlparse(value)
        if value and (parsed.scheme != "https" or not parsed.netloc or "*" in parsed.netloc):
            errors.append(key)
    database = urlparse(settings.database_url)
    try:
        dev_database = (
            settings.database_url
            == "postgresql+psycopg://chronobeam:chronobeam_dev_only@127.0.0.1:55432/chronobeam"
            or "chronobeam_dev_only" in unquote(settings.database_url)
            or database.port == 55432
        )
    except ValueError:
        dev_database = True
    if not database.scheme.startswith("postgresql") or not database.hostname or dev_database:
        errors.append("CHRONOBEAM_DATABASE_URL")
    for origin in settings.cors_origins:
        parsed = urlparse(origin)
        if "*" in origin or parsed.scheme != "https" or not parsed.netloc:
            errors.append("CHRONOBEAM_CORS_ORIGINS")
            break
    allowed_log_levels = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
    if settings.log_level.upper() not in allowed_log_levels:
        errors.append("CHRONOBEAM_LOG_LEVEL")
    try:
        values = (item.strip() for item in settings.forwarded_allow_ips.split(","))
        for value in filter(None, values):
            ipaddress.ip_network(value, strict=False)
    except ValueError:
        errors.append("CHRONOBEAM_FORWARDED_ALLOW_IPS")
    return sorted(set(errors))


def validate_production_settings(settings: Settings) -> None:
    errors = production_settings_errors(settings)
    if errors:
        raise RuntimeError("production configuration invalid: " + ", ".join(errors))


@lru_cache
def get_settings() -> Settings:
    return Settings()
