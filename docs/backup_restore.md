# Backup and restore

Chronobeam's CLI produces a PostgreSQL custom-format dump and a non-personal
JSON manifest. It restores only into an empty target database and verifies the
restored application state. `RETENTION_REQUIRES_DPO_IT_CONFIRMATION` remains a
governance marker, not a retention decision.

## CLI contract

Pass database credentials through the environment, never on a command line or
in a committed file. `pg_dump` and `pg_restore` must be installed on the host
running these commands.

```bash
cd backend
CHRONOBEAM_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@DB_HOST:5432/chronobeam' \
  uv run python -m app.backup dump --out /approved/backup/directory

CHRONOBEAM_RESTORE_URL='postgresql+psycopg://USER:PASSWORD@RESTORE_DB_HOST:5432/chronobeam_restore' \
  uv run python -m app.backup restore /approved/backup/directory/chronobeam-TIMESTAMP.dump \
  --database-url-env CHRONOBEAM_RESTORE_URL

CHRONOBEAM_RESTORE_URL='postgresql+psycopg://USER:PASSWORD@RESTORE_DB_HOST:5432/chronobeam_restore' \
  uv run python -m app.backup verify --database-url-env CHRONOBEAM_RESTORE_URL \
  --manifest /approved/backup/directory/chronobeam-TIMESTAMP.manifest.json
```

`--database-url-env NAME` reads the target URL from the named process environment
variable; it does not load `.env` files or fall back to the dump's settings.
Supply the variable through approved secret storage in production. An unset or
empty variable is refused. Existing `restore --database-url URL` and `verify URL`
forms remain available for compatibility, but expose credentials in process argv.
Supplying an argv URL together with `--database-url-env` is refused; neither wins.

