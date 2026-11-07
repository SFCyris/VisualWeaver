#!/usr/bin/env bash
# Shared helpers for VisualWeaver's install/start/stop/restart scripts.
# Sourced by install.sh, start.sh, stop.sh and changeAccess.sh — defines paths and
# helper functions only; performs no actions on its own.

# ── Repo root (directory that contains the entry scripts) ─────────────────────
# BASH_SOURCE[0] is this file (scripts/common.sh); the repo root is its parent's
# parent.
_COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${_COMMON_DIR}/.." && pwd)"

# ── Platform-local data directory — MUST match visualweaver/config.py ───────────
#   macOS:  ~/Library/Application Support/VisualWeaver
#   Linux:  $XDG_DATA_HOME/visualweaver  or  ~/.local/share/visualweaver
#   other:  ~/.visualweaver
# Honors VISUALWEAVER_DATA_DIR (same env var the app reads).
data_dir() {
  if [ -n "${VISUALWEAVER_DATA_DIR:-}" ]; then
    printf '%s' "${VISUALWEAVER_DATA_DIR}"
    return
  fi
  case "$(uname -s)" in
    Darwin) printf '%s' "${HOME}/Library/Application Support/VisualWeaver" ;;
    Linux)  printf '%s' "${XDG_DATA_HOME:-${HOME}/.local/share}/visualweaver" ;;
    *)      printf '%s' "${HOME}/.visualweaver" ;;
  esac
}

DATA_DIR="$(data_dir)"

# ── Paths (all runtime state lives under DATA_DIR, never in the repo) ─────────
LOG_DIR="${DATA_DIR}/log"
APP_PID="${DATA_DIR}/visualweaver.pid"
APP_LOG="${LOG_DIR}/visualweaver.log"

REDIS_DIR="${DATA_DIR}/redis"
REDIS_CONF="${REDIS_DIR}/redis.conf"
REDIS_DATA="${REDIS_DIR}/data"
REDIS_PID="${REDIS_DIR}/redis.pid"
REDIS_LOG="${REDIS_DIR}/redis.log"

# ── Vendored Redis (repo-local, like .venv) ──────────────────────────────────
# install.sh puts a self-contained Redis 8 here and writes redis-env.sh with the
# resolved binary/module/launch-prefix; start.sh/stop.sh source it. On Linux the
# env may instead point at a reused system redis-server (see install.sh).
REDIS_HOME="${REPO_DIR}/.redis"
REDIS_VENDOR_LIB="${REDIS_HOME}/vendor-lib"
REDIS_ENV_FILE="${REDIS_HOME}/redis-env.sh"

# Source the resolved Redis environment (REDIS_SERVER / REDIS_CLI /
# REDIS_MODULE_SEARCH / REDIS_LAUNCH_PREFIX / REDIS_SOURCE). Returns non-zero if
# install.sh hasn't run yet.
load_redis_env() {
  [ -f "${REDIS_ENV_FILE}" ] || return 1
  # shellcheck disable=SC1090
  . "${REDIS_ENV_FILE}"
  return 0
}

# ── Built-in MongoDB (optional; see visualweaver/local_mongo.py) ──────────────
# Two official MongoDB Community containers (mongod + mongot) run by the container
# runtime already on the machine. install.sh writes state.json (port, generated
# passwords, licence acceptance); its presence means "MongoDB is installed".
MONGO_DIR="${DATA_DIR}/mongodb"
MONGO_STATE="${MONGO_DIR}/state.json"

# Which stores install.sh set up for THIS data dir. Redis is keyed on its
# redis.conf (DATA_DIR), not on .redis/redis-env.sh: the vendored Redis is per
# checkout and is shared by every data dir that checkout is pointed at.
have_redis() { [ -f "${REDIS_CONF}" ]; }
have_mongo() { [ -f "${MONGO_STATE}" ]; }

# visualweaver.local_mongo from THIS checkout, against THIS data dir. LM_PY may
# name the interpreter (install.sh uses the system python3 before the venv exists).
lm() {
  local py="${LM_PY:-}"
  [ -n "${py}" ] || py="$(venv_python)"
  PYTHONPATH="${REPO_DIR}${PYTHONPATH:+:${PYTHONPATH}}" VISUALWEAVER_DATA_DIR="${DATA_DIR}" \
    "${py}" -m visualweaver.local_mongo "$@"
}

# The built-in MongoDB's port, from state.json (empty if not installed).
mongo_port() {
  [ -f "${MONGO_STATE}" ] || return 0
  sed -nE 's/^[[:space:]]*"port":[[:space:]]*([0-9]+).*/\1/p' "${MONGO_STATE}" | head -n1
}

