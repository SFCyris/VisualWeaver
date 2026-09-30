#!/usr/bin/env bash
# VisualWeaver — local install / setup.
#
# Sets up everything needed to run VisualWeaver on this machine:
#   1. A Python virtual environment with the app's dependencies.
#   2. A DEDICATED Redis with the Query Engine (search) that redisvl needs,
#      running on its OWN loopback port — never touching the system Redis.
#        • macOS: a self-contained Redis 8 vendored into ./.redis (like .venv).
#        • Linux: reuse an existing redis-with-search, else apt-install Redis 8.
#   3. A redis.conf with AOF persistence (1-second fsync) + RDB snapshots.
#   4. The chosen Redis port written into the app config.
#
# Re-runnable (idempotent). Runtime state lives under the platform data dir; the
# vendored Redis lives in ./.redis (gitignored). Env knobs:
#   VISUALWEAVER_DATA_DIR             override the data directory
#   VISUALWEAVER_REDIS_PORT           force a specific Redis port (default 6389)
#   PYTHON                            interpreter for the venv (default: newest supported python3.x found)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/common.sh
source "${SCRIPT_DIR}/scripts/common.sh"

OS="$(uname -s)"
ARCH="$(uname -m)"   # arm64 / x86_64 (macOS); x86_64 / aarch64 (Linux)

# Pinned Redis 8 (macOS self-contained zip from the official channel the redis
# cask uses). Bump VER + the checksums to upgrade.
REDIS_OSS_VER="8.8.1"
REDIS_OSS_SHA_arm64="eea7fe4452915b7540eeabb50cab7215170f2a31d46aa613cee94810d5ee918a"
REDIS_OSS_SHA_x86_64="0121e0b2faa66ed68771b9f5258d67ad7f7da8b74d256a740b8942719c2d9bd3"

c_info "══ VisualWeaver install ══"
c_info "Repo:      ${REPO_DIR}"
c_info "Data dir:  ${DATA_DIR}"
c_info "Platform:  ${OS} ${ARCH}"
mkdir -p "${DATA_DIR}" "${LOG_DIR}" "${REDIS_DIR}" "${REDIS_DATA}" "${REDIS_HOME}"

# ── 1. Python virtual environment + dependencies ─────────────────────────────
c_info ""
c_info "── Python environment ──"
# The Python versions the pinned dependencies ship binary wheels for. PyMuPDF
# 1.24.3 publishes wheels for CPython 3.8–3.12 only; on anything newer pip falls
# back to compiling all of MuPDF from source, which fails on current macOS SDKs
# (an old bundled zlib mis-detects the platform) and takes minutes to fail on
# Linux. A fresh Homebrew/apt install now hands out 3.13 or 3.14, so pick the
# interpreter here instead of trusting whatever `python3` is first on PATH.
PY_MIN_MINOR=11
PY_MAX_MINOR=12
py_minor() { "$1" -c 'import sys; print(sys.version_info[1] if sys.version_info[0]==3 else 0)' 2>/dev/null || echo 0; }
py_supported() { local m; m="$(py_minor "$1")"; [ "${m}" -ge "${PY_MIN_MINOR}" ] && [ "${m}" -le "${PY_MAX_MINOR}" ]; }
py_install_hint() {
  if [ "${OS}" = "Darwin" ]; then echo "brew install python@3.${PY_MAX_MINOR}";
  else echo "sudo apt-get install -y python3.${PY_MAX_MINOR} python3.${PY_MAX_MINOR}-venv   (or your distro's equivalent)"; fi
}
SYS_PY=""
if [ -n "${PYTHON:-}" ]; then
  # An explicit choice is honoured but still checked, so a typo cannot start a
  # source build.
  command -v "${PYTHON}" >/dev/null 2>&1 || { c_err "PYTHON=${PYTHON} is not an executable."; exit 1; }
  py_supported "${PYTHON}" || { c_err "PYTHON=${PYTHON} is Python 3.$(py_minor "${PYTHON}"); this release needs 3.${PY_MIN_MINOR}–3.${PY_MAX_MINOR}."; exit 1; }
  SYS_PY="${PYTHON}"
