# SPDX-License-Identifier: AGPL-3.0-or-later
"""start.sh / stop.sh start and stop whichever stores install.sh set up.

Runs the REAL scripts in a throwaway checkout + data dir against fakes: a fake
venv python (uvicorn = a sleeping process, `-m visualweaver.local_mongo` =
canned answers), a fake redis-server / redis-cli, and a fake curl answering the
app's /api/health. Nothing real is started and no real port is used.
"""
import json
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import textwrap

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _exe(path: pathlib.Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    path.chmod(0o755)


@pytest.fixture
def stack(tmp_path):
    if not shutil.which("bash"):
        pytest.skip("bash not available")
    repo = tmp_path / "repo"
    for f in ("start.sh", "stop.sh", "scripts/common.sh", "visualweaver/__init__.py"):
        (repo / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(_ROOT / f, repo / f)
    data = tmp_path / "data"
    bindir = tmp_path / "bin"
    log = tmp_path / "calls.log"
    _exe(repo / "venv" / "bin" / "python", f'''\
        #!/bin/sh
        if [ "$1" = "-m" ] && [ "$2" = "uvicorn" ]; then
          env > "{tmp_path}/app.env"; exec sleep 60
        fi
        if [ "$1" = "-m" ] && [ "$2" = "visualweaver.local_mongo" ]; then
          echo "lm $3" >> "{log}"
          case "$3" in
            up)
              if [ "${{FAKE_MONGO:-ok}}" = down ]; then
                echo "docker is installed but not running. Start it (\\`colima start\\`) and run this again." >&2; exit 2
              fi
              echo '{{"running": true, "ready": true, "version": "9.0.4", "total_seconds": 11.7, "runtime": "docker (colima)", "runtime_version": "29.1.0", "server": "9.0.4", "search": "1.78.0"}}'
              exit 0 ;;
            stop) exit 0 ;;
          esac
          exit 0
        fi
        exec python3 "$@"
        ''')
    _exe(bindir / "curl", '''\
        #!/bin/sh
        echo '{"app": "VisualWeaver", "version": "1.14.0"}'
        ''')
    marker = tmp_path / "redis.running"
    _exe(bindir / "fake-redis-server", f'''\
        #!/bin/sh
        echo "redis-server $*" >> "{log}"; touch "{marker}"
        ''')
    _exe(bindir / "fake-redis-cli", f'''\
        #!/bin/sh
        shift 2   # -p PORT
        case "$*" in
          ping) [ -f "{marker}" ] && echo PONG ;;
          FT._LIST) [ -f "{marker}" ] ;;
          "INFO server") printf 'redis_version:8.8.1\\r\\nredis_mode:standalone\\r\\n' ;;
          "MODULE LIST") printf 'name\\nsearch\\nver\\n80800\\npath\\n/x/redisearch.so\\nargs\\n' ;;
          "shutdown save") echo "redis shutdown" >> "{log}"; rm -f "{marker}" ;;
        esac
        ''')
    port = _free_port()
    env = {"PATH": f"{bindir}:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(tmp_path),
           "VISUALWEAVER_DATA_DIR": str(data), "VISUALWEAVER_PORT": str(port), "VISUALWEAVER_HOST": "127.0.0.1"}

    def install(redis=False, mongo=False, default_type="redis", app="default", env_file=None):
        data.mkdir(exist_ok=True)
        if redis:
            (data / "redis").mkdir(exist_ok=True)
            (data / "redis" / "redis.conf").write_text("port 6389\n")
            (repo / ".redis").mkdir(exist_ok=True)
            (repo / ".redis" / "redis-env.sh").write_text(
                f'REDIS_SERVER="{bindir}/fake-redis-server"\nREDIS_CLI="{bindir}/fake-redis-cli"\n'
                'REDIS_MODULE_SEARCH="/x/redisearch.so"\nREDIS_DYLD_FALLBACK=""\nREDIS_SOURCE="fake Redis"\n')
        if mongo:
            (data / "mongodb").mkdir(exist_ok=True)
            (data / "mongodb" / "state.json").write_text('{\n  "port": 27019,\n  "project": "vwa-x"\n}\n')
        (data / "config.json").write_text(json.dumps({"storage": {"default_type": default_type, "app": app}}))
        if env_file is not None:
            (repo / ".visualweaver.env").write_text(env_file)

    def run(script, extra=None):
        r = subprocess.run(["bash", str(repo / script)], capture_output=True, text=True,
                           env={**env, **(extra or {})}, timeout=120)
        out = re.sub(r"\x1b\[[0-9;]*m", "", r.stdout + r.stderr)
        calls = log.read_text() if log.exists() else ""
        return r.returncode, out, calls

    yield type("Stack", (), {"install": staticmethod(install), "run": staticmethod(run),
                             "tmp": tmp_path, "data": data, "port": port})
    pidf = data / "visualweaver.pid"
    if pidf.exists():
        try:
            os.kill(int(pidf.read_text().strip()), signal.SIGKILL)
        except (ValueError, ProcessLookupError):
            pass


def test_a_mongodb_only_install_starts_without_any_redis(stack):
    stack.install(mongo=True, default_type="mongodb")
    rc, out, calls = stack.run("start.sh")
    assert rc == 0, out
    assert "redis" not in calls.lower(), "nothing Redis may be started"
    assert "Redis" not in out, out
    assert "lm up" in calls and "MongoDB up, vector search ready (11.7 s)" in out
    assert "MongoDB:  127.0.0.1:27019 (built-in, vector search, containers) — app data & answer cache" in out
    assert re.search(r"Versions: VisualWeaver 1\.14\.0 · Python \d+\.\d+", out)
    assert "MongoDB 9.0.4 · search 1.78.0 · docker (colima) 29.1.0" in out
    assert "local_mongo logs" in out


def test_both_stores_start_and_report_their_versions(stack):
    stack.install(redis=True, mongo=True)
    rc, out, calls = stack.run("start.sh")
    assert rc == 0, out
    assert "redis-server" in calls and "lm up" in calls
    assert "Redis:    127.0.0.1:6389 (built-in, AOF everysec) — app data & answer cache" in out
    assert "MongoDB:  127.0.0.1:27019 (built-in, vector search, containers)\n" in out
    assert "Redis 8.8.1 · search 8.8.0" in out
    assert "MongoDB 9.0.4 · search 1.78.0" in out


def test_a_redis_only_install_never_calls_local_mongo(stack):
    stack.install(redis=True)
    rc, out, calls = stack.run("start.sh")
    assert rc == 0, out
    assert "lm " not in calls and "MongoDB" not in out
    assert "Redis 8.8.1 · search 8.8.0" in out


def test_a_stopped_runtime_does_not_block_a_redis_first_install(stack):
    stack.install(redis=True, mongo=True)
    rc, out, _ = stack.run("start.sh", {"FAKE_MONGO": "down"})
    assert rc == 0, out
    assert "The built-in MongoDB did not start" in out and "colima start" in out
    assert "Continuing without it" in out and "MongoDB:  NOT running" in out


def test_a_stopped_runtime_stops_a_mongodb_first_start_with_the_fix(stack):
    stack.install(mongo=True, default_type="mongodb")
    rc, out, _ = stack.run("start.sh", {"FAKE_MONGO": "down"})
    assert rc != 0
    assert "The built-in MongoDB did not start" in out and "colima start" in out
    assert not (stack.data / "visualweaver.pid").exists(), "the app must not start without its store"


def test_nothing_installed_says_run_install(stack):
    stack.data.mkdir()
    rc, out, _ = stack.run("start.sh")
    assert rc != 0 and "run ./install.sh first" in out


def test_the_recorded_storage_choice_is_not_handed_to_the_app(stack):
    """VISUALWEAVER_STORAGE in .visualweaver.env records install.sh's choice; the app
    reads it as the container deployment's switch, so a local start must not pass it."""
    stack.install(mongo=True, default_type="mongodb", env_file="VISUALWEAVER_STORAGE=mongodb\n")
    rc, out, _ = stack.run("start.sh")
    assert rc == 0, out
    app_env = (stack.tmp / "app.env").read_text()
    assert "VISUALWEAVER_STORAGE" not in app_env


def test_stop_stops_mongodb_and_keeps_its_data(stack):
    stack.install(mongo=True, default_type="mongodb")
    assert stack.run("start.sh")[0] == 0
    rc, out, calls = stack.run("stop.sh")
    assert rc == 0, out
    assert "lm stop" in calls and "MongoDB stopped." in out and "data kept" in out
    assert "Redis" not in out
    assert not (stack.data / "visualweaver.pid").exists()


def test_stop_with_both_stops_both(stack):
    stack.install(redis=True, mongo=True)
    assert stack.run("start.sh")[0] == 0
    rc, out, calls = stack.run("stop.sh")
    assert rc == 0, out
    assert "redis shutdown" in calls and "lm stop" in calls
    assert "Redis stopped." in out and "MongoDB stopped." in out


def test_the_search_version_is_read_after_redis_has_loaded_its_data(tmp_path):
    """A Redis that was just started answers INFO while it loads its data but refuses
    MODULE LIST ("LOADING") — the version check waits instead of printing "search ?"."""
    import pathlib
    import subprocess
    root = pathlib.Path(__file__).resolve().parents[1]
    t = (root / "scripts" / "common.sh").read_text()
    fn = t[t.index("redis_versions() {"):t.index("\n}\n", t.index("redis_versions() {")) + 3]
    n = tmp_path / "n"
    n.write_text("0")
    stub = f'''
rcli() {{
  shift 2
  c=$(cat "{n}")
  case "$1 $2" in
    "INFO persistence") c=$((c + 1)); echo "$c" > "{n}"; [ "$c" -le 2 ] && echo "loading:1" || echo "loading:0" ;;
    "INFO server") echo "redis_version:8.8.1" ;;
    "MODULE LIST") [ "$c" -le 2 ] && {{ echo "LOADING Redis is loading the dataset in memory"; return 1; }}
                   printf 'name\\nsearch\\nver\\n80800\\npath\\n/x\\nname\\nvectorset\\nver\\n1\\n' ;;
  esac
}}
sleep() {{ :; }}
'''
    r = subprocess.run(["bash", "-c", "set -euo pipefail\n" + stub + fn + "\nredis_versions 6391"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "8.8.1 8.8.0", r.stdout
