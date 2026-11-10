# Chronobeam — developer entrypoint.
#
# Thin wrappers around canonical project commands. Run from repository root.
# Prerequisites (documented in README.md): docker, uv, pnpm (corepack), just.
# No secrets live in this file.

set shell := ["bash", "-uc"]

alias c := check

# ---------------------------------------------------------------------------
# Bootstrap / install
# ---------------------------------------------------------------------------

# One-command clean-checkout bring-up: prerequisites, dev DB, deps, migrations.
bootstrap: check-tools db-up install migrate seed-dev
    @echo "Bootstrap complete. Run 'just dev' to start backend + frontend."

check-tools:
    @command -v docker >/dev/null || { echo "missing prerequisite: docker (see README)"; exit 1; }
    @command -v uv >/dev/null || { echo "missing prerequisite: uv (see README)"; exit 1; }
    @command -v pnpm >/dev/null || { echo "missing prerequisite: pnpm (see README)"; exit 1; }
    @echo "toolchain OK"

# Install backend (uv sync) and frontend (pnpm) dependencies from lockfiles.
install:
    uv sync --project backend --locked
    cd frontend && pnpm install --frozen-lockfile

# ---------------------------------------------------------------------------
# Development servers
# ---------------------------------------------------------------------------

# Start backend (uvicorn, :8000) and frontend (vite, :5173) together.
# For E2E runs prefer two terminals: `just backend` + `just frontend`.
dev:
    @echo "Chronobeam dev: backend :8000, frontend :5173 (Ctrl-C stops both)"
    @trap 'kill 0' EXIT; (just backend) & (just frontend) & wait

# Backend dev server. CHRONOBEAM_ENV=dev is forced: unset must stay
# fail-closed (production), and the dev-auth header is dev/test only.
backend:
    cd backend && CHRONOBEAM_ENV=dev uv run uvicorn app.main:app --reload --port 8000

frontend:
    cd frontend && pnpm dev

# Dump the generated OpenAPI document to stdout.
openapi:
    cd backend && uv run python -c "import json; from app.main import app; print(json.dumps(app.openapi(), indent=2))"

# Regenerate the committed HTTP-contract digest (api-contract.json, repo root).
# The digest records the property sets of the response models the frontend
# depends on, so a backend schema change shows up as a reviewable diff.
api-contract:
    cd backend && CHRONOBEAM_ENV=dev uv run python ../scripts/api_contract.py > ../api-contract.json

# Fail when the committed digest is stale: backend contract drift is a build
# failure, not something discovered later in a browser.
api-check: api-contract
    git diff --exit-code -- api-contract.json

# ---------------------------------------------------------------------------
# Database (local development only)
# ---------------------------------------------------------------------------

db-up:
    docker compose up -d --wait db

db-down:
    docker compose down

# Wipe the local dev database volume. Destructive: deletes all dev data.
db-reset:
    docker compose down -v --remove-orphans

# Apply all migrations.
migrate:
    cd backend && uv run alembic upgrade head

# Verify migrations apply from a clean schema and can be downgraded/re-applied.
# Runs against the TEST database (chronobeam_test): the gate downgrades to base,
# which must never run against the seeded dev database.
migration-check: db-up
    cd backend && uv run python -m app.db_admin migration-check

# Seed the two synthetic employees + ADMIN_SEED master data into the DEV
# database. Refuses unless CHRONOBEAM_ENV=dev (no startup seeding; this is the
# explicit developer action after `just migrate`). Also publishes a bounded
# statutory-holiday overlay (2026-10-01..2035-12-31) on the open-ended coverage
# edition, so a dev month shows real public holidays. Idempotent.
seed-dev: db-up
    cd backend && CHRONOBEAM_ENV=dev uv run python -m app.dev_seed

# ---------------------------------------------------------------------------
# Demo environment
# ---------------------------------------------------------------------------

# DESTRUCTIVE: wipe the local database volume and re-apply migrations, so
# `just demo-seed` starts from an empty schema. This is what lets the demo own
# 2026-01-01 for the statutory calendar instead of inheriting the dev seed's
# coverage-edition compromise (Jan-Sep 2026 left ordinary).
demo-reset: db-reset db-up migrate
    @echo "Demo database reset (empty schema, migrations applied). Seed it with 'just demo-seed', or use 'just demo-reseed' for reset + seed in one step."

# Seed the deterministic demo state: the statutory calendar for the whole demo
# horizon (2026-01-01..2035-12-31), 14 fictional personas covering the product's
# paths, and their September 2026 facts through 8 October. Every persona but the
# onboarding one is onboarded in September. Refuses unless CHRONOBEAM_ENV=dev.
# Idempotent: re-running replays existing facts rather than duplicating them.
demo-seed: db-up
    cd backend && CHRONOBEAM_ENV=dev uv run python -m app.demo_seed

# The demo personas + the deterministic September 2026 demo state into the DEV
# database (fictional identities only; refuses unless CHRONOBEAM_ENV=dev).
# `demo-seed` is the supported entry point and seeds exactly the same state.
seed-demo: db-up
    cd backend && CHRONOBEAM_ENV=dev uv run python -m app.demo_seed

