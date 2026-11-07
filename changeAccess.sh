#!/usr/bin/env bash
# VisualWeaver — choose who can open the web UI: this machine only, or any device
# on your network. Records the choice as VISUALWEAVER_HOST in .visualweaver.env
# (the same file install.sh writes); start.sh / restart.sh pick it up.

# Started as `zsh changeAccess.sh` or `sh changeAccess.sh`: hand over to bash.
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -euo pipefail

# Captured before common.sh fills unset variables from .visualweaver.env, so a
# value exported in the shell can be told apart from the one in the file.
_SHELL_HOST="${VISUALWEAVER_HOST-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/common.sh
. "${SCRIPT_DIR}/scripts/common.sh"

ENV_FILE="${REPO_DIR}/.visualweaver.env"
# Every line common.sh would read as VISUALWEAVER_HOST (indented, spaced, exported).
HOST_LINE_RE='^[[:space:]]*(export[[:space:]]+)?VISUALWEAVER_HOST[[:space:]]*='

usage() {
  cat <<EOF
Usage: ./changeAccess.sh <local|lan>

Sets who can open the VisualWeaver web UI started with ./start.sh.

  local          Only this machine (binds 127.0.0.1). Open it at http://localhost:<port>.
  lan            Any device on your network (binds 0.0.0.0). VisualWeaver has NO
                 built-in login, so use this only on a trusted network.
  -h             Show this help.

Run without an option to see the current setting. The choice is saved as
VISUALWEAVER_HOST in .visualweaver.env and applies the next time VisualWeaver
starts (./restart.sh if it is running now). Docker Compose uses the port
mapping in docker-compose.yml instead.
EOF
}

# The VISUALWEAVER_HOST that start.sh would take from .visualweaver.env — read by
# common.sh itself in a clean subshell, so both always agree (first line wins,
# quotes and CRLF handled the same way). Empty when the file sets nothing.
file_host() {
  env -u VISUALWEAVER_HOST bash -c '. "$1/scripts/common.sh" >/dev/null 2>&1; printf %s "${VISUALWEAVER_HOST-}"' _ "${REPO_DIR}"
}

describe() {
  case "$1" in
    127.0.0.1|localhost|::1) printf 'local (this machine only)' ;;
    0.0.0.0|::|"")           printf 'lan (any device on the network)' ;;
    *)                       printf 'custom (%s)' "$1" ;;
  esac
}

shell_override_note() {
  c_warn "NOTE: VISUALWEAVER_HOST=${_SHELL_HOST} is exported in this shell and overrides the file"
  c_warn "      for anything started from it — run 'unset VISUALWEAVER_HOST' before ./start.sh."
}

if [ $# -ne 1 ]; then
  usage >&2
  echo >&2
  _cur="$(file_host)"
  if [ -n "${_cur}" ]; then
    c_info "Current setting: $(describe "${_cur}")"
  else
    c_info "Current setting: $(describe "") — the default; nothing is set in .visualweaver.env yet"
  fi
  if [ -n "${_SHELL_HOST}" ] && [ "${_SHELL_HOST}" != "${_cur}" ]; then shell_override_note; fi
  exit 2
fi

case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
  -h)                usage; exit 0 ;;
  local)              NEW_HOST="127.0.0.1"; MODE="local" ;;
  lan)                NEW_HOST="0.0.0.0";   MODE="lan" ;;
  *) c_err "Unknown option: $1"; echo >&2; usage >&2; exit 2 ;;
esac

# Nothing to do only when the file holds exactly one host line and it is the
# plain one we would write; duplicates, spacing, `export`, CRLF or a trailing
# comment all get rewritten so the file says what start.sh will do.
CHANGED=0
if [ -f "${ENV_FILE}" ] \
   && [ "$(grep -cE "${HOST_LINE_RE}" "${ENV_FILE}" || true)" = "1" ] \
   && grep -qx "VISUALWEAVER_HOST=${NEW_HOST}" "${ENV_FILE}"; then
  c_ok "Access is already set to ${MODE} (VISUALWEAVER_HOST=${NEW_HOST}) — nothing to change."
else
  # Build the new file next to the old one, then swap it in with one rename so a
  # failed write never leaves a partial file. `cp -p` first gives the temp file
  # the original's permissions. A symlinked file is written through instead.
  tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
  trap 'rm -f "${tmp}"' EXIT
  if [ -f "${ENV_FILE}" ]; then
    cp -p "${ENV_FILE}" "${tmp}"
    { grep -vE "${HOST_LINE_RE}" "${ENV_FILE}" || [ $? -eq 1 ]; } > "${tmp}"
  else
    echo "# VisualWeaver per-machine overrides (gitignored — never committed)." > "${tmp}"
  fi
  echo "VISUALWEAVER_HOST=${NEW_HOST}" >> "${tmp}"
  if [ -L "${ENV_FILE}" ]; then
    cat "${tmp}" > "${ENV_FILE}"
  else
    mv -f "${tmp}" "${ENV_FILE}"
  fi
  CHANGED=1
  c_ok "Access set to ${MODE} (VISUALWEAVER_HOST=${NEW_HOST}) in .visualweaver.env."
fi

if [ "${MODE}" = "lan" ]; then
  c_warn "WARNING: any device on your network can open VisualWeaver — it has NO built-in login."
  c_warn "         Use this only on a trusted network. Go back with:  ./changeAccess.sh local"
fi

# A variable exported in the shell beats the file (see scripts/common.sh).
if [ -n "${_SHELL_HOST}" ] && [ "${_SHELL_HOST}" != "${NEW_HOST}" ]; then shell_override_note; fi

if [ "${CHANGED}" = 1 ] && pid_alive "${APP_PID}"; then
  c_info "VisualWeaver is running — run ./restart.sh to apply the change."
fi
