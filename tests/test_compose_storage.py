# SPDX-License-Identifier: AGPL-3.0-or-later
"""docker-compose.yml offers the same storage choices as install.sh.

COMPOSE_PROFILES unset or `redis` → Redis; `mongodb` → MongoDB only; `both` → both.
The pure checks read the file; the profile checks ask `docker compose config`
(the CLI only, no daemon); one test brings the mongodb profile up for real under
its own `vwa-` project, with the app replaced by a probe that runs the app's own
start wrapper as the app's uid and then a vector search — skipped when no
container runtime is running.
"""
import json
import os
import pathlib
import shutil
import subprocess

import pytest
import yaml

from visualweaver import local_mongo as lm

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_COMPOSE = _ROOT / "docker-compose.yml"
_MONGO_SERVICES = ("mongodb-secrets", "mongodb", "mongodb-search")


def _file():
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


def test_the_mongodb_images_are_the_installers_pinned_images():
    s = _file()["services"]
    assert s["mongodb"]["image"] == lm.SERVER_IMAGE == s["mongodb-secrets"]["image"]
    assert s["mongodb-search"]["image"] == lm.SEARCH_IMAGE


def test_compose_mongodb_is_never_published():
    s = _file()["services"]
    for name in _MONGO_SERVICES:
        assert "ports" not in s[name], f"{name} must stay on the compose network"
        assert set(s[name]["profiles"]) == {"mongodb", "both"}


def test_mongodb_secrets_are_generated_owner_only():
    s = _file()["services"]
    script = s["mongodb-secrets"]["command"][0]
    assert "/dev/urandom" in script and "umask 077" in script
    assert "chown 10001:10001 /secrets/app-password && chmod 0400 /secrets/app-password" in script
    assert "chmod 0400 /secrets/mongot-password" in script
    assert "chown -R 1000:1000 /secrets/mongod" in script
    assert s["mongodb"]["environment"]["MONGODB_INITDB_ROOT_PASSWORD_FILE"] == "/secrets/mongod/admin-password"
    assert all(v.endswith(":ro") for v in s["mongodb"]["volumes"] if "secrets" in v)


def _config(profiles):
    if not shutil.which("docker"):
        pytest.skip("docker CLI not available")
    env = {k: v for k, v in os.environ.items() if k != "COMPOSE_PROFILES"}
    if profiles is not None:
        env["COMPOSE_PROFILES"] = profiles
    r = subprocess.run(["docker", "compose", "-p", "vwa-cfgtest", "-f", str(_COMPOSE), "config", "--format", "json"],
                       capture_output=True, text=True, env=env, timeout=60)
    if r.returncode and "is not a docker command" in r.stderr:
        pytest.skip("docker compose plugin not available")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.parametrize("profiles,services,storage", [
    (None, {"visualweaver", "redis"}, "redis"),
    ("redis", {"visualweaver", "redis"}, "redis"),
    ("mongodb", {"visualweaver", "mongodb-secrets", "mongodb", "mongodb-search"}, "mongodb"),
    ("both", {"visualweaver", "redis", "mongodb-secrets", "mongodb", "mongodb-search"}, "both"),
])
def test_each_profile_starts_its_stores_and_tells_the_app(profiles, services, storage):
    c = _config(profiles)
    assert set(c["services"]) == services
    env = c["services"]["visualweaver"]["environment"]
    assert env["VISUALWEAVER_STORAGE"] == storage
    published = [p for svc in c["services"].values() for p in svc.get("ports", [])]
    assert [str(p.get("published")) for p in published] == ["8420"], "only the web UI is published"


@pytest.mark.parametrize("value,storage,redis,mongo", [
    ("redis", "redis", "redis", "unset"),
    ("", "redis", "redis", "unset"),
    ("mongodb", "mongodb", "unset", "mongodb://mongodb:27017/?authSource=admin"),
    ("both", "both", "redis", "mongodb://mongodb:27017/?authSource=admin"),
    ("mongodb,redis", "both", "redis", "mongodb://mongodb:27017/?authSource=admin"),
    ("redis, mongodb", "both", "redis", "mongodb://mongodb:27017/?authSource=admin"),
])
def test_the_app_wrapper_hands_on_only_the_chosen_stores(tmp_path, value, storage, redis, mongo):
    """Runs the app service's real start wrapper (with `exec visualweaver` replaced by
    an echo) under sh, with the environment compose gives it."""
    svc = _file()["services"]["visualweaver"]
    env = dict(e.split("=", 1) for e in svc["environment"])
    env["VISUALWEAVER_STORAGE"] = value
    pw = tmp_path / "app-password"; pw.write_text("s3cret\n")
    script = (svc["command"][2].replace("$$", "$")
              .replace("/run/visualweaver-mongodb/app-password", str(pw))
              .replace("exec visualweaver", 'echo "S=$VISUALWEAVER_STORAGE R=${VISUALWEAVER_REDIS_HOST:-unset} '
                                            'M=${VISUALWEAVER_MONGODB_URI:-unset} P=${VISUALWEAVER_MONGODB_PASSWORD:-unset}"'))
    r = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin", **env})
    assert r.returncode == 0, r.stderr
    pw_expect = "s3cret" if mongo != "unset" else "unset"
    assert r.stdout.strip() == f"S={storage} R={redis} M={mongo} P={pw_expect}"


