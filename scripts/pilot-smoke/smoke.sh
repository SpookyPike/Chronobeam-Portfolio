#!/usr/bin/env bash
# Production-image, isolated-PostgreSQL pilot smoke.  No dev server or dev DB is used.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/chronobeam-pilot-smoke.XXXXXX")"
DB_CONTAINER="chronobeam-smoke-db"
APP_CONTAINER="chronobeam-smoke-app"
DB_PORT="${CHRONOBEAM_SMOKE_DB_PORT:-55433}"
APP_PORT="${CHRONOBEAM_SMOKE_APP_PORT:-18091}"
IDP_PORT="${CHRONOBEAM_SMOKE_IDP_PORT:-18443}"
DB_USER="chronobeam_smoke"
DB_NAME="chronobeam"
RESTORE_DB_NAME="chronobeam_restore_smoke"
# Hex keeps the per-run OpenSSL secret URL-safe without placing it in argv.
DB_PASSWORD="$(openssl rand -hex 32)"
DB_URL="postgresql+psycopg://${DB_USER}:${DB_PASSWORD}@127.0.0.1:${DB_PORT}/${DB_NAME}"
RESTORE_DB_URL="postgresql+psycopg://${DB_USER}:${DB_PASSWORD}@127.0.0.1:${DB_PORT}/${RESTORE_DB_NAME}"
ISSUER="https://127.0.0.1:${IDP_PORT}"
PUBLIC_BASE_URL="https://127.0.0.1:${APP_PORT}"
IDP_PID=""

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

unexpected_error() {
    local status=$?
    echo "FAIL: unexpected command failure at line $1 (exit $status)" >&2
    exit "$status"
}

# Set to 1 only once this run has passed the existing-container guard, so a refusal
# never removes a container that belongs to another run.
OWN_RESOURCES=0
cleanup() {
    local status=$?
    set +e
    if [[ -n "$IDP_PID" ]]; then kill "$IDP_PID" 2>/dev/null; fi
    if [[ "$OWN_RESOURCES" == "1" ]]; then
        docker rm -f -v "$APP_CONTAINER" >/dev/null 2>&1
        # This container intentionally does not use --rm: the readiness-recovery
        # proof must stop then start the same database.  -v removes its anonymous
        # PostgreSQL volume here, including on failures.
        docker rm -f -v "$DB_CONTAINER" >/dev/null 2>&1
    fi
    rm -rf "$SCRATCH"
    exit "$status"
}
trap cleanup EXIT INT TERM
trap 'unexpected_error $LINENO' ERR

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "required command is unavailable: $1"
}

expect_status() {
    local label=$1 url=$2 wanted=$3 body="$SCRATCH/response.json"
    local actual
    actual="$(curl --silent --show-error --output "$body" --write-out '%{http_code}' "$url")" \
        || fail "$label request failed"
    [[ "$actual" == "$wanted" ]] || fail "$label expected HTTP $wanted, got $actual: $(tr '\n' ' ' < "$body")"
    echo "PASS: $label -> HTTP $actual"
}

expect_json() {
    local label=$1 file=$2 expression=$3
    python3 - "$file" "$expression" <<'PY' || fail "$label JSON assertion failed"
import json
import sys

value = json.load(open(sys.argv[1], encoding="utf-8"))
if not eval(  # smoke-owned assertions, never evaluated from external input
    sys.argv[2], {"__builtins__": {"bool": bool}}, {"value": value}
):
    raise SystemExit(1)
PY
    echo "PASS: $label"
}

request_with_headers() {
    local method=$1 url=$2 headers=$3 body=$4
    shift 4
    curl --silent --show-error --request "$method" --dump-header "$headers" --output "$body" \
        --write-out '%{http_code}' "$@" "$url"
}

wait_for() {
    local label=$1 url=$2 attempts=${3:-40}
    local i
    for ((i = 1; i <= attempts; i++)); do
        if curl --silent --show-error --fail "$url" >/dev/null 2>&1; then
            echo "PASS: $label"
            return
        fi
        sleep 1
    done
    fail "$label did not become ready"
}

