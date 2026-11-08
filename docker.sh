#!/usr/bin/env bash
# VisualWeaver in Docker, in one command: choose the storage (Redis, MongoDB or both),
# start everything, wait until it answers, and print the address and versions.
# Everything else happens inside the containers (MongoDB's passwords, keyfile, replica
# set and users are generated on its first start). Choices are kept in `.env` next to
# docker-compose.yml, so plain `docker compose …` commands use them too.

# Started as `zsh docker.sh` or `sh docker.sh`: hand over to bash.
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE=".env"
LICENSE_URL="https://www.mongodb.com/licensing/server-side-public-license"

if [ -t 1 ]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'; else B= G= Y= R= N=; fi
info() { printf '%s\n' "$*"; }
ok()   { printf '%s\n' "${G}$*${N}"; }
warn() { printf '%s\n' "${Y}$*${N}" >&2; }
die()  { printf '%s\n' "${R}$*${N}" >&2; exit 1; }

usage() {
  cat <<EOF
Usage: ./docker.sh <command>

  up [redis|mongodb|both] [--build]
                  Start VisualWeaver with the chosen storage (asked once, then kept):
                    redis    Redis 8 with search (default)
                    mongodb  MongoDB with vector search, no Redis
                    both     Redis for app data and the answer cache; MongoDB for RAG too
                  --build builds the app image from this checkout instead of pulling it
                  (kept for later runs); --pull goes back to the published image.
  status          What is running, where, and which versions.
  logs [service]  Follow the logs (visualweaver, redis, mongodb, mongodb-search).
  backup [dir]    Save the app data and every store (stops them for a few seconds).
  restore <dir>   Replace the data with a backup, then start.
  stop            Stop the containers (everything is kept).
  down            Remove the containers (the data volumes are kept).
  -h              Show this help.

Settings are kept in .env: COMPOSE_PROFILES (the storage), VISUALWEAVER_WEB_PORT (the
port on this machine, default 8420), VISUALWEAVER_WEB_BIND (127.0.0.1 = this machine only),
VISUALWEAVER_ALLOWED_HOSTS, VISUALWEAVER_OLLAMA_URL.
EOF
}

# ── .env helpers (KEY=VALUE lines; the first one wins, as in docker compose) ─
env_get() {   # never fails: no .env yet is simply "not set"
  [ -f "${ENV_FILE}" ] || return 0
  sed -nE "s/^[[:space:]]*$1=[\"']?([^\"']*)[\"']?[[:space:]]*$/\1/p" "${ENV_FILE}" 2>/dev/null | head -n1 || true
}
env_set() {
  local key="$1" val="$2"
  if grep -qE "^[[:space:]]*${key}=" "${ENV_FILE}" 2>/dev/null; then
    awk -v k="${key}" -v v="${val}" '$0 ~ "^[[:space:]]*" k "=" {print k "=" v; next} {print}' \
      "${ENV_FILE}" > "${ENV_FILE}.tmp" && mv "${ENV_FILE}.tmp" "${ENV_FILE}"
  else
    [ -s "${ENV_FILE}" ] && [ -n "$(tail -c1 "${ENV_FILE}")" ] && echo >> "${ENV_FILE}"
    [ -s "${ENV_FILE}" ] || echo "# VisualWeaver Docker settings (written by ./docker.sh; edit freely)." > "${ENV_FILE}"
    echo "${key}=${val}" >> "${ENV_FILE}"
  fi
}

# ── Docker ──────────────────────────────────────────────────────────────────
compose_version() { docker compose version --short 2>/dev/null | sed -E 's/^v//'; }
version_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]; }

need_docker() {
  command -v docker >/dev/null 2>&1 || die "Docker is not installed. Install Docker Engine (Linux), Docker Desktop, Colima, OrbStack or Rancher Desktop, then run this again."
  local v; v="$(compose_version)"
  [ -n "${v}" ] || die "The Docker Compose plugin is missing (\`docker compose version\` fails). Install it and run this again."
  local tries=0
  until docker info >/dev/null 2>&1; do
    tries=$((tries + 1))
    [ "${tries}" -ge 3 ] && die "Docker is installed but its engine does not answer. Start it (Docker Desktop, \`colima start\`, OrbStack, Rancher Desktop, or \`sudo systemctl start docker\`) and run this again."
    sleep 5
  done
  COMPOSE_VERSION="${v}"
}

