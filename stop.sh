#!/usr/bin/env bash
# VisualWeaver — stop the app AND the stores install.sh set up for it: the
# dedicated local Redis and/or the built-in MongoDB (containers; data kept).
#
# Only touches VisualWeaver's own instances (its pidfiles, its private Redis
# port, its own MongoDB compose project); never a system Redis, another
# project's process or other containers.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/common.sh
source "${SCRIPT_DIR}/scripts/common.sh"

load_redis_env || true   # gives rcli the resolved REDIS_CLI; falls back to PATH
RPORT="$(redis_port)"
STORE_REDIS=0; have_redis && STORE_REDIS=1
STORE_MONGO=0; have_mongo && STORE_MONGO=1

# Send TERM, wait up to ~10s, then KILL. $1 = pidfile, $2 = label.
stop_pidfile() {
  local f="$1" label="$2" p
  if ! pid_alive "${f}"; then
    [ -f "${f}" ] && rm -f "${f}"
    return 1
  fi
  p="$(cat "${f}")"
  c_info "Stopping ${label} (pid ${p})…"
  kill "${p}" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "${p}" 2>/dev/null || { rm -f "${f}"; c_ok "${label} stopped."; return 0; }
    sleep 0.5
  done
  c_warn "${label} did not exit on TERM — sending KILL."
  kill -9 "${p}" 2>/dev/null || true
  rm -f "${f}"
  return 0
}

# ── 1. App ────────────────────────────────────────────────────────────────────
# Pidfile first; if that knows nothing, look for OUR app listening on the app
# port anyway — an instance started by hand (plain `uvicorn …`) has no pidfile,
# and "App was not running" while it kept the port meant restart.sh silently
# left the OLD build serving. Only a process whose command line is recognisably
# this app is ever killed; anything else on the port is left alone.
stop_port_fallback() {
  local p cmd
  p="$(lsof -tnP -iTCP:"${APP_PORT}" -sTCP:LISTEN 2>/dev/null | head -n1 || true)"
  [ -z "${p}" ] && return 1
  cmd="$(ps -p "${p}" -o command= 2>/dev/null || true)"
  if ! printf '%s' "${cmd}" | grep -qE 'uvicorn[^|]*visualweaver|visualweaver\.main|-m *visualweaver'; then
    c_warn "Port ${APP_PORT} is held by a process that is not VisualWeaver (pid ${p}) — leaving it."
    return 1
  fi
  c_info "Stopping app found on port ${APP_PORT} (pid ${p}, started outside these scripts)…"
  kill "${p}" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "${p}" 2>/dev/null || { c_ok "App stopped."; return 0; }
    sleep 0.5
  done
  c_warn "App did not exit on TERM — sending KILL."
  kill -9 "${p}" 2>/dev/null || true
  return 0
}
stop_pidfile "${APP_PID}" "app" || stop_port_fallback || c_info "App was not running."

# ── 2. Redis (clean shutdown flushes AOF/RDB) ────────────────────────────────
# Skipped when this data dir has no Redis (a MongoDB-only install).
if [ "${STORE_REDIS}" = 1 ]; then
  if rcli -p "${RPORT}" ping >/dev/null 2>&1; then
    c_info "Shutting down dedicated Redis on 127.0.0.1:${RPORT} (saving)…"
    # SHUTDOWN persists per the save points + AOF, then exits. The client sees the
    # connection drop, so a non-zero exit here is expected — verify by pinging.
    rcli -p "${RPORT}" shutdown save >/dev/null 2>&1 || true
    down=0
    for _ in $(seq 1 20); do
      if ! rcli -p "${RPORT}" ping >/dev/null 2>&1; then down=1; break; fi
      sleep 0.5
    done
    if [ "${down}" = "1" ]; then
      c_ok "Redis stopped."
      rm -f "${REDIS_PID}"
    else
      c_warn "Redis still responding — forcing via pidfile."
      stop_pidfile "${REDIS_PID}" "redis" || true
    fi
  else
    # Not responding on the port; clean up a stale pidfile if any.
    stop_pidfile "${REDIS_PID}" "redis" || c_info "Redis was not running."
  fi
fi

# ── 3. Built-in MongoDB (containers stopped, data kept in their volumes) ──────
if [ "${STORE_MONGO}" = 1 ]; then
  c_info "Stopping the built-in MongoDB (data kept)…"
  _rc=0
  _out="$(lm stop 2>&1)" || _rc=$?
  if [ "${_rc}" = 0 ]; then
    c_ok "MongoDB stopped."
  elif ! lm runtime >/dev/null 2>&1; then
    c_info "MongoDB is not running: the container runtime is not running (data kept)."
  else
    c_warn "$(printf '%s' "${_out}" | tail -n 5)"
  fi
fi

c_ok "══ VisualWeaver stopped ══"