for command in docker openssl curl python3 uv; do require_command "$command"; done
[[ -x "$ROOT/backend/.venv/bin/python" ]] || fail "backend virtual environment is required"
docker pull postgres:18-alpine >/dev/null || fail "could not pull postgres:18-alpine"
if docker container inspect "$DB_CONTAINER" >/dev/null 2>&1 || docker container inspect "$APP_CONTAINER" >/dev/null 2>&1; then
    fail "a pilot-smoke container already exists; refuse to disturb another run"
fi
OWN_RESOURCES=1

echo "PASS: clean scratch workspace -> $SCRATCH"
openssl genrsa -out "$SCRATCH/ca-key.pem" 2048 >/dev/null 2>&1
openssl req -x509 -new -key "$SCRATCH/ca-key.pem" -sha256 -days 1 -out "$SCRATCH/ca.pem" \
    -subj "/CN=Chronobeam pilot smoke CA" >/dev/null 2>&1
openssl genrsa -out "$SCRATCH/idp-key.pem" 2048 >/dev/null 2>&1
openssl req -new -key "$SCRATCH/idp-key.pem" -out "$SCRATCH/idp.csr" \
    -subj "/CN=127.0.0.1" >/dev/null 2>&1
printf 'subjectAltName=IP:127.0.0.1\n' > "$SCRATCH/idp.ext"
openssl x509 -req -in "$SCRATCH/idp.csr" -CA "$SCRATCH/ca.pem" -CAkey "$SCRATCH/ca-key.pem" \
    -CAcreateserial -out "$SCRATCH/idp-cert.pem" -days 1 -sha256 -extfile "$SCRATCH/idp.ext" \
    >/dev/null 2>&1
echo "PASS: local CA and IdP TLS certificate generated"

(cd "$ROOT/backend" && uv run python "$ROOT/scripts/pilot-smoke/fake_idp.py" \
    --issuer "$ISSUER" --client-id chronobeam-smoke --audience chronobeam-smoke \
    --redirect-uri "$PUBLIC_BASE_URL/api/v1/auth/callback" --cert "$SCRATCH/idp-cert.pem" \
    --key "$SCRATCH/idp-key.pem" --port "$IDP_PORT") >"$SCRATCH/idp.log" 2>&1 &
IDP_PID=$!
for _ in $(seq 1 20); do
    if curl --silent --show-error --fail --cacert "$SCRATCH/ca.pem" "$ISSUER/health" >/dev/null 2>&1; then
        echo "PASS: fake IdP TLS health and trust chain"
        break
    fi
    sleep 1
done
curl --silent --show-error --fail --cacert "$SCRATCH/ca.pem" "$ISSUER/health" >/dev/null \
    || fail "fake IdP TLS health failed"

docker run -d --name "$DB_CONTAINER" -e "POSTGRES_USER=$DB_USER" -e "POSTGRES_PASSWORD=$DB_PASSWORD" \
    -e "POSTGRES_DB=$DB_NAME" -p "127.0.0.1:${DB_PORT}:5432" postgres:18-alpine >/dev/null
for _ in $(seq 1 40); do
    if docker exec "$DB_CONTAINER" pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then break; fi
    sleep 1
done
docker exec "$DB_CONTAINER" pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null \
    || fail "isolated PostgreSQL did not become ready"
echo "PASS: isolated PostgreSQL ready on 127.0.0.1:${DB_PORT}"

