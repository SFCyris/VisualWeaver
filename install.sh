#!/usr/bin/env bash
# VisualWeaver — local install / setup.
#
# Sets up everything needed to run VisualWeaver on this machine:
#   0. The storage choice: Redis 8 with search (default), MongoDB with vector
#      search, or both — recorded in .visualweaver.env.
#   1. A Python virtual environment with the app's dependencies.
#   2. Redis (when chosen): a DEDICATED Redis with the Query Engine (search) that
#      redisvl needs, on its OWN loopback port — never touching the system Redis.
#        • macOS: a self-contained Redis 8 vendored into ./.redis (like .venv).
#        • Linux: reuse an existing redis-with-search, else apt-install Redis 8.
#      Its redis.conf has AOF persistence (1-second fsync) + RDB snapshots.
#   3. MongoDB (when chosen): MongoDB's official Community server + search
#      containers, run by the container runtime already on this machine
#      (visualweaver/local_mongo.py), after its licence (SSPL) is accepted.
#   4. The installed stores written into the app config.
#
# Re-runnable (idempotent; never removes a store). Runtime state lives under the
# platform data dir; the vendored Redis lives in ./.redis (gitignored). Env knobs:
#   VISUALWEAVER_DATA_DIR             override the data directory
#   VISUALWEAVER_STORAGE              redis | mongodb | both, without asking (default: ask; redis without a terminal)
#   VISUALWEAVER_ACCEPT_MONGODB_LICENSE=1  accept MongoDB's SSPL licence without asking (needed without a terminal)
#   VISUALWEAVER_REDIS_PORT           force a specific Redis port (default 6389)
#   VISUALWEAVER_MONGODB_PORT         force the built-in MongoDB's port (default 27019)
#   VISUALWEAVER_MONGODB_PROJECT      container/volume name prefix for a new built-in MongoDB (default visualweaver-mongodb,
#                                     plus a hash of the data dir when it is not the platform default)
#   PYTHON                            interpreter for the venv (default: newest supported python3.x found)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Captured before common.sh fills unset variables from .visualweaver.env: only a
# real environment variable answers the storage question; the file's value is
# the recorded choice.
_STORAGE_ENV="${VISUALWEAVER_STORAGE:-}"
_PORT_ENV="${VISUALWEAVER_PORT:-}"
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
mkdir -p "${DATA_DIR}" "${LOG_DIR}"

# ── 0. Storage: Redis, MongoDB, or both ──────────────────────────────────────
# Asked first — with MongoDB's container-runtime and licence checks — so every
# question comes before the long dependency install. The choice is recorded in
# the gitignored .visualweaver.env. Nothing is ever removed: a re-run keeps what
# is installed (recorded, or found in the data dir) and may add the other store.
# VISUALWEAVER_STORAGE in the environment answers without asking; a run without
# a terminal and without it keeps what is installed, or installs Redis.
ENV_FILE="${REPO_DIR}/.visualweaver.env"
MONGO_VER="$(sed -nE 's/^SERVER_VERSION = "([0-9]+\.[0-9]+).*/\1/p' "${REPO_DIR}/visualweaver/local_mongo.py" 2>/dev/null | head -n1)"
MONGO_LICENSE_URL="https://www.mongodb.com/licensing/server-side-public-license"
storage_norm() {
  case "$1" in
    redis|Redis) printf 'redis' ;;
    mongodb|mongo|MongoDB) printf 'mongodb' ;;
    both|Both) printf 'both' ;;
    *) return 1 ;;
  esac
}
storage_union() {
  if [ -z "$1" ]; then printf '%s' "$2"
  elif [ -z "$2" ] || [ "$1" = "$2" ]; then printf '%s' "$1"
  else printf 'both'; fi
}
storage_label() {
  case "$1" in
    redis)   printf 'Redis 8 with search' ;;
    mongodb) printf 'MongoDB %s with vector search' "${MONGO_VER}" ;;
    both)    printf 'Redis 8 with search and MongoDB %s with vector search' "${MONGO_VER}" ;;
  esac
}
storage_has() { [ "${STORAGE}" = "$1" ] || [ "${STORAGE}" = "both" ]; }
storage_record() {
  [ "${STORAGE}" = "${STORAGE_RECORDED}" ] && return 0
  envfile_set VISUALWEAVER_STORAGE "${STORAGE}" \
    "# Storage set up by install.sh: redis | mongodb | both. Re-run ./install.sh to add the other."
  STORAGE_RECORDED="${STORAGE}"
}
STORAGE_RECORDED=""
if grep -qE '^[[:space:]]*(export[[:space:]]+)?VISUALWEAVER_STORAGE=' "${ENV_FILE}" 2>/dev/null; then
  _rec="$(envfile_get VISUALWEAVER_STORAGE)"   # first wins, as in common.sh
  STORAGE_RECORDED="$(storage_norm "${_rec}")" \
    || { c_warn "Ignoring VISUALWEAVER_STORAGE=${_rec} in .visualweaver.env (use redis, mongodb or both)."; STORAGE_RECORDED=""; }
