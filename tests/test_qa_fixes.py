# SPDX-License-Identifier: AGPL-3.0-or-later
"""Defects found by the 1.15.0 QA review, each pinned by the behaviour a user relies on."""
import asyncio
import io
import json

import pytest


class _HashStore:
    """The two hash calls _watch_seen_prune makes, in memory."""
    def __init__(self, data):
        self.data = dict(data)

    def hgetall(self, key):
        return dict(self.data)

    def hdel(self, key, *fields):
        for f in fields:
            self.data.pop(f, None)
        return len(fields)


def test_scanning_a_folder_does_not_prune_a_sibling_folder_with_the_same_prefix():
    from visualweaver import ws
    store = _HashStore({"i\x00/w/docs/a.txt": "1", "i\x00/w/docs/gone.txt": "2",
                        "i\x00/w/docs2/y.txt": "3", "j\x00/w/docs/a.txt": "4"})
    seen, stale = ws._watch_seen_prune(store, "i", [("/w/docs/a.txt", "1")], "/w/docs")
    assert stale == ["i\x00/w/docs/gone.txt"]
    assert "i\x00/w/docs2/y.txt" in store.data and "j\x00/w/docs/a.txt" in store.data
    # a root given with a trailing slash behaves the same
    _, stale = ws._watch_seen_prune(_HashStore({"i\x00/w/docs/z.txt": "1", "i\x00/w/docs2/y": "2"}),
                                    "i", [], "/w/docs/")
    assert stale == ["i\x00/w/docs/z.txt"]


def test_a_config_import_keeps_the_builtin_redis_of_a_redis_first_install(app_module, cfg):
    from fastapi.testclient import TestClient
    from visualweaver import appcore, config, state
    cfg["storage"] = {"default_type": "redis", "app": "default"}
    cfg["redis"] = {"host": "127.0.0.1", "port": 16999, "db": 0, "password": "", "ssl": False}
    saved = []
    orig = config.save_config
    config.save_config = lambda c: saved.append(c)
    try:
        body = json.dumps({"redis": {"host": "nas.example", "port": 6390}, "provider": "ollama"}).encode()
        r = TestClient(appcore.app).post("/api/config/import", files={"file": ("c.json", body, "application/json")})
    finally:
        config.save_config = orig
    assert r.status_code == 200, r.text
    assert (state._config["redis"]["host"], state._config["redis"]["port"]) == ("127.0.0.1", 16999)
    # on a MongoDB-first install the redis section is not the built-in store and may change
    new = {"redis": {"host": "nas.example"}}
    config.keep_installed_storage(new, {"storage": {"default_type": "mongodb"}, "redis": {"host": "x"}})
    assert new["redis"] == {"host": "nas.example"}


def test_credentials_in_a_mongodb_uri_never_reach_the_browser(app_module, cfg, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from visualweaver import appcore, config, constants
    raw = {"storage": {"default_type": "mongodb", "app": "default"},
           "mongodb": {"type": "mongodb", "uri": "mongodb://bob:s3cr%40t@db.example:27017/?authSource=admin",
                       "database": "visualweaver"},
           "redis_endpoints": [{"name": "atlas", "type": "mongodb",
                                "uri": "mongodb+srv://amy:hunter2@c0.ab.mongodb.net/?retryWrites=true"}]}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(raw))
    monkeypatch.setattr(constants, "CONFIG_PATH", p)
    loaded = config.load_config()
    assert loaded["mongodb"]["uri"] == "mongodb://db.example:27017/?authSource=admin"
    assert (loaded["mongodb"]["username"], loaded["mongodb"]["password"]) == ("bob", "s3cr@t")
    ep = loaded["redis_endpoints"][0]
    assert ep["uri"] == "mongodb+srv://c0.ab.mongodb.net/?retryWrites=true" and ep["password"] == "hunter2"
    cfg.clear(); cfg.update(loaded)
    c = TestClient(appcore.app)
    for path in ("/api/config", "/api/config/export", "/api/redis/endpoints"):
        text = c.get(path).text
        assert "s3cr" not in text and "hunter2" not in text, path