def _runtime_or_skip():
    cmd, why = lm.runtime()
    if not cmd:
        pytest.skip(f"no container runtime: {why}")
    return cmd


_PROBE = r'''
const d = db.getSiblingDB(process.env.VISUALWEAVER_MONGODB_DATABASE);
d.probe.insertMany([{_id: 1, v: [1, 0, 0]}, {_id: 2, v: [0, 1, 0]}]);
d.probe.createSearchIndex("vec", "vectorSearch", {fields: [{type: "vector", path: "v", numDimensions: 3, similarity: "cosine"}]});
const t0 = Date.now(); let rows = [];
while (Date.now() - t0 < 180000) {
  const ix = d.probe.getSearchIndexes("vec")[0];
  if (ix && ix.queryable) {
    rows = d.probe.aggregate([{$vectorSearch: {index: "vec", path: "v", queryVector: [0.9, 0.1, 0], numCandidates: 10, limit: 1}}]).toArray();
    if (rows.length) break;
  }
  sleep(500);
}
print("USER " + d.runCommand({connectionStatus: 1}).authInfo.authenticatedUsers[0].user);
print("TOP " + (rows.length ? rows[0]._id : "none"));
'''


def test_real_mongodb_profile_serves_vector_search_to_the_app_user(tmp_path):
    cmd = _runtime_or_skip()
    project = f"vwa-composetest-{os.getpid()}"
    wrapper = _file()["services"]["visualweaver"]["command"][2]
    # The file's text is already compose-escaped ($$); only the probe's own part is escaped here.
    probe_exec = ('echo "STORAGE=$VISUALWEAVER_STORAGE REDIS=${VISUALWEAVER_REDIS_HOST:-unset}"; '
                  'exec mongosh "$VISUALWEAVER_MONGODB_URI" -u "$VISUALWEAVER_MONGODB_USERNAME" '
                  '-p "$VISUALWEAVER_MONGODB_PASSWORD" --quiet --file /probe.js')
    run = wrapper.replace("exec visualweaver", probe_exec.replace("$", "$$"))
    override = {
        "services": {
            "visualweaver": {"image": lm.SERVER_IMAGE, "container_name": f"{project}-app", "user": "10001:10001",
                             "entrypoint": ["sh", "-c"], "command": [run], "ports": "RESET", "restart": "no",
                             "environment": {"HOME": "/tmp"},
                             "configs": [{"source": "probe", "target": "/probe.js"}]},
            **{n: {"container_name": f"{project}-{n}"} for n in (*_MONGO_SERVICES, "redis")},
        },
        "configs": {"probe": {"content": _PROBE.replace("$", "$$")}},
    }
    f = tmp_path / "override.yml"
    f.write_text(yaml.safe_dump(override, width=10000).replace("ports: RESET", "ports: !reset []"))
    dc = [*cmd, "-p", project, "-f", str(_COMPOSE), "-f", str(f)]
    env = {**os.environ, "COMPOSE_PROFILES": "mongodb"}
    try:
        up = subprocess.run(dc + ["up", "-d", "--wait", "mongodb-search"], capture_output=True, text=True,
                            env=env, timeout=600)
        assert up.returncode == 0, up.stderr[-2000:]
        r = subprocess.run(dc + ["run", "--rm", "--no-deps", "visualweaver"], capture_output=True, text=True,
                           env=env, timeout=600)
        assert "STORAGE=mongodb REDIS=unset" in r.stdout, r.stdout + r.stderr
        assert "USER visualweaver" in r.stdout and "TOP 1" in r.stdout, r.stdout + r.stderr
        stop = subprocess.run(dc + ["stop", "mongodb"], capture_output=True, text=True, env=env, timeout=120)
        assert stop.returncode == 0
        code = subprocess.run([cmd[0], "inspect", "-f", "{{.State.ExitCode}}", f"{project}-mongodb"],
                              capture_output=True, text=True, timeout=30).stdout.strip()
        assert code == "0", "mongod must shut down cleanly on stop, not be killed after the grace period"
    finally:
        subprocess.run(dc + ["down", "-v"], capture_output=True, env=env, timeout=300)
