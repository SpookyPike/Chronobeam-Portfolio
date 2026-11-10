#!/usr/bin/env bash
# Chronobeam — the demo environment in one command (F8 presentation ergonomics).
#
#   just demo                  bring the environment up and leave it running
#   just demo-open tabs        every persona as a tab in ONE Chrome window
#   just demo-open windows     ... or one separate Chrome window per persona
#   just demo-reseed           DESTRUCTIVE: wipe and re-seed the demo database
#   just demo-down             stop the servers this script started
#
# It composes the EXISTING canonical demo setup — `db-up`, `migrate`,
# `demo-seed` and the ordinary dev servers — and adds nothing to the
# application. Re-running is deterministic: a service that is already healthy is
# reused, and a port that is already serving a DIFFERENT principal is reported
# rather than silently adopted, so the demo can neither start a duplicate nor
# show the wrong identity. Only processes this script recorded are ever stopped.
#
# Persona isolation is by ORIGIN. In dev/test the principal is the per-request
# `X-Chronobeam-Dev-Principal` header and there is no cookie session
# (app/auth/deps.py), so one dev server per persona — each pinning that header
# in its own proxy (frontend/vite.config.ts, CHRONOBEAM_DEV_PRINCIPAL) — yields
# independent identities. That holds for tabs of one window exactly as for
# separate windows: a tab and a window are each a top-level browsing context,
# and no two personas share an origin. Production is untouched — the
# header is honoured only outside production, and this proxy is dev tooling.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_DIR="$ROOT/.demo"
PID_DIR="$RUN_DIR/pids"
LOG_DIR="$RUN_DIR/logs"

BACKEND_PORT=8000
BACKEND_READY="http://127.0.0.1:${BACKEND_PORT}/health/ready"
READY_TIMEOUT="${CHRONOBEAM_DEMO_READY_TIMEOUT:-60}"

# label:port:dev-principal — the presentation personas, in the order the demo
# tells its story: the employee acts, HR is the requester, the admin holds no
# business-data reach, and the next two widen the two boundaries the story turns
# on — a second employee with a clean ledger (the lifecycle walkthrough) and the
# recipient who may read submitted packages. Last is the onboarding persona: the
# only employee seeded with NO facts, because the clean-cutover guard refuses to
# establish a start state for anyone who already holds facts before the start
# month — and every other persona is onboarded in September — so it is
# the only one whose start-state form can be demonstrated live. Every subject is
# seeded by `just demo-seed` and registered in the dev seam
# (app/auth/dev_principals.py); a typo would surface as a 401, so
# `backend/tests/test_demo_ergonomics.py` pins the mapping.
PERSONAS=(
    "Employee:5173:demo-employee"
    "HR:5174:demo-hr"
    "Admin:5175:demo-admin"
    "Clean:5176:demo-clean"
    "Recipient:5177:demo-recipient"
    "Onboarding:5178:demo-onboarding"
)

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

usage() {
    cat >&2 <<'EOF'
usage: demo.sh <up | open <windows|tabs> | down>

  up              start the demo environment (idempotent, health-checked)
  open windows    open every persona as its own Chrome window
  open tabs       open every persona as a tab in one Chrome window
  down            stop the servers this script started
EOF
    exit 2
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "required command is unavailable: $1"
}

# --- readiness -------------------------------------------------------------

# Poll a health/contract check until it passes. The wait is bounded and the
# verdict comes from the check, never from elapsed time.
wait_until() {
    local label=$1 deadline
    shift
    deadline=$((SECONDS + READY_TIMEOUT))
    while ((SECONDS < deadline)); do
        if "$@" >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.2
    done
    fail "${label} was not ready within ${READY_TIMEOUT}s. Logs: ${LOG_DIR}"
}

backend_ready() {
    curl -fsS -o /dev/null --max-time 5 "$BACKEND_READY" 2>/dev/null
}

port_answers() {
    curl -s -o /dev/null --max-time 5 "http://127.0.0.1:$1/" 2>/dev/null
}

# One field of the identity card the frontend's proxy resolves for a port, so a
# check asserts the persona actually reached the backend rather than that some
# HTTP server is listening.
identity_field() {
    curl -fsS --max-time 5 "http://127.0.0.1:$1/api/v1/me" 2>/dev/null |
        sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p"
}

# A frontend is ready only when its proxy resolves the persona it was started
# for — a listening socket is not the same claim.
frontend_ready() {
    [[ "$(identity_field "$1" subject || true)" == "$2" ]]
}

# --- processes -------------------------------------------------------------

# Start a service in its own process group and record its pid. `uv run` and
# `pnpm exec` both wrap the real server, so the group — not just the wrapper —
# is what has to be signalled on the way down.
start_service() {
    local name=$1 log=$2 cwd=$3
    shift 3
    set -m
    (cd "$cwd" && exec "$@") >"$log" 2>&1 &
    printf '%s\n' "$!" >"${PID_DIR}/${name}.pid"
    set +m
}