# ── storage choice ──────────────────────────────────────────────────────────
normalise_storage() {
  case "$(printf '%s' "$1" | tr 'A-Z' 'a-z' | tr -d ' ')" in
    ""|redis)                           echo redis ;;
    mongo|mongodb)                      echo mongodb ;;
    both|redis,mongodb|mongodb,redis)   echo both ;;
    *)                                  echo "" ;;
  esac
}

choose_storage() {
  local arg="$1" s
  if [ -n "${arg}" ]; then
    s="$(normalise_storage "${arg}")"
    [ -n "${s}" ] || die "Unknown storage '${arg}' — use redis, mongodb or both."
  elif [ -n "$(env_get COMPOSE_PROFILES)" ]; then
    s="$(normalise_storage "$(env_get COMPOSE_PROFILES)")"
    info "Storage: ${s} (from .env — \`./docker.sh up redis|mongodb|both\` changes it)."
  elif [ -t 0 ]; then
    info ""
    info "${B}Which storage should VisualWeaver use?${N}"
    info "  1) Redis 8 with search            (default; lightweight)"
    info "  2) MongoDB with vector search     (no Redis)"
    info "  3) Both                           (Redis for app data and the answer cache; MongoDB for RAG too)"
    local c=""; read -r -p "Choose [1/2/3] (default 1): " c </dev/tty || c=""
    case "${c}" in 2*) s=mongodb ;; 3*) s=both ;; *) s=redis ;; esac
  else
    s=redis
  fi
  STORAGE="${s}"
}

accept_mongodb_license() {
  [ "${STORAGE}" = redis ] && return 0
  [ "$(env_get VISUALWEAVER_ACCEPT_MONGODB_LICENSE)" = 1 ] && return 0
  info ""
  info "${B}── MongoDB licence ──${N}"
  info "MongoDB Community Server and MongoDB Search are licensed by MongoDB, Inc. under the"
  info "Server Side Public License v1 (SSPL): ${LICENSE_URL}"
  info "Their images are pulled from MongoDB's Docker Hub namespace (mongodb/…); they are not"
  info "part of VisualWeaver and are not distributed with it. The SSPL is not an OSI-approved"
  info "open-source licence; some organisations' policies do not allow it."
  if [ "${VISUALWEAVER_ACCEPT_MONGODB_LICENSE:-}" = 1 ]; then
    info "Accepted via VISUALWEAVER_ACCEPT_MONGODB_LICENSE=1."
  elif [ -t 0 ]; then
    local a=""; read -r -p "Type yes to accept and continue: " a </dev/tty || a=""
    [ "$(printf '%s' "${a}" | tr 'A-Z' 'a-z')" = yes ] || die "Not accepted — nothing was downloaded. Use \`./docker.sh up redis\` for Redis only."
  else
    die "Accept MongoDB's licence with VISUALWEAVER_ACCEPT_MONGODB_LICENSE=1, or use \`./docker.sh up redis\`."
  fi
  env_set VISUALWEAVER_ACCEPT_MONGODB_LICENSE 1
}

# ── port and host names ─────────────────────────────────────────────────────
port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
ours_on() { docker ps --filter "name=^visualweaver$" --format '{{.Ports}}' 2>/dev/null | grep -q ":$1->8420"; }

choose_port() {
  local p; p="$(env_get VISUALWEAVER_WEB_PORT)"; p="${p:-8420}"
  if port_busy "${p}" && ! ours_on "${p}"; then
    local holder=""
    command -v lsof >/dev/null 2>&1 && holder="$(lsof -nP -iTCP:"${p}" -sTCP:LISTEN -Fc 2>/dev/null | sed -n 's/^c//p' | head -n1)"
    local q="${p}"
    while port_busy "${q}" && ! ours_on "${q}"; do q=$((q + 1)); done
    warn "Port ${p} is in use${holder:+ by ${holder}} — using ${q} instead."
    p="${q}"
  fi
  env_set VISUALWEAVER_WEB_PORT "${p}"
  WEB_PORT="${p}"
}