URL options `sslmode`, `sslrootcert`, `sslcert`, `sslkey` and `sslcrl` are passed
to child tools as `PGSSLMODE`, `PGSSLROOTCERT`, `PGSSLCERT`, `PGSSLKEY` and
`PGSSLCRL`, respectively, overriding inherited values only when supplied. No TLS
default is added, and an explicit `sslmode=disable` remains `disable`.
On the dump/restore child-tool paths, unsupported, repeated or empty query options
are refused instead of dropped (`verify` connects through the SQLAlchemy driver,
which applies the URL's own options itself).
URLs whose query parsers disagree are also refused; percent-encode reserved
characters in credentials and option values, for example `%23` for `#`.
`sslpassword` is explicitly unsupported: [libpq has no environment variable for
it](https://www.postgresql.org/docs/16/libpq-connect.html#LIBPQ-CONNECT-SSLPASSWORD).
The mappings follow [PostgreSQL 16's libpq environment
documentation](https://www.postgresql.org/docs/16/libpq-envars.html).

`restore` rejects a target that already has tables. The manifest records dump
SHA-256, timestamp, PostgreSQL version, schema revision, application/build
provenance, aggregate table counts, and the retention marker; it intentionally
does not contain employee facts.

Create the restore database through the approved PostgreSQL administration
process before invoking `restore`. It must be a genuinely empty target, not a
database where tables were manually dropped. The CLI does not overwrite a dump
or manifest and fails if the adjacent manifest does not match the dump's
SHA-256.

For a routine restore rehearsal, use a separate, access-controlled restore
target rather than a database in active use. Treat the dump itself as personal-data
storage even though its manifest is aggregate-only.

## Execution contract (what the CLI guarantees)

**One snapshot per dump.** The manifest's `table_counts` and the dump itself come
from the SAME exported snapshot: the CLI opens a `REPEATABLE READ` transaction,
reads the counts, calls `pg_export_snapshot()`, runs `pg_dump --snapshot=<id>`
against that snapshot, then ends the transaction. A write landing mid-dump
therefore cannot make the manifest describe a state the dump does not contain —
which matters because `verify` compares the restored counts against that same
manifest, so a mismatch would surface as a failed verification of a perfectly
good dump.

**The cost of that, written down:** the exporting connection stays **idle in
transaction** for the whole dump, pinning the xmin horizon and holding back
vacuum for that period. At small scale (small database, short dump) this sits
inside the maintenance window. It is a deliberate trade, not an
oversight — if a dump ever grows large enough for vacuum starvation to matter,
the fix is a separate snapshot-holder design, not reverting to post-dump counts.

**Client/server pairing, both directions.** The rule has two halves and both
are enforced before a tool runs:

| Direction | Rule | Why |
|---|---|---|
| Dump | `pg_dump` major >= source server major | `pg_dump` cannot read a newer server ([PostgreSQL 16 pg_dump Notes](https://www.postgresql.org/docs/16/app-pgdump.html#APP-PGDUMP-NOTES)). |
| Restore | dump client major <= target server major | A dump written by a newer client can carry a setting the older server rejects. |

The restore half is not hypothetical. PostgreSQL 17's `pg_dump` emits
`SET transaction_timeout = 0`, which a 16 server rejects with
`unrecognized configuration parameter`; with `--exit-on-error` that aborts the
restore and leaves the target partially populated. `pg_restore` itself performs
no version comparison, so the check uses the dump client recorded in the
manifest, not the `pg_restore` binary.

Practical consequence: **the host's client tools must match the database
server's major version.** A host with a newer client than the server can dump
but not restore, and the CLI says so before touching the target. Source server
and dump-client versions are recorded in the manifest. RPO, RTO and retention
remain deployment-owner decisions.

**A failed restore is not retried in place.** `pg_restore` runs with
`--exit-on-error`, so a partial restore cannot be recorded as a good one. If
either `pg_restore` or post-restore verification fails, the target is left
unusable **by design** and the CLI names the disposal step (see
`docs/operations.md`); it does not attempt an in-place production restore.

Child failures report the tool and its exit status, never the child's output:
`pg_restore failed (exit 1); the tool's own output is in
<dump>.restore.log. PostgreSQL writes this to the child's stderr, not to a log
file.` The message stays data-free because PostgreSQL stderr can quote COPY row
contents or connection secrets. The stderr is not discarded: it is written to a
`0600` file beside the dump (`<dump>.dump.log` or `<dump>.restore.log`), inside
the operator's own dump directory, so a failed restore is diagnosable without
the payload reaching an exception, an application log line or a CI transcript.
Read that file, then follow the disposal guidance; do not retry in place.

## Operator checklist

| Concern | Decision / location |
|---|---|
| PostgreSQL client tools | On the **host** that runs the CLI. The serving image deliberately contains no client tools. |
| Version pairing | Dump client major ≥ source server major; dump client major ≤ target server major. Use client tools that match the server's major. Source/dump-client versions are recorded in the manifest. |
| Credentials | Dump uses `CHRONOBEAM_DATABASE_URL`; restore/verify use `--database-url-env NAME`. Prefer environment over the retained argv forms; never commit secrets. |
| Network path | The approved protected channel to the database host. |
| Destination | An approved, access-controlled backup directory; the dump is personal-data storage even though its manifest is aggregate-only. |
| Scheduled invocation | Deployment-owned (a scheduler/cron outside this repository). |
| Partial-restore disposal | `docs/operations.md`, "partial restore disposal". |
| Retention | `RETENTION_REQUIRES_DPO_IT_CONFIRMATION` — a marker, not a decision. |

## Verification standard

A successful `pg_restore` process is insufficient. Require the CLI to print
`verification: ok`: it checks schema revision, table counts, and application
integrity across all 15 domains (employee, identity/roles, schedule, work
interval, shift, OCD, overtime, travel, balance, submission, disclosure, audit,
policy, calendar, day status). Also require the restored service's
`/health/ready` to return 200 before accepting recovery.

Two domains are newer and worth naming, because a restored database can be
structurally intact yet operationally wrong without them:

- **`calendar`** — an inverted edition range, two editions of one key sharing an
  effective date, a day row outside its own edition's range, an orphan day row,
  and, most importantly, **coverage of every frozen snapshot's month**. The check
  is coverage, not non-overlap: overlapping edition ranges are legal, and the
  `calendar_version_ids` provenance column is JSONB rather than a foreign key, so
  referential existence on that list is deliberately not what is asserted.
- **`day status`** — an orphan employee reference, a `FULL` fact that does not
  cover the whole day, a `HALF` fact that names no morning/afternoon, a row with
  more than one successor (forked chain), two current heads in one
  employee/day/category/day-part slot, and a missing superseded predecessor.

Record the released application version/build SHA, backup manifest name,
verification result, readiness result, and the responsible operator in the
approved operational record. Do not record passwords, database URLs containing
passwords, row contents, session data, or employee names in that record.

## Upgrade and rollback

Upgrade: backup, deploy the new artifact, run `uv run alembic upgrade head`,
start, require `/health/ready` 200, **publish the calendar edition covering the
new deployment's range** (see `docs/deployment.md` — an upgrade that skips this
answers 409 `CALENDAR_BLOCKED` on every month view), and smoke-test one
authenticated employee read.

Rollback: restore the prior application artifact first. Downgrade schema only
when its downgrade path is proven and no data has been written under the newer
schema. Otherwise restore the pre-upgrade backup into a fresh empty database,
then verify it using the command above and readiness. A short maintenance
window is accepted; no zero-downtime guarantee is made.

Migration `0016_calendar_and_day_status` refuses its own downgrade while
`day_status_event` holds rows, so a rollback after day-status facts exist must go
through a restore rather than an Alembic downgrade.

If an upgrade fails before application data is written under the new schema,
the approved owner may decide whether its proven Alembic downgrade is suitable.
If that evidence is absent, stop and use a fresh restore target; do not improvise
SQL changes against live data. A recovered database becomes the active
database only through the approved platform cutover process.

## Responsibility split

Chronobeam owns dump/restore/verification behavior. The deployment platform owns
scheduling, off-host transfer, approved storage, encryption at rest, protected
access, monitoring and alerting. Retention, deletion, legal preservation
requirements and backup retention are `RETENTION_REQUIRES_DPO_IT_CONFIRMATION`; this
document does not supply guessed periods or legal basis.

The application has no scheduler, cloud-storage integration, or key-management
service for backups. Those omissions are deliberate: schedule frequency, RPO,
RTO, storage location, encryption/key controls, access reviews, and deletion
must be bound to the deployment platform and owner decisions rather than
encoded as assumptions in Chronobeam.
