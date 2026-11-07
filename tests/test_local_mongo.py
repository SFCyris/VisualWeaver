# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.local_mongo — the built-in MongoDB with vector search.

The pure parts (the generated compose file, the runtime diagnosis, the port rule)
run everywhere. One test runs the real lifecycle — init → up → stop → up (data
kept) → down — on a scratch data dir and its own compose project, and skips
cleanly when no container runtime is running.
"""
import json
import os
import pathlib
import socket
import subprocess
import sys
import textwrap
import time

import pytest
import yaml

from visualweaver import local_mongo as lm

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _state(port=27123):
    st = lm.new_state(port=port, project="vwa-unit")
    return st


def _compose(st=None):
    return yaml.safe_load(lm.render_compose(st or _state()))


# ── the generated compose file ───────────────────────────────────────────────

def test_secrets_reach_the_containers_as_owner_only_files():
    c = _compose()
    mounts = {m["target"]: m for svc in c["services"].values() for m in svc.get("configs", [])}
    for target in ("/etc/vw-keyfile", "/etc/vw-search-password"):
        assert mounts[target]["mode"] == 0o400, f"{target} must be 0400 (mongod / mongot refuse others)"
    assert mounts["/etc/vw-keyfile"]["uid"] == "1000", "the server image runs mongod as uid 1000"


def test_only_mongod_is_published_and_only_on_loopback():
    st = _state(27123)
    c = _compose(st)
    assert c["services"]["mongod"]["ports"] == ["127.0.0.1:27123:27017"]
    assert "ports" not in c["services"]["search"], "mongot must not be published at all"
    assert "network_mode" not in c["services"]["mongod"]


def test_images_are_pinned_by_tag_and_digest():
    c = _compose()
    assert c["services"]["mongod"]["image"] == lm.SERVER_IMAGE
    assert c["services"]["search"]["image"] == lm.SEARCH_IMAGE
    for img in (lm.SERVER_IMAGE, lm.SEARCH_IMAGE):
        name, _, digest = img.partition("@")
        assert ":" in name and digest.startswith("sha256:") and len(digest) == 71, img
        assert not img.startswith("mongodb/mongodb-atlas-local"), "installs use the Community images"
    assert lm.SERVER_VERSION in lm.SERVER_IMAGE and lm.SEARCH_VERSION in lm.SEARCH_IMAGE


def test_the_generated_passwords_are_the_ones_in_the_compose_file():
    st = _state()
    text = lm.render_compose(st)
    for key in ("app_password", "search_password", "admin_password", "keyfile"):
        assert st[key] in text
    c = yaml.safe_load(text)
    assert c["name"] == "vwa-unit"
    assert set(c["volumes"]) == {"db", "search"}, "data lives in named volumes, never host folders"
    for svc in c["services"].values():
        for v in svc.get("volumes", []):
            assert not v.startswith(("/", ".", "~")), f"host bind mount {v!r}"


def test_init_keeps_passwords_and_port_and_files_are_private(tmp_path):
    st1 = lm.init(port=27124, base=tmp_path, project="vwa-unit")
    st2 = lm.init(base=tmp_path, project="vwa-other")
    assert st1 == st2, "a re-run must keep the passwords, port and project"
    d = lm.state_dir(tmp_path)
    assert oct(d.stat().st_mode & 0o777) == "0o700"
    for f in ("state.json", "compose.yml"):
        assert oct((d / f).stat().st_mode & 0o777) == "0o600", f
    assert lm.init(port=27125, base=tmp_path)["port"] == 27125, "an explicit port moves it"


def test_the_project_name_can_be_set_for_a_new_install(tmp_path, monkeypatch):
    monkeypatch.setenv("VISUALWEAVER_MONGODB_PROJECT", "vwa-envproject")
    assert lm.init(port=27126, base=tmp_path)["project"] == "vwa-envproject"


def _script_cli(tmp_path, name, body):
    d = tmp_path / "bin"; d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text("#!/bin/sh\n" + body + "\n")
    f.chmod(0o755)
    return d


def test_a_slow_runtime_is_retried_and_reported_busy_not_stopped(tmp_path, monkeypatch):
    """`docker info` can take longer than its limit on a loaded machine. That is a busy
    runtime, not a stopped one: telling the user to start it sends them the wrong way."""
    monkeypatch.setattr(lm, "_INFO_TIMEOUTS", (0.3, 0.6))
    # slower than both limits: busy
    monkeypatch.setenv("PATH", str(_script_cli(tmp_path / "a", "docker", "sleep 3")) + ":/usr/bin:/bin")
    cmd, why = lm.runtime()
    assert cmd is None and "did not answer" in why and "not running" not in why, why
    # slower than the first limit only: the retry finds it
    marker = tmp_path / "called"
    body = (f'case "$1" in info) if [ -f "{marker}" ]; then echo {{}}; exit 0; fi; touch "{marker}"; sleep 3;; '
            f'compose) echo "Docker Compose version v2.30.0";; context) echo colima;; esac')
    monkeypatch.setenv("PATH", str(_script_cli(tmp_path / "b", "docker", body)) + ":/usr/bin:/bin")
    cmd, why = lm.runtime()
    assert cmd == ["docker", "compose"] and "colima" in why, why
    # a CLI whose daemon is down still reads "not running"
    monkeypatch.setenv("PATH", str(_script_cli(tmp_path / "c", "docker", "echo 'Cannot connect' >&2; exit 1")) + ":/usr/bin:/bin")
    cmd, why = lm.runtime()
    assert cmd is None and "not running" in why, why


def test_each_data_dir_gets_its_own_containers_and_volumes(tmp_path, monkeypatch):
    """Two data dirs on one machine must not share a compose project: the second one's
    generated passwords would not match the users stored in the first one's volume."""
    monkeypatch.delenv("VISUALWEAVER_MONGODB_PROJECT", raising=False)
    a, b = tmp_path / "a", tmp_path / "b"
    pa, pb = lm.init(port=27128, base=a)["project"], lm.init(port=27129, base=b)["project"]
    assert pa != pb and pa.startswith("visualweaver-mongodb-") and pb.startswith("visualweaver-mongodb-")
    assert lm.init(base=a)["project"] == pa, "the name is fixed once created"
    monkeypatch.setattr(lm, "_platform_data_dir", lambda: a)
    assert lm.default_project(a) == "visualweaver-mongodb", "the platform data dir keeps the plain name"
    assert lm.default_project(b) == pb