fi
REDIS_BEFORE=0; have_redis && REDIS_BEFORE=1
MONGO_BEFORE=0; have_mongo && MONGO_BEFORE=1
STORAGE_INSTALLED=""
[ "${REDIS_BEFORE}" = 1 ] && STORAGE_INSTALLED="redis"
[ "${MONGO_BEFORE}" = 1 ] && STORAGE_INSTALLED="$(storage_union "${STORAGE_INSTALLED}" mongodb)"
STORAGE_CURRENT="$(storage_union "${STORAGE_RECORDED}" "${STORAGE_INSTALLED}")"
STORAGE_TTY=0; [ -t 0 ] && STORAGE_TTY=1
# "already installed" when the data dir has it; "already chosen" when only the
# record in .visualweaver.env says so (e.g. an earlier run that stopped early).
storage_seen() { [ "${STORAGE_INSTALLED}" = "$1" ] && printf 'already installed' || printf 'already chosen'; }

c_info ""
c_info "── Storage ──"
if [ -n "${_STORAGE_ENV}" ]; then
  STORAGE_REQ="$(storage_norm "${_STORAGE_ENV}")" \
    || { c_err "VISUALWEAVER_STORAGE=${_STORAGE_ENV} is not a storage choice — use redis, mongodb or both."; exit 1; }
  c_info "Storage from VISUALWEAVER_STORAGE: $(storage_label "${STORAGE_REQ}")."
elif [ "${STORAGE_CURRENT}" = "both" ]; then
  STORAGE_REQ="both"
  c_info "Storage $(storage_seen both): $(storage_label both)."
elif [ -n "${STORAGE_CURRENT}" ]; then
  _other="redis"; [ "${STORAGE_CURRENT}" = "redis" ] && _other="mongodb"
  c_info "Storage $(storage_seen "${STORAGE_CURRENT}"): $(storage_label "${STORAGE_CURRENT}")."
  STORAGE_REQ="${STORAGE_CURRENT}"
  if [ "${STORAGE_TTY}" = 1 ]; then
    _choice=""
    read -r -p "Also install $(storage_label "${_other}")? [y/N]: " _choice </dev/tty || _choice=""
    case "${_choice}" in y|Y|yes|Yes|YES) STORAGE_REQ="${_other}" ;; esac
  else
    c_info "(VISUALWEAVER_STORAGE=both ./install.sh adds $(storage_label "${_other}").)"
  fi
elif [ "${STORAGE_TTY}" = 1 ]; then
  c_info "VisualWeaver keeps knowledge bases (RAG), conversations and the answer cache in a database."
  c_info "  1) Redis 8 with search                 (runs natively; no containers needed)"
  c_info "  2) MongoDB ${MONGO_VER} with vector search      (runs in containers: Colima, Podman, OrbStack, Rancher Desktop or Docker)"
  c_info "  3) Both                                (Redis holds app data and the answer cache; MongoDB is available for RAG)"
  _choice=""
  read -r -p "Choose [1/2/3] (default 1): " _choice </dev/tty || _choice=""
  case "${_choice}" in
    2|2\)|mongodb|mongo|MongoDB) STORAGE_REQ="mongodb" ;;
    3|3\)|both|Both)             STORAGE_REQ="both" ;;
    *)                           STORAGE_REQ="redis" ;;
  esac
else
  STORAGE_REQ="redis"
  c_info "No terminal — installing $(storage_label redis) (set VISUALWEAVER_STORAGE=mongodb or both to choose otherwise)."
fi
STORAGE="$(storage_union "${STORAGE_CURRENT}" "${STORAGE_REQ}")"
if [ "${STORAGE}" != "${STORAGE_REQ}" ]; then
  c_info "$(storage_label "${STORAGE_CURRENT}") stays installed (nothing is removed) — setting up both."
fi

