# Testing and verification

This page describes repository commands and evidence boundaries. It makes no claim that any
command passed. Commands are defined in [Justfile](../Justfile); database skip behavior is in
[backend/tests/conftest.py](../backend/tests/conftest.py#L57-L77).

## Canonical commands

| Command | What it checks and limits |
|---|---|
| `just lint` | Ruff over the repository and frontend lint. [Justfile](../Justfile) |
| `just format-check` | Ruff and frontend formatting checks. [Justfile](../Justfile) |
| `just typecheck` | Pyright and frontend typecheck. Optional `ty` is excluded from `check` and `ci`. [Justfile](../Justfile) |
| `just arch` | Frontend dependency-cruiser architecture and cycle check. [Justfile](../Justfile) |
| `just api-check` | Regenerates the API contract digest and checks whether the committed digest changed. [Justfile](../Justfile) |
| `just test-backend` | Backend pytest suite. PostgreSQL-dependent fixtures skip when PostgreSQL is unreachable unless `CHRONOBEAM_REQUIRE_DB=1`, which turns unavailability into a runtime error. [Justfile](../Justfile), [fixture](../backend/tests/conftest.py#L57-L77) |
| `just test-frontend` | Vitest frontend suite. [Justfile](../Justfile) |
| `just check` | `lint`, `format-check`, `typecheck`, `arch`, `api-check` and both test suites. [Justfile](../Justfile) |
| `CHRONOBEAM_REQUIRE_DB=1 just ci` | CI aggregate: database required, quality checks, frontend/backend tests and migration check. [Justfile](../Justfile), [CI workflow](../.github/workflows/ci.yml) |
| `just e2e` | Playwright journeys configured for desktop, desktop-small, mobile and mobile-small projects. Several `/api/v1/**` journeys use fixed route stubs, so they exercise UI behavior but do not prove backend persistence or authorization. [Justfile](../Justfile), [Playwright projects](../frontend/playwright.config.ts#L19-L37), [stub example](../frontend/e2e/working-time.spec.ts#L103-L108) |
| `just migration-check` | Creates a unique scratch DB, migrates to head, downgrades to base, reapplies head and drops the scratch DB. It does not silently skip. Expected marker: `migration apply/downgrade/re-apply OK (scratch database)`. [Justfile](../Justfile), [implementation](../backend/app/db_admin.py#L46-L57) |
| `just pilot-smoke` | Isolated production image, PostgreSQL and TLS fake identity provider; exercises login/session/CSRF/write/logout, backup/restore and corrupt-restore path. Read the full log for its named pass markers and final `pilot smoke complete`. This is synthetic fake-identity-provider evidence, not live identity-provider or deployment verification. [Justfile](../Justfile), [smoke script](../scripts/pilot-smoke/smoke.sh#L104-L115) |

The smoke markers include `fake IdP TLS health and trust chain`, `migrations applied in production
app container`, `OIDC login -> 302 fake authorize with state, nonce, PKCE S256`,
`CSRF-authorized work interval -> HTTP 201`, and
`backup dump, empty-target restore, and every-domain verification`. [Marker locations](../scripts/pilot-smoke/smoke.sh#L165), [write marker](../scripts/pilot-smoke/smoke.sh#L203), [recovery markers](../scripts/pilot-smoke/smoke.sh#L284-L373).

## Database semantics and optional-input skips

Run backend checks requiring persistence with `CHRONOBEAM_REQUIRE_DB=1`; otherwise some DB-backed
fixtures skip when PostgreSQL cannot be reached. The CI aggregate sets this flag so DB
unavailability fails rather than silently reducing coverage. Migration-check has its own scratch
database lifecycle and does not silently skip. Stubbed Playwright journeys are not a replacement
for live-backend acceptance evidence.

A few tests skip when PostgreSQL is unreachable, or when a required CLI binary is missing (`bash` for the demo-ergonomics test, `pg_dump`/`psql` for the backup checks). A generated `docs/POLICY_SNAPSHOT.json` is absent until you create it, and the one test that compares against it skips until then. One further test skips when both corrected credit editions are already resolvable, because its precondition no longer holds. Those skips concern
unavailable source inputs, not evidence that an application behavior passed. The rule-status drift
guard reads the tracked catalogue and does not skip.

For a narrow backend run, use `cd backend && CHRONOBEAM_REQUIRE_DB=1 uv run pytest -vv -ra`;
frontend tests use `just test-frontend`. `CHRONOBEAM_REQUIRE_DB=1 just ci` is the aggregate. A
separate live synthetic acceptance pass is needed to establish full-stack persistence/authz; do not
infer it from route-stubbed Playwright journeys. This document records command semantics only.