def test_connection_cfg_is_a_builtin_loopback_connection():
    st = _state(27127)
    cfg = lm.connection_cfg(st)
    assert cfg == {"type": "mongodb", "uri": "mongodb://127.0.0.1:27127/?authSource=admin",
                   "username": "visualweaver", "password": st["app_password"],
                   "database": "visualweaver", "builtin": True}


# ── what the installer can call before the virtualenv exists ─────────────────

def test_early_commands_need_only_the_standard_library(tmp_path):
    """install.sh runs runtime/images/init/accept-license with any python3; the app's
    dependencies (fastapi, via visualweaver.constants) must not be imported."""
    code = textwrap.dedent("""
        import sys
        from visualweaver import local_mongo as lm
        lm.init(port=27128)
        st = lm.load_state(); st["license_accepted"] = "x"; lm.save_state(st)
        print("visualweaver.constants" in sys.modules, "fastapi" in sys.modules)
    """)
    env = {**os.environ, "VISUALWEAVER_DATA_DIR": str(tmp_path), "PYTHONPATH": str(_ROOT)}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["False", "False"]
    assert (tmp_path / "mongodb" / "state.json").exists()


def test_images_prints_the_pinned_images_and_their_size(tmp_path):
    env = {**os.environ, "VISUALWEAVER_DATA_DIR": str(tmp_path), "PYTHONPATH": str(_ROOT), "PATH": "/nonexistent"}
    r = subprocess.run([sys.executable, "-m", "visualweaver.local_mongo", "images"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 4, "no runtime → not downloaded"
    assert "mongodb/mongodb-community-server:9.0.4-ubi9-slim" in r.stdout
    assert "mongodb/mongodb-community-search:1.78.0" in r.stdout
    assert lm.IMAGES_SIZE in r.stdout and lm.IDLE_MEMORY in r.stdout


# ── runtime diagnosis (fake CLIs) ────────────────────────────────────────────

def _fake_cli(bindir, name, info_rc=0, compose_rc=0):
    p = bindir / name
    p.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        case "$1" in
          info) exit {info_rc};;
          compose) exit {compose_rc};;
          context) echo colima; exit 0;;
          version) echo 29.1.0; exit 0;;
        esac
        exit 0
        """))
    p.chmod(0o755)


def test_no_runtime_names_the_options(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    cmd, why = lm.runtime()
    assert cmd is None
    assert "No container runtime found" in why and "Colima" in why and "Podman" in why


def test_an_installed_but_stopped_runtime_says_how_to_start_it(tmp_path, monkeypatch):
    _fake_cli(tmp_path, "docker", info_rc=1)
    _fake_cli(tmp_path, "colima")
    monkeypatch.setenv("PATH", str(tmp_path))
    cmd, why = lm.runtime()
    assert cmd is None
    assert "installed but not running" in why and "`colima start`" in why


def test_a_running_runtime_without_compose_says_so(tmp_path, monkeypatch):
    _fake_cli(tmp_path, "docker", compose_rc=1)
    monkeypatch.setenv("PATH", str(tmp_path))
    cmd, why = lm.runtime()
    assert cmd is None and "compose` is not available" in why


def test_a_working_runtime_is_described_with_its_version(tmp_path, monkeypatch):
    _fake_cli(tmp_path, "docker")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert lm.runtime() == (["docker", "compose"], "docker (colima)")
    assert lm.runtime_info() == {"runtime": "docker (colima)", "runtime_version": "29.1.0"}


# ── ports ────────────────────────────────────────────────────────────────────

def _listen():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


def test_a_port_taken_by_something_else_moves_to_the_next_free_one(tmp_path, monkeypatch):
    s, port = _listen()
    try:
        lm.init(port=port, base=tmp_path, project="vwa-unit")
        monkeypatch.setattr(lm, "probe", lambda st: {"running": False, "ready": False})
        r = lm.ensure_port(base=tmp_path)
        assert r["moved_from"] == port and r["port"] > port
        assert "python" in r["holder"].lower() or r["holder"] == "", r["holder"]
        st = lm.load_state(tmp_path)
        assert st["port"] == r["port"], "the new port is recorded"
        assert f"127.0.0.1:{r['port']}:27017" in (lm.state_dir(tmp_path) / "compose.yml").read_text()
    finally:
        s.close()


def test_a_port_held_by_this_mongodb_itself_is_kept(tmp_path, monkeypatch):
    s, port = _listen()
    try:
        lm.init(port=port, base=tmp_path, project="vwa-unit")
        monkeypatch.setattr(lm, "probe", lambda st: {"running": True, "ready": True})
        assert lm.ensure_port(base=tmp_path) == {"port": port, "moved_from": None, "holder": ""}
    finally:
        s.close()


def test_a_free_recorded_port_is_kept_and_avoided_ports_are_skipped(tmp_path, monkeypatch):
    s, port = _listen(); s.close()
    lm.init(port=port, base=tmp_path, project="vwa-unit")
    monkeypatch.setattr(lm, "probe", lambda st: {"running": False, "ready": False})
    assert lm.ensure_port(base=tmp_path)["moved_from"] is None
    r = lm.ensure_port(base=tmp_path, avoid=(port,))
    assert r["moved_from"] == port and r["port"] != port


# ── up: a running MongoDB is reused without going through the runtime ────────

def _fake_compose(calls, rc=0):
    return lambda args, base=None, **k: calls.append(args) or subprocess.CompletedProcess(args, rc, "", "boom")


def test_up_returns_at_once_when_already_running_what_was_last_applied(tmp_path, monkeypatch):
    lm.init(port=27129, base=tmp_path, project="vwa-unit")
    monkeypatch.setattr(lm, "probe", lambda st: {"running": True, "ready": True, "version": "9.0.4", "error": None})
    calls = []
    monkeypatch.setattr(lm, "_compose", _fake_compose(calls))
    first = lm.up(base=tmp_path)
    assert calls and calls[0][:2] == ["up", "-d"] and "already_running" not in first
    assert (lm.state_dir(tmp_path) / "applied.sha256").exists()

    def boom(*a, **k):
        raise AssertionError("compose must not run when MongoDB already answers")
    monkeypatch.setattr(lm, "_compose", boom)
    out = lm.up(base=tmp_path)
    assert out["already_running"] is True and out["total_seconds"] < 1


def test_up_goes_through_compose_when_the_state_changed_since_the_last_up(tmp_path, monkeypatch):
    """A moved port or a new image pin must recreate the containers, even though
    the old ones still answer."""
    lm.init(port=27130, base=tmp_path, project="vwa-unit")
    monkeypatch.setattr(lm, "probe", lambda st: {"running": True, "ready": True, "version": "9.0.4", "error": None})
    calls = []
    monkeypatch.setattr(lm, "_compose", _fake_compose(calls))
    lm.up(base=tmp_path)
    monkeypatch.setattr(lm, "SERVER_IMAGE", lm.SERVER_IMAGE.replace("9.0.4", "9.0.5"))
    out = lm.up(base=tmp_path)
    assert len(calls) == 2 and "already_running" not in out


def test_a_start_that_never_gets_ready_shows_the_container_logs(tmp_path, monkeypatch):
    lm.init(port=27131, base=tmp_path, project="vwa-unit")
    monkeypatch.setattr(lm, "_compose", _fake_compose([]))
    monkeypatch.setattr(lm, "_tail", lambda base=None, n=15: "search-1 | mongot: auth failed")
    monkeypatch.setattr(lm, "wait_ready", lambda st, timeout=240: (_ for _ in ()).throw(
        lm.LocalMongoError("The built-in MongoDB is not ready after 240 s")))
    with pytest.raises(lm.LocalMongoError) as e:
        lm.up(base=tmp_path)
    assert "not ready" in str(e.value) and "mongot: auth failed" in str(e.value)


def test_a_failed_stop_is_reported(tmp_path, monkeypatch):
    lm.init(port=27132, base=tmp_path, project="vwa-unit")
    monkeypatch.setattr(lm, "_compose", _fake_compose([], rc=1))
    with pytest.raises(lm.LocalMongoError, match="Could not stop"):
        lm.stop(base=tmp_path)


def test_a_new_state_uses_the_default_port_and_ensure_port_reports_its_holder(tmp_path, monkeypatch):
    s, port = _listen()
    try:
        monkeypatch.setattr(lm, "DEFAULT_PORT", port)
        assert lm.new_state()["port"] == port, "a fresh state must not move off a busy port silently"
        lm.init(base=tmp_path, project="vwa-unit")
        monkeypatch.setattr(lm, "probe", lambda st: {"running": False, "ready": False})
        r = lm.ensure_port(base=tmp_path)
        assert r["moved_from"] == port and r["port"] != port
    finally:
        s.close()


def test_a_port_in_time_wait_is_free(tmp_path):
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0)); srv.listen(); port = srv.getsockname()[1]
    c = socket.create_connection(("127.0.0.1", port)); a, _ = srv.accept()
    a.close(); time.sleep(0.2); c.close(); srv.close(); time.sleep(0.2)
    assert lm._port_free(port)
    s, busy = _listen()
    try:
        assert not lm._port_free(busy)
    finally:
        s.close()


def test_permission_denied_on_the_docker_socket_says_so(tmp_path, monkeypatch):
    p = tmp_path / "docker"
    p.write_text('#!/bin/sh\n[ "$1" = info ] && { echo "permission denied while trying to connect to the Docker daemon socket" >&2; exit 1; }\nexit 0\n')
    p.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    cmd, why = lm.runtime()
    assert cmd is None and "permission denied" in why and "docker` group" in why


# ── the real lifecycle ───────────────────────────────────────────────────────

def _runtime_or_skip():
    cmd, why = lm.runtime()
    if not cmd:
        pytest.skip(f"no container runtime: {why}")
    return cmd


def test_real_init_up_stop_up_keeps_data_then_down(tmp_path):
    cmd = _runtime_or_skip()
    pymongo = pytest.importorskip("pymongo")
    base = tmp_path / "data"
    project = f"vwa-lmtest-{os.getpid()}"
    s, port = _listen(); s.close()
    st = lm.init(port=port, base=base, project=project)
    compose = [*cmd, "-p", project, "-f", str(lm.state_dir(base) / "compose.yml")]
    try:
        first = lm.up(base=base, timeout=300)
        assert first["ready"], first
        assert first["version"] == lm.SERVER_VERSION

        cfg = lm.connection_cfg(st)
        cli = pymongo.MongoClient(cfg["uri"], username=cfg["username"], password=cfg["password"],
                                  directConnection=True, serverSelectionTimeoutMS=10000)
        cli[cfg["database"]].probe.insert_one({"_id": "kept", "n": 1})
        cli.close()

        again = lm.up(base=base, timeout=300)
        assert again.get("already_running") is True, "a running MongoDB is reused"

        t0 = time.time()
        lm.stop(base=base)
        stop_s = time.time() - t0
        assert not lm.probe(st)["running"], "stopped"
        code = subprocess.run([cmd[0], "inspect", "-f", "{{.State.ExitCode}}", f"{project}-mongod-1"],
                              capture_output=True, text=True, timeout=30).stdout.strip()
        assert code == "0", f"mongod must shut down cleanly (exit {code}), not be killed after the grace period"
        # The exit code above is the real check (a SIGKILL after the grace period is 137);
        # wall time only catches a hang — it varies with the machine's load.
        assert stop_s < 60, f"stop took {stop_s:.1f} s"
        second = lm.up(base=base, timeout=300)
        assert second["ready"] and not second.get("already_running")
        cli = pymongo.MongoClient(cfg["uri"], username=cfg["username"], password=cfg["password"],
                                  directConnection=True, serverSelectionTimeoutMS=10000)
        assert cli[cfg["database"]].probe.find_one({"_id": "kept"})["n"] == 1, "data survives stop/start"
        cli.close()

        lm.down(base=base)
        ps = subprocess.run(compose + ["ps", "-a", "-q"], capture_output=True, text=True, timeout=60)
        assert ps.stdout.strip() == "", "down removes the containers"
        vols = subprocess.run([cmd[0], "volume", "ls", "-q", "--filter", f"name={project}_"],
                              capture_output=True, text=True, timeout=60).stdout.split()
        assert f"{project}_db" in vols, "down keeps the data volume"
    finally:
        subprocess.run(compose + ["down", "-v"], capture_output=True, timeout=300)


def test_a_nearly_full_disk_is_reported(monkeypatch, tmp_path):
    """mongot (1.78) stops building indexes above 85 % disk use and resumes below 80 %."""
    import subprocess as sp

    def df(pct):
        out = f"Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/vdb1 100 {pct} {100 - pct} {pct}% /data/mongot\n"
        return lambda args, base=None, timeout=900, capture=True: sp.CompletedProcess(args, 0, out, "")
    monkeypatch.setattr(lm, "_compose", df(83))
    note = lm.disk_note(tmp_path)
    assert note["disk_used_pct"] == 83 and "85 %" in note["disk_warning"] and "docker image prune" in note["disk_warning"]
    monkeypatch.setattr(lm, "_compose", df(60))
    assert lm.disk_note(tmp_path) == {"disk_used_pct": 60}


def test_a_moved_port_is_followed_by_the_app_config(tmp_path, monkeypatch):
    """Another program took the built-in MongoDB's port after the install: the port moves
    and config.json's built-in connection follows; other connections are left alone."""
    import json
    import socket
    monkeypatch.delenv("VISUALWEAVER_MONGODB_PROJECT", raising=False)
    base = tmp_path / "data"
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        lm.init(port=taken, base=base)
        monkeypatch.setattr(lm, "probe", lambda st: {"running": False})   # it is not our MongoDB
        moved = lm.ensure_port(base)
    assert moved["moved_from"] == taken and moved["port"] != taken
    st = lm.load_state(base)
    cfg = {"storage": {"default_type": "redis", "app": "default"},
           "redis_endpoints": [{"name": "local-mongodb", "type": "mongodb", "builtin": True,
                                "uri": f"mongodb://127.0.0.1:{taken}/?authSource=admin"},
                               {"name": "mine", "type": "mongodb", "uri": f"mongodb://127.0.0.1:{taken}/"}]}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    assert lm.rewire_config(p, st) is True
    eps = {e["name"]: e for e in json.loads(p.read_text())["redis_endpoints"]}
    assert eps["local-mongodb"]["uri"] == f"mongodb://127.0.0.1:{moved['port']}/?authSource=admin"
    assert eps["mine"]["uri"] == f"mongodb://127.0.0.1:{taken}/", "a user's own connection is not touched"
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert lm.rewire_config(p, st) is False, "nothing left to change"
    # a MongoDB-first install keeps it in config.mongodb
    p.write_text(json.dumps({"mongodb": {"type": "mongodb", "builtin": True,
                                         "uri": f"mongodb://127.0.0.1:{taken}/?authSource=admin"}}))
    assert lm.rewire_config(p, st) and str(moved["port"]) in json.loads(p.read_text())["mongodb"]["uri"]