# MongoDB cannot be set up on this run: offer Redis instead, or stop.
storage_fallback() {
  if [ "${STORAGE_TTY}" != 1 ]; then
    c_err "Stopping: MongoDB cannot be set up ($1). Nothing was downloaded."
    c_err "Fix that and re-run, or install with Redis only:   VISUALWEAVER_STORAGE=redis ./install.sh"
    exit 1
  fi
  local _q="Continue with $(storage_label redis) instead? [Y/n]: "
  [ "${STORAGE}" = "both" ] && _q="Continue with $(storage_label redis) only? [Y/n]: "
  local _a=""
  read -r -p "${_q}" _a </dev/tty || _a=""
  case "${_a}" in
    n|N|no|No|NO) c_err "Install stopped. Nothing was downloaded for MongoDB."; exit 1 ;;
  esac
  STORAGE="redis"
  c_ok "Continuing with $(storage_label redis). Re-run ./install.sh any time to add MongoDB."
}

# Container runtime + licence, before anything is downloaded.
MONGO_ACCEPT=0; MONGO_SKIP=0; MONGO_RUNTIME=""; MONGO_PREFLIGHT=0
mongo_preflight() {
  MONGO_PREFLIGHT=1
  storage_has mongodb || return 0
  local _rc=0
  MONGO_RUNTIME="$(lm runtime 2>&1)" || _rc=$?
  if [ "${_rc}" = 0 ]; then
    c_ok "Container runtime: ${MONGO_RUNTIME}"
  else
    c_warn "MongoDB's vector search runs in Linux containers, so it needs a container runtime:"
    c_warn "  ${MONGO_RUNTIME}"
    c_info "Container runtimes it works with (VisualWeaver does not install one for you):"
    if [ "${OS}" = "Darwin" ]; then
      c_info "  • Colima — free, open source:   brew install colima docker docker-compose, add the compose"
      c_info "    plugin as \`brew info docker-compose\` describes, then: colima start"
    else
      c_info "  • Docker Engine — free, open source (your distribution's docker.io / docker-ce + the compose plugin)"
    fi
    c_info "  • Podman — free, open source (with podman compose)"
    c_info "  • Rancher Desktop — free, open source"
    c_info "  • OrbStack — has its own commercial terms"
    c_info "  • Docker Desktop — free for personal use, education, non-commercial open source and small"
    c_info "    businesses; companies with 250 or more employees or US\$10 million or more in annual"
    c_info "    revenue need a paid Docker subscription"
    if [ "${MONGO_BEFORE}" = 1 ]; then
      c_warn "The built-in MongoDB stays installed (its data is kept). Start the runtime, then run ./start.sh."
      MONGO_SKIP=1
      return 0
    fi
    storage_fallback "no container runtime is running"
    return 0
  fi
  if [ -f "${MONGO_STATE}" ] && grep -qE '"license_accepted":[[:space:]]*"' "${MONGO_STATE}"; then
    c_info "MongoDB licence (SSPL v1) accepted on an earlier install."
    return 0
  fi
  c_info ""
  c_info "── MongoDB licence ──"
  c_info "MongoDB Community Server and MongoDB Search are licensed by MongoDB, Inc. under the"
  c_info "Server Side Public License v1 (SSPL):  ${MONGO_LICENSE_URL}"
  c_info "They are downloaded from MongoDB (Docker Hub, mongodb/…) to this machine. They are not"
  c_info "part of VisualWeaver and are not distributed with it."
  c_info "The SSPL is not an OSI-approved open-source licence; some organisations' policies do not"
  c_info "allow it. Check yours before accepting."
  c_info "MongoDB is a trademark of MongoDB, Inc. VisualWeaver is an independent project and is not"
  c_info "endorsed by MongoDB, Inc."
  if [ "${VISUALWEAVER_ACCEPT_MONGODB_LICENSE:-}" = "1" ]; then
    c_info "Accepted via VISUALWEAVER_ACCEPT_MONGODB_LICENSE=1."
    MONGO_ACCEPT=1
  elif [ "${STORAGE_TTY}" = 1 ]; then
    local _a=""
    read -r -p "Accept the SSPL and download MongoDB? Type yes to accept: " _a </dev/tty || _a=""
    case "${_a}" in
      yes|Yes|YES) MONGO_ACCEPT=1; c_ok "MongoDB licence accepted." ;;
      *) c_warn "MongoDB licence not accepted."; storage_fallback "its licence was not accepted" ;;
    esac
  else
    c_err "Stopping: a non-interactive MongoDB install needs the licence accepted explicitly:"
    c_err "  VISUALWEAVER_ACCEPT_MONGODB_LICENSE=1 VISUALWEAVER_STORAGE=${STORAGE} ./install.sh"
    c_err "Nothing was downloaded. Or install with Redis only:   VISUALWEAVER_STORAGE=redis ./install.sh"
    exit 1
  fi
}

