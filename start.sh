#!/usr/bin/env bash
# VisualWeaver — start the installed stores (the dedicated local Redis and/or the
# built-in MongoDB), then the app.
#
# Idempotent: whatever is already running is left alone. The app binds to
# all interfaces (0.0.0.0) by default and warns so on every start — there is no
# built-in auth, so on an untrusted network set VISUALWEAVER_HOST=127.0.0.1; do NOT
# expose the port to a network without a reverse proxy / auth in front. Override
# with VISUALWEAVER_HOST / VISUALWEAVER_PORT, or a bare port as the first argument.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/common.sh
source "${SCRIPT_DIR}/scripts/common.sh"

[ -n "${1:-}" ] && APP_PORT="$1"   # optional: ./start.sh 9000

mkdir -p "${LOG_DIR}"

# ── Preconditions ─────────────────────────────────────────────────────────────
STORE_REDIS=0; have_redis && STORE_REDIS=1
STORE_MONGO=0; have_mongo && STORE_MONGO=1
if [ "${STORE_REDIS}" = 0 ] && [ "${STORE_MONGO}" = 0 ]; then
  c_err "No storage installed for ${DATA_DIR} (no Redis config, no built-in MongoDB) — run ./install.sh first."
  exit 1
fi
if [ ! -x "$(venv_python)" ] && [ ! -x "${REPO_DIR}/venv/bin/python" ]; then
  c_err "No virtualenv — run ./install.sh first."
  exit 1
fi
PY="${REPO_DIR}/venv/bin/python"
[ -x "${PY}" ] || PY="$(venv_python)"

# A store the app cannot work without (the built-in `default` connection, or
# the one holding app data) stops the start when it fails; the other one only
# warns, so e.g. a stopped container runtime does not keep a Redis-first
# install from starting.
read -r DEFAULT_TYPE _ <<<"$(storage_roles)"
APP_STORE="$(app_store_type)"
store_required() { [ "${DEFAULT_TYPE}" = "$1" ] || [ "${APP_STORE}" = "$1" ]; }
store_failed() {   # $1 = redis|mongodb, $2 = what is unavailable without it
  if store_required "$1"; then
    local what="Redis"; [ "$1" = mongodb ] && what="MongoDB"
    if [ "${DEFAULT_TYPE}" = "$1" ]; then
      c_err "The built-in ${what} is VisualWeaver's main storage, so the app cannot start without it."
    else
      c_err "VisualWeaver keeps its app data (conversations, the answer cache) on the built-in ${what},"
      c_err "so the app cannot start without it."
    fi
    c_err "Fix the cause above and run ./start.sh again."
    exit 1
  fi
  c_warn "Continuing without it: $2 until it runs (./start.sh again starts it)."
}

# ── 1. Redis ──────────────────────────────────────────────────────────────────
REDIS_OK=0
if [ "${STORE_REDIS}" = 1 ]; then
  RPORT="$(redis_port)"
  mkdir -p "${REDIS_DIR}" "${REDIS_DATA}"
  if ! load_redis_env; then
    c_err "Redis not provisioned (no ${REDIS_ENV_FILE}) — run ./install.sh first."
    store_failed redis "RAG instances on the built-in Redis are unavailable"
  elif redis_up "${RPORT}"; then
    REDIS_OK=1
  else
    store_failed redis "RAG instances on the built-in Redis are unavailable"
  fi
  # Confirm the Query Engine (search) is loaded — the app needs FT.* / vectors.
  if [ "${REDIS_OK}" = 1 ]; then
    if rcli -p "${RPORT}" FT._LIST >/dev/null 2>&1; then
      c_ok "Query Engine (search) available."
    else
      c_warn "Redis is up but the search module did not load — FT.* will fail."
      c_warn "Re-run ./install.sh (it locates and loads redisearch.so)."
    fi
  fi
fi

# ── 1b. Built-in MongoDB ──────────────────────────────────────────────────────
# `local_mongo up` starts the containers (or finds them running) and returns
# once mongod accepts the app user AND search indexes can be managed. On a
# failure its message says why: the runtime is not running (and how to start
# it), or the containers' last log lines.
MONGO_OK=0
MONGO_RUNTIME_OK=1
MPORT=""
if [ "${STORE_MONGO}" = 1 ]; then
  MPORT="$(mongo_port)"
  # The container runtime is started here when it is installed but stopped (after a
  # reboot, typically), and waited for; stop.sh stops it again if it was started here
  # and nothing else runs in it.
  c_info "Checking the container runtime for the built-in MongoDB (starting it if needed)…"
  _rt="$(lm ensure-runtime 2>&1)" || MONGO_RUNTIME_OK=0
  if [ "${MONGO_RUNTIME_OK}" = 1 ] && [ "$(printf '%s' "${_rt}" | json_field started)" = "True" ]; then
    c_ok "Started $(printf '%s' "${_rt}" | json_field name) (container runtime) in $(printf '%s' "${_rt}" | json_field seconds) s."
  fi
