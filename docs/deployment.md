# Deployment runbook

This describes the intended production shape and operator sequence. It does not provide a
production Compose file or assert that a deployment exists. See [CONFIGURATION.md](CONFIGURATION.md),
[operations.md](operations.md), [backup_restore.md](backup_restore.md) and
[security/THREAT_MODEL.md](security/THREAT_MODEL.md).

## Topology and ownership

The intended path is browser → reverse proxy/TLS → one application container serving the compiled
SPA and FastAPI → PostgreSQL. OIDC uses a separate identity provider. Future external HR absence,
master-data, shift/OCD and payroll adapters require documented contracts and are not live by
default. The app sets trusted proxy sources from `CHRONOBEAM_FORWARDED_ALLOW_IPS`; the approved
proxy, host routing and TLS termination are deployment-platform responsibilities.

The application image runs non-root (UID/GID 10001), serves port 8000, and contains the SPA when
built. The serving container does not contain PostgreSQL client utilities; `pg_dump` and
`pg_restore` belong on the host running backup operations. The database is the sole primary
persistence system. No server hostname, network, secret store or production Compose configuration
is supplied here.

## Build and configuration

Build the application image from the repository:

```bash
just image-build
```

The recipe labels the image with `git rev-parse HEAD`. That label is a **label, not a proof of
content**: the recipe copies the working tree, so an uncommitted edit is baked into an image
labelled with the last commit. Build from a clean checkout and treat the image digest recorded
beside the SHA as the identity of the artefact. Container base-image digests are pinned, but
`apt-get upgrade` means rebuilt image bytes are not guaranteed to be identical; scan and identify
the exact image digest used for deployment.

Supply production variables through the deployment configuration and secret store.
Required settings, defaults, startup refusals, secret classification and owners are enumerated in
[CONFIGURATION.md](CONFIGURATION.md). Do not copy local development database credentials from
`.env.example`. No production values are guessed here. In particular, the OIDC issuer/discovery,
client registration, public HTTPS origin, database URL, trusted proxy ranges, recipients,
cutover operators and rollout month are deployment inputs.

## Database and release sequence

Create an empty PostgreSQL database through the approved process. Before a release,
perform the approved backup procedure. Apply the migration from the released artifact:

```bash
cd backend
uv run alembic upgrade head
```

Then start the application artifact, require `/health/ready` to return 200 and confirm the expected
schema revision and application build SHA through the operator surface. A liveness
response alone is not database/schema readiness. Follow the quiesced single-version procedure in
[operations.md](operations.md) for migrations that add writer-sensitive constraints or change
lock namespaces.

`just pilot-smoke` is an isolated local synthetic test of the production-code path with PostgreSQL
and a TLS fake identity provider. It is not a production deployment or a TLS/database/backup
validation. See [TESTING.md](TESTING.md).

## Calendar and employee setup

Before an intended operating range can be used, the service needs calendar coverage; an uncovered
period fails closed with a calendar-blocked response. Calendar classifications are deployment
inputs. The command can derive a regional statutory holiday set; employer-free dates must be
supplied by the responsible owner and must not be guessed. Example for a reviewed statutory range:

```bash
cd backend
uv run python -m app.calendar_admin publish --statutory-sh \
  --from 2026-01-01 --to 2035-12-31 --dry-run
```

Review the dry-run output and provenance before a separately authorized publish. Do not
copy this example range as an approved production horizon.

Master-data import and identity binding are separate controlled operations. The master-data CSV
does not bind OIDC subjects; imported employees start inactive. The operator must
use the documented master-data and identity CLI procedures, then confirm account readiness. No
real authoritative master-data feed is included.

Normal rollout begins at a month boundary. Previous employee months remain in Excel/archive; there
is no production historical workbook upload. Only an authorized, validated opening state is seeded
once, using the auditable cutover flow.

## Deployment preconditions

The application itself does not establish: hosting and TLS/host routing; proxy trust ranges;
production OIDC client/claims and a live identity provider; protected database transport; platform
encryption at rest and key management; secret storage; backup/snapshot protection and schedule;
monitoring and incident route; operator privileges; recipient and cutover allowlists; rollout
boundary; or legal basis, controller classification, retention/deletion, employee notice,
processing inventory and impact assessment. Those are supplied and decided outside this
repository; a successful local build or smoke test does not imply them. The stable gaps are indexed
in [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).