# local_mongo's runtime check needs only the standard library, so any python3
# can run it now; with none on PATH it runs after the virtualenv is built.
if [ -x "${REPO_DIR}/venv/bin/python" ]; then LM_PY="${REPO_DIR}/venv/bin/python"
else LM_PY="$(command -v python3 2>/dev/null || true)"; fi
if [ -n "${LM_PY}" ]; then
  mongo_preflight
  storage_record
fi
c_ok "Storage: $(storage_label "${STORAGE}")."

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
VENV="${REPO_DIR}/venv"
# A venv left behind by an earlier run on an unsupported Python (the source-build
# failure) must not be reused: it has no PyMuPDF and its pip would repeat the
# failure. Rebuild it on the interpreter chosen above.
if [ -x "${VENV}/bin/python" ] && ! py_supported "${VENV}/bin/python"; then
  c_warn "Existing venv runs Python 3.$(py_minor "${VENV}/bin/python") (unsupported) — recreating it on ${SYS_PY}."
  rm -rf "${VENV}"
fi
if [ -x "${VENV}/bin/python" ]; then
  c_info "Keeping the existing virtualenv (Python $("${VENV}/bin/python" -c 'import platform; print(platform.python_version())'); delete ${VENV} to rebuild it on $(command -v "${SYS_PY}"))."
else
  c_info "Creating virtualenv at ${VENV} with $(command -v "${SYS_PY}") (Python 3.$(py_minor "${SYS_PY}"))"
  "${SYS_PY}" -m venv "${VENV}"
fi
c_info "Installing Python dependencies…"
# pip's own chatter (build-backend notices) goes to a log; it is shown only on failure.
PIP_LOG="${LOG_DIR}/pip-install.log"
pip_quiet() {
  "${VENV}/bin/python" -m pip "$@" >>"${PIP_LOG}" 2>&1 || {
    c_err "Installing the Python dependencies failed. The last lines of ${PIP_LOG}:"
    tail -n 30 "${PIP_LOG}" >&2
    exit 1
  }
}
: > "${PIP_LOG}"
pip_quiet install --upgrade pip
if [ -f "${REPO_DIR}/pyproject.toml" ]; then
  pip_quiet install -e "${REPO_DIR}"
else
  pip_quiet install -r "${REPO_DIR}/requirements.txt"
fi
c_ok "Python dependencies installed (details: ${PIP_LOG})."

# Every local_mongo call from here on can use the app's own environment.
LM_PY="${VENV}/bin/python"
if [ "${MONGO_PREFLIGHT}" != 1 ]; then
  mongo_preflight
  storage_record
fi

# ── 1b. Web UI port ──────────────────────────────────────────────────────────
# VISUALWEAVER_PORT (environment or .visualweaver.env), default 8420. A port held
# by a VisualWeaver (this one running, or one answering /api/health) is kept; one
# held by anything else moves to the next free port, recorded in .visualweaver.env.
c_info ""
c_info "── Ports ──"
if port_in_use "${APP_PORT}" && ! app_port_ours "${APP_PORT}"; then
  _holder="$(port_holder "${APP_PORT}")"
  _new="$(find_free_port "$((APP_PORT + 1))")"
  c_warn "Web UI port ${APP_PORT} is in use by ${_holder:-another program} — using ${_new}."
  envfile_set VISUALWEAVER_PORT "${_new}" "# Web UI port (set by install.sh: ${APP_PORT} was in use)."
  APP_PORT="${_new}"
  export VISUALWEAVER_PORT="${APP_PORT}"
elif port_in_use "${APP_PORT}"; then
  c_ok "Web UI port: ${APP_PORT} (this VisualWeaver is running on it)"
else
  c_ok "Web UI port: ${APP_PORT}"
fi
# A port given as VISUALWEAVER_PORT=… ./install.sh is recorded, so start.sh uses it too.
if [ -n "${_PORT_ENV:-}" ] && [ "$(envfile_get VISUALWEAVER_PORT)" != "${APP_PORT}" ]; then
  envfile_set VISUALWEAVER_PORT "${APP_PORT}" "# Web UI port (set by install.sh)."
fi