fi
if [ "${STORE_MONGO}" = 1 ] && [ "${MONGO_RUNTIME_OK}" = 0 ]; then
  c_err "The built-in MongoDB cannot start: its container runtime is not available."
  printf '%s\n' "${_rt}" | tail -n 5 >&2
  store_failed mongodb "RAG instances on the built-in MongoDB are unavailable"
elif [ "${STORE_MONGO}" = 1 ]; then
  # Another program may have taken the port since the install: move to the next free
  # one and point the app's connection at it (before the app starts and reads it).
  _avoid=(--avoid "${APP_PORT}"); [ -n "${RPORT:-}" ] && _avoid+=(--avoid "${RPORT}")
  _pj="$(lm ensure-port "${_avoid[@]}" --config "${DATA_DIR}/config.json" 2>&1)" || _pj=""
  _from="$(printf '%s' "${_pj}" | json_field moved_from)"
  if [ -n "${_from}" ]; then
    _holder="$(printf '%s' "${_pj}" | json_field holder)"
    c_warn "MongoDB port ${_from} is in use by ${_holder:-another program} — using $(printf '%s' "${_pj}" | json_field port) (recorded; the app follows it)."
  fi
  MPORT="$(mongo_port)"
  c_info "Starting the built-in MongoDB on 127.0.0.1:${MPORT} (containers)…"
  _rc=0
  _out="$(lm up 2>&1)" || _rc=$?
  if [ "${_rc}" = 0 ]; then
    _secs="$(printf '%s' "${_out}" | sed -nE 's/.*"total_seconds":[[:space:]]*([0-9.]+).*/\1/p' | tail -n1)"
    if [ "$(printf '%s' "${_out}" | json_field already_running)" = "True" ]; then
      c_info "MongoDB already running, vector search ready."
    else
      c_ok "MongoDB up, vector search ready (${_secs:-?} s)."
    fi
    _dw="$(printf '%s' "${_out}" | json_field disk_warning)"
    [ -n "${_dw}" ] && [ "${_dw}" != "None" ] && c_warn "${_dw}"
    MONGO_OK=1
  else
    c_err "The built-in MongoDB did not start:"
    printf '%s\n' "${_out}" | tail -n 30 >&2
    store_failed mongodb "RAG instances on the built-in MongoDB are unavailable"
  fi
fi

# ── 2. App ────────────────────────────────────────────────────────────────────
if pid_alive "${APP_PID}"; then
  c_info "App already running (pid $(cat "${APP_PID}")) — leaving it."
elif port_in_use "${APP_PORT}"; then
  # Identify the squatter before shrugging: an instance started by hand (no
  # pidfile) is still OURS, and pretending "another process" has the port while
  # the banner below announces the repo version made a failed swap look like a
  # successful one (a stale 1.5.0 kept serving under a "v1.7.0 is running" banner).
  _live="$(curl -fsS -m 2 "http://127.0.0.1:${APP_PORT}/api/health" 2>/dev/null || true)"
  if printf '%s' "${_live}" | grep -q '"app" *: *"VisualWeaver"'; then
    _lv="$(printf '%s' "${_live}" | sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p')"
    c_warn "Port ${APP_PORT} already serves VisualWeaver v${_lv:-?} — an instance these scripts don't track (no pidfile)."
    c_warn "Not starting a second app. Run ./restart.sh (stop.sh now finds and stops port-squatting instances too)."
  else
    c_err "Port ${APP_PORT} is in use by something that is NOT VisualWeaver — not starting."
    exit 1
  fi
