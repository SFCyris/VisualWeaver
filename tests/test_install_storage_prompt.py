# SPDX-License-Identifier: AGPL-3.0-or-later
"""install.sh's storage choice, port checks and final validation.

These run the REAL blocks of install.sh — the storage question (with MongoDB's
container-runtime and licence checks), the web UI / Redis port checks, and the
"ready to use" validation — in a throwaway repo copy whose scripts/common.sh is
the real one. visualweaver.local_mongo, the port probes and the stores are
replaced by fakes, so nothing is downloaded, started or bound. Interactive cases
run under a pseudo-terminal; non-interactive ones with a closed stdin.
"""
import os
import pathlib
import pty
import re
import select
import shutil
import signal
import subprocess
import textwrap
import time

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INSTALL = _ROOT / "install.sh"


def _between(start: str, end: str) -> str:
    t = _INSTALL.read_text(encoding="utf-8")
    return t[t.index(start):t.index(end)]


# A fake `lm` (visualweaver.local_mongo). FAKE_RT=down makes `runtime` fail the
# way local_mongo does when Docker/Colima is installed but stopped.
_FAKE_LM = textwrap.dedent('''\
    lm() {
      echo "lm $*" >> "${LMLOG}"
      case "$1" in
        runtime)
          if [ "${FAKE_RT:-ok}" = ok ]; then echo "docker compose — docker (colima)"; return 0; fi
          echo "docker is installed but not running. Start it (\\`colima start\\`) and run this again."; return 3 ;;
      esac
      return 0
    }
    ''')