# Sections 2–4 run only when Redis is part of the storage choice; a MongoDB-only
# install neither downloads nor installs Redis.
CHOSEN_PORT=""; REDIS_SOURCE=""
if storage_has redis; then
  mkdir -p "${REDIS_DIR}" "${REDIS_DATA}" "${REDIS_HOME}"
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
  # The port in redis.conf is kept while it is free or held by THIS Redis (a
  # re-run while it runs); anything else on it moves Redis to the next free port.
  WANT_PORT="${VISUALWEAVER_REDIS_PORT:-${DEFAULT_REDIS_PORT}}"
  [ -f "${REDIS_CONF}" ] && { EXIST="$(redis_port)"; [ -n "${EXIST}" ] && WANT_PORT="${EXIST}"; }
  if [ "${WANT_PORT}" != "${APP_PORT}" ] && port_in_use "${WANT_PORT}" && redis_is_ours "${WANT_PORT}"; then
    CHOSEN_PORT="${WANT_PORT}"
  else
    CHOSEN_PORT="$(find_free_port "${WANT_PORT}" "${APP_PORT}")"
  fi
  if [ "${CHOSEN_PORT}" != "${WANT_PORT}" ]; then
    _holder="$(port_holder "${WANT_PORT}")"
    [ "${WANT_PORT}" = "${APP_PORT}" ] && _holder="the web UI"
    c_warn "Redis port ${WANT_PORT} is in use by ${_holder:-another program} — using ${CHOSEN_PORT}."
  fi
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
fi

# ── 4b. MongoDB with vector search (containers) ──────────────────────────────
# visualweaver/local_mongo.py owns the topology (mongod + mongot, generated
# passwords, 127.0.0.1-only port, named volumes); this only drives it. Its state
# (port, passwords) is created once and kept on every re-run, like redis.conf.
MONGO_PORT=""
if storage_has mongodb; then
  c_info ""
  c_info "── MongoDB ${MONGO_VER} with vector search ──"
  if [ -n "${VISUALWEAVER_MONGODB_PORT:-}" ]; then
    lm init --port "${VISUALWEAVER_MONGODB_PORT}" >/dev/null
  else
    lm init >/dev/null
  fi
  # A recorded port is kept while it is free or held by this MongoDB itself;
  # otherwise the next free one is recorded in its state.
  _avoid=(--avoid "${APP_PORT}"); [ -n "${CHOSEN_PORT}" ] && _avoid+=(--avoid "${CHOSEN_PORT}")
  _pj="$(lm ensure-port "${_avoid[@]}")"
  _from="$(printf '%s' "${_pj}" | json_field moved_from)"
  if [ -n "${_from}" ]; then
    _holder="$(printf '%s' "${_pj}" | json_field holder)"
    c_warn "MongoDB port ${_from} is in use by ${_holder:-another program} — using $(printf '%s' "${_pj}" | json_field port)."
  fi
  MONGO_PORT="$(mongo_port)"
  [ "${MONGO_ACCEPT}" = 1 ] && lm accept-license
  c_ok "Built-in MongoDB port: ${MONGO_PORT} (loopback only; the search process is not published)"
  if [ "${MONGO_SKIP}" != 1 ]; then
    c_info "Images, from MongoDB's own Docker Hub namespace:"
    if lm images; then
      c_ok "Already downloaded."
    else
      c_info "Downloading (one time)…"
      lm pull || { c_err "Could not download the MongoDB images — check the network and the container runtime, then re-run ./install.sh."; exit 1; }
      c_ok "MongoDB images downloaded."
    fi
  fi
fi

# ── 5. Point the app config at the installed stores ──────────────────────────
# storage.default_type says what the built-in `default` connection is: the store
# installed first (data never moves between names). A store added later becomes
# a built-in extra connection: `local-mongodb` or `local-redis`. Re-runs update
# these in place, never add a second copy, and never change storage.app (the
# connection holding app data + the answer cache, which is chosen in the UI).
c_info "Wiring app config…"
APP_CFG="${DATA_DIR}/config.json"
CFG_FRESH=0
[ -f "${APP_CFG}" ] || { CFG_FRESH=1; [ -f "${REPO_DIR}/config.example.json" ] && cp "${REPO_DIR}/config.example.json" "${APP_CFG}"; }
MONGO_CONN=""
storage_has mongodb && MONGO_CONN="$(lm connection)"
REDIS_CFG_PORT=""
storage_has redis && REDIS_CFG_PORT="${CHOSEN_PORT}"
"${VENV}/bin/python" - "${APP_CFG}" "${REDIS_CFG_PORT}" "${MONGO_CONN}" "${REDIS_BEFORE}" "${MONGO_BEFORE}" "${CFG_FRESH}" <<'PYEOF'
import json, os, sys
path, rport, mconn, redis_before, mongo_before, fresh = sys.argv[1:7]
redis_port = int(rport) if rport else None
mongo = json.loads(mconn) if mconn else None
cfg = {}
if os.path.exists(path):
    try:
        cfg = json.load(open(path))
    except Exception:
        # Unreadable: keep it aside rather than lose its settings silently.
        import time
        aside = f"{path}.unreadable-{time.strftime('%Y%m%d-%H%M%S')}"
        os.replace(path, aside)
        print(f"  config.json could not be read; kept as {aside}, starting from defaults")
        cfg = {}