def test_resaving_the_app_data_connection_rebuilds_the_answer_cache(app_module, cfg):
    from visualweaver import routes_redis, state
    cfg["storage"] = {"default_type": "redis", "app": "atlas"}
    state._semantic_cache = object()
    routes_redis._drop_client("other")
    assert state._semantic_cache is not None, "another connection leaves the cache alone"
    routes_redis._drop_client("atlas")
    assert state._semantic_cache is None


@pytest.fixture
def two_stores(app_module, redis_client):
    """Two Redis app stores on separate databases of the private test server."""
    import redis
    from conftest import REDIS_HOST, REDIS_PORT
    from visualweaver import appstore
    a = appstore.RedisAppStore("a", redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=5))
    b = appstore.RedisAppStore("b", redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=6))
    for s in (a, b):
        s.raw.flushdb()            # dbs 5/6 of the throwaway test server, used only here
    yield a, b
    for s in (a, b):
        s.raw.flushdb()


def test_a_round_trip_does_not_bring_back_deleted_conversations_or_old_answers(two_stores):
    from visualweaver import cache, constants, routes_storage
    a, b = two_stores
    a.set("session:private", json.dumps([{"role": "user", "content": "private"}]), ttl=600)
    a.set("session:kept", json.dumps([{"role": "user", "content": "kept"}]), ttl=600)
    a.set(f"{constants.CACHE_PREFIX}old", "OLD ANSWER")          # an answer cached while on A
    routes_storage.copy_app_data(a, b)                            # A -> B
    b.delete("session:private")                                   # deleted while on B
    routes_storage.copy_app_data(b, a)                            # B -> A
    assert a.get("session:private") is None, "a conversation deleted on B came back"
    assert a.get("session:kept") is not None
    assert cache.clear_cache_entries(a) == 1 and a.keys(constants.CACHE_PREFIX) == []


def test_redis_search_verdicts_are_rechecked_and_db_nonzero_cannot_hold_indexes(app_module, redis_client, monkeypatch):
    import redis
    from conftest import REDIS_HOST, REDIS_PORT
    from visualweaver import routes_redis, routes_settings, state
    name = "qa-probe"
    state._search_available.pop(name, None)
    db0 = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)
    db3 = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=3)
    try:
        # a stale "no" (a module loaded later) is checked again after the recheck interval
        state._search_available[name] = False
        routes_settings._search_probed_at[name] = 0.0
        assert routes_settings.probe_search(db0, name) is True
        # "yes" is cached; Test (fresh) always checks again — here against a db the index cannot live in
        assert routes_settings.probe_search(db3, name) is True, "cached"
        assert routes_settings.probe_search(db3, name, fresh=True) is False
        assert routes_redis._db_note(3, False)["search_note"].startswith("Redis search works only in database 0")
        assert routes_redis._db_note(0, False) == {} and routes_redis._db_note(3, True) == {}
    finally:
        state._search_available.pop(name, None)
        routes_settings._search_probed_at.pop(name, None)


def test_a_name_on_a_connection_that_is_down_cannot_be_reused(app_module, cfg, monkeypatch):
    from fastapi import HTTPException
    from visualweaver import rag_admin, routes_instances, state
    monkeypatch.setattr(rag_admin, "list_rag_instances", lambda: [])       # the down store is skipped
    monkeypatch.setitem(state._known_instance_names, "qa-down", {"handbook"})
    monkeypatch.setitem(state._endpoint_health, "qa-down", False)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(routes_instances.api_create_instance({"name": "handbook"}))
    assert ei.value.status_code == 409 and "can't be reached" in ei.value.detail
    # once it is reachable again the normal listing is the source of truth
    monkeypatch.setitem(state._endpoint_health, "qa-down", True)
    assert rag_admin.names_on_unreachable_connections() == {}


