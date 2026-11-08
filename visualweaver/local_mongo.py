# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.local_mongo — the built-in MongoDB with vector search.

MongoDB's search process (mongot) runs on Linux only, so the built-in MongoDB is two
official MongoDB Community containers on any OS — the database (mongod, a one-member
replica set) and the search process (mongot) — run by the container runtime already on
the machine (Docker, Colima, OrbStack, Rancher Desktop or Podman). Nothing is bundled:
the images are pulled from MongoDB's own Docker Hub namespace, pinned by tag and digest.

Layout, all under <DATA_DIR>/mongodb/ (mode 0700):
  state.json   port, compose project, generated passwords, licence acceptance (0600)
  compose.yml  generated from state.json on every start (0600 — it holds the passwords)

The data lives in the runtime's named volumes (<project>_db, <project>_search), not in
host folders: Colima and Docker Desktop share only some host paths with their VM, and
a repo or data dir elsewhere would mount as an empty directory. The configuration and
secrets go in through the compose file's inline `configs`, for the same reason.

mongod listens on 127.0.0.1 only; mongot is not published at all. Access control is on:
the app signs in as "visualweaver" (readWrite on its database), mongot as "mongotUser"
(searchCoordinator), and "vwadmin" (root) exists for maintenance.

Used by install.sh / start.sh / stop.sh through the command line below, and by the test
suite, so the tests run the exact topology users get:

  python -m visualweaver.local_mongo runtime            # which container runtime, or why none
  python -m visualweaver.local_mongo images             # the pinned images (exit 0 = already downloaded)
  python -m visualweaver.local_mongo init [--port N]    # create state + compose (idempotent)
  python -m visualweaver.local_mongo ensure-port        # keep the port if usable, else next free (JSON)
  python -m visualweaver.local_mongo accept-license     # record the licence acceptance
  python -m visualweaver.local_mongo pull               # download the pinned images
  python -m visualweaver.local_mongo up [--timeout S]   # start and wait until search is ready
  python -m visualweaver.local_mongo stop | down        # stop (keep data) | remove containers (keep data)
  python -m visualweaver.local_mongo logs [--tail N]    # the containers' recent log lines
  python -m visualweaver.local_mongo status             # JSON: installed, running, ready, port
  python -m visualweaver.local_mongo connection         # JSON: the app's connection config

runtime, images, init and accept-license need only the standard library when
VISUALWEAVER_DATA_DIR is set, so install.sh can ask its questions before the
virtualenv exists.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

SERVER_IMAGE = ("mongodb/mongodb-community-server:9.0.4-ubi9-slim"
                "@sha256:92ac2e5cde7e6cb0a60bf12c5d822b51f5412d47736e57a7781e21210e3076ac")
SEARCH_IMAGE = ("mongodb/mongodb-community-search:1.78.0"
                "@sha256:68b89d6cfc31edc0056b7ef54e7a2b9fa7299422f6399d9a937203dafbb88756")
SERVER_VERSION = "9.0.4"
SEARCH_VERSION = "1.78.0"
# Measured on the pinned images (linux/arm64, 2026-11-06); shown before the download.
IMAGES_SIZE = "about 1.9 GB"
IDLE_MEMORY = "about 520 MB"

PROJECT = "visualweaver-mongodb"
DEFAULT_PORT = 27019
APP_USER, SEARCH_USER, ADMIN_USER = "visualweaver", "mongotUser", "vwadmin"
DATABASE = "visualweaver"
LICENSE_NAME = "Server Side Public License v1 (SSPL)"
LICENSE_URL = "https://www.mongodb.com/licensing/server-side-public-license"


class LocalMongoError(Exception):
    """Something the person installing can act on; the message says what."""


# ── where things live ────────────────────────────────────────────────────────