storage = cfg.setdefault("storage", {})
if fresh == "1":
    # A config this run created from the example records no installed store yet.
    storage.pop("default_type", None)
first = storage.get("default_type")
if first not in ("redis", "mongodb"):
    if mongo_before == "1" and redis_before != "1":
        first = "mongodb"
    elif redis_before == "1" or redis_port is not None:
        first = "redis"
    else:
        first = "mongodb"
    if first == "mongodb" and redis_before != "1":
        cfg.pop("redis", None)   # a MongoDB-first install has no built-in Redis config
storage["default_type"] = first
storage.setdefault("app", "default")
eps = cfg.get("redis_endpoints")
if not isinstance(eps, list):
    eps = []

def put_endpoint(entry):
    """Add or update a built-in connection. A connection the user added under the same
    name is never taken over (its instances refer to it): the built-in gets the next
    free name instead — and keeps it on later runs, where it is found as a built-in."""
    base = entry["name"]
    for e in eps:
        if isinstance(e, dict) and e.get("builtin") and e.get("type") == entry.get("type") \
                and (e.get("name") == base or str(e.get("name", "")).startswith(base + "-")):
            e.update({**entry, "name": e["name"]})
            return e["name"]
    names = {e.get("name") for e in eps if isinstance(e, dict)}
    name, n = base, 2
    while name in names:
        name, n = f"{base}-{n}", n + 1
    eps.append({**entry, "name": name})
    return name

if redis_port is not None:
    if first == "redis":
        cfg.setdefault("redis", {})
        cfg["redis"].update({"host": "127.0.0.1", "port": redis_port})
        cfg["redis"].setdefault("db", 0); cfg["redis"].setdefault("password", ""); cfg["redis"].setdefault("ssl", False)
        print(f"  config.json redis → 127.0.0.1:{redis_port} (built-in `default`)")
    else:
        n = put_endpoint({"name": "local-redis", "type": "redis", "host": "127.0.0.1", "port": redis_port,
                          "db": 0, "password": "", "ssl": False, "builtin": True})
        print(f"  config.json redis_endpoints {n} → 127.0.0.1:{redis_port}")
if mongo is not None:
    if first == "mongodb":
        cfg["mongodb"] = {**(cfg.get("mongodb") or {}), **mongo}
        print(f"  config.json mongodb → {mongo['uri'].split('/?')[0]} (built-in `default`)")
    else:
        n = put_endpoint({"name": "local-mongodb", **mongo})
        print(f"  config.json redis_endpoints {n} → {mongo['uri'].split('/?')[0]}")
cfg["redis_endpoints"] = eps
print(f"  config.json storage → default_type={first}, app={storage['app']}")
os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # holds the MongoDB password
with os.fdopen(fd, "w") as f:
    json.dump(cfg, f, indent=2)
os.replace(tmp, path)
PYEOF
c_ok "App config points at the installed stores."

# ── 5b. Start the built-in MongoDB and wait until search is ready ────────────
# Left running; start.sh finds it up, stop.sh stops it (data kept).
if storage_has mongodb && [ "${MONGO_SKIP}" != 1 ]; then
  c_info "Starting the built-in MongoDB (a first start takes about 15 s)…"
  _rc=0
  _out="$(lm up 2>&1)" || _rc=$?
  if [ "${_rc}" != 0 ]; then
    c_err "The built-in MongoDB did not become ready:"
    printf '%s\n' "${_out}" | tail -n 30 >&2
    c_err "Fix the cause above, then run ./install.sh again (nothing is lost: settings and data are kept)."
    exit 1
  fi
  _secs="$(printf '%s' "${_out}" | sed -nE 's/.*"total_seconds":[[:space:]]*([0-9.]+).*/\1/p' | tail -n1)"
  if [ "$(printf '%s' "${_out}" | json_field already_running)" = "True" ]; then
    c_ok "MongoDB already running with vector search on 127.0.0.1:${MONGO_PORT}."
  else
    c_ok "MongoDB ready with vector search on 127.0.0.1:${MONGO_PORT} (${_secs:-?} s)."
  fi
  _dw="$(printf '%s' "${_out}" | json_field disk_warning)"
  [ -n "${_dw}" ] && [ "${_dw}" != "None" ] && c_warn "${_dw}"