# The recorded pid, if that process is still alive. Absent or stale ⇒ nothing.
alive_pid() {
    local file="${PID_DIR}/$1.pid" pid
    [[ -f "$file" ]] || return 1
    pid="$(<"$file")"
    [[ -n "$pid" ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    printf '%s\n' "$pid"
}

# --- up --------------------------------------------------------------------

start_backend() {
    echo "==> backend"
    if backend_ready; then
        echo "    already ready on :${BACKEND_PORT} — reusing"
        return
    fi
    # The demo/pilot posture: employee plan entry is ENABLED for both domains, so
    # the planned-vs-actual, deviation, shift-report, OCD-call and compensation
    # workflows are all exercisable without an external planner or admin preload.
    # Both default OFF in production config (see .env.example).
    start_service backend "${LOG_DIR}/backend.log" "${ROOT}/backend" \
        env CHRONOBEAM_ENV=dev \
        CHRONOBEAM_ENABLE_SHIFT_PLAN_ENTRY=true \
        CHRONOBEAM_ENABLE_OCD_PLAN_ENTRY=true \
        uv run uvicorn app.main:app --port "$BACKEND_PORT"
    wait_until "backend" backend_ready
    echo "    up on :${BACKEND_PORT}"
}

start_frontends() {
    local spec role port subject current
    for spec in "${PERSONAS[@]}"; do
        IFS=: read -r role port subject <<<"$spec"
        echo "==> ${role} (:${port}, principal ${subject})"
        current="$(identity_field "$port" subject || true)"
        if [[ "$current" == "$subject" ]]; then
            echo "    already serving ${subject} — reusing"
            continue
        fi
        if [[ -n "$current" ]] || port_answers "$port"; then
            fail "port ${port} is already in use and is not serving '${subject}'. Stop that server (it is not this demo's) and re-run."
        fi
        start_service "${role,,}" "${LOG_DIR}/${role,,}.log" "${ROOT}/frontend" \
            env CHRONOBEAM_DEV_PRINCIPAL="$subject" pnpm exec vite --port "$port" --strictPort
        wait_until "${role} frontend" frontend_ready "$port" "$subject"
        echo "    up as ${subject}"
    done
}

presenter_card() {
    local spec role port subject name
    echo
    echo "Demo environment ready."
    for spec in "${PERSONAS[@]}"; do
        IFS=: read -r role port subject <<<"$spec"
        name="$(identity_field "$port" display_name || true)"
        printf '  %-9s %-14s http://127.0.0.1:%s\n' "$role" "${name:-$subject}" "$port"
    done
    echo "  admin    backend        http://127.0.0.1:${BACKEND_PORT}/health/ready"
    echo
    echo "  Zoom — share ONE Chrome window and switch tabs:  just demo-open tabs"
    echo "  Other — one window per persona:                  just demo-open windows"
    echo "  Reset: just demo-reseed          Stop: just demo-down"
}

up() {
    require_command docker
    require_command uv
    require_command pnpm
    require_command curl
    mkdir -p "$PID_DIR" "$LOG_DIR"

    echo "==> database"
    (cd "$ROOT" && docker compose up -d --wait db)
    (cd "$ROOT/backend" && uv run alembic upgrade head)

    echo "==> demo seed (idempotent)"
    if ! (cd "$ROOT/backend" && CHRONOBEAM_ENV=dev uv run python -m app.demo_seed); then
        fail "the demo seed refused. If this database was built by 'just seed-dev', run 'just demo-reseed' (DESTRUCTIVE) first."
    fi

    start_backend
    start_frontends
    presenter_card
}

# --- open ------------------------------------------------------------------

chrome_bin() {
    local candidate
    if [[ -n "${CHRONOBEAM_DEMO_BROWSER:-}" ]]; then
        command -v "$CHRONOBEAM_DEMO_BROWSER" >/dev/null 2>&1 || return 1
        printf '%s\n' "$CHRONOBEAM_DEMO_BROWSER"
        return 0
    fi
    for candidate in google-chrome google-chrome-stable chromium chromium-browser; do
        if command -v "$candidate" >/dev/null 2>&1; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

open_personas() {
    local mode=${1:-}
    case "$mode" in
        windows | tabs) ;;
        *) usage ;;
    esac
    require_command curl

    local chrome urls=() spec role port subject
    chrome="$(chrome_bin)" ||
        fail "no Chrome/Chromium found; set CHRONOBEAM_DEMO_BROWSER to the browser binary."
    for spec in "${PERSONAS[@]}"; do
        IFS=: read -r role port subject <<<"$spec"
        [[ "$(identity_field "$port" subject || true)" == "$subject" ]] ||
            fail "${role} is not up on :${port} as '${subject}' — run 'just demo' first."
        urls+=("http://127.0.0.1:${port}")
    done

    # Chrome must not block the shell when it is not already running, and the
    # script never signals the browser: only these launches are started, and
    # `demo-down` leaves every browser process alone.
    if [[ "$mode" == "tabs" ]]; then
        "$chrome" --new-window "${urls[@]}" >/dev/null 2>&1 &
    else
        for url in "${urls[@]}"; do
            "$chrome" --new-window "$url" >/dev/null 2>&1 &
        done
    fi
    disown -a 2>/dev/null || true
    echo "Opened ${#urls[@]} persona ${mode}: ${urls[*]}"
}

# --- down ------------------------------------------------------------------

down() {
    local names=(backend) spec role stopped=0 name pid
    for spec in "${PERSONAS[@]}"; do
        IFS=: read -r role _ _ <<<"$spec"
        names+=("${role,,}")
    done
    for name in "${names[@]}"; do
        if pid="$(alive_pid "$name")"; then
            kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
            stopped=$((stopped + 1))
        fi
        rm -f "${PID_DIR}/${name}.pid"
    done
    echo "Stopped ${stopped} demo server(s) started by this script."
    echo "Servers started elsewhere (e.g. 'just dev') and the database container are left running."
}

case "${1:-}" in
    up) up ;;
    open) open_personas "${2:-}" ;;
    down) down ;;
    *) usage ;;
esac