# This machine's own names (as given, its short name and <short name>.local), so a browser
# that uses them is answered: inside the container the app does not know the host's name
# (IP addresses and localhost are always answered).
allow_host_names() {
  local cur names n; cur="$(env_get VISUALWEAVER_ALLOWED_HOSTS)"
  names="$(for n in "$(hostname -f 2>/dev/null || true)" "$(hostname 2>/dev/null || true)"; do
             [ -n "${n}" ] && printf '%s\n%s\n%s.local\n' "${n}" "${n%%.*}" "${n%%.*}"
           done | tr 'A-Z' 'a-z' | grep -v '^localhost' | grep -v '\.arpa$' | grep -v '^[0-9]*\(\.\|$\)' \
             | sort -u || true)"
  for n in ${names}; do
    case ",${cur}," in *",${n},"*) ;; *) cur="${cur:+${cur},}${n}" ;; esac
  done
  [ -n "${cur}" ] && env_set VISUALWEAVER_ALLOWED_HOSTS "${cur}"
  return 0
}

lan_address() {
  local ip=""
  if command -v ipconfig >/dev/null 2>&1; then
    for i in en0 en1 en2; do ip="$(ipconfig getifaddr "${i}" 2>/dev/null || true)"; [ -n "${ip}" ] && break; done
  fi
  [ -z "${ip}" ] && ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  printf '%s' "${ip}"
}

# MongoDB's search process stops building indexes above 85 % disk use (resumes below
# 80 %): new documents are then silently not found. Its disk is the runtime's VM disk.
disk_warning() {
  docker ps --filter "name=^visualweaver-mongodb-search$" --format '{{.Names}}' 2>/dev/null | grep -q . || return 0
  local pct
  pct="$(docker exec visualweaver-mongodb-search df -P /data/mongot 2>/dev/null \
         | awk 'NR==2 {u=$3; a=$4; if (u+a>0) printf "%d", (100*u/(u+a))+0.5}')"
  [ -n "${pct}" ] && [ "${pct}" -ge 80 ] || return 0
  warn "  Disk:     ${pct} % full. MongoDB's search stops building indexes above 85 % (and resumes"
  warn "            below 80 %), so new documents would not be found. Free space with"
  warn "            \`docker image prune\` / \`docker builder prune\`, or give the Docker VM a larger disk."
}

# The model provider chosen in the app config, checked as install.sh checks it: the
# checkout's visualweaver/provider_check.py (standard library only) is piped into the
# running container, so it sees the container's config, environment and network.
provider_report() {
  local out="" kind="" msg="" fix=""
  if [ -f visualweaver/provider_check.py ]; then
    out="$(docker exec -i visualweaver python - /data/config.json --docker < visualweaver/provider_check.py 2>/dev/null || true)"
  fi
  IFS=$'\t' read -r kind msg fix <<<"${out}" || true
  case "${kind}" in
    ok)   info "  Provider: ${G}✓${N} ${msg}" ;;
    warn) info "  Provider: ${Y}⚠${N} ${msg}"
          [ -n "${fix}" ] && warn "            Before the first answer: ${fix}" ;;
    *)    info "  Provider: not checked — open Settings → Providers once the page is up." ;;
  esac
}

