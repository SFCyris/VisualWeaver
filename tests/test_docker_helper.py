# SPDX-License-Identifier: AGPL-3.0-or-later
"""./docker.sh with a fake `docker` on PATH: the storage choice, the MongoDB licence
gate, the Compose version check, the port check and what it asks Compose to do."""
import os
import pathlib
import shutil
import socket
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]

FAKE_DOCKER = r"""#!/bin/sh
echo "$*" >> "$FAKE_LOG"
case "$1 $2" in
  "compose version") echo "${FAKE_COMPOSE_VERSION:-2.30.0}"; exit 0 ;;
  "info "*|"info") exit 0 ;;
esac
case "$1" in
  info) exit 0 ;;
  ps) exit 0 ;;
  inspect) exit 1 ;;
  compose) exit 0 ;;
esac
exit 0
"""


@pytest.fixture
def repo(tmp_path):
    if not shutil.which("bash"):
        pytest.skip("bash not available")
    r = tmp_path / "repo"
    r.mkdir()
    shutil.copy(_ROOT / "docker.sh", r / "docker.sh")
    (r / "docker-compose.yml").write_text("services: {}\n")
    b = tmp_path / "bin"
    b.mkdir()
    (b / "docker").write_text(FAKE_DOCKER)
    (b / "docker").chmod(0o755)
    (b / "curl").write_text("#!/bin/sh\n" + 'echo \'{"app":{"version":"9.9.9","python":"3.12.1"},'
                            '"storage":[{"label":"Built-in Redis","product":"Redis","version":"8.8.1",'
                            '"search":"Redis Query Engine 8.8.0","ok":true}]}\'\n')
    (b / "curl").chmod(0o755)
    return r, b, tmp_path / "docker.log"


def run(repo, *args, env=None):
    r, b, log = repo
    e = {"PATH": f"{b}:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(r), "FAKE_LOG": str(log), **(env or {})}
    p = subprocess.run(["bash", str(r / "docker.sh"), *args], cwd=r, env=e, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=60)
    calls = log.read_text().splitlines() if log.exists() else []
    envfile = (r / ".env").read_text() if (r / ".env").exists() else ""
    return p, calls, envfile


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_redis_is_recorded_and_started_and_waited_for(repo):
    p, calls, env = run(repo, "up", "redis", env={"VISUALWEAVER_WEB_PORT": ""})
    assert p.returncode == 0, p.stderr
    assert "COMPOSE_PROFILES=redis" in env and "VISUALWEAVER_WEB_PORT=" in env
    assert any(c.startswith("compose pull") for c in calls)
    assert any(c.startswith("compose up -d --remove-orphans --wait") for c in calls), calls
    # the report prints the running versions (with the machine's own python3, any 3.x)
    assert "Versions: VisualWeaver 9.9.9" in p.stdout and "Redis 8.8.1 · Redis Query Engine 8.8.0 (ready)" in p.stdout
    # the recorded choice is used next time without asking
    p, _, _ = run(repo, "up")
    assert p.returncode == 0 and "Storage: redis (from .env" in p.stdout


def test_mongodb_needs_the_licence_before_anything_is_downloaded(repo):
    p, calls, env = run(repo, "up", "mongodb")
    assert p.returncode != 0 and "VISUALWEAVER_ACCEPT_MONGODB_LICENSE=1" in p.stderr
    assert not any(c.startswith("compose pull") or c.startswith("compose up") for c in calls)
    assert "COMPOSE_PROFILES" not in env
    p, calls, env = run(repo, "up", "both", env={"VISUALWEAVER_ACCEPT_MONGODB_LICENSE": "1"})
    assert p.returncode == 0, p.stderr
    assert "COMPOSE_PROFILES=both" in env and "VISUALWEAVER_ACCEPT_MONGODB_LICENSE=1" in env
    assert "Server Side Public License" in p.stdout


def test_mongodb_is_refused_on_a_compose_too_old_for_it(repo):
    p, calls, _ = run(repo, "up", "mongodb", env={"VISUALWEAVER_ACCEPT_MONGODB_LICENSE": "1",
                                                  "FAKE_COMPOSE_VERSION": "2.20.3"})
    assert p.returncode != 0 and "2.23.1 or newer" in p.stderr
    assert not any(c.startswith("compose up") for c in calls)


def test_a_busy_port_moves_to_the_next_free_one(repo):
    r, _, _ = repo
    port = _free_port()
    (r / ".env").write_text(f"VISUALWEAVER_WEB_PORT={port}\n")
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))
        s.listen()
        p, _, env = run(repo, "up", "redis")
    assert p.returncode == 0, p.stderr
    assert f"Port {port} is in use" in p.stderr
    assert f"VISUALWEAVER_WEB_PORT={port + 1}" in env or f"VISUALWEAVER_WEB_PORT={port + 2}" in env


def test_an_unknown_storage_is_refused(repo):
    p, calls, _ = run(repo, "up", "postgres")
    assert p.returncode != 0 and "Unknown storage 'postgres'" in p.stderr