else
  c_info "Starting VisualWeaver on http://${APP_HOST}:${APP_PORT}…"
  # VISUALWEAVER_STORAGE in .visualweaver.env records install.sh's choice; the
  # app reads it as the container deployment's storage switch, so it is not
  # passed on here — config.json describes a local install.
  [ -n "${VISUALWEAVER_MONGODB_URI:-}" ] || unset VISUALWEAVER_STORAGE
  # Likewise VISUALWEAVER_REDIS_HOST/PORT would give a MongoDB-first install a
  # built-in Redis it does not have.
  [ "${DEFAULT_TYPE}" = "mongodb" ] && unset VISUALWEAVER_REDIS_HOST VISUALWEAVER_REDIS_PORT
  # --app-dir puts the repo on sys.path so `main:app` imports without a cd — and
  # without a wrapping subshell, so $! is the uvicorn process itself. (The subshell
  # form captured the wrapper's PID, so stop.sh killed the wrapper and orphaned the
  # real server.) After the package split this becomes the `visualweaver` console script.
  # --no-proxy-headers: the sign-in check (auth.trusted_proxies) must see each connection's
  # real peer address, not one taken from an X-Forwarded-For header.
  _log_off="$( [ -f "${APP_LOG}" ] && wc -c <"${APP_LOG}" | tr -d ' ' || echo 0)"
  VISUALWEAVER_HOST="${APP_HOST}" VISUALWEAVER_PORT="${APP_PORT}" nohup "${PY}" -m uvicorn visualweaver.main:app \
    --app-dir "${REPO_DIR}" --host "${APP_HOST}" --port "${APP_PORT}" --no-proxy-headers >>"${APP_LOG}" 2>&1 &
  echo $! > "${APP_PID}"
  # Poll /api/health until ready.
  up=0
  for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:${APP_PORT}/api/health" >/dev/null 2>&1; then up=1; break; fi
    if ! pid_alive "${APP_PID}"; then
      c_err "App process exited during startup. Last log lines:"
      tail -n 25 "${APP_LOG}" >&2 || true
      exit 1
    fi
    sleep 1
  done
  if [ "${up}" != "1" ]; then
    c_err "App did not become healthy in time. Last log lines:"
    tail -n 25 "${APP_LOG}" >&2 || true
    exit 1
  fi
  c_ok "App up (pid $(cat "${APP_PID}"))."
fi

# ── Status banner ─────────────────────────────────────────────────────────────
# Version: ask the RUNNING server what it is — the repo file only says what the
# next start would serve, and quoting it after a skipped start announced a
# version that was not actually running. File value is the fallback only.
_health="$(curl -fsS -m 2 "http://127.0.0.1:${APP_PORT}/api/health" 2>/dev/null || true)"
APP_VERSION="$(printf '%s' "${_health}" | sed -n 's/.*"version" *: *"\([^"]*\)".*/\1/p')"
[ -z "${APP_VERSION}" ] && APP_VERSION="$(sed -n 's/^__version__ *= *["'"'"']\(.*\)["'"'"']/\1/p' "${REPO_DIR}/visualweaver/__init__.py" 2>/dev/null)"
[ -z "${APP_VERSION}" ] && APP_VERSION="?"

# Best-effort primary LAN IPv4, for the case where the app is bound to all interfaces.
_lan_ip() {
  local ip=""
  if command -v ipconfig >/dev/null 2>&1; then
    for _if in en0 en1 en2 en3; do ip="$(ipconfig getifaddr "${_if}" 2>/dev/null || true)"; [ -n "${ip}" ] && break; done
  fi
  [ -z "${ip}" ] && command -v hostname >/dev/null 2>&1 && ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  printf '%s' "${ip}"
}

# Every URL the UI can be reached at, given the bind host.
APP_URLS=()
case "${APP_HOST}" in
  0.0.0.0|::|"")
    APP_URLS+=("http://localhost:${APP_PORT}")
    _ip="$(_lan_ip)"; [ -n "${_ip}" ] && APP_URLS+=("http://${_ip}:${APP_PORT}  (LAN)")
    ;;
  127.0.0.1|localhost)
    APP_URLS+=("http://127.0.0.1:${APP_PORT}" "http://localhost:${APP_PORT}")
    ;;
  *)
    APP_URLS+=("http://${APP_HOST}:${APP_PORT}")
    ;;
esac

c_info ""
c_ok "══ VisualWeaver v${APP_VERSION} is running ══"
c_info "  Web UI:   ${APP_URLS[0]}"
for _u in "${APP_URLS[@]:1}"; do c_info "            ${_u}"; done
_role() { [ "${APP_STORE}" = "$1" ] && printf ' — app data & answer cache'; return 0; }
if [ "${STORE_REDIS}" = 1 ]; then
  if [ "${REDIS_OK}" = 1 ]; then c_info "  Redis:    127.0.0.1:${RPORT} (built-in, AOF everysec)$(_role redis)"
  else c_warn "  Redis:    NOT running (127.0.0.1:${RPORT})"; fi