echo "Building fresh chronobeam:local production image"
(cd "$ROOT" && just image-build)
docker run -d --name "$APP_CONTAINER" --network host \
    -v "$SCRATCH/ca.pem:/etc/chronobeam/ca.pem:ro" -e SSL_CERT_FILE=/etc/chronobeam/ca.pem \
    -e CHRONOBEAM_ENV=production -e "CHRONOBEAM_PUBLIC_BASE_URL=$PUBLIC_BASE_URL" \
    -e "CHRONOBEAM_OIDC_DISCOVERY_URL=$ISSUER/.well-known/openid-configuration" \
    -e "CHRONOBEAM_OIDC_ISSUER=$ISSUER" -e CHRONOBEAM_OIDC_CLIENT_ID=chronobeam-smoke \
    -e CHRONOBEAM_OIDC_AUDIENCE=chronobeam-smoke -e CHRONOBEAM_OIDC_CLIENT_AUTH_METHOD=none \
    -e "CHRONOBEAM_DATABASE_URL=$DB_URL" -e CHRONOBEAM_FORWARDED_ALLOW_IPS=127.0.0.1 \
    -e CHRONOBEAM_SESSION_IDLE_TTL_SECONDS=900 -e CHRONOBEAM_SESSION_ABSOLUTE_TTL_SECONDS=3600 \
    chronobeam:local .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$APP_PORT" --workers 1 \
    --no-access-log --forwarded-allow-ips 127.0.0.1 >/dev/null
sleep 1
if [[ "$(docker inspect -f '{{.State.Running}}' "$APP_CONTAINER")" != true ]]; then
    docker logs "$APP_CONTAINER" >&2 || true
    fail "production app container exited during startup"
fi
wait_for "production app liveness" "http://127.0.0.1:${APP_PORT}/health/live" 40
expect_status "unmigrated readiness" "http://127.0.0.1:${APP_PORT}/health/ready" 503
expect_json "unmigrated readiness is categorical" "$SCRATCH/response.json" \
    'value.get("status") == "unavailable" and value.get("checks", {}).get("database") == "unreachable"'

docker exec "$APP_CONTAINER" uv run --no-dev --no-sync alembic upgrade head
echo "PASS: migrations applied in production app container"
expect_status "migrated readiness" "http://127.0.0.1:${APP_PORT}/health/ready" 200
expect_json "migrated readiness payload" "$SCRATCH/response.json" 'value == {"status": "ok"}'
expect_status "liveness compatibility alias" "http://127.0.0.1:${APP_PORT}/health" 200

docker exec -i "$DB_CONTAINER" psql -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" <<'SQL'
INSERT INTO employee (external_employee_id, idp_subject, legal_name, active)
VALUES ('smoke-0001', 'smoke-subject-001', 'Smoke Pilot (synthetic)', true);
INSERT INTO employment_contract (
    employee_id, valid_from, fte, weekly_required_minutes, pay_group_bucket,
    source, source_ref, version
)
SELECT id, CURRENT_DATE, 1.0, 2340, 'UNKNOWN', 'ADMIN_SEED', 'pilot-smoke fixture', 1
FROM employee WHERE external_employee_id = 'smoke-0001';
INSERT INTO work_schedule_version (
    employee_id, valid_from, weekly_required_minutes,
    mon_required_minutes, tue_required_minutes, wed_required_minutes, thu_required_minutes,
    fri_required_minutes, sat_required_minutes, sun_required_minutes, source, version
)
SELECT id, CURRENT_DATE, 2340, 468, 468, 468, 468, 468, 0, 0, 'ADMIN_SEED', 1
FROM employee WHERE external_employee_id = 'smoke-0001';
SQL
echo "PASS: synthetic employee, contract, and schedule fixture inserted"
expect_status "readiness after synthetic fixture" "http://127.0.0.1:${APP_PORT}/health/ready" 200

LOGIN_HEADERS="$SCRATCH/login.headers"; LOGIN_BODY="$SCRATCH/login.body"
status="$(request_with_headers GET "http://127.0.0.1:${APP_PORT}/api/v1/auth/login?return_to=/month" "$LOGIN_HEADERS" "$LOGIN_BODY")"
[[ "$status" == 302 ]] || fail "OIDC login expected HTTP 302, got $status"
AUTH_URL="$(sed -n 's/^[Ll]ocation:[[:space:]]*//p' "$LOGIN_HEADERS" | tr -d '\r')"
[[ "$AUTH_URL" == "$ISSUER/authorize"* ]] || fail "login did not redirect to the fake IdP"
python3 - "$AUTH_URL" <<'PY' || fail "OIDC login parameters are incomplete"
import sys
from urllib.parse import parse_qs, urlparse
q = parse_qs(urlparse(sys.argv[1]).query)
required = ("state", "nonce", "code_challenge")
if any(not q.get(key, [""])[0] for key in required) or q.get("code_challenge_method") != ["S256"]:
    raise SystemExit(1)