def data_dir() -> Path:
    # Same rule as constants.DATA_DIR; reading the env var first keeps the
    # installer's early calls free of the app's dependencies.
    env = os.environ.get("VISUALWEAVER_DATA_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    from .constants import DATA_DIR
    return Path(DATA_DIR)


def state_dir(base: Path | None = None) -> Path:
    return (base or data_dir()) / "mongodb"


def load_state(base: Path | None = None) -> dict | None:
    p = state_dir(base) / "state.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def save_state(st: dict, base: Path | None = None) -> None:
    _write_private(state_dir(base) / "state.json", json.dumps(st, indent=2) + "\n")


# ── container runtime ────────────────────────────────────────────────────────

def _start_hint(found: list[str]) -> str:
    """How to start the runtime(s) that are installed here."""
    hints = []
    if shutil.which("colima"):
        hints.append("`colima start`")
    for app, label in (("OrbStack", "open OrbStack"), ("Rancher Desktop", "open Rancher Desktop"),
                       ("Docker", "open Docker Desktop")):
        if Path(f"/Applications/{app}.app").exists():
            hints.append(label)
    if "podman" in found and sys.platform == "darwin":
        hints.append("`podman machine start`")
    if not hints and sys.platform.startswith("linux") and "docker" in found:
        hints.append("`sudo systemctl start docker`")
    if not hints:
        hints.append("start Docker Desktop, Colima (`colima start`), OrbStack, Rancher Desktop "
                     "or Podman (`podman machine start`)")
    return " or ".join(hints)


# `docker info` can take tens of seconds on a loaded machine: a slow answer is retried
# with a longer limit, and one that never comes is reported as busy, not as stopped.
_INFO_TIMEOUTS = (20, 60)


def runtime() -> tuple[list[str] | None, str]:
    """(compose command, description) for the first working runtime, else (None, why).

    A CLI that is installed but not running (Docker Desktop closed, `colima` stopped)
    is reported as such — the fix is to start it, not to install anything."""
    found, no_compose, denied, busy = [], [], [], []
    for cli in ("docker", "podman"):
        if not shutil.which(cli):
            continue
        found.append(cli)
        info = None
        for limit in _INFO_TIMEOUTS:
            try:
                info = subprocess.run([cli, "info", "--format", "{{json .}}"], capture_output=True,
                                      text=True, timeout=limit)
                break
            except subprocess.TimeoutExpired:
                continue
            except OSError:
                break
        if info is None:
            busy.append(cli)
            continue
        if info.returncode:
            if "permission denied" in (info.stderr or "").lower():
                denied.append(cli)
            continue
        try:
            comp = subprocess.run([cli, "compose", "version"], capture_output=True, text=True,
                                  timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            comp = None
        if comp is None or comp.returncode:
            no_compose.append(cli)
            continue
        ctx = ""
        if cli == "docker":
            try:
                c = subprocess.run(["docker", "context", "show"], capture_output=True, text=True, timeout=10)
                ctx = c.stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                ctx = ""
        return [cli, "compose"], f"{cli}{f' ({ctx})' if ctx else ''}"
    if busy:
        return None, (f"{busy[0]} is running but did not answer within {int(sum(_INFO_TIMEOUTS))} s — "
                      f"it is busy or stuck. Wait a moment and run this again; if it keeps happening, "
                      f"restart {busy[0]}.")
    if denied:
        return None, (f"{denied[0]} is running, but this user may not use it (permission denied). "
                      f"Add the user to the `docker` group (`sudo usermod -aG docker $USER`, then "
                      f"sign out and in again) and run this again.")
    if no_compose:
        return None, (f"{no_compose[0]} is running but `{no_compose[0]} compose` is not available. "
                      f"Install the Compose plugin (macOS: `brew install docker-compose`; Linux: "
                      f"the docker-compose-plugin package, or podman-compose) and run this again.")
    if found:
        return None, (f"{' and '.join(found)} is installed but not running. Start it "
                      f"({_start_hint(found)}) and run this again.")
    return None, ("No container runtime found. MongoDB's search runs in Linux containers, so one "
                  "is needed: Colima (`brew install colima docker docker-compose`), Podman, "
                  "Rancher Desktop, OrbStack or Docker Desktop.")


# ── starting and stopping the runtime ───────────────────────────────────────
# The container runtime is started when it is installed but stopped — the usual state
# after a reboot — instead of telling the user to do it. Which one: the runtime the
# `docker` CLI is pointed at (its context), else Podman's machine. It is stopped again
# only if it was started here and nothing else runs in it.

def _docker_context() -> str:
    try:
        r = subprocess.run(["docker", "context", "show"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _startable_runtime() -> dict | None:
    """{name, start, stop} for the installed runtime that `docker`/`podman` would use."""
    app = lambda a: Path(f"/Applications/{a}.app").exists()
    if shutil.which("docker"):
        ctx = _docker_context()
        if ctx.startswith("colima") and shutil.which("colima"):
            prof = ctx.split("-", 1)[1] if "-" in ctx else ""
            extra = ["--profile", prof] if prof else []
            return {"name": "Colima", "start": ["colima", "start", *extra], "stop": ["colima", "stop", *extra]}
        if ctx == "orbstack" and app("OrbStack"):
            return {"name": "OrbStack", "start": ["open", "-a", "OrbStack"],
                    "stop": ["osascript", "-e", 'quit app "OrbStack"']}
        if ctx.startswith("rancher-desktop") and app("Rancher Desktop"):
            return {"name": "Rancher Desktop", "start": ["open", "-a", "Rancher Desktop"],
                    "stop": ["osascript", "-e", 'quit app "Rancher Desktop"']}
        if ctx in ("desktop-linux", "default") and app("Docker"):
            return {"name": "Docker Desktop", "start": ["open", "-a", "Docker"],
                    "stop": ["osascript", "-e", 'quit app "Docker"']}
        if shutil.which("colima") and ctx in ("", "default"):
            return {"name": "Colima", "start": ["colima", "start"], "stop": ["colima", "stop"]}
    if shutil.which("podman") and sys.platform == "darwin":
        return {"name": "Podman", "start": ["podman", "machine", "start"], "stop": ["podman", "machine", "stop"]}
    return None


def _runtime_mark(base: Path | None = None) -> Path:
    """Where "VisualWeaver started the runtime" is recorded — its own file, so the
    installer can record it before the built-in MongoDB's state exists."""
    return state_dir(base) / "runtime.json"


def ensure_runtime(base: Path | None = None, timeout: float = 300) -> dict:
    """Make sure the container runtime answers; start it when it is installed but
    stopped, and wait for it. {runtime, started, seconds}. Raises LocalMongoError with
    what to do when it cannot be started here (Linux daemon, busy, not installed)."""
    cmd, why = runtime()
    if cmd:
        return {"runtime": why, "started": False, "seconds": 0}
    if "is installed but not running" not in why:
        raise LocalMongoError(why)                    # busy, no permission, no compose, none installed
    rt = _startable_runtime()
    if rt is None:
        raise LocalMongoError(why)                    # e.g. a Linux daemon: needs sudo
    t0 = time.time()
    log_path = data_dir() / "log" / "container-runtime.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as logf:
        logf.write(f"\n── {time.strftime('%Y-%m-%d %H:%M:%S')} starting {rt['name']}: {' '.join(rt['start'])}\n")
        logf.flush()
        try:
            r = subprocess.run(rt["start"], stdout=logf, stderr=subprocess.STDOUT, timeout=timeout)
        except subprocess.TimeoutExpired:
            r = None
    while time.time() - t0 < timeout:
        cmd, why2 = runtime()
        if cmd:
            _write_private(_runtime_mark(base), json.dumps({
                "name": rt["name"], "stop": rt["stop"], "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}) + "\n")
            return {"runtime": why2, "started": True, "name": rt["name"],
                    "seconds": round(time.time() - t0, 1)}
        if r is not None and r.returncode not in (0, None) and rt["name"] in ("Colima", "Podman"):
            break                                     # the start command itself failed
        time.sleep(2)
    raise LocalMongoError(f"{rt['name']} did not start within {int(time.time() - t0)} s. "
                          f"Its output is in {log_path}; start it by hand ({' '.join(rt['start'])}) "
                          f"and run this again.")


def release_runtime(base: Path | None = None, timeout: float = 180) -> dict:
    """Stop the container runtime again if ensure_runtime started it and no container
    is running in it any more (other projects' containers keep it up). Set
    VISUALWEAVER_KEEP_RUNTIME=1 to always leave it running."""
    mark = _runtime_mark(base)
    try:
        rs = json.loads(mark.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rs = None
    if not rs:
        return {"stopped": False, "reason": "not started by VisualWeaver"}
    if os.environ.get("VISUALWEAVER_KEEP_RUNTIME", "").strip() == "1":
        return {"stopped": False, "name": rs["name"], "reason": "VISUALWEAVER_KEEP_RUNTIME=1"}
    cmd, _ = runtime()
    if cmd:
        r = subprocess.run([cmd[0], "ps", "-q"], capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            n = len(r.stdout.split())
            return {"stopped": False, "name": rs["name"],
                    "reason": f"{n} other container{'s' if n != 1 else ''} still running"}
        t0 = time.time()
        try:
            subprocess.run(rs["stop"], capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"stopped": False, "name": rs["name"], "reason": f"did not stop within {timeout} s"}
        for _ in range(int(timeout / 2)):
            if runtime()[0] is None:
                break
            time.sleep(2)
        else:
            return {"stopped": False, "name": rs["name"], "reason": "still answering after the stop command"}
        mark.unlink(missing_ok=True)
        return {"stopped": True, "name": rs["name"], "seconds": round(time.time() - t0, 1)}
    mark.unlink(missing_ok=True)                      # already stopped by someone else
    return {"stopped": False, "name": rs["name"], "reason": "already stopped"}


def runtime_info() -> dict:
    """{runtime, runtime_version}: the runtime that would be used and its server version."""
    cmd, why = runtime()
    if not cmd:
        return {"runtime": None, "runtime_version": None}
    ver = ""
    try:
        fmt = "{{.Server.Version}}" if cmd[0] == "docker" else "{{.Client.Version}}"
        r = subprocess.run([cmd[0], "version", "--format", fmt], capture_output=True, text=True, timeout=20)
        ver = r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {"runtime": why, "runtime_version": ver or None}


def images_present() -> bool:
    """Both pinned images are already in the runtime's image store (no download needed)."""
    cmd, _ = runtime()
    if not cmd:
        return False
    try:
        r = subprocess.run([cmd[0], "image", "inspect", SERVER_IMAGE, SEARCH_IMAGE],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


# ── configuration ────────────────────────────────────────────────────────────

def _port_free(port: int) -> bool:
    """Nothing listens on the port (no connection is made). SO_REUSEADDR, as servers
    bind, so connections left in TIME_WAIT do not count; the wildcard and the
    loopback address are both tried, since a listener on one does not block a
    reuse-bind on the other."""
    for host in ("0.0.0.0", "127.0.0.1"):
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
            except OSError:
                return False
    return True


def new_state(port: int | None = None, project: str = PROJECT) -> dict:
    """A new state on `port` (default 27019). Whether that port is free is decided by
    ensure_port(), which also says what holds it."""
    return {
        "project": project,
        "port": port or DEFAULT_PORT,
        "database": DATABASE,
        "app_password": secrets.token_hex(24),
        "search_password": secrets.token_hex(24),
        "admin_password": secrets.token_hex(24),
        "keyfile": secrets.token_hex(384),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "license_accepted": None,
    }


def _indent(text: str, n: int) -> str:
    pad = " " * n
    return "\n".join(pad + line if line else line for line in text.splitlines())


# The server image's entrypoint (PID 1) neither handles nor forwards SIGTERM, so a
# plain `stop` waited out the 10 s grace period and then SIGKILLed mongod. This
# wrapper runs the entrypoint as a child and hands TERM/INT to mongod itself,
# which shuts down cleanly in about a second. `$$` escapes compose interpolation.
MONGOD_ENTRYPOINT = (
    '/usr/local/bin/docker-entrypoint "$$@" & ep=$$!; '
    'fwd() { for p in /proc/[0-9]*; do [ "$$(cat "$$p/comm" 2>/dev/null)" = mongod ] '
    '&& kill -TERM "$${p#/proc/}" 2>/dev/null; done; }; '
    'trap fwd TERM INT; '
    'while kill -0 "$$ep" 2>/dev/null; do wait "$$ep"; rc=$$?; done; exit "$${rc:-0}"'
)


def render_compose(st: dict) -> str:
    """The compose file for a state. Pure — the passwords come from `st`."""
    mongod_conf = f"""net:
  port: 27017
  bindIpAll: true
replication:
  replSetName: rs0
security:
  authorization: enabled
  keyFile: /etc/vw-keyfile
setParameter:
  searchIndexManagementHostAndPort: search:27028
  mongotHost: search:27028
  skipAuthenticationToSearchIndexManagementServer: false
  useGrpcForSearch: true
  searchTLSMode: disabled
"""
    mongot_conf = f"""syncSource:
  replicaSet:
    hostAndPort: "mongod:27017"
    scramAuth:
      username: "{SEARCH_USER}"
      passwordFile: "/etc/vw-search-password"
      authSource: "admin"
      tls:
        enabled: false
  replicationReader:
    readPreference: "primaryPreferred"
storage:
  dataPath: "/data/mongot"
server:
  grpc:
    address: "0.0.0.0:27028"
    tls:
      mode: "disabled"
metrics:
  enabled: false
  address: "0.0.0.0:9946"
healthCheck:
  address: "0.0.0.0:8080"
logging:
  verbosity: INFO
"""
    users_js = (
        'const admin = db.getSiblingDB("admin");\n'
        f'admin.createUser({{user: "{SEARCH_USER}", pwd: "{st["search_password"]}", roles: ["searchCoordinator"]}});\n'
        f'admin.createUser({{user: "{APP_USER}", pwd: "{st["app_password"]}", roles: ['
        f'{{role: "readWrite", db: "{st["database"]}"}}, {{role: "clusterMonitor", db: "admin"}}]}});\n'
    )
    # $$ escapes compose interpolation; the passwords are hex / urlsafe (no quotes).
    return f"""# Generated by VisualWeaver (visualweaver/local_mongo.py) — rewritten on every start.
# Holds passwords: keep it private (mode 0600).
name: {st["project"]}
configs:
  mongod_conf:
    content: |
{_indent(mongod_conf, 6)}
  mongot_conf:
    content: |
{_indent(mongot_conf, 6)}
  init_users:
    content: |
{_indent(users_js, 6)}
  keyfile:
    content: "{st["keyfile"]}"
  search_password:
    content: "{st["search_password"]}"
services:
  mongod:
    image: {SERVER_IMAGE}
    entrypoint: ["bash", "-c", {json.dumps(MONGOD_ENTRYPOINT)}, "vw-mongod"]
    command: ["--config", "/etc/mongod.conf", "--replSetMember=mongod:27017"]
    environment:
      MONGODB_INITDB_ROOT_USERNAME: {ADMIN_USER}
      MONGODB_INITDB_ROOT_PASSWORD: "{st["admin_password"]}"
    configs:
      - {{source: mongod_conf, target: /etc/mongod.conf}}
      - {{source: init_users, target: /docker-entrypoint-initdb.d/10-users.js}}
      - {{source: keyfile, target: /etc/vw-keyfile, uid: "1000", gid: "1000", mode: 0400}}
    volumes: ["db:/data/db"]
    ports: ["127.0.0.1:{st["port"]}:27017"]
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "mongosh", "--quiet", "--eval", "db.adminCommand('ping').ok"]
      interval: 10s
      timeout: 5s
      retries: 6
      start_period: 120s
      start_interval: 1s
  search:
    image: {SEARCH_IMAGE}
    configs:
      - {{source: mongot_conf, target: /mongot-community/config.default.yml}}
      - {{source: search_password, target: /etc/vw-search-password, uid: "0", gid: "0", mode: 0400}}
    volumes: ["search:/data/mongot"]
    depends_on:
      mongod: {{condition: service_healthy}}
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:8080/health"]
      interval: 10s
      timeout: 5s
      retries: 6
      start_period: 120s
      start_interval: 1s
volumes:
  db:
  search:
"""


def connection_cfg(st: dict) -> dict:
    """The app's connection to the built-in MongoDB (a storage-connection entry)."""
    return {
        "type": "mongodb",
        "uri": f"mongodb://127.0.0.1:{st['port']}/?authSource=admin",
        "username": APP_USER,
        "password": st["app_password"],
        "database": st["database"],
        "builtin": True,
    }


def admin_uri(st: dict) -> str:
    return f"mongodb://{ADMIN_USER}:{st['admin_password']}@127.0.0.1:{st['port']}/?authSource=admin"


# ── lifecycle ────────────────────────────────────────────────────────────────

def _platform_data_dir() -> Path:
    """The data dir a plain install uses (constants._default_data_dir without the env)."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "VisualWeaver"
    if sys.platform.startswith("linux"):
        xdg = os.environ.get("XDG_DATA_HOME", "").strip()
        return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "visualweaver"
    return Path.home() / ".visualweaver"


def default_project(base: Path | None = None) -> str:
    """"visualweaver-mongodb" for the platform data dir; for any other data dir the same
    name plus a short hash of its path. Container and volume names are per machine, so
    two data dirs (a second checkout, a test install) sharing one name would share — and
    clobber — one database whose users were created with the other dir's passwords."""
    here = (base or data_dir()).expanduser().resolve()
    try:
        if here == _platform_data_dir().expanduser().resolve():
            return PROJECT
    except OSError:
        pass
    return f"{PROJECT}-{hashlib.sha1(str(here).encode()).hexdigest()[:8]}"


def init(port: int | None = None, base: Path | None = None, project: str | None = None) -> dict:
    """Create the state on first use; keep it (passwords, port, data) on every re-run.

    The compose project (container and volume names) is fixed at creation: `project`,
    else VISUALWEAVER_MONGODB_PROJECT, else default_project() for this data dir."""
    st = load_state(base)
    if st is None:
        st = new_state(port, project or os.environ.get("VISUALWEAVER_MONGODB_PROJECT", "").strip()
                       or default_project(base))
    elif port and port != st["port"]:
        st["port"] = port
    save_state(st, base)
    write_compose(st, base)
    return st


def port_holder(port: int) -> str:
    """What listens on 127.0.0.1:<port>, as "<command> (pid N)", when lsof can tell."""
    try:
        r = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    pid = cmd = ""
    for line in r.stdout.splitlines():
        if line.startswith("p") and not pid:
            pid = line[1:]
        elif line.startswith("c") and not cmd:
            cmd = line[1:]
    return f"{cmd} (pid {pid})" if pid else ""


def ensure_port(base: Path | None = None, avoid: tuple[int, ...] = ()) -> dict:
    """Keep the recorded port while it is free or held by this MongoDB itself (our
    app user signs in); otherwise move to the next free port and record it.

    {port, moved_from, holder}: holder names what took the old port, if known."""
    st = load_state(base)
    if st is None:
        raise LocalMongoError("The built-in MongoDB is not installed (run ./install.sh).")
    port = st["port"]
    if port not in avoid and (_port_free(port) or probe(st)["running"]):
        return {"port": port, "moved_from": None, "holder": ""}
    holder = port_holder(port) if port not in avoid else "another VisualWeaver service"
    new = next((p for p in range(port + 1, port + 200) if p not in avoid and _port_free(p)), None)
    if new is None:
        raise LocalMongoError(f"No free port found after {port} for the built-in MongoDB.")
    st["port"] = new
    save_state(st, base)
    write_compose(st, base)
    return {"port": new, "moved_from": port, "holder": holder}


def rewire_config(config_path: Path, st: dict) -> bool:
    """Point the app's connection to the built-in MongoDB at the port in `st` (after
    ensure_port moved it). Only the built-in entry is touched — `config.mongodb` or the
    `builtin` MongoDB entry of redis_endpoints on this machine's loopback. Returns
    whether config.json changed."""
    try:
        cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    uri = connection_cfg(st)["uri"]
    changed = False
    targets = [cfg.get("mongodb")] + list(cfg.get("redis_endpoints") or [])
    for c in targets:
        if not isinstance(c, dict) or c.get("type") != "mongodb" or not c.get("builtin"):
            continue
        if str(c.get("uri", "")).startswith("mongodb://127.0.0.1:") and c["uri"] != uri:
            c["uri"] = uri
            changed = True
    if changed:
        _write_private(Path(config_path), json.dumps(cfg, indent=2) + "\n")
    return changed


def write_compose(st: dict, base: Path | None = None) -> Path:
    p = state_dir(base) / "compose.yml"
    _write_private(p, render_compose(st))
    return p


def _compose(args: list[str], base: Path | None = None, timeout: int = 900,
             capture: bool = True) -> subprocess.CompletedProcess:
    cmd, why = runtime()
    if not cmd:
        raise LocalMongoError(why)
    st = load_state(base)
    if not st:
        raise LocalMongoError("The built-in MongoDB is not installed (run ./install.sh).")
    f = write_compose(st, base)
    try:
        return subprocess.run(cmd + ["-p", st["project"], "-f", str(f)] + args,
                              capture_output=capture, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise LocalMongoError(f"`{' '.join(cmd + args[:1])}` did not finish within {timeout} s — "
                              f"is the container runtime responding?") from None


def pull(base: Path | None = None) -> None:
    r = _compose(["pull", "-q"], base, capture=False)
    if r.returncode:
        raise LocalMongoError("Could not download the MongoDB images (see above).")


def up(base: Path | None = None, timeout: float = 240) -> dict:
    """Start (or keep running) and wait until search is ready. The result carries
    `total_seconds`, the whole start including the runtime's part."""
    t0 = time.time()
    # Already running exactly what the current state describes (start.sh after
    # install.sh, or a second start): answer at once instead of going through the
    # runtime. "Exactly" is the compose file last applied by an `up` — a changed
    # port or image pin goes through compose, which recreates the containers.
    st = load_state(base)
    applied = state_dir(base) / "applied.sha256"
    if st and applied.exists() and applied.read_text().strip() == _digest(st):
        p = probe(st)
        if p["ready"]:
            p["total_seconds"] = round(time.time() - t0, 1)
            p["already_running"] = True
            return p
    r = _compose(["up", "-d", "--wait", "--wait-timeout", str(int(timeout))], base)
    if r.returncode:
        raise LocalMongoError("The built-in MongoDB did not start:\n" + _tail(base) + "\n"
                              + (r.stderr or "")[-600:])
    if st:
        _write_private(applied, _digest(st) + "\n")
    try:
        out = wait_ready(load_state(base), timeout=max(30.0, timeout - (time.time() - t0)))
    except LocalMongoError as e:
        raise LocalMongoError(f"{e}\n{_tail(base)}") from None
    out["total_seconds"] = round(time.time() - t0, 1)
    return out


def _digest(st: dict) -> str:
    return hashlib.sha256(render_compose(st).encode()).hexdigest()


def stop(base: Path | None = None) -> dict:
    """Stop the containers and check they are down. {was_running, seconds, mongod_exit}:
    mongod_exit 0 is a clean shutdown (137 = killed after the grace period)."""
    st = load_state(base)
    t0 = time.time()
    running = _compose(["ps", "-q", "--status", "running"], base, timeout=60)
    was_running = bool((running.stdout or "").strip())
    r = _compose(["stop"], base)
    if r.returncode:
        raise LocalMongoError("Could not stop the built-in MongoDB:\n" + (r.stderr or "")[-600:])
    left = _compose(["ps", "-q", "--status", "running"], base, timeout=60)
    if (left.stdout or "").strip():
        raise LocalMongoError("The built-in MongoDB's containers are still running after the stop.")
    code = None
    try:
        ids = _compose(["ps", "-aq", "mongod"], base, timeout=60).stdout.split()
        if ids:
            cmd, _ = runtime()
            ins = subprocess.run([cmd[0], "inspect", "-f", "{{.State.ExitCode}}", ids[0]],
                                 capture_output=True, text=True, timeout=30)
            code = int(ins.stdout.strip()) if ins.returncode == 0 and ins.stdout.strip() else None
    except Exception:
        code = None
    return {"was_running": was_running, "seconds": round(time.time() - t0, 1), "mongod_exit": code,
            "project": (st or {}).get("project")}


def down(base: Path | None = None) -> None:
    """Remove the containers; the named volumes (the data) stay."""
    r = _compose(["down"], base)
    if r.returncode:
        raise LocalMongoError("Could not remove the built-in MongoDB's containers:\n" + (r.stderr or "")[-600:])


def logs(base: Path | None = None, n: int = 40) -> str:
    """The containers' last `n` log lines each (for "why did it not start")."""
    r = _compose(["logs", "--no-color", "--tail", str(n)], base, timeout=60)
    return (r.stdout or "") + (r.stderr or "")


def _tail(base: Path | None = None, n: int = 15) -> str:
    try:
        return _compose(["logs", "--no-color", "--tail", str(n)], base, timeout=60).stdout[-3000:]
    except Exception:
        return ""


def probe(st: dict) -> dict:
    """{running, ready, version, error} — ready means the app user can sign in and
    search indexes can be managed (mongot connected)."""
    try:
        from . import mongo_store
        cfg = {"name": "local-mongodb", **connection_cfg(st)}
        p = mongo_store.probe(cfg)
        return {"running": bool(p.get("ok")), "ready": bool(p.get("ok") and p.get("vector_search")),
                "version": p.get("version"), "error": p.get("error")}
    except Exception as e:
        return {"running": False, "ready": False, "version": None, "error": str(e)[:300]}


def wait_ready(st: dict, timeout: float = 240) -> dict:
    t0, last = time.time(), {}
    while time.time() - t0 < timeout:
        last = probe(st)
        if last["ready"]:
            last["seconds"] = round(time.time() - t0, 1)
            return last
        time.sleep(1)
    raise LocalMongoError(f"The built-in MongoDB is not ready after {int(timeout)} s: "
                          f"{last.get('error') or 'search not available yet'}")


# MongoDB's search process (mongot 1.78 defaults) stops building new indexes above
# 85 % disk use and resumes below 80 %; above 90 % it stops updating existing ones.
# Its data lives in the container runtime's disk (Docker Desktop's / Colima's VM disk),
# which images fill up — a full disk then shows only as searches that find nothing.
DISK_WARN_PCT = 80
DISK_HINT = ("MongoDB's search stops building indexes when its disk is over 85 % full "
             "(and resumes below 80 %), so new documents would not be found. Free space in "
             "the container runtime (`docker image prune`, `docker builder prune`) or give "
             "its VM a larger disk.")


def disk_usage(base: Path | None = None) -> int | None:
    """Percent used of the disk holding the search indexes (read inside the search
    container), or None when it cannot be read."""
    try:
        r = _compose(["exec", "-T", "search", "df", "-P", "/data/mongot"], base, timeout=60)
        line = (r.stdout or "").strip().splitlines()[-1].split()
        used, avail = int(line[2]), int(line[3])
        return round(100 * used / max(1, used + avail))
    except Exception:
        return None


def disk_note(base: Path | None = None) -> dict:
    pct = disk_usage(base)
    out = {"disk_used_pct": pct}
    if pct is not None and pct >= DISK_WARN_PCT:
        out["disk_warning"] = f"The disk holding MongoDB's search indexes is {pct} % full. {DISK_HINT}"
    return out


def status(base: Path | None = None) -> dict:
    st = load_state(base)
    if not st:
        return {"installed": False}
    out = {"installed": True, "port": st["port"], "project": st["project"],
           "server": SERVER_VERSION, "search": SEARCH_VERSION,
           "license_accepted": st.get("license_accepted")}
    out.update(probe(st))
    out.update(runtime_info())
    if out.get("running"):
        out.update(disk_note(base))
    return out


# ── command line ─────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m visualweaver.local_mongo",
                                 description="The built-in MongoDB (with vector search).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("runtime", help="which container runtime will be used, or why none")
    sub.add_parser("images", help="the pinned images; exit 0 when already downloaded, 4 when not")
    p_init = sub.add_parser("init", help="create the state and compose file (idempotent)")
    p_init.add_argument("--port", type=int)
    sub.add_parser("accept-license", help="record that the MongoDB licence was accepted")
    p_port = sub.add_parser("ensure-port", help="keep the recorded port if usable, else move to the next free one (JSON)")
    p_port.add_argument("--avoid", type=int, action="append", default=[])
    p_port.add_argument("--config", help="config.json to point at the new port when it moves")
    sub.add_parser("pull", help="download the pinned images")
    p_up = sub.add_parser("up", help="start and wait until search is ready")
    p_up.add_argument("--timeout", type=float, default=240)
    sub.add_parser("stop", help="stop the containers (data kept); JSON: was_running, seconds, mongod_exit")
    p_rt = sub.add_parser("ensure-runtime", help="start the container runtime if it is installed but stopped (JSON)")
    p_rt.add_argument("--timeout", type=float, default=300)
    sub.add_parser("release-runtime", help="stop the runtime again if VisualWeaver started it and nothing else runs (JSON)")
    sub.add_parser("down", help="remove the containers (data kept in volumes)")
    p_logs = sub.add_parser("logs", help="the containers' recent log lines")
    p_logs.add_argument("--tail", type=int, default=40)
    sub.add_parser("status", help="JSON status")
    sub.add_parser("connection", help="JSON connection config for the app")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "runtime":
            cmd, why = runtime()
            print(why if not cmd else f"{' '.join(cmd)} — {why}")
            return 0 if cmd else 3
        if a.cmd == "images":
            print(f"  {SERVER_IMAGE.split('@')[0]}   (MongoDB Community Server {SERVER_VERSION})")
            print(f"  {SEARCH_IMAGE.split('@')[0]}         (MongoDB Search {SEARCH_VERSION})")
            print(f"  pinned by digest; {IMAGES_SIZE} on disk, {IDLE_MEMORY} of memory when idle")
            return 0 if images_present() else 4
        if a.cmd == "init":
            st = init(a.port)
            print(json.dumps({"port": st["port"], "project": st["project"]}))
        elif a.cmd == "accept-license":
            st = load_state() or init()
            st["license_accepted"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            save_state(st)
        elif a.cmd == "pull":
            pull()
        elif a.cmd == "ensure-port":
            out = ensure_port(avoid=tuple(a.avoid))
            if out.get("moved_from") and a.config:
                out["config_updated"] = rewire_config(Path(a.config), load_state())
            print(json.dumps(out))
        elif a.cmd == "up":
            out = up(timeout=a.timeout)
            out.update(runtime_info(), server=SERVER_VERSION, search=SEARCH_VERSION)
            out.update(disk_note())
            print(json.dumps(out))
        elif a.cmd == "stop":
            print(json.dumps(stop()))
        elif a.cmd == "ensure-runtime":
            print(json.dumps(ensure_runtime(timeout=a.timeout)))
        elif a.cmd == "release-runtime":
            print(json.dumps(release_runtime()))
        elif a.cmd == "down":
            down()
        elif a.cmd == "logs":
            print(logs(n=a.tail), end="")
        elif a.cmd == "status":
            print(json.dumps(status()))
        elif a.cmd == "connection":
            st = load_state()
            if not st:
                raise LocalMongoError("The built-in MongoDB is not installed (run ./install.sh).")
            print(json.dumps(connection_cfg(st)))
        return 0
    except LocalMongoError as e:
        print(str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