else
  # Newest supported first; a Homebrew python@3.x lands in brew's prefix, which
  # may not be on PATH yet on a brand-new machine.
  candidates=()
  for m in $(seq "${PY_MAX_MINOR}" -1 "${PY_MIN_MINOR}"); do
    candidates+=("python3.${m}")
    [ "${OS}" = "Darwin" ] && command -v brew >/dev/null 2>&1 && candidates+=("$(brew --prefix 2>/dev/null)/opt/python@3.${m}/bin/python3.${m}")
  done
  candidates+=("python3")
  for c in "${candidates[@]}"; do
    if command -v "${c}" >/dev/null 2>&1 && py_supported "${c}"; then SYS_PY="$(command -v "${c}")"; break; fi
  done
fi
if [ -z "${SYS_PY}" ]; then
  found="$(command -v python3 2>/dev/null || true)"
  if [ -n "${found}" ]; then
    c_err "Found ${found} (Python 3.$(py_minor "${found}")), but this release needs Python 3.${PY_MIN_MINOR}–3.${PY_MAX_MINOR}:"
    c_err "its pinned PyMuPDF has no prebuilt wheel for newer Pythons and would try to compile MuPDF from source."
  else
    c_err "python3 not found."
  fi
  c_err "Install a supported version and re-run:   $(py_install_hint)"
  c_err "Or point at one explicitly:              PYTHON=/path/to/python3.${PY_MAX_MINOR} ./install.sh"
  exit 1
fi
c_info "Using $(command -v "${SYS_PY}") (Python 3.$(py_minor "${SYS_PY}"))"
VENV="${REPO_DIR}/venv"
# A venv left behind by an earlier run on an unsupported Python (the source-build
# failure) must not be reused: it has no PyMuPDF and its pip would repeat the
# failure. Rebuild it on the interpreter chosen above.
if [ -x "${VENV}/bin/python" ] && ! py_supported "${VENV}/bin/python"; then
  c_warn "Existing venv runs Python 3.$(py_minor "${VENV}/bin/python") (unsupported) — recreating it on ${SYS_PY}."
  rm -rf "${VENV}"
fi
[ -x "${VENV}/bin/python" ] || { c_info "Creating virtualenv at ${VENV}"; "${SYS_PY}" -m venv "${VENV}"; }
c_info "Installing Python dependencies…"
"${VENV}/bin/python" -m pip install --upgrade pip >/dev/null
if [ -f "${REPO_DIR}/pyproject.toml" ]; then
  "${VENV}/bin/python" -m pip install -e "${REPO_DIR}" >/dev/null
else
  "${VENV}/bin/python" -m pip install -r "${REPO_DIR}/requirements.txt" >/dev/null
fi
c_ok "Python dependencies installed."

# ── 2. Provision a Redis with the search module ──────────────────────────────
c_info ""
c_info "── Redis (with Query Engine) ──"

# These get resolved by the platform branch below, then written to redis-env.sh.
REDIS_SERVER=""; REDIS_CLI=""; REDIS_MODULE_SEARCH=""; REDIS_DYLD_FALLBACK=""; REDIS_SOURCE=""

# Locate redisearch.so beside a redis-server (Linux reuse / apt install).
find_search_module() {
  local rs; rs="$(command -v redis-server 2>/dev/null || true)"
  local dirs=()
  [ -n "${rs}" ] && dirs+=("$(cd "$(dirname "${rs}")/.." 2>/dev/null && pwd)/lib")
  command -v brew >/dev/null 2>&1 && dirs+=("$(brew --prefix 2>/dev/null)/lib/redis/modules")
  dirs+=(/usr/lib/redis/modules /var/lib/redis/modules /opt/redis-stack/lib)
  local d
  for d in "${dirs[@]}"; do [ -f "${d}/redisearch.so" ] && { printf '%s' "${d}/redisearch.so"; return 0; }; done
  return 1
}
redis_major() { redis-server --version 2>/dev/null | sed -nE 's/.*v=([0-9]+).*/\1/p'; }