fi

# ── 6. Network exposure ──────────────────────────────────────────────────────
# The web UI has no built-in auth, so where it listens is a decision the person
# installing should make consciously, once. The answer is persisted in the
# gitignored .visualweaver.env (a real VISUALWEAVER_HOST env var still wins).
# A choice already recorded there is respected; a non-interactive run (no TTY,
# CI, `./install.sh < /dev/null`) is not asked and keeps the shipped default.
ENV_FILE="${REPO_DIR}/.visualweaver.env"
# Matched exactly as common.sh reads it (indented, spaced, `export`-prefixed): a line it
# honours but this check missed was asked again, and the new answer appended below it
# was then ignored — "only this machine" kept the app on the network.
if [ -n "$(envfile_get VISUALWEAVER_HOST)" ]; then
  APP_HOST="$(envfile_get VISUALWEAVER_HOST)"   # first wins, as in common.sh
  c_info "Network exposure already set in .visualweaver.env: VISUALWEAVER_HOST=${APP_HOST} (run ./changeAccess.sh local|lan to change it)."
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
  envfile_set VISUALWEAVER_HOST "${APP_HOST}" \
    "# 127.0.0.1 = only this machine; 0.0.0.0 = any device on the network (no built-in auth!)."
  if [ "${APP_HOST}" = "0.0.0.0" ]; then c_warn "Web UI will be reachable from your network — start.sh prints the address and a reminder each start.";
  else c_ok "Web UI restricted to this machine. To open it up later: ./changeAccess.sh lan"; fi
else
  c_info "No terminal — keeping the default network exposure (VISUALWEAVER_HOST=${APP_HOST}); run ./changeAccess.sh local|lan to change it."
fi

# ── 7. Validation: is it ready to use? ───────────────────────────────────────
# Each installed piece is checked for real: the venv imports the app, each store
# starts and answers with its search, the web UI port can be bound. The stores
# are left running (start.sh reuses them). Any ✗ ends the install non-zero.
c_info ""
c_info "── Validation ──"
V_FAIL=0; V_FIXES=()
v_ok()   { printf '  \033[0;32m✓\033[0m %-18s %s\n' "$1" "$2"; }
v_fail() { printf '  \033[0;31m✗\033[0m %-18s %s\n' "$1" "$2"; V_FAIL=1; V_FIXES+=("$1: $3"); }
VER_PY=""; VER_REDIS=""; VER_SEARCH=""; VER_MONGO=""; VER_MSEARCH=""; VER_RT=""
VER_APP="$(sed -n 's/^__version__ *= *["'"'"']\(.*\)["'"'"']/\1/p' "${REPO_DIR}/visualweaver/__init__.py" 2>/dev/null)"

if _o="$(PYTHONPATH="${REPO_DIR}" VISUALWEAVER_DATA_DIR="${DATA_DIR}" "${VENV}/bin/python" -c \
      'import platform, visualweaver.main; print(platform.python_version())' 2>&1)"; then
  VER_PY="$(printf '%s\n' "${_o}" | tail -n1)"
  v_ok "Python venv" "Python ${VER_PY}, VisualWeaver ${VER_APP:-?} imports (${VENV})"
else
  v_fail "Python venv" "the app does not import" \
    "the virtualenv is incomplete ($(printf '%s\n' "${_o}" | tail -n1)). Delete ${VENV} and run ./install.sh again."
fi

if storage_has redis; then
  if redis_up "${CHOSEN_PORT}" >/dev/null 2>&1 && rcli -p "${CHOSEN_PORT}" FT._LIST >/dev/null 2>&1; then
    read -r VER_REDIS VER_SEARCH <<<"$(redis_versions "${CHOSEN_PORT}")"
    v_ok "Redis" "127.0.0.1:${CHOSEN_PORT}  Redis ${VER_REDIS}, search ${VER_SEARCH} (FT._LIST answers)"
  elif rcli -p "${CHOSEN_PORT}" ping >/dev/null 2>&1; then
    v_fail "Redis" "127.0.0.1:${CHOSEN_PORT} answers, but the search module is not loaded" \
      "redis.conf's loadmodule (${REDIS_MODULE_SEARCH}) did not load — see ${REDIS_LOG}, then run ./install.sh again."
  else
    v_fail "Redis" "127.0.0.1:${CHOSEN_PORT} does not start" \
      "see the last lines of ${REDIS_LOG}; free the port or run ./install.sh again."
  fi