def test_the_builtin_connection_names_are_reserved(app_module, cfg, monkeypatch):
    from fastapi import HTTPException
    from visualweaver import config, routes_redis
    monkeypatch.setattr(config, "save_config", lambda c: None)
    cfg["redis_endpoints"] = []
    for name in ("local-mongodb", "local-redis"):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(routes_redis.api_add_endpoint({"name": name, "type": "redis", "host": "10.0.0.9"}))
        assert ei.value.status_code == 400 and "reserved" in ei.value.detail


def test_an_instance_colour_can_never_carry_markup(app_module, cfg, monkeypatch):
    from fastapi import HTTPException
    from visualweaver import rag_admin, routes_instances
    evil = '#f00"><img src=x onerror=alert(1)>'
    for ok in ("#abc", "#a1B2c3", "#6366f1ff"):
        assert rag_admin.safe_color(ok) == ok
    for bad in (evil, "red", "#12", "url(x)", None, 7):
        assert rag_admin.safe_color(bad) == rag_admin.DEFAULT_COLOR
    monkeypatch.setattr(rag_admin, "list_rag_instances", lambda: [])
    with pytest.raises(HTTPException) as ei:
        asyncio.run(routes_instances.api_create_instance({"name": "qa-colour", "color": evil}))
    assert ei.value.status_code == 400


def test_the_answer_cache_loads_its_model_from_the_local_cache(app_module, cfg, monkeypatch, tmp_path):
    """By repo name the vectorizer asks the Hub about every file before loading; with the
    Hub slow or unreachable the cache stayed off for minutes after a start."""
    import redisvl.utils.vectorize as vz
    from visualweaver import cache, embeddings
    seen = []

    class _Fake:
        def __init__(self, model="", **kw):
            seen.append(model)
    monkeypatch.setattr(vz, "HFTextVectorizer", _Fake)
    snap = tmp_path / "snapshot"
    snap.mkdir()
    monkeypatch.setattr(embeddings, "_local_snapshot", lambda repo: str(snap) if "MiniLM" in repo else None)
    cfg.setdefault("embedding", {})["model"] = "all-MiniLM-L6-v2"
    cache._make_cache_vectorizer()
    cfg["embedding"]["model"] = "some-org/not-cached-model"
    cache._make_cache_vectorizer()
    assert seen == [str(snap), "some-org/not-cached-model"]


def test_an_ollama_url_from_the_environment_applies_to_the_run_only(app_module, cfg, monkeypatch, tmp_path):
    """In a container "localhost" is the container: VISUALWEAVER_OLLAMA_URL points the app at
    Ollama on the host while the setting is the shipped default, and is never saved."""
    from visualweaver import config, constants, startup, state
    monkeypatch.setattr(state, "_ollama_env_url", "")
    monkeypatch.setattr(constants, "CONFIG_PATH", tmp_path / "config.json")
    cfg["ollama"] = dict(constants.DEFAULT_CONFIG["ollama"])
    monkeypatch.setenv("VISUALWEAVER_OLLAMA_URL", "http://host.docker.internal:11434")
    startup.apply_ollama_env()
    assert (cfg["ollama"]["host"], cfg["ollama"]["port"]) == ("http://host.docker.internal", 11434)
    config.save_config(cfg)
    saved = json.loads((tmp_path / "config.json").read_text())["ollama"]
    assert (saved["host"], saved["port"]) == ("http://localhost", 11434), "the env address is not saved"
    # an address chosen in Settings wins over the environment
    monkeypatch.setattr(state, "_ollama_env_url", "")
    cfg["ollama"] = {"host": "http://gpu-box", "port": 11434, "model": ""}
    startup.apply_ollama_env()
    assert cfg["ollama"]["host"] == "http://gpu-box"


def test_an_answer_without_any_retrieved_context_is_not_cached():
    """A knowledge-base question that found nothing (index still building, documents not
    added yet) must not be replayed from the cache once the context exists."""
    from visualweaver import cache
    assert cache.worth_caching(["handbook"], []) is False
    assert cache.worth_caching(["handbook"], None) is False
    assert cache.worth_caching(["handbook"], [{"text": "x"}]) is True
    assert cache.worth_caching([], []) is True            # no RAG asked for: cached as before