if [ "${OS}" = "Darwin" ]; then
  # ── macOS: vendor a self-contained Redis 8 into ./.redis ───────────────────
  command -v brew >/dev/null 2>&1 || { c_err "Homebrew is required on macOS (for openssl@3): https://brew.sh"; exit 1; }

  # (a) openssl@3 — a fundamental lib the prebuilt binaries link against.
  if ! [ -d "$(brew --prefix)/opt/openssl@3" ]; then
    c_info "Installing openssl@3 (system library the Redis binaries need)…"
    brew install openssl@3 >/dev/null
  fi
  c_ok "openssl@3 present."

  # (b) Vendor the official Redis 8 zip (checksum-pinned) into ./.redis/redis-oss.
  sha_var="REDIS_OSS_SHA_${ARCH}"; EXPECT_SHA="${!sha_var:-}"
  [ -n "${EXPECT_SHA}" ] || { c_err "No pinned checksum for arch '${ARCH}'."; exit 1; }
  DIST="${REDIS_HOME}/redis-oss"
  if [ -x "${DIST}/bin/redis-server" ] && "${DIST}/bin/redis-server" --version 2>/dev/null | grep -q "v=${REDIS_OSS_VER}"; then
    c_ok "Redis ${REDIS_OSS_VER} already vendored in .redis/."
  else
    local_url="https://packages.redis.io/homebrew/redis-oss-${REDIS_OSS_VER}-${ARCH}.zip"
    c_info "Downloading self-contained Redis ${REDIS_OSS_VER} (${ARCH})…"
    ZIP="${REDIS_HOME}/redis-oss.zip"
    curl -fsSL -o "${ZIP}" "${local_url}"
    GOT_SHA="$(shasum -a 256 "${ZIP}" | awk '{print $1}')"
    [ "${GOT_SHA}" = "${EXPECT_SHA}" ] || { c_err "Checksum mismatch for redis-oss zip (got ${GOT_SHA})."; exit 1; }
    rm -rf "${DIST}"; mkdir -p "${DIST}"
    unzip -q "${ZIP}" -d "${DIST}"; rm -f "${ZIP}"
    c_ok "Redis ${REDIS_OSS_VER} vendored (checksum verified)."
  fi

  # (c) 16KB libunwind stub — redisearch.so links llvm's libunwind.1.dylib, but
  # needs only the standard _Unwind_* ABI, which libSystem provides. A tiny
  # re-export stub satisfies it (no llvm / no 400MB download).
  mkdir -p "${REDIS_VENDOR_LIB}"
  if ! [ -f "${REDIS_VENDOR_LIB}/libunwind.1.dylib" ]; then
    c_info "Building libunwind shim (search-module dependency)…"
    cc -nostdlib -dynamiclib -o "${REDIS_VENDOR_LIB}/libunwind.1.dylib" \
       -install_name libunwind.1.dylib -Wl,-reexport-lSystem \
       || { c_err "Could not build the libunwind shim (need Xcode Command Line Tools: xcode-select --install)."; exit 1; }
  fi
  c_ok "libunwind shim ready."

  REDIS_SERVER="${DIST}/bin/redis-server"
  REDIS_CLI="${DIST}/bin/redis-cli"
  REDIS_MODULE_SEARCH="${DIST}/lib/redis/modules/redisearch.so"
  REDIS_DYLD_FALLBACK="${REDIS_VENDOR_LIB}"
  REDIS_SOURCE="vendored Redis ${REDIS_OSS_VER} (.redis/)"