PY
echo "PASS: OIDC login -> 302 fake authorize with state, nonce, PKCE S256"

# The login attempt is bound to the browser that started it, so the pre-auth
# cookie must be carried back on the callback (this script keeps no cookie jar).
LOGIN_COOKIE_HEADER="$(grep -i '^set-cookie: __Host-Chronobeam-Login=' "$LOGIN_HEADERS" | tr -d '\r' || true)"
[[ "$LOGIN_COOKIE_HEADER" == *"HttpOnly"* && "$LOGIN_COOKIE_HEADER" == *"Secure"* && "$LOGIN_COOKIE_HEADER" == *"SameSite=lax"* && "$LOGIN_COOKIE_HEADER" == *"Path=/"* ]] \
    || fail "login did not set the hardened pre-auth binding cookie"
LOGIN_COOKIE_PAIR="${LOGIN_COOKIE_HEADER#set-cookie: }"; LOGIN_COOKIE_PAIR="${LOGIN_COOKIE_PAIR#Set-Cookie: }"; LOGIN_COOKIE_PAIR="${LOGIN_COOKIE_PAIR%%;*}"
[[ "$LOGIN_COOKIE_PAIR" == __Host-Chronobeam-Login=* ]] || fail "pre-auth binding cookie value was not found"
echo "PASS: OIDC login -> 302 and hardened __Host pre-auth binding cookie"

AUTHORIZE_HEADERS="$SCRATCH/authorize.headers"; AUTHORIZE_BODY="$SCRATCH/authorize.body"
status="$(request_with_headers GET "$AUTH_URL" "$AUTHORIZE_HEADERS" "$AUTHORIZE_BODY" --cacert "$SCRATCH/ca.pem")"
[[ "$status" == 302 ]] || fail "fake authorize expected HTTP 302, got $status"
CALLBACK_PUBLIC_URL="$(sed -n 's/^[Ll]ocation:[[:space:]]*//p' "$AUTHORIZE_HEADERS" | tr -d '\r')"
[[ "$CALLBACK_PUBLIC_URL" == "$PUBLIC_BASE_URL/api/v1/auth/callback?"* ]] \
    || fail "fake authorize did not return the exact public callback URI"
CALLBACK_APP_URL="${CALLBACK_PUBLIC_URL/https:\/\/127.0.0.1:${APP_PORT}/http:\/\/127.0.0.1:${APP_PORT}}"
CALLBACK_HEADERS="$SCRATCH/callback.headers"; CALLBACK_BODY="$SCRATCH/callback.body"
status="$(request_with_headers GET "$CALLBACK_APP_URL" "$CALLBACK_HEADERS" "$CALLBACK_BODY" -H "Cookie: $LOGIN_COOKIE_PAIR")"
[[ "$status" == 303 ]] || fail "OIDC callback expected HTTP 303, got $status: $(tr '\n' ' ' < "$CALLBACK_BODY")"
COOKIE_HEADER="$(grep -i '^set-cookie: __Host-Chronobeam-Session=' "$CALLBACK_HEADERS" | tr -d '\r' || true)"
[[ "$COOKIE_HEADER" == *"HttpOnly"* && "$COOKIE_HEADER" == *"Secure"* && "$COOKIE_HEADER" == *"SameSite=lax"* && "$COOKIE_HEADER" == *"Path=/"* ]] \
    || fail "callback did not set the hardened session cookie"