# "<default_type> <app> <app_builtin_type>" from config.json: which store the
# built-in `default` connection is, which connection holds app data + the answer
# cache, and — when that connection is one of the installed stores — its type
# ("-" for a connection the user added).
storage_roles() {
  local py; py="$(venv_python)"
  "${py}" -c 'import json, sys
try:
    c = json.load(open(sys.argv[1]))
except Exception:
    c = {}
s = c.get("storage") if isinstance(c.get("storage"), dict) else {}
dt, app = s.get("default_type") or "redis", s.get("app") or "default"
kind = dt if app == "default" else next(
    (e.get("type") or "redis" for e in c.get("redis_endpoints") or []
     if isinstance(e, dict) and e.get("name") == app and e.get("builtin")), "-")
print(dt, app, kind)' "${DATA_DIR}/config.json" 2>/dev/null \
    || echo "redis default redis"
}

# The installed store ("redis" | "mongodb") that holds app data, or "" when it is a
# connection the user added.
app_store_type() {
  local dt app kind
  read -r dt app kind <<<"$(storage_roles)"
  [ "${kind}" = "-" ] && kind=""
  printf '%s' "${kind}"
}

# redis-cli wrapper: prefer the resolved REDIS_CLI, else a redis-cli on PATH.
rcli() {
  if [ -n "${REDIS_CLI:-}" ] && [ -x "${REDIS_CLI}" ]; then
    "${REDIS_CLI}" "$@"
  else
    redis-cli "$@"
  fi
}

# ── Per-machine overrides (optional, gitignored) ───────────────────────────────
# .visualweaver.env lets you set persistent overrides without editing tracked files or
# re-typing env vars each run — e.g. VISUALWEAVER_HOST=127.0.0.1 to serve this machine only.
# Real environment variables still win (only unset ones are filled from the file).
if [ -f "${REPO_DIR}/.visualweaver.env" ]; then
  # `|| [ -n "${_k}" ]` keeps a last line that has no trailing newline.
  while IFS='=' read -r _k _v || [ -n "${_k}" ]; do
    _k="${_k#"${_k%%[![:space:]]*}"}"; _k="${_k%"${_k##*[![:space:]]}"}"   # trim key
    case "${_k}" in export[[:space:]]*) _k="${_k#export}"; _k="${_k#"${_k%%[![:space:]]*}"}" ;; esac
    case "${_k}" in ''|\#*) continue ;; esac                              # skip blanks / comments
    case "${_k}" in [!A-Za-z_]*|*[!A-Za-z0-9_]*) continue ;; esac          # not a variable name
    _v="${_v%$'\r'}"                                                      # CRLF line endings
    _v="${_v%\"}"; _v="${_v#\"}"                                          # strip optional quotes
    [ -z "${!_k:-}" ] && export "${_k}=${_v}"                             # real env vars win
  done < "${REPO_DIR}/.visualweaver.env"
fi

# ── Ports ─────────────────────────────────────────────────────────────────────
# App (web UI): default 8420, overridable via VISUALWEAVER_PORT.
APP_PORT="${VISUALWEAVER_PORT:-8420}"
# App bind host: ALL interfaces by default, so a fresh checkout is reachable from
# other devices on the LAN (a phone, a second machine, a VM's host) without a
# per-machine override. There is no built-in auth, so start.sh prints a warning
# naming the LAN address every time; VISUALWEAVER_HOST=127.0.0.1 restricts it to
# this machine only.
APP_HOST="${VISUALWEAVER_HOST:-0.0.0.0}"

# Dedicated Redis port for VisualWeaver's OWN instance. Default 6389 keeps clear
# of the standard 6379 so it never collides with a system/other Redis. The
# authoritative value is whatever install.sh wrote into redis.conf.
DEFAULT_REDIS_PORT=6389

# Read the Redis port that install.sh chose (from redis.conf), falling back to
# the app config, then the default.
redis_port() {
  if [ -f "${REDIS_CONF}" ]; then
    local p
    p="$(awk '/^[[:space:]]*port[[:space:]]+[0-9]+/ {print $2; exit}' "${REDIS_CONF}" 2>/dev/null)"
    if [ -n "${p}" ]; then printf '%s' "${p}"; return; fi
  fi
  if [ -f "${DATA_DIR}/config.json" ] && command -v python3 >/dev/null 2>&1; then
    local p
    p="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("redis",{}).get("port",""))' "${DATA_DIR}/config.json" 2>/dev/null)"
    if [ -n "${p}" ]; then printf '%s' "${p}"; return; fi
  fi
  printf '%s' "${DEFAULT_REDIS_PORT}"
}