elif [ "${OS}" = "Linux" ]; then
  # ── Linux: reuse an existing redis-with-search, else apt-install Redis 8 ────
  if command -v redis-server >/dev/null 2>&1 && MOD="$(find_search_module)"; then
    MAJ="$(redis_major)"
    REDIS_SERVER="$(command -v redis-server)"; REDIS_CLI="$(command -v redis-cli)"
    REDIS_MODULE_SEARCH="${MOD}"; REDIS_SOURCE="reused system redis-server v${MAJ:-?}"
    if [ "${MAJ:-0}" -ge 8 ]; then
      c_ok "Reusing system redis-server v${MAJ} with the Query Engine (its own instance on a private port; system Redis untouched)."
    else
      c_warn "Reusing system redis-server v${MAJ} with the Query Engine — it works, but Redis 8 is recommended."
      c_warn "To upgrade: install Redis 8 from https://packages.redis.io/deb (apt), then re-run ./install.sh."
    fi
  else
    c_info "No redis-server with the search module found — installing Redis 8 from packages.redis.io…"
    if command -v apt-get >/dev/null 2>&1; then
      SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
      ${SUDO} apt-get install -y lsb-release curl gpg
      curl -fsSL https://packages.redis.io/gpg | ${SUDO} gpg --dearmor -o /usr/share/keyrings/redis-archive-keyring.gpg
      ${SUDO} chmod 644 /usr/share/keyrings/redis-archive-keyring.gpg
      echo "deb [signed-by=/usr/share/keyrings/redis-archive-keyring.gpg] https://packages.redis.io/deb $(lsb_release -cs) main" \
        | ${SUDO} tee /etc/apt/sources.list.d/redis.list >/dev/null
      ${SUDO} apt-get update
      ${SUDO} apt-get install -y redis
      MOD="$(find_search_module)" || { c_err "Redis installed but redisearch.so not found."; exit 1; }
      REDIS_SERVER="$(command -v redis-server)"; REDIS_CLI="$(command -v redis-cli)"
      REDIS_MODULE_SEARCH="${MOD}"; REDIS_SOURCE="apt-installed Redis 8"
      c_ok "Redis 8 installed (VisualWeaver runs its own instance on a private port)."
    else
      c_err "No apt-get. Install Redis 8 with the search module (https://redis.io/docs/latest/operate/oss_and_stack/install/), or use the redis:8 Docker image, then re-run."
      exit 1
    fi
  fi
else
  c_err "Unsupported OS '${OS}'. Use the redis:8 Docker image."
  exit 1
fi

# Persist the resolved Redis environment for start.sh/stop.sh.
{
  echo "# Generated by install.sh — resolved Redis runtime for VisualWeaver."
  echo "REDIS_SERVER=\"${REDIS_SERVER}\""
  echo "REDIS_CLI=\"${REDIS_CLI}\""
  echo "REDIS_MODULE_SEARCH=\"${REDIS_MODULE_SEARCH}\""
  echo "REDIS_DYLD_FALLBACK=\"${REDIS_DYLD_FALLBACK}\""
  echo "REDIS_SOURCE=\"${REDIS_SOURCE}\""
} > "${REDIS_ENV_FILE}"
c_info "Redis source: ${REDIS_SOURCE}"

# ── 3. Choose a dedicated Redis port ─────────────────────────────────────────
WANT_PORT="${VISUALWEAVER_REDIS_PORT:-${DEFAULT_REDIS_PORT}}"
[ -f "${REDIS_CONF}" ] && { EXIST="$(redis_port)"; [ -n "${EXIST}" ] && WANT_PORT="${EXIST}"; }
CHOSEN_PORT="$(find_free_port "${WANT_PORT}")"
[ "${CHOSEN_PORT}" != "${WANT_PORT}" ] && c_warn "Port ${WANT_PORT} in use — using ${CHOSEN_PORT}."
c_ok "Dedicated Redis port: ${CHOSEN_PORT} (loopback only)"

# ── 4. Generate the dedicated redis.conf ─────────────────────────────────────
c_info "Writing ${REDIS_CONF}"
{
  echo "# VisualWeaver — dedicated local Redis instance. Generated by install.sh."
  echo "# Loopback-only, private port, own data dir. Re-run install.sh to regenerate."
  echo ""
  echo "port ${CHOSEN_PORT}"
  echo "bind 127.0.0.1 -::1"
  echo "protected-mode yes"
  echo "dir \"${REDIS_DATA}\""            # quoted — path may contain spaces
  echo "pidfile \"${REDIS_PID}\""
  echo "logfile \"${REDIS_LOG}\""
  # daemonize yes so start.sh can launch redis-server DIRECTLY (not via nohup —
  # macOS SIP strips DYLD_* when exec'ing /usr/bin/nohup, which would break the
  # libunwind shim the vendored search module needs). Redis self-detaches and
  # writes its own pidfile; the child inherits DYLD_FALLBACK across the fork.
  echo "daemonize yes"
  echo ""
  echo "# Persistence: AOF with 1-second fsync + periodic RDB snapshots"
  echo "appendonly yes"
  echo "appendfsync everysec"
  echo "save 900 1"
  echo "save 300 10"
  echo "save 60 10000"
  echo ""
  echo "# The Query Engine (search) that redisvl requires (loaded at startup)"
  echo "loadmodule \"${REDIS_MODULE_SEARCH}\""
} > "${REDIS_CONF}"
c_ok "redis.conf written (AOF everysec, RDB, search module)."