COOKIE_PAIR="${COOKIE_HEADER#set-cookie: }"; COOKIE_PAIR="${COOKIE_PAIR#Set-Cookie: }"; COOKIE_PAIR="${COOKIE_PAIR%%;*}"
[[ "$COOKIE_PAIR" == __Host-Chronobeam-Session=* ]] || fail "session cookie value was not found"
# Every terminal callback response also clears the pre-auth binding cookie.
CLEARED_HEADER="$(grep -i '^set-cookie: __Host-Chronobeam-Login=' "$CALLBACK_HEADERS" | tr -d '\r' || true)"
[[ "$CLEARED_HEADER" == *"Max-Age=0"* && "$CLEARED_HEADER" == *"Secure"* && "$CLEARED_HEADER" == *"Path=/"* ]] \
    || fail "callback did not clear the pre-auth binding cookie with __Host- attributes"
echo "PASS: OIDC callback -> 303, hardened __Host session cookie, pre-auth binding cleared"

SESSION_BODY="$SCRATCH/session.json"
curl --silent --show-error -H "Cookie: $COOKIE_PAIR" -o "$SESSION_BODY" \
    "http://127.0.0.1:${APP_PORT}/api/v1/auth/session"
expect_json "authenticated session and CSRF token" "$SESSION_BODY" \
    'value.get("authenticated") is True and bool(value.get("csrf_token"))'
CSRF_TOKEN="$(python3 - "$SESSION_BODY" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["csrf_token"])
PY
)"
ME_BODY="$SCRATCH/me.json"
status="$(curl --silent --show-error -H "Cookie: $COOKIE_PAIR" -o "$ME_BODY" --write-out '%{http_code}' "http://127.0.0.1:${APP_PORT}/api/v1/me")"
[[ "$status" == 200 ]] || fail "authenticated /me expected HTTP 200, got $status"
expect_json "synthetic subject mapped by OIDC" "$ME_BODY" 'value.get("subject") == "smoke-subject-001"'

TODAY="$(TZ=Europe/Berlin date +%F)"; MONTH="$(TZ=Europe/Berlin date +%Y-%m)"

# F5: the calendar is an explicit operator prerequisite. Until an edition is
# published, every month view fails closed (409 CALENDAR_BLOCKED) instead of
# silently treating a holiday as an ordinary working day. The pilot smoke proves
# BOTH halves: the refusal, and the successful read after publication.
status="$(curl --silent --show-error -H "Cookie: $COOKIE_PAIR" -o "$SCRATCH/calendar-blocked.json" \
    --write-out '%{http_code}' "http://127.0.0.1:${APP_PORT}/api/v1/me/months/${MONTH}")"
[[ "$status" == 409 ]] || fail "unpublished calendar month read expected HTTP 409, got $status"
expect_json "unpublished calendar fails closed" "$SCRATCH/calendar-blocked.json" \
    'value.get("detail", {}).get("code") == "CALENDAR_BLOCKED"'

CALENDAR_YEAR="$(TZ=Europe/Berlin date +%Y)"
CALENDAR_FILE="$SCRATCH/calendar-days.csv"
printf 'day,day_class\n%s-10-03,PUBLIC_HOLIDAY\n' "$CALENDAR_YEAR" > "$CALENDAR_FILE"
docker cp "$CALENDAR_FILE" "$APP_CONTAINER:/tmp/calendar-days.csv" >/dev/null
docker exec "$APP_CONTAINER" uv run --no-dev --no-sync python -m app.calendar_admin publish \
    --file /tmp/calendar-days.csv --version "smoke-${CALENDAR_YEAR}-v1" \
    --from "${CALENDAR_YEAR}-01-01" --source "PILOT_SMOKE_FIXTURE" \
    || fail "calendar edition publication failed"
echo "PASS: calendar edition published through the operator CLI"