fi

if storage_has mongodb; then
  _st="$(lm status 2>/dev/null || true)"
  VER_RT="$(printf '%s' "${_st}" | json_field runtime) $(printf '%s' "${_st}" | json_field runtime_version)"
  VER_RT="${VER_RT% }"; VER_RT="${VER_RT# }"
  if [ "$(printf '%s' "${_st}" | json_field ready)" = "True" ]; then
    VER_MONGO="$(printf '%s' "${_st}" | json_field version)"
    VER_MSEARCH="$(printf '%s' "${_st}" | json_field search)"
    v_ok "MongoDB" "127.0.0.1:${MONGO_PORT}  MongoDB ${VER_MONGO}, search ${VER_MSEARCH} (vector search ready)"
  else
    _why="$(printf '%s' "${_st}" | json_field error)"
    if [ "${MONGO_SKIP}" = 1 ] || [ -z "$(printf '%s' "${_st}" | json_field runtime)" ]; then
      v_fail "MongoDB" "127.0.0.1:${MONGO_PORT} not running" \
        "start the container runtime ($(lm runtime 2>&1 | tail -n1)), then run ./start.sh."
    else
      v_fail "MongoDB" "127.0.0.1:${MONGO_PORT} not ready" \
        "${_why:-search is not available}. Its log: venv/bin/python -m visualweaver.local_mongo logs — then run ./install.sh again."
    fi
  fi
  if [ -n "${VER_RT}" ]; then v_ok "Container runtime" "${VER_RT}"
  else v_fail "Container runtime" "not running" "start it (see above), then run ./start.sh."; fi
fi

if ! port_in_use "${APP_PORT}"; then
  v_ok "Web UI port" "${APP_HOST}:${APP_PORT} is free"
elif app_port_ours "${APP_PORT}"; then
  v_ok "Web UI port" "${APP_HOST}:${APP_PORT} is served by VisualWeaver already (./restart.sh picks up this install)"
else
  _holder="$(port_holder "${APP_PORT}")"
  v_fail "Web UI port" "${APP_PORT} is taken by ${_holder:-another program}" \
    "stop that program, or choose another port: VISUALWEAVER_PORT=<port> ./install.sh"
fi

if [ "${V_FAIL}" = 1 ]; then
  c_err ""
  c_err "══ Install finished, but VisualWeaver is not ready to use ══"
  for _f in "${V_FIXES[@]}"; do c_err "  • ${_f}"; done
  exit 1
fi

# ── 8. Summary ───────────────────────────────────────────────────────────────
c_info ""
c_ok "══ Install complete — ready to use ══"
c_info "Start:    ${REPO_DIR}/start.sh        (web UI on http://${APP_HOST}:${APP_PORT})"
c_info "Stop:     ${REPO_DIR}/stop.sh         (stops the app and its databases; data is kept)"
c_info "Restart:  ${REPO_DIR}/restart.sh"
c_info "Access:   ${REPO_DIR}/changeAccess.sh local|lan   (this machine only / any device on the network)"
storage_has redis && c_info "Redis:    ${REDIS_SOURCE} on 127.0.0.1:${CHOSEN_PORT} (running now)"
storage_has mongodb && c_info "MongoDB:  ${MONGO_VER} with vector search on 127.0.0.1:${MONGO_PORT} (containers via ${MONGO_RUNTIME:-the container runtime}; running now)"
case "$(app_store_type)" in
  redis)   c_info "App data & answer cache: built-in Redis (Settings → Storage to change)" ;;
  mongodb) c_info "App data & answer cache: built-in MongoDB (Settings → Storage to change)" ;;
esac
c_info "Versions: VisualWeaver ${VER_APP:-?} · Python ${VER_PY:-?}"
storage_has redis && c_info "          Redis ${VER_REDIS:-?} · search ${VER_SEARCH:-?}"
storage_has mongodb && c_info "          MongoDB ${VER_MONGO:-?} · search ${VER_MSEARCH:-?} · ${VER_RT:-?}"
exit 0