# ── 5. Point the app config at this instance ─────────────────────────────────
c_info "Wiring app config → 127.0.0.1:${CHOSEN_PORT}"
APP_CFG="${DATA_DIR}/config.json"
[ -f "${APP_CFG}" ] || { [ -f "${REPO_DIR}/config.example.json" ] && cp "${REPO_DIR}/config.example.json" "${APP_CFG}"; }
"${VENV}/bin/python" - "${APP_CFG}" "${CHOSEN_PORT}" <<'PYEOF'
import json, os, sys
path, port = sys.argv[1], int(sys.argv[2])
cfg = {}
if os.path.exists(path):
    try: cfg = json.load(open(path))
    except Exception: cfg = {}
cfg.setdefault("redis", {})
cfg["redis"].update({"host": "127.0.0.1", "port": port})
cfg["redis"].setdefault("db", 0); cfg["redis"].setdefault("password", ""); cfg["redis"].setdefault("ssl", False)
os.makedirs(os.path.dirname(path), exist_ok=True)
json.dump(cfg, open(path, "w"), indent=2)
print(f"  config.json redis → 127.0.0.1:{port}")
PYEOF
c_ok "App config points at the dedicated Redis."

# ── 6. Network exposure ──────────────────────────────────────────────────────
# The web UI has no built-in auth, so where it listens is a decision the person
# installing should make consciously, once. The answer is persisted in the
# gitignored .visualweaver.env (a real VISUALWEAVER_HOST env var still wins).
# A choice already recorded there is respected; a non-interactive run (no TTY,
# CI, `./install.sh < /dev/null`) is not asked and keeps the shipped default.
ENV_FILE="${REPO_DIR}/.visualweaver.env"
if grep -qE '^[[:space:]]*VISUALWEAVER_HOST=' "${ENV_FILE}" 2>/dev/null; then
  APP_HOST="$(sed -nE 's/^[[:space:]]*VISUALWEAVER_HOST=[\"]?([^\"[:space:]]+).*/\1/p' "${ENV_FILE}" | tail -1)"
  c_info "Network exposure already set in .visualweaver.env: VISUALWEAVER_HOST=${APP_HOST} (edit that file to change it)."
elif [ -t 0 ]; then
  c_info ""
  c_info "── Network exposure ──"
  c_info "VisualWeaver has NO built-in authentication. Who should be able to open the web UI?"
  c_info "  1) Only this machine          (http://127.0.0.1:${APP_PORT})"
  c_info "  2) Any device on my network   (http://<this machine's LAN address>:${APP_PORT}) — trusted networks only"
  _choice=""
  read -r -p "Choose [1/2] (default 1): " _choice </dev/tty || _choice=""
  case "${_choice}" in
    2|2\)|lan|LAN|network) APP_HOST="0.0.0.0" ;;
    *)                      APP_HOST="127.0.0.1" ;;
  esac
  {
    echo "# VisualWeaver per-machine overrides (gitignored). Written by install.sh; edit freely."
    echo "# 127.0.0.1 = only this machine; 0.0.0.0 = any device on the network (no built-in auth!)."
    echo "VISUALWEAVER_HOST=${APP_HOST}"
  } >> "${ENV_FILE}"
  if [ "${APP_HOST}" = "0.0.0.0" ]; then c_warn "Web UI will be reachable from your network — start.sh prints the address and a reminder each start.";
  else c_ok "Web UI restricted to this machine. To open it up later: set VISUALWEAVER_HOST=0.0.0.0 in .visualweaver.env."; fi
else
  c_info "No terminal — keeping the default network exposure (VISUALWEAVER_HOST=${APP_HOST}); set it in .visualweaver.env to change."
fi

c_info ""
c_ok "══ Install complete ══"
c_info "Start:    ${REPO_DIR}/start.sh        (web UI on http://${APP_HOST}:${APP_PORT})"
c_info "Stop:     ${REPO_DIR}/stop.sh         (stops the app AND its Redis)"
c_info "Restart:  ${REPO_DIR}/restart.sh"
c_info "Redis:    ${REDIS_SOURCE} on 127.0.0.1:${CHOSEN_PORT}"