INTERVAL_JSON="{\"start_local\":\"${TODAY}T09:00:00\",\"end_local\":\"${TODAY}T09:30:00\",\"work_type\":\"ON_SITE\"}"
status="$(curl --silent --show-error --request POST -H "Cookie: $COOKIE_PAIR" -H 'Content-Type: application/json' \
    --data "$INTERVAL_JSON" -o "$SCRATCH/csrf-denied.json" --write-out '%{http_code}' \
    "http://127.0.0.1:${APP_PORT}/api/v1/me/work-intervals")"
[[ "$status" == 403 ]] || fail "missing CSRF write expected HTTP 403, got $status"
expect_json "missing CSRF is rejected" "$SCRATCH/csrf-denied.json" \
    'value.get("detail", {}).get("code") == "CSRF_REJECTED"'
status="$(curl --silent --show-error --request POST -H "Cookie: $COOKIE_PAIR" -H "X-CSRF-Token: $CSRF_TOKEN" \
    -H 'Content-Type: application/json' --data "$INTERVAL_JSON" -o "$SCRATCH/interval.json" --write-out '%{http_code}' \
    "http://127.0.0.1:${APP_PORT}/api/v1/me/work-intervals")"
[[ "$status" == 201 ]] || fail "CSRF-authorized interval create expected HTTP 201, got $status"
echo "PASS: CSRF-authorized work interval -> HTTP 201"
status="$(curl --silent --show-error -H "Cookie: $COOKIE_PAIR" -o "$SCRATCH/month.json" --write-out '%{http_code}' \
    "http://127.0.0.1:${APP_PORT}/api/v1/me/months/${MONTH}")"
[[ "$status" == 200 ]] || fail "current month read expected HTTP 200, got $status"
expect_json "month read includes smoke interval" "$SCRATCH/month.json" 'value.get("interval_count", 0) >= 1'

status="$(curl --silent --show-error --request POST -H "Cookie: $COOKIE_PAIR" -H "X-CSRF-Token: $CSRF_TOKEN" \
    -o "$SCRATCH/logout.body" --write-out '%{http_code}' "http://127.0.0.1:${APP_PORT}/api/v1/auth/logout")"
[[ "$status" == 204 ]] || fail "logout expected HTTP 204, got $status"
echo "PASS: logout -> HTTP 204"
status="$(curl --silent --show-error -H "Cookie: $COOKIE_PAIR" -o "$SCRATCH/post-logout.json" --write-out '%{http_code}' "http://127.0.0.1:${APP_PORT}/api/v1/me")"
[[ "$status" == 401 ]] || fail "logout must reject old cookie, got HTTP $status"
[[ "$(docker exec "$DB_CONTAINER" psql -At -U "$DB_USER" -d "$DB_NAME" -c 'SELECT count(*) FROM auth_session')" == 0 ]] \
    || fail "logout did not delete the auth session"
echo "PASS: old session cookie rejected and session row removed"

docker stop "$DB_CONTAINER" >/dev/null
expect_status "readiness while PostgreSQL is stopped" "http://127.0.0.1:${APP_PORT}/health/ready" 503
expect_json "stopped PostgreSQL is categorical" "$SCRATCH/response.json" \
    'value.get("checks", {}).get("database") == "unreachable"'
docker start "$DB_CONTAINER" >/dev/null
for _ in $(seq 1 40); do
    if docker exec "$DB_CONTAINER" pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then break; fi
    sleep 1
done
expect_status "readiness recovers without app restart" "http://127.0.0.1:${APP_PORT}/health/ready" 200

mkdir "$SCRATCH/backup"
run_backup() {
    (cd "$ROOT/backend" && CHRONOBEAM_ENV=production CHRONOBEAM_DATABASE_URL="$DB_URL" \
        uv run python -m app.backup "$@")
}
run_backup dump --out "$SCRATCH/backup" | tee "$SCRATCH/backup-dump.log"
DUMP_FILE="$(find "$SCRATCH/backup" -maxdepth 1 -name '*.dump' -type f -print -quit)"
[[ -n "$DUMP_FILE" ]] || fail "backup dump was not created"
docker exec "$DB_CONTAINER" createdb -U "$DB_USER" "$RESTORE_DB_NAME"
(cd "$ROOT/backend" && CHRONOBEAM_ENV=production CHRONOBEAM_DATABASE_URL="$DB_URL" \
    uv run python -m app.backup restore "$DUMP_FILE" --database-url "$RESTORE_DB_URL") | tee "$SCRATCH/backup-restore.log"