def _repo(tmp_path, env_file=None, redis_installed=False, mongo_installed=None):
    """A scratch checkout + data dir. mongo_installed: None | "accepted" | "unaccepted"."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True, exist_ok=True)
    (repo / "visualweaver").mkdir(exist_ok=True)
    shutil.copy(_ROOT / "scripts" / "common.sh", repo / "scripts" / "common.sh")
    shutil.copy(_ROOT / "visualweaver" / "local_mongo.py", repo / "visualweaver" / "local_mongo.py")
    if env_file is not None:
        (repo / ".visualweaver.env").write_text(env_file)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    if redis_installed:
        (data / "redis").mkdir(exist_ok=True)
        (data / "redis" / "redis.conf").write_text("port 6389\n")
    if mongo_installed:
        (data / "mongodb").mkdir(exist_ok=True)
        acc = '"2026-11-06T10:00:00+0100"' if mongo_installed == "accepted" else "null"
        (data / "mongodb" / "state.json").write_text(f'{{\n  "port": 27019,\n  "license_accepted": {acc}\n}}\n')
    return repo, data


def _harness(tmp_path, repo, body: str, tail: str, pre: str = "") -> pathlib.Path:
    h = tmp_path / "h.sh"
    h.write_text(
        "set -euo pipefail\n"
        '_STORAGE_ENV="${VISUALWEAVER_STORAGE:-}"; _PORT_ENV="${VISUALWEAVER_PORT:-}"\n'
        f'source "{repo}/scripts/common.sh"\n'
        'c_info(){ echo "INFO $*"; }; c_ok(){ echo "OK $*"; }; c_warn(){ echo "WARN $*"; }; c_err(){ echo "ERR $*"; }\n'
        'OS="Darwin"\n' + _FAKE_LM + pre + body + tail)
    return h


def _env(tmp_path, data, extra=None, path="/usr/bin:/bin"):
    e = {"PATH": path, "HOME": str(tmp_path), "VISUALWEAVER_DATA_DIR": str(data),
         "LMLOG": str(tmp_path / "lm.log")}
    e.update(extra or {})
    return e


def _run_tty(h, env, answers):
    if not shutil.which("bash"):
        pytest.skip("bash not available")
    pid, fd = pty.fork()
    if pid == 0:  # child
        os.execvpe("bash", ["bash", str(h)], env)
    out = b""
    deadline = time.monotonic() + 30   # a question nobody answers must fail the test, not hang it
    try:
        os.write(fd, "".join(a + "\n" for a in answers).encode())
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        else:
            os.kill(pid, signal.SIGKILL)
            out += b"\nHARNESS: timed out waiting for the script (an unanswered question?)\n"
    finally:
        _, status = os.waitpid(pid, 0)
        os.close(fd)
    return os.waitstatus_to_exitcode(status), _plain(out.decode(errors="replace"))


def _run_no_tty(h, env):
    r = subprocess.run(["bash", str(h)], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       env=env, timeout=60)
    return r.returncode, _plain(r.stdout + r.stderr)


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


# ── storage choice ───────────────────────────────────────────────────────────

_STORAGE_TAIL = ('echo "RESULT STORAGE=${STORAGE} ACCEPT=${MONGO_ACCEPT} SKIP=${MONGO_SKIP} '
                 'PREFLIGHT=${MONGO_PREFLIGHT}"\n')


def _storage(tmp_path, answers=None, extra=None, path="/usr/bin:/bin", **repo_kw):
    repo, data = _repo(tmp_path, **repo_kw)
    h = _harness(tmp_path, repo, _between("# ── 0. Storage", "# ── 1. Python virtual environment"), _STORAGE_TAIL)
    env = _env(tmp_path, data, extra, path)
    rc, out = _run_tty(h, env, answers) if answers is not None else _run_no_tty(h, env)
    m = re.search(r"RESULT STORAGE=(\S*) ACCEPT=(\d) SKIP=(\d) PREFLIGHT=(\d)", out)
    res = dict(zip(("storage", "accept", "skip", "preflight"), m.groups())) if m else None
    env_file = repo / ".visualweaver.env"
    recorded = None
    if env_file.exists():
        r = re.search(r"^VISUALWEAVER_STORAGE=(\S+)", env_file.read_text(), re.M)
        recorded = r.group(1) if r else None
    lmlog = (tmp_path / "lm.log").read_text() if (tmp_path / "lm.log").exists() else ""
    return rc, out, res, recorded, lmlog


def test_pressing_enter_installs_redis_like_today(tmp_path):
    rc, out, res, recorded, lmlog = _storage(tmp_path, [""])
    assert rc == 0, out
    assert "Choose [1/2/3]" in out and "Redis 8 with search" in out and "MongoDB 9.0 with vector search" in out
    assert res["storage"] == "redis" and recorded == "redis"
    assert "lm runtime" not in lmlog, "a Redis install must not look for a container runtime"
    assert "SSPL" not in out


def test_choosing_mongodb_checks_the_runtime_and_asks_for_the_licence(tmp_path):
    rc, out, res, recorded, lmlog = _storage(tmp_path, ["2", "yes"])
    assert rc == 0, out
    assert res == {"storage": "mongodb", "accept": "1", "skip": "0", "preflight": "1"}
    assert recorded == "mongodb"
    assert "lm runtime" in lmlog and "Container runtime: docker compose — docker (colima)" in out
    assert "Server Side Public License v1" in out
    assert "https://www.mongodb.com/licensing/server-side-public-license" in out
    assert "not distributed with it" in out and "licensed by MongoDB, Inc." in out
    assert "trademark of MongoDB, Inc." in out
    assert out.index("Container runtime") < out.index("── MongoDB licence"), "runtime is checked before the licence"


def test_choosing_both(tmp_path):
    rc, out, res, recorded, _ = _storage(tmp_path, ["3", "yes"])
    assert rc == 0, out
    assert res["storage"] == "both" and res["accept"] == "1" and recorded == "both"


def test_declining_the_licence_offers_redis_and_enter_takes_it(tmp_path):
    rc, out, res, recorded, _ = _storage(tmp_path, ["2", "", ""])
    assert rc == 0, out
    assert "MongoDB licence not accepted" in out and "Continue with Redis 8 with search instead? [Y/n]" in out
    assert res["storage"] == "redis" and res["accept"] == "0" and recorded == "redis"


def test_the_licence_needs_the_word_yes(tmp_path):
    rc, out, res, recorded, _ = _storage(tmp_path, ["2", "y", ""])
    assert rc == 0, out
    assert "MongoDB licence not accepted" in out and res["accept"] == "0" and res["storage"] == "redis"


def test_declining_the_licence_and_redis_stops_without_recording(tmp_path):
    rc, out, res, recorded, _ = _storage(tmp_path, ["2", "no", "n"])
    assert rc != 0 and res is None
    assert "Install stopped" in out and recorded is None


def test_no_runtime_explains_the_options_and_offers_redis(tmp_path):
    rc, out, res, recorded, _ = _storage(tmp_path, ["2", "y"], extra={"FAKE_RT": "down"})
    assert rc == 0, out
    assert "installed but not running" in out and "colima start" in out
    for name in ("Colima", "Podman", "Rancher Desktop", "OrbStack", "Docker Desktop"):
        assert name in out, name
    assert "250 or more employees" in out and "paid Docker subscription" in out
    assert "does not install one for you" in out
    assert "SSPL" not in out, "no licence question without a runtime"
    assert res["storage"] == "redis" and recorded == "redis"


def test_both_without_runtime_continues_with_redis_only(tmp_path):
    rc, out, res, _, _ = _storage(tmp_path, ["3", ""], extra={"FAKE_RT": "down"})
    assert rc == 0, out
    assert "Continue with Redis 8 with search only? [Y/n]" in out and res["storage"] == "redis"


def test_non_interactive_default_is_redis(tmp_path):
    rc, out, res, recorded, lmlog = _storage(tmp_path)
    assert rc == 0, out
    assert "No terminal — installing Redis 8 with search" in out
    assert res["storage"] == "redis" and recorded == "redis" and "lm runtime" not in lmlog


def test_non_interactive_mongodb_without_the_licence_variable_stops(tmp_path):
    rc, out, res, recorded, _ = _storage(tmp_path, extra={"VISUALWEAVER_STORAGE": "mongodb"})
    assert rc != 0 and res is None
    assert "VISUALWEAVER_ACCEPT_MONGODB_LICENSE=1" in out and "Nothing was downloaded" in out
    assert recorded is None


def test_non_interactive_mongodb_with_the_licence_variable(tmp_path):
    rc, out, res, recorded, _ = _storage(
        tmp_path, extra={"VISUALWEAVER_STORAGE": "mongodb", "VISUALWEAVER_ACCEPT_MONGODB_LICENSE": "1"})
    assert rc == 0, out
    assert "Accepted via VISUALWEAVER_ACCEPT_MONGODB_LICENSE=1" in out and "Server Side Public License" in out
    assert res["storage"] == "mongodb" and res["accept"] == "1" and recorded == "mongodb"


def test_non_interactive_mongodb_without_a_runtime_stops(tmp_path):
    rc, out, res, recorded, _ = _storage(
        tmp_path, extra={"VISUALWEAVER_STORAGE": "both", "VISUALWEAVER_ACCEPT_MONGODB_LICENSE": "1",
                         "FAKE_RT": "down"})
    assert rc != 0 and res is None
    assert "MongoDB cannot be set up" in out and "VISUALWEAVER_STORAGE=redis ./install.sh" in out
    assert recorded is None


def test_an_unknown_storage_value_is_refused(tmp_path):
    rc, out, _, _, _ = _storage(tmp_path, extra={"VISUALWEAVER_STORAGE": "postgres"})
    assert rc != 0 and "use redis, mongodb or both" in out


def test_a_recorded_choice_is_kept_and_the_menu_is_not_shown_again(tmp_path):
    rc, out, res, recorded, _ = _storage(tmp_path, [""], env_file="VISUALWEAVER_HOST=127.0.0.1\nVISUALWEAVER_STORAGE=redis\n")
    assert rc == 0, out
    assert "Storage already chosen: Redis 8 with search" in out and "Choose [1/2/3]" not in out
    assert "Also install MongoDB" in out
    assert res["storage"] == "redis" and recorded == "redis"


def test_a_re_run_can_add_the_other_store(tmp_path):
    repo_env = "VISUALWEAVER_HOST=127.0.0.1\nVISUALWEAVER_STORAGE=redis\n"
    rc, out, res, recorded, _ = _storage(tmp_path, ["y", "yes"], env_file=repo_env, redis_installed=True)
    assert rc == 0, out
    assert res["storage"] == "both" and recorded == "both"
    text = (tmp_path / "repo" / ".visualweaver.env").read_text()
    assert "VISUALWEAVER_HOST=127.0.0.1" in text and text.count("VISUALWEAVER_STORAGE=") == 1


def test_nothing_is_removed_when_the_other_store_is_requested(tmp_path):
    rc, out, res, recorded, _ = _storage(
        tmp_path, extra={"VISUALWEAVER_STORAGE": "redis"}, env_file="VISUALWEAVER_STORAGE=mongodb\n",
        mongo_installed="accepted")
    assert rc == 0, out
    assert "stays installed (nothing is removed)" in out
    assert res["storage"] == "both" and recorded == "both"


def test_an_existing_redis_install_without_a_record_counts_as_redis(tmp_path):
    rc, out, res, recorded, _ = _storage(tmp_path, redis_installed=True)
    assert rc == 0, out
    assert "Storage already installed: Redis 8 with search" in out
    assert res["storage"] == "redis" and recorded == "redis"


def test_both_installed_asks_nothing_and_keeps_the_accepted_licence(tmp_path):
    rc, out, res, recorded, _ = _storage(tmp_path, [], redis_installed=True, mongo_installed="accepted")
    assert rc == 0, out
    assert "Storage already installed: Redis 8 with search and MongoDB" in out
    assert "[y/N]" not in out and "Type yes" not in out
    assert "accepted on an earlier install" in out
    assert res["storage"] == "both" and res["accept"] == "0"


def test_an_installed_mongodb_with_its_runtime_stopped_is_kept_not_failed(tmp_path):
    rc, out, res, _, _ = _storage(tmp_path, env_file="VISUALWEAVER_STORAGE=mongodb\n",
                                  mongo_installed="accepted", extra={"FAKE_RT": "down"})
    assert rc == 0, out
    assert "stays installed (its data is kept)" in out
    assert res["storage"] == "mongodb" and res["skip"] == "1"


def test_without_any_python3_the_checks_wait_for_the_virtualenv(tmp_path):
    tools = tmp_path / "tools"; tools.mkdir()
    for name in ("bash", "sh", "sed", "grep", "awk", "mv", "cat", "head", "uname", "lsof", "mkdir", "dirname", "tail", "tr"):
        real = shutil.which(name)
        if real:
            os.symlink(real, tools / name)
    rc, out, res, recorded, lmlog = _storage(tmp_path, extra={"VISUALWEAVER_STORAGE": "mongodb"}, path=str(tools))
    assert rc == 0, out
    assert res["preflight"] == "0" and "lm runtime" not in lmlog
    assert recorded is None, "recorded only once the checks have run"


# ── ports ────────────────────────────────────────────────────────────────────

# FAKE_BUSY: ports something listens on; FAKE_OURS: ports held by VisualWeaver itself.
_FAKE_PORTS = textwrap.dedent('''\
    port_in_use() { case " ${FAKE_BUSY:-} " in *" $1 "*) return 0 ;; esac; return 1; }
    app_port_ours() { case " ${FAKE_OURS:-} " in *" $1 "*) return 0 ;; esac; return 1; }
    redis_is_ours() { case " ${FAKE_OURS:-} " in *" $1 "*) return 0 ;; esac; return 1; }
    port_holder() { printf 'nginx (pid 4242)'; }
    ''')


def _app_port(tmp_path, env_file=None, extra=None):
    repo, data = _repo(tmp_path, env_file=env_file)
    h = _harness(tmp_path, repo, _between("# ── 1b. Web UI port", "# Sections 2–4 run only"),
                 'echo "RESULT APP_PORT=${APP_PORT}"\n', pre=_FAKE_PORTS)
    rc, out = _run_no_tty(h, _env(tmp_path, data, extra))
    env_text = (repo / ".visualweaver.env").read_text() if (repo / ".visualweaver.env").exists() else ""
    return rc, out, env_text


def test_a_taken_web_ui_port_moves_to_the_next_free_one_and_is_recorded(tmp_path):
    rc, out, env_text = _app_port(tmp_path, extra={"FAKE_BUSY": "8420 8421"})
    assert rc == 0, out
    assert "RESULT APP_PORT=8422" in out
    assert "Web UI port 8420 is in use by nginx (pid 4242) — using 8422" in out
    assert re.search(r"^VISUALWEAVER_PORT=8422$", env_text, re.M)


def test_a_web_ui_port_held_by_visualweaver_itself_is_kept(tmp_path):
    rc, out, env_text = _app_port(tmp_path, extra={"FAKE_BUSY": "8420", "FAKE_OURS": "8420"})
    assert rc == 0 and "RESULT APP_PORT=8420" in out and "VisualWeaver is running on it" in out
    assert "VISUALWEAVER_PORT" not in env_text


def test_a_recorded_web_ui_port_is_kept_on_a_re_run(tmp_path):
    rc, out, env_text = _app_port(tmp_path, env_file="VISUALWEAVER_PORT=9100\n", extra={"FAKE_BUSY": "8420"})
    assert rc == 0 and "RESULT APP_PORT=9100" in out and "Web UI port: 9100" in out
    assert env_text == "VISUALWEAVER_PORT=9100\n", "an unchanged port is not rewritten"


def test_a_recorded_web_ui_port_taken_since_is_moved_and_rewritten(tmp_path):
    rc, out, env_text = _app_port(tmp_path, env_file="# mine\nVISUALWEAVER_PORT=9100\n", extra={"FAKE_BUSY": "9100"})
    assert rc == 0 and "RESULT APP_PORT=9101" in out
    assert env_text.count("VISUALWEAVER_PORT=") == 1 and "VISUALWEAVER_PORT=9101" in env_text
    assert env_text.startswith("# mine\n")


def _redis_port(tmp_path, conf_port=None, extra=None, app_port="8420"):
    repo, data = _repo(tmp_path)
    if conf_port:
        (data / "redis").mkdir(exist_ok=True)
        (data / "redis" / "redis.conf").write_text(f"port {conf_port}\n")
    block = _between("# ── 3. Choose a dedicated Redis port", "# ── 4. Generate the dedicated redis.conf")
    h = _harness(tmp_path, repo, block, 'echo "RESULT CHOSEN_PORT=${CHOSEN_PORT}"\n',
                 pre=_FAKE_PORTS + f"APP_PORT={app_port}\n")
    return _run_no_tty(h, _env(tmp_path, data, extra))


def test_a_running_redis_of_this_install_keeps_its_port(tmp_path):
    rc, out = _redis_port(tmp_path, conf_port=6389, extra={"FAKE_BUSY": "6389", "FAKE_OURS": "6389"})
    assert rc == 0 and "RESULT CHOSEN_PORT=6389" in out and "in use" not in out


def test_a_redis_port_taken_by_something_else_moves_and_names_the_holder(tmp_path):
    rc, out = _redis_port(tmp_path, conf_port=6389, extra={"FAKE_BUSY": "6389"})
    assert rc == 0 and "RESULT CHOSEN_PORT=6390" in out
    assert "Redis port 6389 is in use by nginx (pid 4242) — using 6390" in out


def test_this_installs_redis_is_recognised_through_a_relative_data_dir(tmp_path):
    """Redis reports the directory it changed into (physical path); the data dir may
    be given with `..` — the comparison must still say "ours", or a re-run while
    Redis runs moves it to a second port and starts a second server on the same data."""
    repo, data = _repo(tmp_path)
    (data / "redis" / "data").mkdir(parents=True)
    fake = 'rcli() { printf "dir\\n%s\\n" "' + str(data.resolve()) + '/redis/data"; }\n'
    h = _harness(tmp_path, repo, 'redis_is_ours 6389 && echo OURS || echo NOT_OURS\n'
                 'REDIS_DATA=/elsewhere; redis_is_ours 6389 && echo OURS2 || echo NOT_OURS2\n', "", pre=fake)
    rc, out = _run_no_tty(h, _env(tmp_path, tmp_path / "repo" / ".." / "data"))
    assert rc == 0, out
    assert "OURS" in out.split() and "NOT_OURS2" in out.split()


def test_the_redis_port_never_takes_the_web_ui_port(tmp_path):
    rc, out = _redis_port(tmp_path, extra={"VISUALWEAVER_REDIS_PORT": "8420"})
    assert rc == 0 and "RESULT CHOSEN_PORT=8421" in out and "in use by the web UI" in out


# ── validation ───────────────────────────────────────────────────────────────

_FAKE_STORES = textwrap.dedent('''\
    redis_up() { [ "${FAKE_REDIS:-ok}" != down ]; }
    rcli() {
      case "$*" in
        *FT._LIST*) [ "${FAKE_REDIS:-ok}" = ok ] ;;
        *ping*) [ "${FAKE_REDIS:-ok}" != down ] ;;
        *) return 0 ;;
      esac
    }
    redis_versions() { printf '8.8.1 8.8.0'; }
    lm() {
      case "$1" in
        status)
          if [ "${FAKE_MONGO:-ok}" = ok ]; then
            echo '{"installed": true, "ready": true, "version": "9.0.4", "search": "1.78.0", "runtime": "docker (colima)", "runtime_version": "29.1.0"}'
          else
            echo '{"installed": true, "ready": false, "error": "search not available yet", "runtime": "docker (colima)", "runtime_version": "29.1.0"}'
          fi ;;
        runtime) echo "docker compose — docker (colima)" ;;
      esac
    }
    ''')


def _validate(tmp_path, storage="both", extra=None):
    repo, data = _repo(tmp_path)
    venv = repo / "venv" / "bin"; venv.mkdir(parents=True)
    (venv / "python").write_text(textwrap.dedent('''\
        #!/bin/sh
        case "$2" in
          *visualweaver.main*)
            if [ -n "${FAKE_PY_FAIL:-}" ]; then echo "ModuleNotFoundError: No module named 'fitz'" >&2; exit 1; fi
            echo 3.12.4; exit 0 ;;
        esac
        exec python3 "$@"
        '''))
    (venv / "python").chmod(0o755)
    (repo / "visualweaver" / "__init__.py").write_text('__version__ = "1.14.0"\n')
    pre = (_FAKE_PORTS + _FAKE_STORES + f'VENV="{repo}/venv"; STORAGE={storage}; CHOSEN_PORT=6389; '
           'MONGO_PORT=27019; MONGO_SKIP=0; APP_HOST=127.0.0.1; APP_PORT=8420; REDIS_MODULE_SEARCH=/x/redisearch.so\n'
           'storage_has() { [ "${STORAGE}" = "$1" ] || [ "${STORAGE}" = "both" ]; }\n')
    h = _harness(tmp_path, repo, _between("# ── 7. Validation", "# ── 8. Summary"),
                 'echo "RESULT VALID VER_REDIS=${VER_REDIS} VER_MONGO=${VER_MONGO} VER_RT=${VER_RT} VER_PY=${VER_PY}"\n',
                 pre=pre)
    return _run_no_tty(h, _env(tmp_path, data, extra))


def test_validation_passes_with_versions(tmp_path):
    rc, out = _validate(tmp_path)
    assert rc == 0, out
    assert "✓ Python venv" in out and "Python 3.12.4, VisualWeaver 1.14.0 imports" in out
    assert "✓ Redis" in out and "127.0.0.1:6389  Redis 8.8.1, search 8.8.0" in out
    assert "✓ MongoDB" in out and "127.0.0.1:27019  MongoDB 9.0.4, search 1.78.0" in out
    assert "✓ Container runtime  docker (colima) 29.1.0" in out
    assert "✓ Web UI port" in out and "✗" not in out
    assert "RESULT VALID VER_REDIS=8.8.1 VER_MONGO=9.0.4 VER_RT=docker (colima) 29.1.0 VER_PY=3.12.4" in out


def test_validation_redis_only_does_not_touch_mongodb(tmp_path):
    rc, out = _validate(tmp_path, storage="redis", extra={"FAKE_MONGO": "down"})
    assert rc == 0, out
    assert "MongoDB" not in out and "Container runtime" not in out


def test_a_redis_without_search_fails_validation_with_the_fix(tmp_path):
    rc, out = _validate(tmp_path, storage="redis", extra={"FAKE_REDIS": "nosearch"})
    assert rc != 0
    assert "✗ Redis" in out and "search module is not loaded" in out
    assert "not ready to use" in out and "loadmodule (/x/redisearch.so) did not load" in out


def test_a_mongodb_that_is_not_ready_fails_validation(tmp_path):
    rc, out = _validate(tmp_path, storage="mongodb", extra={"FAKE_MONGO": "down"})
    assert rc != 0
    assert "✗ MongoDB" in out and "search not available yet" in out and "local_mongo logs" in out


def test_a_broken_venv_fails_validation(tmp_path):
    rc, out = _validate(tmp_path, storage="redis", extra={"FAKE_PY_FAIL": "1"})
    assert rc != 0
    assert "✗ Python venv" in out and "No module named 'fitz'" in out and "Delete" in out


def test_a_web_ui_port_taken_by_another_program_fails_validation(tmp_path):
    rc, out = _validate(tmp_path, storage="redis", extra={"FAKE_BUSY": "8420"})
    assert rc != 0
    assert "✗ Web UI port" in out and "nginx (pid 4242)" in out and "VISUALWEAVER_PORT=<port> ./install.sh" in out


def test_a_web_ui_port_served_by_visualweaver_passes(tmp_path):
    rc, out = _validate(tmp_path, storage="redis", extra={"FAKE_BUSY": "8420", "FAKE_OURS": "8420"})
    assert rc == 0, out
    assert "served by VisualWeaver already" in out


# ── app config (config.json) ─────────────────────────────────────────────────

def _config_script() -> str:
    block = _between("# ── 5. Point the app config at the installed stores", "# ── 5b. Start the built-in MongoDB")
    return block[block.index("<<'PYEOF'\n") + len("<<'PYEOF'\n"):block.index("\nPYEOF")]


_MONGO = {"type": "mongodb", "uri": "mongodb://127.0.0.1:27019/?authSource=admin", "username": "visualweaver",
          "password": "pw", "database": "visualweaver", "builtin": True}


def _write_config(tmp_path, cfg, redis_port="", mongo=None, redis_before="0", mongo_before="0", fresh="0"):
    import json
    import sys
    path = tmp_path / "config.json"
    if cfg is not None:
        path.write_text(json.dumps(cfg))
    script = tmp_path / "cfg.py"
    script.write_text(_config_script())
    r = subprocess.run([sys.executable, str(script), str(path), redis_port, json.dumps(mongo) if mongo else "",
                        redis_before, mongo_before, fresh], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert oct(path.stat().st_mode & 0o777) == "0o600", "config.json holds the MongoDB password"
    return json.loads(path.read_text())


def test_a_mongodb_first_install_has_no_builtin_redis(tmp_path):
    template = {"redis": {"host": "localhost", "port": 6379}, "redis_endpoints": [], "provider": "ollama"}
    c = _write_config(tmp_path, template, mongo=_MONGO)
    assert c["storage"] == {"default_type": "mongodb", "app": "default"}
    assert "redis" not in c and c["mongodb"] == _MONGO and c["redis_endpoints"] == []
    assert c["provider"] == "ollama", "other settings are kept"


def test_a_mongodb_only_install_from_the_shipped_example_config_has_mongodb_as_default(tmp_path):
    """A fresh install starts from config.example.json: whatever storage it carries must
    not make Redis the built-in store of an install that has no Redis."""
    import json
    example = json.loads((_ROOT / "config.example.json").read_text())
    c = _write_config(tmp_path, example, mongo=_MONGO, fresh="1")
    assert c["storage"]["default_type"] == "mongodb" and c["storage"]["app"] == "default"
    assert c["mongodb"] == _MONGO and "redis" not in c and c["redis_endpoints"] == []
    # even if the example did name a store, a config created this run ignores it
    c = _write_config(tmp_path, {**example, "storage": {"default_type": "redis", "app": "default"}},
                      mongo=_MONGO, fresh="1")
    assert c["storage"]["default_type"] == "mongodb" and "redis" not in c


def test_redis_added_to_a_mongodb_first_install_is_local_redis(tmp_path):
    c = _write_config(tmp_path, None, mongo=_MONGO)
    c = _write_config(tmp_path, c, redis_port="6389", mongo=_MONGO, mongo_before="1")
    assert c["storage"]["default_type"] == "mongodb" and "redis" not in c
    assert c["redis_endpoints"] == [{"name": "local-redis", "type": "redis", "host": "127.0.0.1", "port": 6389,
                                     "db": 0, "password": "", "ssl": False, "builtin": True}]


def test_both_on_a_first_install_makes_redis_the_default(tmp_path):
    c = _write_config(tmp_path, None, redis_port="6389", mongo=_MONGO)
    assert c["storage"]["default_type"] == "redis"
    assert c["redis"]["host"] == "127.0.0.1" and c["redis"]["port"] == 6389
    assert c["redis_endpoints"] == [{"name": "local-mongodb", **_MONGO}]
    assert "mongodb" not in c


def test_an_existing_redis_install_keeps_redis_as_default_when_mongodb_is_added(tmp_path):
    old = {"redis": {"host": "127.0.0.1", "port": 6389, "db": 0, "password": "", "ssl": False},
           "redis_endpoints": [{"name": "atlas", "type": "mongodb", "uri": "mongodb+srv://x"}]}
    c = _write_config(tmp_path, old, redis_port="6389", mongo=_MONGO, redis_before="1")
    assert c["storage"]["default_type"] == "redis"
    assert [e["name"] for e in c["redis_endpoints"]] == ["atlas", "local-mongodb"]


def test_re_runs_update_in_place_and_never_touch_the_app_store_choice(tmp_path):
    c = _write_config(tmp_path, None, redis_port="6389", mongo=_MONGO)
    c["storage"]["app"] = "local-mongodb"                      # chosen in the UI
    c["redis_endpoints"][0]["label_extra"] = "kept"
    moved = {**_MONGO, "uri": "mongodb://127.0.0.1:27020/?authSource=admin"}
    c = _write_config(tmp_path, c, redis_port="6390", mongo=moved, redis_before="1", mongo_before="1")
    c = _write_config(tmp_path, c, redis_port="6390", mongo=moved, redis_before="1", mongo_before="1")
    assert c["storage"] == {"default_type": "redis", "app": "local-mongodb"}
    assert len(c["redis_endpoints"]) == 1, "no duplicate built-in entries"
    assert c["redis_endpoints"][0]["uri"].startswith("mongodb://127.0.0.1:27020")
    assert c["redis_endpoints"][0]["label_extra"] == "kept"
    assert c["redis"]["port"] == 6390


def test_a_web_ui_port_given_on_the_command_line_is_recorded(tmp_path):
    rc, out, env_text = _app_port(tmp_path, env_file="VISUALWEAVER_HOST=127.0.0.1\n",
                                  extra={"VISUALWEAVER_PORT": "9200"})
    assert rc == 0 and "RESULT APP_PORT=9200" in out
    assert "VISUALWEAVER_HOST=127.0.0.1\n" in env_text and re.search(r"^VISUALWEAVER_PORT=9200$", env_text, re.M)


# ── common.sh helpers, for real ──────────────────────────────────────────────

def _common(tmp_path, body, env_file=None, extra=None, path="/usr/bin:/bin:/usr/sbin:/sbin"):
    repo, data = _repo(tmp_path, env_file=env_file)
    h = tmp_path / "c.sh"
    h.write_text(f'set -euo pipefail\nsource "{repo}/scripts/common.sh"\n' + body)
    r = subprocess.run(["bash", str(h)], capture_output=True, text=True, timeout=60,
                       env={"PATH": path, "HOME": str(tmp_path), "VISUALWEAVER_DATA_DIR": str(data), **(extra or {})})
    return r.returncode, r.stdout + r.stderr, repo, data


def test_envfile_set_keeps_a_last_line_without_newline(tmp_path):
    rc, out, repo, _ = _common(tmp_path, 'envfile_set VISUALWEAVER_STORAGE both "# storage"\n',
                               env_file="VISUALWEAVER_HOST=127.0.0.1")
    assert rc == 0, out
    assert (repo / ".visualweaver.env").read_text() == "VISUALWEAVER_HOST=127.0.0.1\n# storage\nVISUALWEAVER_STORAGE=both\n"


def test_envfile_set_replaces_an_export_line_instead_of_adding_a_second(tmp_path):
    rc, out, repo, _ = _common(tmp_path, 'envfile_set VISUALWEAVER_PORT 9300\necho "GOT=$(envfile_get VISUALWEAVER_PORT)"\n',
                               env_file="export VISUALWEAVER_PORT=9100\n")
    assert rc == 0, out
    assert (repo / ".visualweaver.env").read_text() == "VISUALWEAVER_PORT=9300\n" and "GOT=9300" in out


def test_a_port_with_only_time_wait_connections_is_free_and_a_listener_is_not(tmp_path):
    """After a server closes connections they linger in TIME_WAIT; a plain bind test
    called that port taken, so restart.sh refused to start and install moved ports."""
    import socket
    import sys
    import time as _t
    venv = tmp_path / "repo" / "venv" / "bin"; venv.mkdir(parents=True)
    os.symlink(sys.executable, venv / "python")
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0)); srv.listen(); port = srv.getsockname()[1]
    c = socket.create_connection(("127.0.0.1", port)); a, _ = srv.accept()
    a.close(); _t.sleep(0.2); c.close(); srv.close(); _t.sleep(0.2)          # server closed first → TIME_WAIT
    rc, out, _, _ = _common(tmp_path, f'port_in_use {port} && echo BUSY || echo FREE\n')
    assert rc == 0 and out.strip() == "FREE", out
    other = socket.socket(); other.bind(("0.0.0.0", 0)); other.listen(); busy = other.getsockname()[1]
    try:
        # lsof sees this test's own socket; hide it to exercise the bind test alone.
        fake = tmp_path / "fakebin"; fake.mkdir()
        (fake / "lsof").write_text("#!/bin/sh\nexit 1\n"); (fake / "lsof").chmod(0o755)
        rc, out, _, _ = _common(tmp_path, f'port_in_use {busy} && echo BUSY || echo FREE\n',
                                path=f"{fake}:/usr/bin:/bin")
        assert out.strip() == "BUSY", out
    finally:
        other.close()


def test_only_this_installs_app_counts_as_holding_the_web_ui_port(tmp_path):
    fake = tmp_path / "fakebin"; fake.mkdir()
    (fake / "lsof").write_text("#!/bin/sh\necho 4242\n"); (fake / "lsof").chmod(0o755)
    (fake / "ps").write_text('#!/bin/sh\necho "$FAKE_CMD"\n'); (fake / "ps").chmod(0o755)
    path = f"{fake}:/usr/bin:/bin"
    repo = tmp_path / "repo"
    body = 'app_port_ours 8420 && echo OURS || echo FOREIGN\n'
    _, out, _, _ = _common(tmp_path, body, path=path, extra={
        "FAKE_CMD": "/x/venv/bin/python -m uvicorn visualweaver.main:app --app-dir /other/checkout --host 0.0.0.0"})
    assert out.strip() == "FOREIGN", "another checkout's VisualWeaver is not ours"
    _, out, _, _ = _common(tmp_path, body, path=path, extra={
        "FAKE_CMD": f"{repo}/venv/bin/python -m uvicorn visualweaver.main:app --app-dir {repo} --host 0.0.0.0"})
    assert out.strip() == "OURS"
    (tmp_path / "data" / "visualweaver.pid").write_text("4242\n")
    _, out, _, _ = _common(tmp_path, body, path=path, extra={"FAKE_CMD": "something else"})
    assert out.strip() == "OURS", "the pid in this data dir's pidfile is ours"


# ── MongoDB provisioning and start (sections 4b, 5b) with a fake local_mongo ─

_FAKE_LM_PROVISION = textwrap.dedent('''\
    lm() {
      echo "lm $*" >> "${LMLOG}"
      case "$1" in
        init) mkdir -p "${MONGO_DIR}"; printf '{\\n  "port": %s\\n}\\n' "${FAKE_PORT:-27019}" > "${MONGO_STATE}" ;;
        ensure-port)
          if [ -n "${FAKE_MOVE:-}" ]; then
            printf '{\\n  "port": 27020\\n}\\n' > "${MONGO_STATE}"
            echo '{"port": 27020, "moved_from": 27019, "holder": "mongod (pid 77)"}'
          else echo '{"port": 27019, "moved_from": null, "holder": ""}'; fi ;;
        images) echo "  mongodb/mongodb-community-server:9.0.4-ubi9-slim"; [ -n "${FAKE_PULLED:-}" ] && return 0; return 4 ;;
        pull) return "${FAKE_PULL_RC:-0}" ;;
        up)
          if [ -n "${FAKE_UP_FAIL:-}" ]; then echo "The built-in MongoDB did not start:"; echo "mongod | boom"; return 2; fi
          if [ -n "${FAKE_RUNNING:-}" ]; then echo '{"ready": true, "already_running": true, "total_seconds": 0.2}'
          else echo '{"ready": true, "total_seconds": 11.7}'; fi ;;
      esac
      return 0
    }
    ''')


def _provision(tmp_path, extra=None, accept="1"):
    repo, data = _repo(tmp_path)
    block = _between("# ── 4b. MongoDB with vector search", "# ── 5. Point the app config") + \
        _between("# ── 5b. Start the built-in MongoDB", "# ── 6. Network exposure")
    pre = (_FAKE_LM_PROVISION + f'STORAGE=mongodb; MONGO_ACCEPT={accept}; MONGO_SKIP=0; APP_PORT=8420; CHOSEN_PORT=""\n'
           'storage_has() { [ "${STORAGE}" = "$1" ] || [ "${STORAGE}" = "both" ]; }\n'
           'MONGO_VER=9.0\n')
    h = _harness(tmp_path, repo, block, 'echo "RESULT MONGO_PORT=${MONGO_PORT}"\n', pre=pre)
    rc, out = _run_no_tty(h, _env(tmp_path, data, extra))
    lmlog = (tmp_path / "lm.log").read_text() if (tmp_path / "lm.log").exists() else ""
    return rc, out, lmlog


def test_mongodb_is_initialised_its_licence_recorded_and_its_images_pulled(tmp_path):
    rc, out, lmlog = _provision(tmp_path)
    assert rc == 0, out
    calls = [l.split()[1] for l in lmlog.splitlines()]
    assert calls[:3] == ["init", "ensure-port", "accept-license"], calls
    assert "pull" in calls and calls.index("images") < calls.index("pull") < calls.index("up")
    assert "mongodb/mongodb-community-server:9.0.4-ubi9-slim" in out and "Downloading (one time)" in out
    assert "MongoDB ready with vector search on 127.0.0.1:27019 (11.7 s)." in out
    assert "RESULT MONGO_PORT=27019" in out


def test_an_earlier_licence_acceptance_is_not_recorded_again_and_images_are_not_pulled_twice(tmp_path):
    rc, out, lmlog = _provision(tmp_path, accept="0", extra={"FAKE_PULLED": "1", "FAKE_RUNNING": "1"})
    assert rc == 0, out
    assert "accept-license" not in lmlog and "lm pull" not in lmlog
    assert "Already downloaded." in out and "MongoDB already running with vector search" in out


def test_a_taken_mongodb_port_is_reported_with_its_holder(tmp_path):
    rc, out, _ = _provision(tmp_path, extra={"FAKE_MOVE": "1"})
    assert rc == 0, out
    assert "MongoDB port 27019 is in use by mongod (pid 77) — using 27020." in out
    assert "RESULT MONGO_PORT=27020" in out


def test_a_failed_download_or_start_stops_the_install_with_the_reason(tmp_path):
    rc, out, _ = _provision(tmp_path, extra={"FAKE_PULL_RC": "1"})
    assert rc != 0 and "Could not download the MongoDB images" in out
    rc, out, _ = _provision(tmp_path / "b", extra={"FAKE_UP_FAIL": "1"}) if (tmp_path / "b").mkdir() is None else (0, "", "")
    assert rc != 0 and "did not become ready" in out and "mongod | boom" in out


def test_a_users_own_connection_named_like_a_builtin_is_never_taken_over(tmp_path):
    mine = {"name": "local-mongodb", "type": "mongodb", "uri": "mongodb://10.0.0.5:27017", "username": "me",
            "password": "pw", "database": "kb"}
    old = {"redis": {"host": "127.0.0.1", "port": 6389}, "storage": {"default_type": "redis", "app": "default"},
           "redis_endpoints": [mine]}
    c = _write_config(tmp_path, old, redis_port="6389", mongo=_MONGO, redis_before="1")
    eps = {e["name"]: e for e in c["redis_endpoints"]}
    assert eps["local-mongodb"] == mine, "the user's connection is untouched"
    assert eps["local-mongodb-2"] == {"name": "local-mongodb-2", **_MONGO}
    # a re-run updates the built-in where it is, without adding a third entry
    moved = {**_MONGO, "uri": "mongodb://127.0.0.1:27020/?authSource=admin"}
    c = _write_config(tmp_path, c, redis_port="6389", mongo=moved, redis_before="1", mongo_before="1")
    assert [e["name"] for e in c["redis_endpoints"]] == ["local-mongodb", "local-mongodb-2"]
    assert c["redis_endpoints"][1]["uri"].startswith("mongodb://127.0.0.1:27020")
