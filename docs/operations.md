# Operations

This is the operator runbook for a configured deployment. It does not establish that a production
platform exists or authorize processing of real employee data. The deployment owner is responsible
for proxy, database, protected storage, monitoring, backup scheduling and incident escalation. See
[deployment.md](deployment.md) for release preparation and [backup_restore.md](backup_restore.md)
for dump/restore commands.

## Runtime and health

The runtime is one application container serving the SPA and FastAPI, backed by PostgreSQL. The
application has no separate worker service, broker, cache or scheduler. The current application
runs as a single process; worker count and database pool sizing must be decided together by the
deployment owner. The application runs migrations as a release step, not as a background job.

Check liveness and readiness separately through the approved proxy path:

```bash
curl --fail "${CHRONOBEAM_PUBLIC_BASE_URL%/}/health/live"
curl --fail "${CHRONOBEAM_PUBLIC_BASE_URL%/}/health/ready"
```

Liveness means only that the process responds. Readiness checks production configuration,
PostgreSQL reachability and schema revision against the application Alembic head. A readiness
failure categorizes invalid configuration, unreachable database or schema mismatch; it does not
expose employee facts. Do not use a restart to mask database or migration failure.

## Logs and security events

Use container/platform logs. Application request logging is intended for technical
metadata, request ID, matched route template, status and latency; unmatched requests use a fixed
marker. Uvicorn access logs redact the raw request target. Do not log credentials, bearer/session/CSRF tokens,
authorization codes, identity-provider payloads, comments/reasons, sickness details, work intervals, report
payloads or backup credentials. Retain only the technical information needed for the approved
incident route. Proxy and identity-provider log behavior is outside the application and is not
established by application code.

Do not add session replay, third-party analytics, productivity telemetry, keyboard/mouse activity,
GPS, screenshots or ledger-content logging. Audit events are a limited accountability record, not
employee productivity monitoring.

## Routine authentication maintenance

Expired sessions and login attempts can be pruned with the released production configuration and
approved DB access:

```bash
cd backend
uv run python -m app.db_admin cleanup-auth
```

Session expiry is an expected authentication result. Ask the employee to sign in again through the
configured identity provider; do not create a local password or manually alter session rows. A
successful identity-provider assertion still requires a mapped, active employee. If an identity
subject must change, use the controlled `app.identity_admin set-binding` CLI path with its required
reason; that action records lineage and invalidates the employee's sessions. Run the backend command
`uv run python -m app.identity_admin set-binding --help` to inspect its operator arguments.

## Employee onboarding and calendar readiness

Employees establish their own opening state through self-service onboarding. The operator opening
balance route remains a controlled cutover path, not an ordinary correction tool. A requested
month must be within the rollout boundary and have policy and calendar coverage. `CALENDAR_BLOCKED`
means the necessary calendar edition is absent; publish through the approved calendar CLI using
owner-supplied classifications. Never infer a work calendar or employer-free day.

The admin system status can report whether the calendar is loaded and whether configured
submission recipients are usable. Empty recipient/cutover allowlists fail closed. Do not resolve
these states by granting broad ledger visibility or by bypassing owner authorization.

## Releases and migrations

Before a release, follow the backup procedure in [backup_restore.md](backup_restore.md). Apply
`uv run alembic upgrade head` from `backend/` using the approved maintenance process, start the
intended artifact and require `/health/ready` 200. Check the application build SHA in the admin
system status and perform an approved mapped-principal read.

Use a quiesced single-version transition for migrations that add constraints a running writer
could violate or change coordination-lock namespaces: stop the app, migrate, start one version and
confirm its build SHA. Do not overlap old and new processes during such transitions. A failed
migration preflight stops the release; do not bypass it with manual SQL.

## Incidents and recovery

For readiness `503`, first inspect the categorical cause. For unexpected `5xx`, retain request ID,
technical timestamp and status for the incident process. Do not request broad
employee-ledger access to investigate. If OIDC login is unavailable, do not include browser
redirects, authorization codes, cookies or tokens in a ticket.

Restore only through [backup_restore.md](backup_restore.md). A failed restore target is partially
populated and unusable: drop and recreate it through approved PostgreSQL administration, then
retry on an empty target. Require the backup verifier's `verification: ok` and service readiness
before accepting recovery. Do not retry restore in place or improvise schema downgrade against
employee data.

## Scope and ownership

The application does not define platform monitoring, incident routing, RPO/RTO, backup schedule,
retention/deletion or operator privileges; those are deployment-owner decisions. See
[KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) for the limits this repository does establish.