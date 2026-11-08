# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings → Storage backend: /api/storage, moving the app data between Redis and
MongoDB, /api/components, built-in connections, the storage config and its env.

The app's own keys (sessions, usage, stats, watched-folder signatures) are moved under
KEY_PREFIX for every test here, so nothing lands on the suite Redis outside the
namespace conftest cleans. config.json goes to a temporary file: a switch persists the
config, and later tests that boot the app read it.
"""
import asyncio
import json
import os
import socket
import time

import pytest
from fastapi import HTTPException

from conftest import KEY_PREFIX, REDIS_DB, REDIS_HOST, REDIS_PORT

MONGO = "local-mongodb"


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]          # closed again on return: nothing listens


@pytest.fixture
def app_keys(monkeypatch):
    from visualweaver import projects, rag_admin, sessions, ws
    monkeypatch.setattr(projects, "_KEY", f"{KEY_PREFIX}projects")
    monkeypatch.setattr(sessions, "_SESSION_PREFIX", f"{KEY_PREFIX}session:")
    monkeypatch.setattr(sessions, "_USAGE_KEY", f"{KEY_PREFIX}usage")
    monkeypatch.setattr(sessions, "_INDEX_KEY", f"{KEY_PREFIX}session_index")
    monkeypatch.setattr(sessions, "_INDEX_META_KEY", f"{KEY_PREFIX}session_index_meta")
    monkeypatch.setattr(rag_admin, "_RAG_STATS_KEY", f"{KEY_PREFIX}rag_stats")
    monkeypatch.setattr(ws, "_WATCH_SEEN_KEY", f"{KEY_PREFIX}watch")


@pytest.fixture
def config_file(monkeypatch, tmp_path):
    from visualweaver import constants
    p = tmp_path / "config.json"
    monkeypatch.setattr(constants, "CONFIG_PATH", p)
    return p


@pytest.fixture
def stores(app_module, cfg, clean_redis, mongo_uri, app_keys, config_file, monkeypatch):
    """Built-in Redis (the suite's) as "default" + a built-in MongoDB "local-mongodb"
    (its own database), app data on "default"."""
    import copy
    from visualweaver import mongo_store, rag_admin, redis_store, state
    uri, user, pw = mongo_store.split_uri_credentials(mongo_uri)
    db = f"vwtest_sw_{os.getpid()}_{int(time.time() * 1000) % 10**8}"
    saved = (copy.deepcopy(state._rag_stats), dict(state._endpoint_health), dict(state._search_available),
             set(state._rag_stats_dirty))
    monkeypatch.setattr(rag_admin, "_stats_loaded", True)
    cfg.pop("mongodb", None)
    cfg.update({
        "storage": {"default_type": "redis", "app": "default"},
        "redis": {"host": REDIS_HOST, "port": REDIS_PORT, "db": REDIS_DB, "password": "", "ssl": False},
        "redis_endpoints": [{"name": MONGO, "type": "mongodb", "uri": uri, "username": user,
                             "password": pw, "database": db, "builtin": True}],
        "sessions": {"persist": True, "ttl": 3600},
    })
    state._endpoint_health.clear()
    state._search_available.clear()
    redis_store.invalidate_redis_clients()
    rag_admin.invalidate_rag_meta()
    ep = redis_store.r_for(MONGO)
    assert mongo_store.is_mongo(ep)
    yield {"redis": clean_redis, "mongo": ep, "password": pw, "db": db}
    try:
        ep.client.drop_database(db)
    finally:
        state._rag_stats.clear(); state._rag_stats.update(saved[0])
        state._endpoint_health.clear(); state._endpoint_health.update(saved[1])
        state._search_available.clear(); state._search_available.update(saved[2])
        state._rag_stats_dirty.clear(); state._rag_stats_dirty.update(saved[3])
        redis_store.invalidate_redis_clients()
        rag_admin.invalidate_rag_meta()


def _switch(name: str, **extra) -> dict:
    from visualweaver import routes_storage
    return asyncio.run(routes_storage.api_storage_set_app({"connection": name, **extra}))


def _server_version() -> str:
    from visualweaver import local_mongo
    return local_mongo.SERVER_VERSION


def _stats_row(queries: int) -> dict:
    from visualweaver import state
    row = dict(state.RAG_STATS_TEMPLATE)
    row.update(queries=queries, hits=queries, scored=queries, raw_hist=[0] * state.RAW_HIST_BUCKETS,
               cited_hist=[0] * state.CITED_HIST_RANKS)
    row["raw_hist"][3] = queries
    return row


def _write_app_data(tag: str, n_sessions: int, usage_in: int, watch: dict) -> dict:
    """App data written through the app's own functions, into whichever store is current."""
    from visualweaver import appstore, rag_admin, sessions, state, ws
    convs = {f"{tag}-{i}": [{"role": "user", "content": f"{tag} question {i}"},
                            {"role": "assistant", "content": f"{tag} answer {i}"}]
             for i in range(n_sessions)}
    for sid, msgs in convs.items():
        sessions.save_session(sid, msgs)
    sessions.record_usage("ollama", f"{tag}:7b", {"prompt": usage_in, "completion": usage_in // 2})
    state._rag_stats[f"{KEY_PREFIX}{tag}"] = _stats_row(usage_in)
    state._rag_stats_dirty.add(f"{KEY_PREFIX}{tag}")
    rag_admin.save_rag_stats()
    appstore.current().hset(ws._WATCH_SEEN_KEY, watch)
    return convs


def _session_ttl(store, sid: str):
    from visualweaver import sessions
    return store.ttl(sessions._session_key(sid))


def test_switch_redis_to_mongodb_copies_the_app_data(stores, cfg, config_file):
    from visualweaver import appstore, rag_admin, sessions, state, ws
    convs = _write_app_data("r2m", 3, 120, {"inst\x00/a.txt": "1:10", "inst\x00/b.txt": "2:20"})
    redis_store_ = appstore.for_connection("default")
    mongo_store_ = appstore.for_connection(MONGO)
    # Left on the target by an earlier move: the same key is overwritten, and a
    # conversation only there (deleted since on the source) does not come back.
    mongo_store_.set(sessions._session_key("r2m-0"), json.dumps([{"role": "user", "content": "stale"}]), 3600)
    mongo_store_.set(sessions._session_key("only-on-target"), json.dumps([{"role": "user", "content": "deleted"}]), 3600)
    sessions.save_session("short", [{"role": "user", "content": "short-lived"}])
    redis_store_.set(sessions._session_key("short"), redis_store_.get(sessions._session_key("short")), 120)
    # ...and a conversation registry from that earlier stay, complete then, that knows
    # none of the conversations about to be copied: it must not be what is listed.
    mongo_store_.hset(sessions._INDEX_KEY, {"only-on-target": json.dumps({"title": "deleted"})})
    mongo_store_.hset(sessions._INDEX_KEY, {sessions._INDEX_META_FIELD: json.dumps({"v": 1, "ttl": 3600})})
    state._semantic_cache = object()
    # Projects go with the conversations filed under them; one left on the target by an
    # earlier stay (deleted since on the source) does not come back.
    from visualweaver import projects
    proj = projects.create({"name": "Manuals", "kbs": ["manuals"]})
    mongo_store_.hset(projects._KEY, {"p_000000000000": json.dumps({"name": "gone"})})

    t0 = time.time()
    res = _switch(MONGO)
    took = time.time() - t0

    assert res["ok"] is True and res["app"] == MONGO
    assert res["copied"] == {"conversations": 4, "usage": True, "stats": True, "watch": 2}, res
    assert took < 10, took
    assert appstore.app_connection() == MONGO and appstore.current().kind == "mongodb"
    assert state._semantic_cache is None
    assert json.loads(config_file.read_text())["storage"]["app"] == MONGO

    # Read back through the app, now on MongoDB.
    for sid, msgs in convs.items():
        assert sessions.load_session(sid) == msgs
    listed = {s["id"] for s in sessions.list_sessions_from_redis()}
    assert set(convs) | {"short"} <= listed and "only-on-target" not in listed
    assert sessions.load_session("only-on-target") in (None, [])
    assert 100 <= _session_ttl(mongo_store_, "short") <= 120          # remaining lifetime kept
    assert 3500 <= _session_ttl(mongo_store_, "r2m-1") <= 3600
    assert sessions.usage_totals()["ollama:r2m:7b"] == {"in": 120, "out": 60}
    stats = json.loads(mongo_store_.get(rag_admin._RAG_STATS_KEY))
    assert stats[f"{KEY_PREFIX}r2m"]["queries"] == 120
    assert mongo_store_.hgetall(ws._WATCH_SEEN_KEY) == {"inst\x00/a.txt": "1:10", "inst\x00/b.txt": "2:20"}
    assert [(p["id"], p["name"], p["kbs"]) for p in projects.list_projects()] == [(proj["id"], "Manuals", ["manuals"])]
    # Counters stay counters on MongoDB: the next turn adds to the copied tally.
    sessions.record_usage("ollama", "r2m:7b", {"prompt": 5, "completion": 1})
    assert sessions.usage_totals()["ollama:r2m:7b"] == {"in": 125, "out": 61}
    # The old store keeps its copy.
    assert redis_store_.get(sessions._session_key("r2m-2")) is not None
    assert redis_store_.hgetall(sessions._USAGE_KEY)["ollama:r2m:7b:in"] == "120"

    # Choosing the current store again changes nothing.
    assert _switch(MONGO) == {"ok": True, "app": MONGO, "copied": None}


def test_the_answer_cache_starts_empty_on_the_target(stores, cfg):
    """Real stores, both directions (semcache:* on Redis, vw_cache on MongoDB): the
    source's entries are not copied, and entries left on the target by an earlier stay
    there are removed — they would otherwise be served again after a round trip."""
    from visualweaver import appstore
    entry = f"semcache:{KEY_PREFIX}entry"
    stores["redis"].hset(entry, mapping={"prompt": "q", "response": "a"})
    stores["mongo"].col("vw_cache").insert_one({"_id": f"{KEY_PREFIX}c", "prompt": "old"})
    _switch(MONGO)
    m = appstore.for_connection(MONGO)
    assert not [k for k in m.keys("") if "semcache" in k]                 # not copied
    assert stores["mongo"].col("vw_cache").count_documents({}) == 0       # emptied
    assert stores["redis"].hgetall(entry)[b"prompt"] == b"q"              # the source keeps its own
    _switch("default")
    assert not stores["redis"].exists(entry)                              # emptied on the way back
    assert not stores["redis"].exists(f"{KEY_PREFIX}c")


def test_switch_mongodb_to_redis_copies_different_data_back(stores, cfg):
    """The other direction, with data written while the app was on MongoDB."""
    from visualweaver import appstore, rag_admin, sessions, ws
    cfg["storage"]["app"] = MONGO
    convs = _write_app_data("m2r", 2, 77, {"other\x00/c.md": "9:99"})
    redis_store_ = appstore.for_connection("default")
    assert redis_store_.get(sessions._session_key("m2r-0")) is None      # written to MongoDB only

    res = _switch("default")
    assert res["copied"] == {"conversations": 2, "usage": True, "stats": True, "watch": 1}, res
    assert appstore.current().kind == "redis"
    for sid, msgs in convs.items():
        assert sessions.load_session(sid) == msgs
    assert 3500 <= _session_ttl(redis_store_, "m2r-0") <= 3600
    assert sessions.usage_totals() == {"ollama:m2r:7b": {"in": 77, "out": 38}}
    assert json.loads(redis_store_.get(rag_admin._RAG_STATS_KEY))[f"{KEY_PREFIX}m2r"]["queries"] == 77
    assert redis_store_.hgetall(ws._WATCH_SEEN_KEY) == {"other\x00/c.md": "9:99"}
    assert appstore.for_connection(MONGO).get(sessions._session_key("m2r-1")) is not None


def test_a_switch_is_refused_for_an_unusable_target(stores, cfg, monkeypatch):
    from visualweaver import appstore, routes_settings
    cfg["redis_endpoints"].append({"name": "dead", "type": "redis", "host": "127.0.0.1",
                                   "port": _closed_port(), "db": 0, "password": "", "ssl": False})
    cfg["redis_endpoints"].append({"name": "plain", "type": "redis", "host": REDIS_HOST,
                                   "port": REDIS_PORT, "db": 0, "password": "", "ssl": False})
    for name, words in (("nope", "Unknown storage connection"), ("", "Name the storage connection"),
                        ("dead", "Can't connect to dead")):
        with pytest.raises(HTTPException) as ei:
            _switch(name)
        assert ei.value.status_code == 400 and words in ei.value.detail, ei.value.detail
    real = routes_settings.probe_search
    monkeypatch.setattr(routes_settings, "probe_search",
                        lambda rc, ep="default": False if ep == "plain" else real(rc, ep))
    with pytest.raises(HTTPException) as ei:
        _switch("plain")
    assert ei.value.status_code == 400 and "no vector search" in ei.value.detail
    assert appstore.app_connection() == "default"


def test_get_storage_lists_every_connection(stores, cfg):
    from visualweaver import routes_instances, routes_storage
    cfg["redis_endpoints"].append({"name": "dead", "type": "mongodb", "uri": f"mongodb://127.0.0.1:{_closed_port()}",
                                   "username": "", "password": "", "database": "x"})
    inst = f"{KEY_PREFIX}onmongo"
    asyncio.run(routes_instances.api_create_instance({"name": inst, "endpoint": MONGO}))
    body = asyncio.run(routes_storage.api_storage())
    assert body["app"] == "default" and body["default_type"] == "redis"
    by = {c["name"]: c for c in body["connections"]}
    assert list(by) == ["default", MONGO, "dead"]
    d, m, x = by["default"], by[MONGO], by["dead"]
    assert (d["type"], d["builtin"], d["label"], d["host"], d["ok"], d["vector_search"], d["is_app"]) == \
        ("redis", True, "Built-in Redis", f"{REDIS_HOST}:{REDIS_PORT}", True, True, True)
    assert d["version"] and d["version"].split(".")[0].isdigit()
    assert (m["type"], m["builtin"], m["label"], m["ok"], m["vector_search"], m["is_app"], m["instances"]) == \
        ("mongodb", True, "Built-in MongoDB", True, True, False, 1)
    assert m["version"] == _server_version() and "@" not in m["host"]
    assert (x["builtin"], x["label"], x["ok"], x["vector_search"], x["version"], x["instances"]) == \
        (False, "dead", False, None, None, 0)
    assert stores["password"] not in json.dumps(body)


def test_components_report_versions_and_search(stores, cfg, monkeypatch):
    from visualweaver import constants, routes_storage
    routes_storage._component_cache.clear()
    cfg["redis_endpoints"].append({"name": "dead", "type": "redis", "host": "127.0.0.1",
                                   "port": _closed_port(), "db": 0, "password": "hunter2", "ssl": False})
    t0 = time.time()
    body = asyncio.run(routes_storage.api_components())
    first = time.time() - t0
    assert body["app"]["name"] == "VisualWeaver" and body["app"]["version"] == constants.__version__
    assert body["app"]["python"].count(".") == 2
    by = {c["connection"]: c for c in body["storage"]}
    assert list(by) == ["default", MONGO, "dead"]
    r, m, x = by["default"], by[MONGO], by["dead"]
    assert (r["label"], r["type"], r["builtin"], r["ok"], r["product"]) == \
        ("Built-in Redis", "redis", True, True, "Redis")
    assert r["search"].startswith("Redis Query Engine "), r       # module version, e.g. 8.8.0
    from visualweaver import local_mongo
    assert (m["label"], m["product"], m["version"], m["search"], m["ok"]) == \
        ("Built-in MongoDB", "MongoDB Community", local_mongo.SERVER_VERSION,
         f"MongoDB Search {local_mongo.SEARCH_VERSION}", True)
    assert (x["ok"], x["version"], x["search"], x["builtin"]) == (False, None, None, False) and x["error"]
    assert [l["name"] for l in body["libraries"]] == ["redis-py", "redisvl", "pymongo"]
    assert all(l["version"] for l in body["libraries"])
    if not os.environ.get("VISUALWEAVER_TEST_MONGO_URI"):        # the fixture's own containers
        assert body["runtime"]["containers"]                      # e.g. "docker (colima)"
    else:
        assert "containers" in body["runtime"]
    dumped = json.dumps(body)
    assert stores["password"] not in dumped and "hunter2" not in dumped and "mongodb://" not in dumped
    assert first < 8, first
    # Reused for a minute: the second call does not probe again …
    def _boom(cfg, out):
        raise RuntimeError("probed again")
    monkeypatch.setattr(routes_storage, "_redis_component", _boom)
    monkeypatch.setattr(routes_storage, "_mongo_component", _boom)
    assert asyncio.run(routes_storage.api_components())["storage"] == body["storage"]
    # … and an expired entry is probed afresh.
    again = routes_storage.component_info("default", routes_storage._storage_connections()[0][1], max_age=0)
    assert again["ok"] is False and "probed again" in again["error"]


def test_module_versions_are_formatted_like_the_release():
    from visualweaver import routes_storage
    assert routes_storage._module_version(80800) == "8.8.0"
    assert routes_storage._module_version(80801) == "8.8.1"
    assert routes_storage._module_version(21012) == "2.10.12"
    assert routes_storage._module_version(None) is None


def test_components_without_a_builtin_mongodb_have_no_runtime(stores, cfg):
    from visualweaver import routes_storage
    cfg["redis_endpoints"] = []
    body = asyncio.run(routes_storage.api_components())
    assert "runtime" not in body and [c["connection"] for c in body["storage"]] == ["default"]


def test_startup_log_lines_name_each_connection(stores, cfg):
    from visualweaver import routes_storage
    cfg["redis_endpoints"].append({"name": "dead", "type": "redis", "host": "127.0.0.1",
                                   "port": _closed_port(), "db": 0, "password": "", "ssl": False})
    lines = routes_storage.component_log_lines()
    assert lines[0].startswith("'default' (Built-in Redis): Redis ") and "search: Redis Query Engine" in lines[0]
    from visualweaver import local_mongo
    assert lines[1] == (f"'{MONGO}' (Built-in MongoDB): MongoDB Community {local_mongo.SERVER_VERSION} "
                        f"· search: MongoDB Search {local_mongo.SEARCH_VERSION}")
    assert lines[2].startswith("'dead': Redis — unreachable (")


def test_builtin_connections_cannot_be_removed(stores, cfg):
    from visualweaver import routes_redis
    cfg["redis_endpoints"].append({"name": "extra", "type": "redis", "host": REDIS_HOST, "port": REDIS_PORT,
                                   "db": 0, "password": "", "ssl": False})
    for name, words in (("default", "default"), (MONGO, "built-in")):
        with pytest.raises(HTTPException) as ei:
            routes_redis.api_delete_endpoint(name)
        assert ei.value.status_code == 400 and words in ei.value.detail
    cfg["storage"]["app"] = "extra"
    with pytest.raises(HTTPException) as ei:
        routes_redis.api_delete_endpoint("extra")
    assert ei.value.status_code == 400 and "app data" in ei.value.detail
    cfg["storage"]["app"] = "default"
    assert routes_redis.api_delete_endpoint("extra") == {"ok": True}
    assert [e["name"] for e in cfg["redis_endpoints"]] == [MONGO]
    # Re-saving a built-in keeps it built-in; changing its type is refused.
    with pytest.raises(HTTPException) as ei:
        asyncio.run(routes_redis.api_add_endpoint({"name": MONGO, "type": "redis", "host": "x"}))
    assert ei.value.status_code == 400
    from visualweaver import config
    m = cfg["redis_endpoints"][0]
    asyncio.run(routes_redis.api_add_endpoint({"name": MONGO, "type": "mongodb", "uri": m["uri"],
                                               "username": m["username"], "database": m["database"],
                                               "password": config._SECRET_SENTINEL}))
    m2 = cfg["redis_endpoints"][0]
    assert m2["builtin"] is True and m2["password"] == stores["password"]
    with pytest.raises(HTTPException):
        routes_redis.api_delete_endpoint(MONGO)


def test_endpoint_list_carries_builtin_and_the_real_default_type(stores, cfg, mongo_uri):
    from visualweaver import config, mongo_store, redis_store, routes_redis
    eps = routes_redis.api_list_endpoints()
    assert [(e["name"], e["type"], e["builtin"]) for e in eps] == \
        [("default", "redis", True), (MONGO, "mongodb", True)]
    uri, user, pw = mongo_store.split_uri_credentials(mongo_uri)
    cfg["storage"]["default_type"] = "mongodb"
    cfg["mongodb"] = {"type": "mongodb", "uri": uri, "username": user, "password": pw,
                      "database": stores["db"] + "_d", "builtin": True}
    redis_store.invalidate_redis_clients()
    try:
        d = routes_redis.api_list_endpoints()[0]
        assert (d["name"], d["type"], d["builtin"], d["primary"]) == ("default", "mongodb", True, True)
        assert d["host"] == mongo_store.display_host(uri) and d["password"] == config._SECRET_SENTINEL
        assert pw not in json.dumps(d)
        assert redis_store.endpoint_type("default") == "mongodb"
        assert mongo_store.is_mongo(redis_store.r_for("default"))
    finally:
        redis_store.r_for("default").client.drop_database(stores["db"] + "_d")


# ── config: the built-in MongoDB's password and the installed sections ───────

def test_mongodb_password_is_redacted_and_restored():
    from visualweaver import config
    stored = {"mongodb": {"uri": "mongodb://h:1/", "username": "vw", "password": "s3cret"},
              "redis_endpoints": [{"name": MONGO, "type": "mongodb", "password": "p2", "builtin": True}]}
    red = config._redact_secrets(stored)
    assert red["mongodb"]["password"] == config._SECRET_SENTINEL
    assert red["redis_endpoints"][0]["password"] == config._SECRET_SENTINEL
    assert "s3cret" not in json.dumps(red) and stored["mongodb"]["password"] == "s3cret"
    config._unredact_secrets(red, stored)
    assert red["mongodb"]["password"] == "s3cret" and red["redis_endpoints"][0]["password"] == "p2"
    blank = {"mongodb": {"password": ""}}
    config._unredact_secrets(blank, stored)
    assert blank["mongodb"]["password"] == ""                     # blank != sentinel


def test_a_settings_save_or_import_cannot_repoint_the_installed_storage(app_module, cfg, config_file, request):
    from visualweaver import config, redis_store, routes_settings, state
    import io
    from fastapi import UploadFile
    cfg.update({"storage": {"default_type": "mongodb", "app": MONGO},
                "mongodb": {"type": "mongodb", "uri": "mongodb://h:1/", "password": "pw", "builtin": True},
                "redis_endpoints": [{"name": MONGO, "type": "mongodb", "uri": "mongodb://m:2/",
                                     "password": "p2", "builtin": True},
                                    {"name": "mine", "type": "redis", "host": "x", "port": 1}]})
    stale = config._redact_secrets(state._config)
    stale["storage"] = {"default_type": "redis", "app": "default"}          # read before a switch
    stale["mongodb"] = {"uri": "mongodb://evil:1/"}
    stale["redis_endpoints"] = [{"name": "mine", "type": "redis", "host": "y", "port": 2, "builtin": True}]
    request.addfinalizer(redis_store.invalidate_redis_clients)   # the save rewarms a client for mongodb://h:1/
    import pytest as _pt
    mp = _pt.MonkeyPatch()
    request.addfinalizer(mp.undo)
    mp.setattr(redis_store, "retire_client", lambda c, delay=0: c.close())   # no 600 s retry thread
    asyncio.run(routes_settings.api_save_config(stale))
    assert state._config["storage"] == {"default_type": "mongodb", "app": MONGO}
    assert state._config["mongodb"]["uri"] == "mongodb://h:1/" and state._config["mongodb"]["password"] == "pw"
    eps = {e["name"]: e for e in state._config["redis_endpoints"]}
    assert eps["mine"] == {"name": "mine", "type": "redis", "host": "y", "port": 2}   # no self-made builtin
    assert eps[MONGO]["builtin"] is True and eps[MONGO]["password"] == "p2"
    on_disk = json.loads(config_file.read_text())
    assert on_disk["storage"]["app"] == MONGO

    upload = UploadFile(filename="c.json", file=io.BytesIO(json.dumps(
        {"storage": {"default_type": "redis", "app": "default"}, "redis_endpoints": []}).encode()))
    asyncio.run(routes_settings.api_import_config(upload))
    assert state._config["storage"] == {"default_type": "mongodb", "app": MONGO}
    assert [e["name"] for e in state._config["redis_endpoints"]] == [MONGO]


def test_an_environment_password_is_never_written_to_disk(app_module, cfg, config_file, monkeypatch):
    from visualweaver import config, state
    monkeypatch.setattr(state, "_mongodb_env_password", "from-env")
    cfg.update({"mongodb": {"uri": "mongodb://h:1/", "password": "from-env"},
                "redis_endpoints": [{"name": MONGO, "type": "mongodb", "password": "from-env", "builtin": True}]})
    config.save_config(state._config)
    disk = json.loads(config_file.read_text())
    assert disk["mongodb"]["password"] == "" and disk["redis_endpoints"][0]["password"] == ""
    assert state._config["mongodb"]["password"] == "from-env"     # in memory it stays


# ── VISUALWEAVER_STORAGE / VISUALWEAVER_MONGODB_* ────────────────────────────

@pytest.fixture
def storage_env(app_module, cfg, monkeypatch):
    from visualweaver import state
    for v in ("VISUALWEAVER_STORAGE", "VISUALWEAVER_MONGODB_URI", "VISUALWEAVER_MONGODB_USERNAME",
              "VISUALWEAVER_MONGODB_PASSWORD", "VISUALWEAVER_MONGODB_DATABASE"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(state, "_mongodb_env_password", "")
    monkeypatch.setattr(state, "_storage_env_overlay", {})
    cfg.clear()
    cfg.update({"storage": {"default_type": "redis", "app": "default"}, "redis_endpoints": []})
    return monkeypatch


def test_env_mongodb_makes_the_default_a_mongodb(storage_env, cfg):
    from visualweaver import redis_store, startup, state
    storage_env.setenv("VISUALWEAVER_STORAGE", "MongoDB")
    storage_env.setenv("VISUALWEAVER_MONGODB_URI", "mongodb://vw:p%40ss@mongo:27017/?authSource=admin")
    storage_env.setenv("VISUALWEAVER_MONGODB_DATABASE", "vwdb")
    startup.apply_storage_env()
    assert cfg["storage"]["default_type"] == "mongodb"
    assert cfg["mongodb"] == {"type": "mongodb", "uri": "mongodb://mongo:27017/?authSource=admin",
                              "username": "vw", "password": "p@ss", "database": "vwdb", "builtin": True}
    assert state._mongodb_env_password == "p@ss"
    assert redis_store.endpoint_type("default") == "mongodb"
    assert redis_store.endpoint_cfg("default")["database"] == "vwdb"


def test_env_password_and_username_override_the_uri(storage_env, cfg):
    from visualweaver import startup
    storage_env.setenv("VISUALWEAVER_STORAGE", "mongodb")
    storage_env.setenv("VISUALWEAVER_MONGODB_URI", "mongo:27017")
    storage_env.setenv("VISUALWEAVER_MONGODB_USERNAME", "app")
    storage_env.setenv("VISUALWEAVER_MONGODB_PASSWORD", "envpw")
    storage_env.setenv("VISUALWEAVER_MONGODB_DATABASE", "bad.name")
    startup.apply_storage_env()
    m = cfg["mongodb"]
    assert (m["uri"], m["username"], m["password"], m["database"]) == \
        ("mongodb://mongo:27017/", "app", "envpw", "visualweaver")


@pytest.mark.parametrize("uri", ["http://mongo:27017/", "mongodb://vw:<db_password>@mongo:27017/"])
def test_env_an_unusable_uri_is_ignored(storage_env, cfg, uri):
    from visualweaver import startup
    storage_env.setenv("VISUALWEAVER_STORAGE", "mongodb")
    storage_env.setenv("VISUALWEAVER_MONGODB_URI", uri)
    startup.apply_storage_env()
    assert cfg["storage"]["default_type"] == "redis" and "mongodb" not in cfg


def test_env_both_adds_the_builtin_mongodb_connection(storage_env, cfg):
    from visualweaver import startup
    cfg["redis_endpoints"] = [{"name": MONGO, "type": "mongodb", "uri": "mongodb://old:1/"},
                              {"name": "keep", "type": "redis"}]
    storage_env.setenv("VISUALWEAVER_STORAGE", "both")
    storage_env.setenv("VISUALWEAVER_MONGODB_URI", "mongodb://mongo:27017/")
    startup.apply_storage_env()
    assert cfg["storage"]["default_type"] == "redis"
    eps = {e["name"]: e for e in cfg["redis_endpoints"]}
    assert set(eps) == {"keep", MONGO}
    assert eps[MONGO]["uri"] == "mongodb://mongo:27017/" and eps[MONGO]["builtin"] is True
    assert "mongodb" not in cfg


def test_env_both_on_a_mongodb_first_install_keeps_mongodb_as_the_default(storage_env, cfg):
    """The installer records VISUALWEAVER_STORAGE=both in .visualweaver.env, which the
    start scripts export. On an installation that set up MongoDB first, "default" IS the
    built-in MongoDB: the variable must not re-point it at Redis."""
    from visualweaver import startup
    cfg["storage"]["default_type"] = "mongodb"
    cfg["mongodb"] = {"type": "mongodb", "uri": "mongodb://127.0.0.1:27019/", "builtin": True}
    cfg["redis_endpoints"] = [{"name": "local-redis", "type": "redis", "builtin": True}]
    storage_env.setenv("VISUALWEAVER_STORAGE", "both")
    startup.apply_storage_env()                                  # no URI: as config.json says
    assert cfg["storage"]["default_type"] == "mongodb"
    assert [e["name"] for e in cfg["redis_endpoints"]] == ["local-redis"]
    storage_env.setenv("VISUALWEAVER_MONGODB_URI", "mongodb://mongo:27017/")
    startup.apply_storage_env()                                  # with one: it is the built-in MongoDB
    assert cfg["storage"]["default_type"] == "mongodb"
    assert cfg["mongodb"]["uri"] == "mongodb://mongo:27017/"
    assert [e["name"] for e in cfg["redis_endpoints"]] == ["local-redis"]


def test_env_without_a_connection_changes_nothing(storage_env, cfg):
    from visualweaver import startup
    for choice in ("redis", "both", "mongodb", "sqlite"):
        cfg["storage"]["default_type"] = "mongodb"
        storage_env.setenv("VISUALWEAVER_STORAGE", choice)
        startup.apply_storage_env()
        assert cfg["storage"]["default_type"] == "mongodb", choice
    cfg["storage"]["default_type"] = "redis"
    storage_env.setenv("VISUALWEAVER_STORAGE", "mongodb")
    startup.apply_storage_env()
    assert cfg["storage"]["default_type"] == "redis" and "mongodb" not in cfg


def test_env_storage_is_not_written_to_config_json(storage_env, cfg, config_file):
    """Like the API keys: what the environment set applies to the run only."""
    from visualweaver import config, startup, state
    cfg["redis_endpoints"] = [{"name": "keep", "type": "redis"}]
    storage_env.setenv("VISUALWEAVER_STORAGE", "mongodb")
    storage_env.setenv("VISUALWEAVER_MONGODB_URI", "mongodb://vw:pw@mongo:27017/")
    startup.apply_storage_env()
    cfg["storage"]["app"] = "keep"                              # a real change since: kept
    config.save_config(state._config)
    disk = json.loads(config_file.read_text())
    assert disk["storage"] == {"default_type": "redis", "app": "keep"} and "mongodb" not in disk
    assert cfg["storage"]["default_type"] == "mongodb"           # the run still uses it
    state._storage_env_overlay.clear()
    cfg["storage"]["default_type"] = "redis"
    storage_env.setenv("VISUALWEAVER_STORAGE", "both")
    startup.apply_storage_env()
    config.save_config(state._config)
    assert [e["name"] for e in json.loads(config_file.read_text())["redis_endpoints"]] == ["keep"]


# ── moving the app data: semantics and safety ────────────────────────────────

def test_a_round_trip_keeps_what_was_cleared_cleared(stores, cfg):
    """Usage, stats and watch state follow the app exactly: reset on B, then back on A,
    the old tally must not come back."""
    from visualweaver import appstore, rag_admin, sessions, ws
    _write_app_data("rt", 1, 50, {"i\x00/x": "1:1"})
    _switch(MONGO)
    sessions.clear_usage()
    rag_admin.clear_rag_stats()
    with appstore.gate.write():
        appstore.current().delete(ws._WATCH_SEEN_KEY)
    res = _switch("default")
    assert res["copied"] == {"conversations": 1, "usage": False, "stats": False, "watch": 0}, res
    assert sessions.usage_totals() == {}
    assert appstore.current().get(rag_admin._RAG_STATS_KEY) is None
    assert appstore.current().hgetall(ws._WATCH_SEEN_KEY) == {}


def test_writes_during_a_move_land_on_the_new_store(stores, cfg, monkeypatch):
    """A turn saved while the copy runs must not stay behind on the old store."""
    import threading
    from visualweaver import appstore, routes_storage, sessions
    real = routes_storage.copy_app_data
    seen = {}

    def slow_copy(src, dst):
        t = threading.Thread(target=sessions.save_session, args=("late", [{"role": "user", "content": "x"}]))
        t.start()
        time.sleep(0.4)
        seen["blocked"] = t.is_alive()          # waiting at the gate, not writing to src
        seen["thread"] = t
        return real(src, dst)

    monkeypatch.setattr(routes_storage, "copy_app_data", slow_copy)
    _switch(MONGO)
    seen["thread"].join(10)
    assert seen["blocked"] is True
    assert appstore.for_connection(MONGO).get(sessions._session_key("late")) is not None
    assert appstore.for_connection("default").get(sessions._session_key("late")) is None


def test_a_move_waits_for_a_write_in_flight(stores, cfg):
    import threading
    from visualweaver import appstore
    done = threading.Event()
    holder = threading.Event()

    def writer():
        with appstore.gate.write():
            holder.set()
            time.sleep(0.5)
        done.set()
    threading.Thread(target=writer).start()
    holder.wait(5)
    _switch(MONGO)
    assert done.is_set(), "the move did not wait for the write that was in flight"


def test_a_gone_store_can_be_left_without_copying(stores, cfg):
    from visualweaver import appstore, sessions
    cfg["redis_endpoints"].append({"name": "gone", "type": "redis", "host": "127.0.0.1",
                                   "port": _closed_port(), "db": 0, "password": "", "ssl": False})
    cfg["storage"]["app"] = "gone"
    with pytest.raises(HTTPException) as ei:
        _switch(MONGO)
    assert ei.value.status_code == 400 and '"copy": false' in ei.value.detail
    assert appstore.app_connection() == "gone"
    res = _switch(MONGO, copy=False)
    assert res == {"ok": True, "app": MONGO, "copied": None}
    assert appstore.app_connection() == MONGO
    sessions.save_session("after", [{"role": "user", "content": "y"}])
    assert sessions.load_session("after")


def test_an_unreachable_mongodb_is_not_remembered_as_without_search(stores, cfg):
    from visualweaver import routes_storage, state
    dead = {"name": "flaky", "type": "mongodb", "uri": f"mongodb://127.0.0.1:{_closed_port()}",
            "username": "", "password": "", "database": "x"}
    cfg["redis_endpoints"].append(dead)
    ok, vs, _ = routes_storage._storage_probe("flaky", dead)
    assert (ok, vs) == (False, None) and state._search_available.get("flaky") is None


def test_storage_lists_builtin_connections_first(stores, cfg):
    from visualweaver import routes_storage
    cfg["redis_endpoints"].insert(0, {"name": "added", "type": "redis", "host": REDIS_HOST,
                                      "port": REDIS_PORT, "db": 0, "password": "", "ssl": False})
    cfg["redis_endpoints"].append({"name": "local-redis", "type": "redis", "host": REDIS_HOST,
                                   "port": REDIS_PORT, "db": 0, "password": "", "ssl": False, "builtin": True})
    assert [n for n, _ in routes_storage._storage_connections()] == ["default", MONGO, "local-redis", "added"]


def test_the_app_connection_survives_a_save_import_or_type_change(app_module, cfg, config_file):
    import io
    from fastapi import UploadFile
    from visualweaver import config, routes_redis, routes_settings, state
    cfg.update({"storage": {"default_type": "redis", "app": "appconn"},
                "redis_endpoints": [{"name": "appconn", "type": "redis", "host": "a", "port": 1},
                                    {"name": "other", "type": "redis", "host": "b", "port": 2}]})
    upload = UploadFile(filename="c.json", file=io.BytesIO(json.dumps({"redis_endpoints": []}).encode()))
    asyncio.run(routes_settings.api_import_config(upload))
    assert [e["name"] for e in state._config["redis_endpoints"]] == ["appconn"]
    edited = config._redact_secrets(state._config)
    edited["redis_endpoints"] = [{"name": "appconn", "type": "mongodb", "uri": "mongodb://x/"},
                                 {"name": "new", "type": "redis", "host": "c", "port": 3}]
    config.keep_installed_storage(edited, state._config)
    assert edited["redis_endpoints"] == [{"name": "appconn", "type": "redis", "host": "a", "port": 1},
                                         {"name": "new", "type": "redis", "host": "c", "port": 3}]
    edited["redis_endpoints"][0] = {"name": "appconn", "type": "redis", "host": "moved", "port": 1}
    config.keep_installed_storage(edited, state._config)
    assert edited["redis_endpoints"][0]["host"] == "moved"          # an edit is fine
    with pytest.raises(HTTPException) as ei:
        asyncio.run(routes_redis.api_add_endpoint({"name": "appconn", "type": "mongodb",
                                                   "uri": "mongodb://x:1/"}))
    assert ei.value.status_code == 400 and "app data" in ei.value.detail


def test_stats_restore_retries_when_the_store_was_not_readable(app_module, monkeypatch):
    """A store still starting at boot must not let the first turn overwrite the history."""
    from visualweaver import appstore, rag_admin, state

    class Down:
        kind = "redis"
        def get(self, k): raise ConnectionError("not up yet")
        def set(self, *a, **k): raise AssertionError("flushed before the restore")

    class Up:
        kind = "redis"
        def __init__(self): self.data = {rag_admin._RAG_STATS_KEY: json.dumps({"kept": _stats_row(40)})}
        def get(self, k): return self.data.get(k)
        def set(self, k, v, ttl=None): self.data[k] = v

    import copy
    saved = (copy.deepcopy(state._rag_stats), set(state._rag_stats_dirty))
    monkeypatch.setattr(rag_admin, "_stats_loaded", False)
    monkeypatch.setattr(rag_admin, "_stats_load_failed", False)
    try:
        state._rag_stats.clear()
        monkeypatch.setattr(appstore, "current", lambda: Down())
        rag_admin.load_rag_stats()
        assert rag_admin._stats_loaded is False
        state._rag_stats["fresh"] = _stats_row(1)
        state._rag_stats_dirty.add("fresh")
        rag_admin.save_rag_stats()                                  # Down: neither restored nor flushed
        up = Up()
        monkeypatch.setattr(appstore, "current", lambda: up)
        rag_admin.save_rag_stats()                                  # restores first, then flushes both
        assert rag_admin._stats_loaded is True
        assert set(json.loads(up.data[rag_admin._RAG_STATS_KEY])) == {"kept", "fresh"}
    finally:
        state._rag_stats.clear(); state._rag_stats.update(saved[0])
        state._rag_stats_dirty.clear(); state._rag_stats_dirty.update(saved[1])


def test_connection_target_resolves_every_kind_of_connection(app_module, cfg):
    from visualweaver import appstore
    cfg.update({"storage": {"default_type": "redis", "app": "atlas"},
                "redis": {"host": "r", "port": 6389, "db": 0, "password": "rp", "ssl": False},
                "mongodb": {"type": "mongodb", "uri": "mongodb://m:27019/", "password": "mp", "builtin": True},
                "redis_endpoints": [{"name": "atlas", "type": "mongodb", "uri": "mongodb+srv://c.mongodb.net/",
                                     "password": "ap"},
                                    {"name": "extra", "host": "e", "port": 1}]})
    kind, c = appstore.connection_target("default")
    assert (kind, c["type"], c["host"], c["password"], c["name"]) == ("redis", "redis", "r", "rp", "default")
    assert appstore.connection_target(None)[0] == "mongodb"                 # the app connection
    assert appstore.connection_target(None)[1]["password"] == "ap"
    assert appstore.connection_target("extra") == ("redis", {"name": "extra", "host": "e", "port": 1,
                                                              "type": "redis"})
    assert appstore.connection_target("nope")[1]["name"] == "default"       # as r_for falls back
    cfg["storage"]["default_type"] = "mongodb"
    kind, c = appstore.connection_target("default")
    assert (kind, c["uri"], c["password"], c["builtin"]) == ("mongodb", "mongodb://m:27019/", "mp", True)
    c["password"] = "changed"                                               # a copy
    assert cfg["mongodb"]["password"] == "mp"


def test_a_switch_never_copies_answer_cache_data():
    """The answer cache (semcache:* entries, the visualweaver:semcache_model marker,
    MongoDB's vw_cache) starts empty on the new store: copy_app_data reads only the app's
    own keys."""
    from visualweaver import rag_admin, routes_storage, sessions, ws

    class Recorder:
        def __init__(self, data, hashes):
            self.data, self.hashes, self.touched = data, hashes, []
        def keys(self, prefix):
            self.touched.append(("keys", prefix))
            return [k for k in list(self.data) + list(self.hashes) if k.startswith(prefix)]
        def mget(self, keys):
            self.touched += [("get", k) for k in keys]
            return [self.data.get(k) for k in keys]
        def ttls(self, keys):
            return [None for _ in keys]
        def get(self, k):
            self.touched.append(("get", k)); return self.data.get(k)
        def hgetall(self, k):
            self.touched.append(("hgetall", k)); return dict(self.hashes.get(k, {}))
        def set_many(self, items):
            self.touched += [("set", k) for k, _, _ in items]
        def set(self, k, v, ttl=None):
            self.touched.append(("set", k))
        def delete(self, *keys):
            self.touched += [("delete", k) for k in keys]
        def hset(self, k, m):
            self.touched.append(("hset", k))
        def hincrby(self, k, m):
            self.touched.append(("hincrby", k))

    src = Recorder({"session:a": "[]", "visualweaver:semcache_model": "m", "semcache:abc": "x",
                    rag_admin._RAG_STATS_KEY: "{}"},
                   {"semcache:entry1": {"prompt": "q"}, sessions._USAGE_KEY: {"p:m:in": "1"},
                    ws._WATCH_SEEN_KEY: {"f": "1:1"}})
    dst = Recorder({}, {})
    routes_storage.copy_app_data(src, dst)
    for store in (src, dst):
        assert not [t for t in store.touched if "semcache" in t[1]], store.touched
    assert ("set", "session:a") in dst.touched