def test_a_build_from_source_is_kept_for_later_runs(repo):
    """A plain `up` after `up --build` must not pull the published image over the one
    built from this checkout."""
    p, calls, env = run(repo, "up", "redis", "--build")
    assert p.returncode == 0 and "VISUALWEAVER_BUILD=1" in env
    assert any(c.startswith("compose build visualweaver") for c in calls)
    log = repo[2]
    log.write_text("")
    p, calls, env = run(repo, "up")
    assert any(c.startswith("compose build visualweaver") for c in calls), calls
    assert not any(c.startswith("compose pull --quiet visualweaver") for c in calls), calls
    log.write_text("")
    p, calls, env = run(repo, "up", "--pull")
    assert "VISUALWEAVER_BUILD=0" in env and any(c.startswith("compose pull --quiet visualweaver") for c in calls)



def test_a_nearly_full_disk_is_reported_with_the_fix(repo):
    """mongot stops building indexes above 85 % disk use: the report must say so."""
    r, b, _ = repo
    (b / "docker").write_text(FAKE_DOCKER.replace('case "$1" in\n  info) exit 0 ;;\n  ps) exit 0 ;;',
        'case "$1" in\n  info) exit 0 ;;\n'
        '  ps) case "$*" in *mongodb-search*) echo visualweaver-mongodb-search ;; esac; exit 0 ;;\n'
        '  exec) printf "Filesystem 1024-blocks Used Available Capacity Mounted\\n/dev/vdb1 100 86 14 86%% /data/mongot\\n"; exit 0 ;;'))
    p, _, _ = run(repo, "up", "both", env={"VISUALWEAVER_ACCEPT_MONGODB_LICENSE": "1"})
    assert p.returncode == 0, p.stderr
    assert "86 % full" in p.stderr and "docker image prune" in p.stderr


def test_a_web_ui_limited_to_this_machine_is_not_advertised_on_the_network(repo):
    r, _, _ = repo
    (r / ".env").write_text("VISUALWEAVER_WEB_BIND=127.0.0.1\n")
    p, _, _ = run(repo, "up", "redis")
    assert p.returncode == 0, p.stderr
    assert "this machine only" in p.stdout and "other devices on your network" not in p.stdout


# ── the model provider, as install.sh reports it ─────────────────────────────

FAKE_DOCKER_EXEC = r"""#!/bin/sh
echo "$*" >> "$FAKE_LOG"
case "$1" in
  exec)
    # `docker exec -i visualweaver python - /data/config.json --docker`: run the piped
    # check here, with the test's config standing in for the container's /data/config.json.
    [ -n "${FAKE_EXEC_FAIL:-}" ] && exit 1
    shift; while [ "$1" != "python" ]; do shift; done; shift
    shift; shift   # "-" and /data/config.json
    exec python3 - "$FAKE_CFG" "$@" ;;
  compose) [ "$2" = version ] && echo 2.30.0; exit 0 ;;
  inspect) exit 1 ;;
esac
exit 0
"""


def _with_exec(repo, tmp_path, cfg):
    import json
    r, b, _ = repo
    (b / "docker").write_text(FAKE_DOCKER_EXEC)
    (r / "visualweaver").mkdir(exist_ok=True)
    shutil.copy(_ROOT / "visualweaver" / "provider_check.py", r / "visualweaver" / "provider_check.py")
    (tmp_path / "cfg.json").write_text(json.dumps(cfg))
    return {"FAKE_CFG": str(tmp_path / "cfg.json")}


def test_up_reports_a_hosted_provider_without_a_key_with_the_fix(repo, tmp_path):
    """Old: the report said only "Ollama on this machine is used automatically" whatever was
    selected — install.sh checked the provider, docker.sh did not."""
    env = _with_exec(repo, tmp_path, {"provider": "claude", "claude": {"api_key": ""}})
    p, calls, _ = run(repo, "up", "redis", env=env)
    assert p.returncode == 0, p.stderr
    assert "Provider: ⚠ Claude is selected but has no API key" in p.stdout
    assert "ANTHROPIC_API_KEY in docker-compose.yml" in p.stderr
    assert any(c.startswith("exec -i visualweaver python - /data/config.json --docker") for c in calls), calls


def test_up_reports_a_working_provider(repo, tmp_path):
    env = _with_exec(repo, tmp_path, {"provider": "openai", "openai": {"api_key": "sk-test"}})
    p, _, _ = run(repo, "up", "redis", env=env)
    assert "Provider: ✓ OpenAI: API key set in the app config" in p.stdout
    assert "sk-test" not in p.stdout + p.stderr


def test_an_unreadable_provider_check_is_said_not_hidden(repo, tmp_path):
    env = _with_exec(repo, tmp_path, {"provider": "ollama"})
    p, _, _ = run(repo, "up", "redis", env={**env, "FAKE_EXEC_FAIL": "1"})
    assert p.returncode == 0 and "Provider: not checked" in p.stdout