# DESTRUCTIVE reset in one step: wipe the local DB volume, re-apply migrations and
# re-seed the demo state (`demo-reset` + `demo-seed`). Use it to re-run a rehearsal
# or a recorded demo from a known-good state.
demo-reseed: demo-reset demo-seed
    @echo "Demo state re-seeded: fresh schema, the statutory calendar, 14 personas, September 2026 facts through 8 October."

# ---------------------------------------------------------------------------
# Demo environment — one-command presentation bring-up (F8)
# ---------------------------------------------------------------------------

# Start the whole demo environment and LEAVE IT RUNNING: database, migrations,
# demo seed, backend (:8000) and one dev server per presentation persona —
# Employee :5173, HR :5174, Admin :5175, Clean :5176, Recipient :5177,
# Onboarding :5178 (the only fact-free persona, so its start-state form can be
# demonstrated live).
# Idempotent and readiness-checked: a
# service that is already healthy is reused, a port serving a different principal
# is reported instead of adopted, and nothing is ever started twice. Each persona
# is its own origin, so none share an identity (see scripts/demo/demo.sh).
demo:
    bash scripts/demo/demo.sh up

# Open every persona in Chrome: `just demo-open tabs` puts them in ONE window as
# tabs (the mode to share in a Zoom call), while `just demo-open windows` opens
# one separate window per persona. Requires `just demo`.
demo-open mode:
    bash scripts/demo/demo.sh open {{mode}}

# Stop only the demo servers this repository started (`just demo`), leaving the
# database container and any server started elsewhere untouched.
demo-down:
    bash scripts/demo/demo.sh down

# ---------------------------------------------------------------------------
# Quality gates
# ---------------------------------------------------------------------------

# Ruff runs over the WHOLE repository, not just backend/: the settings live in
# the root ruff.toml, so scripts/ is held to the same rules and the same
# 100-column width as the application. `--project backend` only selects the
# interpreter/venv that has ruff installed.
lint:
    uv run --project backend ruff check .
    cd frontend && pnpm lint

format:
    uv run --project backend ruff format .
    cd frontend && pnpm format

format-check:
    uv run --project backend ruff format --check .
    cd frontend && pnpm format:check

typecheck:
    cd backend && uv run pyright
    cd frontend && pnpm typecheck

# ty — second, independent Python type signal (Astral). Pyright above stays the
# blocking authority: ty is pre-1.0 and currently reports findings Pyright does
# not, so this recipe is deliberately NOT part of `check`/`ci`. See the note in
# backend/pyproject.toml for how to read its output.
ty:
    cd backend && uv run ty check

# Knip — unused files, exports, types and dependencies in the frontend.
# Also NOT in `check`/`ci`: it currently reports pre-existing dead exports.
knip:
    cd frontend && pnpm knip

# dependency-cruiser — architecture rules + cycle detection for the frontend.
# Rules live in frontend/.dependency-cruiser.cjs.
arch:
    cd frontend && pnpm depcruise

test: test-backend test-frontend

test-backend:
    cd backend && uv run pytest

test-frontend:
    cd frontend && pnpm test

# Playwright smoke suite (needs `pnpm exec playwright install chromium` once).
e2e:
    cd frontend && pnpm e2e

# Normal local quality gate.
check: lint format-check typecheck arch api-check test

# Reproducible image; build metadata is non-secret provenance only. The label
# must equal HEAD: the build copies the working tree, so a hand-set SHA could
# label uncommitted content with someone else's commit.
image-build:
    build_sha="${CHRONOBEAM_SOURCE_BUILD_SHA:-$(git rev-parse HEAD)}"; [[ "$build_sha" =~ ^[[:xdigit:]]{40}$ ]] || { echo "CHRONOBEAM_SOURCE_BUILD_SHA must be a full 40-character commit SHA" >&2; exit 1; }; if git rev-parse --verify HEAD >/dev/null 2>&1; then [ "$build_sha" = "$(git rev-parse HEAD)" ] || { echo "refusing to label an image with $build_sha: this checkout is at $(git rev-parse HEAD)." >&2; exit 1; }; fi; docker build --build-arg VERSION="$(sed -n 's/^APP_VERSION = "\(.*\)"$/\1/p' backend/app/version.py)" --build-arg BUILD_SHA="$build_sha" -t chronobeam:local .

# Isolated production-image + PostgreSQL + OIDC + recovery proof.
pilot-smoke:
    bash scripts/pilot-smoke/smoke.sh

# Checks expected in CI (no E2E). db-up FIRST: the DB-backed test suite needs
# PostgreSQL; CHRONOBEAM_REQUIRE_DB=1 makes CI fail instead of skipping.
ci: db-up lint format-check typecheck arch api-check test-backend test-frontend migration-check

# Remove generated local artifacts only. Never touches source, docs, or data.
clean:
    rm -rf frontend/node_modules frontend/dist backend/.venv backend/.pytest_cache .pytest_cache
    find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
    @echo "cleaned generated artifacts"