# ── report ──────────────────────────────────────────────────────────────────
report() {
  local port; port="$(env_get VISUALWEAVER_WEB_PORT)"; port="${port:-8420}"
  local json; json="$(curl -fsS -m 10 "http://127.0.0.1:${port}/api/components" 2>/dev/null || true)"
  local lan; lan="$(lan_address)"
  # While sign-in is misconfigured (or config.json could not be loaded) only /api/health
  # answers — 200, "status": "misconfigured" and the reason; every other address is 503.
  local health why; health="$(curl -fsS -m 5 "http://127.0.0.1:${port}/api/health" 2>/dev/null || true)"
  info ""
  ok "══ VisualWeaver is running ══"
  if printf '%s' "${health}" | grep -q '"status" *: *"misconfigured"'; then
    why="$(printf '%s' "${health}" | sed -n 's/.*"detail" *: *"\([^"]*\)".*/\1/p')"
    warn "  Sign-in:  MISCONFIGURED — every request is refused until the auth settings are fixed (see \`./docker.sh logs\`)."
    [ -n "${why}" ] && warn "            ${why}"
  fi
  info "  Web UI:   http://localhost:${port}"
  if [ "$(env_get VISUALWEAVER_WEB_BIND)" = 127.0.0.1 ]; then
    info "            (this machine only — VISUALWEAVER_WEB_BIND=127.0.0.1)"
  elif [ -n "${lan}" ]; then
    info "            http://${lan}:${port}  (other devices on your network — there is no login)"
  fi
  if [ -n "${json}" ]; then
    printf '%s' "${json}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
a = d.get("app") or {}
print("  Versions: VisualWeaver %s · Python %s" % (a.get("version"), a.get("python")))
for s in d.get("storage") or []:
    state = "ready" if s.get("ok") else "not reachable: " + str(s.get("error") or "?")
    extra = " · " + s["search"] if s.get("search") else ""
    print("            %s: %s %s%s (%s)" % (s.get("label"), s.get("product"), s.get("version") or "", extra, state))
' 2>/dev/null || true
  fi
  info "  Storage:  ${STORAGE:-$(normalise_storage "$(env_get COMPOSE_PROFILES)")}  (data in Docker volumes; \`./docker.sh down\` keeps it)"
  disk_warning
  provider_report
  info "  Models:   Ollama on this machine is used automatically; add cloud model keys in Settings → Providers."
  info "  Logs:     ./docker.sh logs      Stop: ./docker.sh stop"
}

# ── commands ────────────────────────────────────────────────────────────────
cmd_up() {
  local arg="" build
  build="$(env_get VISUALWEAVER_BUILD)"; build="${build:-0}"
  for a in "$@"; do
    case "${a}" in
      --build) build=1 ;;
      --pull)  build=0 ;;
      -*) die "Unknown option ${a} (see ./docker.sh -h)" ;;
      *) arg="${a}" ;;
    esac
  done
  need_docker
  choose_storage "${arg}"
  if [ "${STORAGE}" != redis ] && ! version_ge "${COMPOSE_VERSION}" 2.23.1; then
    die "MongoDB needs Docker Compose 2.23.1 or newer (this is ${COMPOSE_VERSION}). Update Docker / the Compose plugin, or use \`./docker.sh up redis\`."
  fi
  accept_mongodb_license
  env_set COMPOSE_PROFILES "${STORAGE}"
  env_set VISUALWEAVER_BUILD "${build}"
  choose_port
  allow_host_names
  export COMPOSE_PROFILES="${STORAGE}"

  if [ "${build}" = 1 ]; then
    # Kept once chosen (VISUALWEAVER_BUILD=1 in .env): pulling would replace the image
    # built from this checkout with the published one. `--pull` goes back to that.
    info "Building the VisualWeaver image from this checkout (the first build takes a while; later ones reuse it)…"
    docker compose build visualweaver
  else
    info "Downloading the images (only what is new)…"
    if ! docker compose pull --quiet visualweaver; then
      warn "Could not pull the VisualWeaver image — building it from this checkout instead."
      docker compose build visualweaver
    fi
    docker compose pull --quiet --ignore-buildable 2>/dev/null || docker compose pull --quiet --ignore-pull-failures || true
  fi
  info "Starting (${STORAGE}) and waiting until everything is ready…"
  local t0=$SECONDS
  if ! docker compose up -d --remove-orphans --wait --wait-timeout 420; then
    docker compose ps
    die "Not ready after waiting. See \`./docker.sh logs\` — the last lines usually say why."
  fi
  ok "Ready in $((SECONDS - t0)) s."
  check_image_matches
  report
}

# An app image older than the storage choice ignores it (the storage would silently
# stay Redis): say so, with the fix.
check_image_matches() {
  [ "${STORAGE}" = redis ] && return 0
  local port; port="$(env_get VISUALWEAVER_WEB_PORT)"; port="${port:-8420}"
  local got; got="$(curl -fsS -m 10 "http://127.0.0.1:${port}/api/storage" 2>/dev/null \
                    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(",".join(c["type"] for c in d["connections"]))' 2>/dev/null || true)"
  case "${got}" in
    *mongodb*) return 0 ;;
  esac
  warn "This VisualWeaver image does not support the ${STORAGE} storage choice (it is older)."
  warn "Build it from this checkout instead:  ./docker.sh up ${STORAGE} --build"
}

# ── backup / restore ────────────────────────────────────────────────────────
# What each container keeps in a volume. Reached through the container itself
# (--volumes-from), so the compose project's volume names do not matter. MongoDB's
# search indexes are not saved: they are rebuilt from the database.
VOLUMES="visualweaver:/data:app visualweaver-redis:/data:redis visualweaver-mongodb:/data/db:mongodb visualweaver-mongodb-secrets:/secrets:mongodb-secrets"

exists() { docker inspect "$1" >/dev/null 2>&1; }
tool_image() { docker inspect --format '{{.Config.Image}}' visualweaver 2>/dev/null || echo ghcr.io/sfcyris/visualweaver:latest; }