(cd "$ROOT/backend" && CHRONOBEAM_ENV=production CHRONOBEAM_DATABASE_URL="$DB_URL" \
    uv run python -m app.backup verify "$RESTORE_DB_URL" --manifest "${DUMP_FILE%.dump}.manifest.json") \
    | tee "$SCRATCH/backup-verify.log"
grep -q '^verification: ok$' "$SCRATCH/backup-verify.log" || fail "restore verification did not report ok"
echo "PASS: backup dump, empty-target restore, and every-domain verification"

# The manifest's counts must describe the SAME snapshot as the dump. Re-deriving
# them after pg_dump would let a mid-dump write into the manifest and make a good
# dump fail verification, so the manifest must still match the restored database.
python3 - "$DUMP_FILE" <<'PY'
import json, sys, pathlib
manifest = json.loads(pathlib.Path(sys.argv[1]).with_suffix(".manifest.json").read_text())
assert manifest.get("table_counts"), "manifest carries no aggregate counts"
assert manifest.get("pg_client_version"), "manifest does not record the client version"
PY
echo "PASS: manifest records snapshot counts and the client tool version"

# Failure path: a corrupt dump must fail loudly and name the disposal step,
# never leave a partially populated target that looks recoverable.
CORRUPT_DIR="$SCRATCH/backup-corrupt"
mkdir "$CORRUPT_DIR"
CORRUPT_DUMP="$CORRUPT_DIR/$(basename "$DUMP_FILE")"
head -c 512 "$DUMP_FILE" >"$CORRUPT_DUMP"
python3 - "$DUMP_FILE" "$CORRUPT_DUMP" <<'PY'
import hashlib, json, pathlib, sys
source, corrupt = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
manifest = json.loads(source.with_suffix(".manifest.json").read_text())
manifest["dump_sha256"] = hashlib.sha256(corrupt.read_bytes()).hexdigest()
corrupt.with_suffix(".manifest.json").write_text(json.dumps(manifest))
PY
docker exec "$DB_CONTAINER" createdb -U "$DB_USER" "${RESTORE_DB_NAME}_bad"
# The failure is EXPECTED here, so the ERR trap is suspended for the duration:
# `set +e` alone does not stop it firing, and the trap would fail the smoke.
trap - ERR
set +e
(cd "$ROOT/backend" && CHRONOBEAM_ENV=production CHRONOBEAM_DATABASE_URL="$DB_URL" \
    uv run python -m app.backup restore "$CORRUPT_DUMP" \
    --database-url "${RESTORE_DB_URL}_bad") >"$SCRATCH/backup-corrupt.log" 2>&1
CORRUPT_EXIT=$?
set -e
trap 'unexpected_error $LINENO' ERR
[[ "$CORRUPT_EXIT" -ne 0 ]] || fail "a corrupt dump was reported as a successful restore"
grep -q 'PARTIALLY POPULATED' "$SCRATCH/backup-corrupt.log" \
    || fail "a failed restore did not name the disposal step"
docker exec "$DB_CONTAINER" dropdb -U "$DB_USER" --if-exists "${RESTORE_DB_NAME}_bad"
echo "PASS: a corrupt dump fails loudly and names the partial-restore disposal step"

docker stop -t 10 "$APP_CONTAINER" >/dev/null
APP_EXIT="$(docker inspect -f '{{.State.ExitCode}}' "$APP_CONTAINER")"
[[ "$APP_EXIT" == 0 ]] || fail "graceful app shutdown exit code was $APP_EXIT"
echo "PASS: production app graceful stop -> exit code 0"
echo "PASS: pilot smoke complete (all isolated resources will now be removed)"