# ── Small utilities ───────────────────────────────────────────────────────────
c_info()  { printf '\033[0;36m%s\033[0m\n' "$*"; }
c_ok()    { printf '\033[0;32m%s\033[0m\n' "$*"; }
c_warn()  { printf '\033[0;33m%s\033[0m\n' "$*" >&2; }
c_err()   { printf '\033[0;31m%s\033[0m\n' "$*" >&2; }

# Is a PID recorded in $1 alive?
pid_alive() {
  local f="$1" p
  [ -f "${f}" ] || return 1
  p="$(cat "${f}" 2>/dev/null || true)"
  [ -n "${p}" ] && kill -0 "${p}" 2>/dev/null
}

# Is anything listening on TCP port $1 (localhost)? lsof sees only this user's
# processes, so a bind test (no connection is made) also catches a port held by
# another user or a container runtime's forwarder. SO_REUSEADDR, as servers use
# it, so connections still in TIME_WAIT after a stop do not count as "in use";
# both the wildcard and the loopback address are tried, since a listener on one
# does not block a reuse-bind on the other.
port_in_use() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1 && return 0
  local py; py="$(venv_python 2>/dev/null || true)"
  [ -n "${py}" ] && [ -x "${py}" ] || return 1
  ! "${py}" -c 'import socket, sys
for host in ("0.0.0.0", "127.0.0.1"):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, int(sys.argv[1])))
    finally:
        s.close()' "$1" 2>/dev/null
}

# Find a free TCP port at/after $1 (used by install.sh if the default is taken),
# skipping any further ports given (ones another VisualWeaver service uses).
find_free_port() {
  local p="$1"; shift
  while port_in_use "${p}" || _port_listed "${p}" "$@"; do p=$((p + 1)); done
  printf '%s' "${p}"
}
_port_listed() { local p="$1" x; shift; for x in "$@"; do [ "${x}" = "${p}" ] && return 0; done; return 1; }

# What listens on TCP port $1, as "<command> (pid N)" — empty when lsof cannot tell.
port_holder() {
  local out pid cmd
  out="$(lsof -nP -iTCP:"$1" -sTCP:LISTEN -Fpc 2>/dev/null || true)"
  pid="$(printf '%s\n' "${out}" | sed -n 's/^p//p' | head -n1)"
  cmd="$(printf '%s\n' "${out}" | sed -n 's/^c//p' | head -n1)"
  [ -n "${pid}" ] && printf '%s (pid %s)' "${cmd:-?}" "${pid}"
  return 0
}

# Is the app port held by THIS install's app — the process in this data dir's
# pidfile, or one started by hand from this checkout (`--app-dir <this repo>`)?
# Such a port is "ours", not a conflict. Another checkout's VisualWeaver is not.
app_port_ours() {
  local p cmd
  p="$(lsof -tnP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | head -n1 || true)"
  [ -n "${p}" ] || return 1
  [ -f "${APP_PID}" ] && [ "$(cat "${APP_PID}" 2>/dev/null)" = "${p}" ] && return 0
  cmd="$(ps -p "${p}" -o command= 2>/dev/null || true)"
  case "${cmd}" in *"--app-dir ${REPO_DIR} "*|*"--app-dir ${REPO_DIR}") return 0 ;; esac
  return 1
}

# Set KEY=VALUE in .visualweaver.env: replace the line(s) if present (also an
# `export KEY=` line), else append (with an optional comment line). The file stays
# the per-machine override file; a last line without a newline is kept intact.
envfile_set() {
  local key="$1" val="$2" comment="${3:-}" f="${REPO_DIR}/.visualweaver.env"
  if grep -qE "^[[:space:]]*(export[[:space:]]+)?${key}=" "${f}" 2>/dev/null; then
    awk -v k="${key}" -v v="${val}" '$0 ~ "^[[:space:]]*(export[[:space:]]+)?" k "=" {print k "=" v; next} {print}' \
      "${f}" > "${f}.tmp" && mv "${f}.tmp" "${f}"
  else
    [ -s "${f}" ] && [ -n "$(tail -c1 "${f}")" ] && echo >> "${f}"
    { [ -n "${comment}" ] && echo "${comment}"; echo "${key}=${val}"; } >> "${f}"
  fi
}

# The value of KEY recorded in .visualweaver.env (first line wins, as above).
envfile_get() {
  sed -nE "s/^[[:space:]]*(export[[:space:]]+)?$1=[\"]?([^\"[:space:]]*).*/\2/p" \
    "${REPO_DIR}/.visualweaver.env" 2>/dev/null | head -n1
}