cmd_backup() {
  need_docker
  exists visualweaver || die "Nothing to back up — VisualWeaver has not been started here (./docker.sh up)."
  local dir="${1:-visualweaver-backup-$(date +%Y%m%d-%H%M%S)}"
  mkdir -p "${dir}"; dir="$(cd "${dir}" && pwd)"
  local running; running="$(docker compose ps --status running -q | wc -l | tr -d ' ')"
  info "Stopping for a consistent copy (a few seconds)…"
  docker compose stop >/dev/null
  local img spec c path name n=0; img="$(tool_image)"
  for spec in ${VOLUMES}; do
    IFS=: read -r c path name <<<"${spec}"
    exists "${c}" || continue
    # Streamed out, not written through a bind mount: Colima and Docker Desktop share
    # only some host folders with their VM, and a write to an unshared one is lost.
    # The downloaded models (.cache) are left out: they are fetched again when needed.
    if docker run --rm --user 0 --entrypoint tar --volumes-from "${c}" "${img}" \
         czf - --exclude=./.cache -C "${path}" . > "${dir}/${name}.tgz" && [ -s "${dir}/${name}.tgz" ]; then
      n=$((n + 1))
    else
      rm -f "${dir}/${name}.tgz"; warn "Could not save ${name}."
    fi
  done
  [ -f .env ] && cp .env "${dir}/env"
  if [ "${running}" -gt 0 ]; then
    info "Starting again…"
    COMPOSE_PROFILES="$(env_get COMPOSE_PROFILES)" docker compose up -d --wait --wait-timeout 420 >/dev/null 2>&1 \
      || warn "Not all containers are ready again — see ./docker.sh status"
  fi
  ok "Backed up ${n} store(s) to ${dir}"
  info "Restore with:  ./docker.sh restore \"${dir}\""
}

cmd_restore() {
  need_docker
  local dir="${1:-}"
  [ -n "${dir}" ] && [ -d "${dir}" ] || die "Usage: ./docker.sh restore <backup folder>"
  dir="$(cd "${dir}" && pwd)"
  if [ -t 0 ]; then
    local a=""; read -r -p "Replace the current data with the backup in ${dir}? Type yes: " a </dev/tty || a=""
    [ "$(printf '%s' "${a}" | tr 'A-Z' 'a-z')" = yes ] || die "Nothing changed."
  fi
  [ -f "${dir}/env" ] && [ ! -f .env ] && cp "${dir}/env" .env
  if ! exists visualweaver; then           # a fresh machine: create the containers first
    export COMPOSE_PROFILES="$(env_get COMPOSE_PROFILES)"
    docker compose up --no-start >/dev/null
  fi
  docker compose stop >/dev/null
  local img spec c path name n=0; img="$(tool_image)"
  for spec in ${VOLUMES}; do
    IFS=: read -r c path name <<<"${spec}"
    [ -f "${dir}/${name}.tgz" ] && exists "${c}" || continue
    docker run --rm -i --user 0 --entrypoint sh --volumes-from "${c}" "${img}" \
      -c "find '${path}' -mindepth 1 -delete && tar xzf - -C '${path}'" < "${dir}/${name}.tgz" && n=$((n + 1))
  done
  # MongoDB's search indexes describe the data that was there: rebuild them from the restore.
  exists visualweaver-mongodb-search && docker run --rm --user 0 --entrypoint sh \
    --volumes-from visualweaver-mongodb-search "${img}" -c "find /data/mongot -mindepth 1 -delete" || true
  ok "Restored ${n} store(s) from ${dir}"
  cmd_up
}

cmd_status() {
  need_docker
  docker compose ps
  docker ps --filter "name=^visualweaver$" --format '{{.Names}}' | grep -q . && report || info "VisualWeaver is not running (./docker.sh up)."
}

case "${1:-}" in
  up)            shift; cmd_up "$@" ;;
  status)        cmd_status ;;
  logs)          shift; need_docker; docker compose logs -f --tail 100 "$@" ;;
  backup)        shift; cmd_backup "$@" ;;
  restore)       shift; cmd_restore "$@" ;;
  stop)          need_docker; docker compose stop; ok "Stopped (everything is kept). Start again with ./docker.sh up" ;;
  down)          need_docker; docker compose down; ok "Containers removed; the data volumes are kept. Start again with ./docker.sh up" ;;
  -h|--help|help) usage ;;
  "")            usage; exit 2 ;;
  *)             warn "Unknown command: $1"; echo >&2; usage >&2; exit 2 ;;
esac