fi
if [ "${STORE_MONGO}" = 1 ]; then
  if [ "${MONGO_OK}" = 1 ]; then c_info "  MongoDB:  127.0.0.1:${MPORT} (built-in, vector search, containers)$(_role mongodb)"
  else c_warn "  MongoDB:  NOT running (127.0.0.1:${MPORT})"; fi
fi
# Versions in use: the app as it reports itself, its Python, Redis and its search
# module as the running server reports them, MongoDB's server version as it
# reports it, and MongoDB Search's from the pinned (digest-locked) image.
_pyver="$("${PY}" -c 'import platform; print(platform.python_version())' 2>/dev/null || echo '?')"
c_info "  Versions: VisualWeaver ${APP_VERSION} · Python ${_pyver}"
if [ "${STORE_REDIS}" = 1 ] && [ "${REDIS_OK}" = 1 ]; then
  read -r _rv _sv <<<"$(redis_versions "${RPORT}")"
  c_info "            Redis ${_rv} · search ${_sv}"
fi
if [ "${STORE_MONGO}" = 1 ] && [ "${MONGO_OK}" = 1 ]; then
  _mv="$(printf '%s' "${_out}" | json_field version)"; [ -n "${_mv}" ] || _mv="$(printf '%s' "${_out}" | json_field server)"
  _ms="$(printf '%s' "${_out}" | json_field search)"
  _rt="$(printf '%s' "${_out}" | json_field runtime) $(printf '%s' "${_out}" | json_field runtime_version)"
  c_info "            MongoDB ${_mv:-?} · search ${_ms:-?} · ${_rt% }"
fi
c_info "  App log:  ${APP_LOG}"
[ "${STORE_REDIS}" = 1 ] && c_info "  Redis log:${REDIS_LOG}"
if [ "${STORE_MONGO}" = 1 ]; then
  _lmenv=""; [ -n "${VISUALWEAVER_DATA_DIR:-}" ] && _lmenv="VISUALWEAVER_DATA_DIR=\"${DATA_DIR}\" "
  c_info "  MongoDB log: cd \"${REPO_DIR}\" && ${_lmenv}venv/bin/python -m visualweaver.local_mongo logs"
fi
c_info "  Stop:     ${REPO_DIR}/stop.sh"
# Sign-in through a reverse proxy (auth in config.json / VISUALWEAVER_AUTH): without the
# proxy's header /api/me answers 401 when it is on, 503 when its settings are broken.
SIGNIN=off
case "$(curl -s -o /dev/null -m 2 -w '%{http_code}' "http://127.0.0.1:${APP_PORT}/api/me" 2>/dev/null)" in
  401) SIGNIN=on ;;
  503) SIGNIN=broken ;;
esac
if [ "${SIGNIN}" != off ]; then
  if [ "${SIGNIN}" = broken ]; then
    c_err "  Sign-in:  MISCONFIGURED — every request is refused until the auth settings are fixed (see the app log)."
    # /api/health still answers then: "status": "misconfigured" and the reason.
    _why="$(printf '%s' "${_health}" | sed -n 's/.*"detail" *: *"\([^"]*\)".*/\1/p')"
    [ -n "${_why}" ] && c_err "            ${_why}"
  else
    c_info "  Sign-in:  through your reverse proxy (trusted header) — open VisualWeaver at the proxy's address."
  fi
  # The app's own checks, from this start's log lines (bind address vs trusted proxies, no admins).
  if [ -n "${_log_off:-}" ]; then
    tail -c +"$((_log_off + 1))" "${APP_LOG}" 2>/dev/null | sed -n 's/.*SIGN-IN WARNING: //p' \
      | while IFS= read -r _l; do c_warn "  WARNING: ${_l}"; done
  fi
fi
# The guard for an unauthenticated service on all interfaces: say exactly which
# address other devices can reach, and how to restrict it, every single start.
if [ "${SIGNIN}" = off ]; then case "${APP_HOST}" in
  0.0.0.0|::|"")
    c_warn "  NOTE: reachable from other devices on your network at ${APP_URLS[1]:-the LAN address above} — VisualWeaver has NO built-in auth."
    c_warn "        On an untrusted network run it localhost-only:  VISUALWEAVER_HOST=127.0.0.1 ./start.sh"
    c_warn "        (or run ./changeAccess.sh local to make that permanent)."
    ;;
  127.0.0.1|localhost)
    c_info "  Only reachable from this machine (VISUALWEAVER_HOST=${APP_HOST}); run ./changeAccess.sh lan for LAN access."
    ;;
  *)
    c_warn "  NOTE: bound to ${APP_HOST} — VisualWeaver has NO built-in auth; put a reverse proxy with auth in front before exposing it."
    ;;
esac; fi