# Start the dedicated Redis on port $1 unless it already answers (needs
# load_redis_env). 0 = up; 1 = failed, with the reason and log tail printed.
redis_up() {
  local port="$1"
  if rcli -p "${port}" ping >/dev/null 2>&1; then
    c_info "Redis already responding on 127.0.0.1:${port}."
    return 0
  fi
  if [ -z "${REDIS_SERVER:-}" ] || { [ ! -x "${REDIS_SERVER}" ] && ! command -v "${REDIS_SERVER}" >/dev/null 2>&1; }; then
    c_err "Resolved redis-server '${REDIS_SERVER:-}' is missing — re-run ./install.sh."
    return 1
  fi
  c_info "Starting dedicated Redis on 127.0.0.1:${port} (${REDIS_SOURCE:-local})…"
  # Launch DIRECTLY (not via nohup): macOS SIP strips DYLD_* when exec'ing the
  # protected /usr/bin/nohup, which would break the vendored libunwind shim.
  # Redis self-daemonizes (daemonize yes) and writes its own pidfile; the log
  # goes to the logfile set in the conf. DYLD_FALLBACK_LIBRARY_PATH points at
  # the vendored libunwind shim on macOS; it is empty (and ignored) on Linux.
  if ! DYLD_FALLBACK_LIBRARY_PATH="${REDIS_DYLD_FALLBACK:-}" "${REDIS_SERVER}" "${REDIS_CONF}"; then
    c_err "redis-server failed to launch. Last log lines:"; tail -n 20 "${REDIS_LOG}" >&2 || true
    return 1
  fi
  # Wait for it to accept connections (a module-load failure aborts the daemon,
  # so the ping never succeeds and the log is shown).
  local _
  for _ in $(seq 1 30); do
    if rcli -p "${port}" ping >/dev/null 2>&1; then
      c_ok "Redis up (pid $(cat "${REDIS_PID}" 2>/dev/null || echo '?'))."
      return 0
    fi
    sleep 0.5
  done
  c_err "Redis did not come up (search module may have failed to load). Last log lines:"
  tail -n 20 "${REDIS_LOG}" >&2 || true
  return 1
}

# Is the Redis answering on $1 THIS data dir's own (its `dir` is REDIS_DATA)?
# Redis reports the directory it changed into, so both sides are compared as
# physical paths (a data dir given with `..` or through a symlink still matches).
redis_is_ours() {
  local d
  d="$(rcli -p "$1" CONFIG GET dir 2>/dev/null | tail -n1 | tr -d '\r')"
  [ -n "${d}" ] && [ -d "${REDIS_DATA}" ] \
    && [ "$(cd "${d}" 2>/dev/null && pwd -P)" = "$(cd "${REDIS_DATA}" && pwd -P)" ]
}

# "<redis_version> <search module version>" of the Redis on port $1 ("?" when unknown).
redis_versions() {
  local v s i
  # A Redis that was just started answers INFO while it loads its data but refuses
  # MODULE LIST ("LOADING …") until the data is in memory: wait for that (up to 60 s).
  for i in $(seq 1 60); do
    rcli -p "$1" INFO persistence 2>/dev/null | tr -d '\r' | grep -q '^loading:1' || break
    sleep 1
  done
  v="$(rcli -p "$1" INFO server 2>/dev/null | tr -d '\r' | sed -nE 's/^redis_version:([^[:space:]]+).*/\1/p' | head -n1)"
  # MODULE LIST (raw): name / search / ver / 80800 / … — the search module is
  # "search" in Redis 8 and "ft" in older stacks; its version is MMmmpp.
  s="$(rcli -p "$1" MODULE LIST 2>/dev/null | tr -d '\r' | awk '
    prev == "name" { m = $0 }
    prev == "ver" && (m == "search" || m == "ft") && !done { n = $0 + 0; printf "%d.%d.%d", n / 10000, (n / 100) % 100, n % 100; done = 1 }
    { prev = $0 }')" || true
  printf '%s %s' "${v:-?}" "${s:-?}"
}

# One field of the last JSON line on stdin (local_mongo's output), empty if absent.
json_field() {
  "$(venv_python)" -c 'import json, sys
lines = [l for l in sys.stdin.read().splitlines() if l.strip().startswith("{")]
try:
    v = json.loads(lines[-1]).get(sys.argv[1]) if lines else None
except Exception:
    v = None
print("" if v is None else v)' "$1" 2>/dev/null || true
}

# The venv python, if the venv exists; else system python3.
venv_python() {
  if [ -x "${REPO_DIR}/venv/bin/python" ]; then
    printf '%s' "${REPO_DIR}/venv/bin/python"
  elif [ -x "${REPO_DIR}/.venv/bin/python" ]; then
    printf '%s' "${REPO_DIR}/.venv/bin/python"
  else
    command -v python3 || command -v python
  fi
}
