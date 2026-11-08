# SPDX-License-Identifier: AGPL-3.0-or-later
"""The answer cache on either app-data store, against real servers.

The cache follows ``storage.app``: on the built-in Redis it is redisvl's SemanticCache,
on a MongoDB connection visualweaver.mongo_cache. Every behaviour test here runs on
both — the suite's private Redis and a throwaway built-in MongoDB (official Community
server + search images via visualweaver.local_mongo, removed with its volumes
afterwards) — with the real embedding model, so the two are compared on the same
vectors. Without a MongoDB the Mongo half is skipped, unless
VISUALWEAVER_REQUIRE_MONGO_TESTS=1.

Redis keys and the index are namespaced under conftest.KEY_PREFIX (CACHE_PREFIX and the
model marker are patched), so the suite's cleanup finds them. The MongoDB tests share one
database, emptied after each test and dropped at the end.
"""
import os
import time

import pytest

from conftest import KEY_PREFIX, REDIS_DB, REDIS_HOST, REDIS_PORT

EP = "mongo-cache-test"
MODEL = "all-MiniLM-L6-v2"           # 384 dims, in the local model cache
OTHER_MODEL = "sentence-transformers/all-mpnet-base-v2"   # 768 dims
THRESHOLD = 0.9


# mongo_uri: the session-wide throwaway built-in MongoDB from conftest.py.


@pytest.fixture(scope="module")
def mongo_db(mongo_uri):
    """One database for the module, dropped at the end. Tests share it and empty it
    rather than paying for a new collection's index each time."""
    from pymongo import MongoClient
    db = f"vwtest_cache_{os.getpid()}_{int(time.time() * 1000) % 10**8}"
    yield mongo_uri, db
    c = MongoClient(mongo_uri, directConnection=True, serverSelectionTimeoutMS=5000)
    try:
        c.drop_database(db)
    finally:
        c.close()


class _Store:
    """One backend under test: which store the app data (and so the cache) is on."""

    def __init__(self, kind, cfg, mongo=None):
        self.kind, self.cfg, self.mongo = kind, cfg, mongo

    def add_mongo(self):
        """Configure the MongoDB connection without using it yet."""
        from visualweaver import redis_store
        if not any(e.get("name") == EP for e in self.cfg.get("redis_endpoints", [])):
            uri, db = self.mongo
            self.cfg.setdefault("redis_endpoints", []).append(
                {"name": EP, "type": "mongodb", "uri": uri, "database": db,
                 "username": "", "password": ""})
            redis_store.invalidate_redis_clients()

    def use(self, kind):
        """Point storage.app at this kind of store — nothing else is reset."""
        if kind == "mongodb":
            self.add_mongo()
        self.cfg["storage"] = {"app": EP if kind == "mongodb" else "default"}
        self.kind = kind

    @property
    def ep(self):
        from visualweaver import redis_store
        return redis_store.r_for(EP)


def _base(cfg, monkeypatch):
    from visualweaver import cache, config, constants, redis_store, state
    monkeypatch.setattr(constants, "CACHE_PREFIX", f"{KEY_PREFIX}semcache:")
    monkeypatch.setattr(cache, "_CACHE_MODEL_KEY", f"{KEY_PREFIX}semcache_model")
    monkeypatch.setattr(cache, "_cache_model_home", None)
    monkeypatch.setattr(cache, "_last_built_model", None)
    monkeypatch.setattr(cache, "_build_failed", None)
    monkeypatch.setattr(config, "save_config", lambda *_a, **_k: None)
    monkeypatch.setattr(state, "_semantic_cache_ready", True)
    cfg["redis"] = {"host": REDIS_HOST, "port": REDIS_PORT, "db": REDIS_DB, "password": "", "ssl": False}
    cfg["redis_endpoints"] = []
    cfg["embedding"] = {**(cfg.get("embedding") or {}), "model": MODEL}
    cfg["cache"] = {"enabled": True, "similarity_threshold": THRESHOLD, "ttl": 3600}
    redis_store.invalidate_redis_clients()
    state._semantic_cache = None
    state._semantic_cache_model = None


def _teardown(store):
    from visualweaver import redis_store, state
    state._semantic_cache = None
    state._semantic_cache_model = None
    try:
        if store.mongo:
            from visualweaver import mongo_cache
            store.ep.col(mongo_cache.COLLECTION).delete_many({})
    finally:
        redis_store.invalidate_redis_clients()


@pytest.fixture(params=["redis", "mongodb"])
def store(request, app_module, cfg, clean_redis, monkeypatch):
    mongo = request.getfixturevalue("mongo_db") if request.param == "mongodb" else None
    _base(cfg, monkeypatch)
    s = _Store("redis", cfg, mongo)
    s.use(request.param)
    _ready(s)
    yield s
    _teardown(s)


@pytest.fixture
def both(app_module, cfg, clean_redis, monkeypatch, mongo_db):
    """Both stores configured; the test chooses with use()."""
    _base(cfg, monkeypatch)
    s = _Store("redis", cfg, mongo_db)
    s.add_mongo()
    yield s
    _teardown(s)


@pytest.fixture
def redis_only(app_module, cfg, clean_redis, monkeypatch):
    _base(cfg, monkeypatch)
    s = _Store("redis", cfg)
    yield s
    _teardown(s)


def _ready(s, timeout=120.0):
    """Build the cache on the current store; on MongoDB wait until its index answers.

    "Answers" means a real lookup returns an entry stored for the purpose (removed
    afterwards) — not the index's `queryable` flag, which mongot reports ~30 s after
    lookups already work, so waiting on it only made each test sit idle. While an index
    is being rebuilt at another width a lookup can fail outright for a moment; that is
    "not answering yet" too."""
    from pymongo.errors import OperationFailure
    from visualweaver import cache
    c = cache._get_semantic_cache()
    assert c is not None, "the cache did not build"
    assert getattr(c, "kind", "redis") == s.kind
    if s.kind == "mongodb":
        probe, scope = f"{KEY_PREFIX} is the answer cache answering yet", f"{KEY_PREFIX}ready"
        eid = c.store(prompt=probe, response="probe", filters={"scope": scope})
        t0, last = time.time(), None
        try:
            while True:
                try:
                    if any(h.get("entry_id") == eid
                           for h in c.check(prompt=probe, filters={"scope": scope})):
                        break
                except OperationFailure as e:
                    last = e
                assert time.time() - t0 < timeout, \
                    f"the cache's vector index never answered a lookup (last error: {last})"
                time.sleep(0.25)
        finally:
            c.col.delete_one({"_id": eid})
    return c


def _lookup(q, scope="s", exact=False):
    from visualweaver import cache
    return cache.cache_lookup(q, cache.effective_threshold(), scope, exact)


def _until(q, pred=bool, timeout=30.0, **kw):
    """mongot indexes asynchronously; poll until the lookup satisfies pred."""
    t0, hit = time.time(), None
    while time.time() - t0 < timeout:
        hit = _lookup(q, **kw)
        if pred(hit):
            return hit
        time.sleep(0.25)
    return hit


def _stats():
    from visualweaver import routes_redis
    return routes_redis.api_cache_stats()


def _cosine(a, b):
    import numpy as np
    from visualweaver import cache
    v = cache._cache_vectorizer()
    x, y = (np.asarray(v.embed(cache._normalize_query(t)), dtype=np.float64) for t in (a, b))
    return float(x @ y / (np.linalg.norm(x) * np.linalg.norm(y)))


def _raw_check(c, q, scope="s", **kw):
    """The store's own check(), past cache_lookup's second threshold test."""
    from visualweaver import cache
    if getattr(c, "kind", "") == "mongodb":
        return c.check(prompt=cache._normalize_query(q), num_results=1, filters={"scope": scope}, **kw)
    from redisvl.query.filter import Tag
    return c.check(prompt=cache._normalize_query(q), num_results=1,
                   filter_expression=Tag("scope") == scope, **kw)


Q = "how long does a cached answer stay in the cache"
Q_REWORDED = "How long does a cached answer stay in the cache?"
UNRELATED = "what is the boiling point of water at sea level"


# ── hit / miss ───────────────────────────────────────────────────────────────

def test_a_close_rewording_is_a_hit(store):
    from visualweaver import cache
    cache.cache_store(Q, "ONE HOUR", [{"text": "chunk", "source": "a.md"}], "s")
    hit = _until(Q_REWORDED)
    assert hit, f"no hit on {store.kind}"
    assert hit["response"] == "ONE HOUR"
    assert hit["score"] >= THRESHOLD
    assert hit["score"] == pytest.approx(_cosine(Q, Q_REWORDED), abs=1e-3), (
        "the reported similarity is not the cosine of the two questions")
    assert hit["chunks"] == [{"text": "chunk", "source": "a.md"}]
    assert hit["entry_id"]


def test_below_the_threshold_is_a_miss(store, cfg):
    from visualweaver import cache
    cache.cache_store(Q, "ONE HOUR", None, "s")
    hit = _until(Q_REWORDED)
    assert hit, "the stored entry never became visible, so a miss would prove nothing"
    assert _lookup(UNRELATED) is None
    # The same rewording, now under a stricter threshold than its own score.
    strict = min(0.9999, hit["score"] + 0.002)
    cfg["cache"]["similarity_threshold"] = strict
    assert _lookup(Q_REWORDED) is None, f"threshold ignored on {store.kind}"
    assert _lookup(Q) is not None, "the identical question must still hit"
    # and the store itself searched at the new distance, not only cache_lookup's re-check
    c = cache._get_semantic_cache()
    assert c.distance_threshold == pytest.approx(round(1 - strict, 4))
    assert _raw_check(c, Q_REWORDED) == []
    got = _raw_check(c, Q_REWORDED, distance_threshold=1 - hit["score"] + 0.002)
    assert len(got) == 1 and got[0]["vector_distance"] == pytest.approx(1 - hit["score"], abs=1e-3)


def test_scopes_do_not_see_each_other(store):
    from visualweaver import cache
    cache.cache_store(Q, "SCOPE A", None, "scope-a")
    cache.cache_store(Q, "NO SCOPE", None, "")
    assert _until(Q, scope="scope-a")["response"] == "SCOPE A"
    assert _until(Q, scope="")["response"] == "NO SCOPE"
    assert _lookup(Q, scope="scope-b") is None


def test_the_exact_partition_matches_only_the_same_question(store):
    from visualweaver import cache
    cache.cache_store("plot f(x) = sin(x)", "SIN X", None, "v")
    assert _until("Plot  f(x) = sin(x)", scope="v", exact=True)["response"] == "SIN X"
    assert _lookup("plot f(x) = sin(2x)", scope="v", exact=True) is None


def test_storing_the_same_question_again_replaces_it(store):
    from visualweaver import cache
    cache.cache_store(Q, "FIRST", None, "s")
    first = _until(Q)
    cache.cache_store(Q, "SECOND", None, "s")
    hit = _until(Q, lambda h: bool(h) and h["response"] == "SECOND")
    assert hit["response"] == "SECOND" and hit["entry_id"] == first["entry_id"]
    assert _stats()["count"] == 1


# ── TTL ──────────────────────────────────────────────────────────────────────

def test_an_expired_entry_is_gone(store, cfg):
    from visualweaver import cache
    cfg["cache"]["ttl"] = 2
    cache._get_semantic_cache()                   # the live object picks the TTL up
    cache.cache_store(Q, "SHORT", None, "s")
    entries = _stats()["entries"]                 # before any hit could refresh it
    assert len(entries) == 1 and 0 <= entries[0]["ttl"] <= 2, entries
    assert _until(Q)["response"] == "SHORT"
    time.sleep(3.2)
    assert _lookup(Q) is None, f"an expired entry was served on {store.kind}"
    assert _stats()["count"] == 0


def test_mongodb_does_not_wait_for_the_ttl_monitor(both):
    """The TTL monitor deletes about once a minute; reads must not serve what it has
    not reached yet."""
    import datetime as dt
    from visualweaver import cache, mongo_cache
    both.use("mongodb")
    _ready(both)
    cache.cache_store(Q, "LIVE", None, "s")
    assert _until(Q)["response"] == "LIVE"
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=5)
    both.ep.col(mongo_cache.COLLECTION).update_many({}, {"$set": {"exp": past}})
    assert both.ep.col(mongo_cache.COLLECTION).count_documents({}) == 1, "already deleted"
    # At once — not after the search index has caught up with the change.
    assert _lookup(Q) is None, "an expired entry was served"
    assert _stats() == {"count": 0, "entries": []}
    for _ in range(8):                            # and a lookup did not revive it
        time.sleep(0.25)
        assert _lookup(Q) is None
    left = both.ep.col(mongo_cache.COLLECTION).find_one({})
    # Gone is fine: the monitor's minute may have come round meanwhile. Present, it must
    # still be expired (a lookup that revived it would have moved exp forward).
    if left is not None:
        assert left["exp"].replace(tzinfo=dt.timezone.utc) < dt.datetime.now(dt.timezone.utc)


def test_a_hit_keeps_the_entry_alive(store, cfg):
    from visualweaver import cache
    cfg["cache"]["ttl"] = 3
    cache._get_semantic_cache()
    cache.cache_store(Q, "KEPT", None, "s")
    assert _until(Q)
    for _ in range(3):                           # 3 × 1.5 s > the 3 s TTL
        time.sleep(1.5)
        assert _lookup(Q), f"a hit did not refresh the TTL on {store.kind}"


# ── model change ─────────────────────────────────────────────────────────────

def test_a_marker_from_another_model_discards_the_entries(store):
    """The marker lives on the store: a restart (no in-process marker) under another
    model must find it there and discard."""
    from visualweaver import cache, state
    cache.cache_store(Q, "OLD MODEL", None, "s")
    assert _until(Q)
    assert cache._stored_cache_model() == cache._cache_embedding_model()
    cache._set_stored_cache_model("sentence-transformers/some-other-model")
    state._semantic_cache = None                  # as after a restart
    state._semantic_cache_model = None
    assert cache._cache_model_changed() is True
    _ready(store)
    assert _stats()["count"] == 0
    assert _lookup(Q) is None
    assert cache._stored_cache_model() == cache._cache_embedding_model()


def test_a_real_model_change_rebuilds_at_the_new_width(store, cfg):
    from visualweaver import cache, mongo_cache
    cache.cache_store(Q, "384", None, "s")
    assert _until(Q)
    cfg["embedding"]["model"] = OTHER_MODEL
    cfg["cache"]["similarity_threshold"] = THRESHOLD
    _ready(store)
    t0 = time.time()
    while cache._cache_index_dim() != 768:        # MongoDB recreates it in the background
        assert time.time() - t0 < 120, f"index width {cache._cache_index_dim()}"
        time.sleep(0.25)
    _ready(store)
    assert _stats()["count"] == 0
    cfg["cache"]["similarity_threshold"] = THRESHOLD   # the change reset it to the model's
    cache.cache_store(Q, "768", None, "s")
    assert _until(Q)["response"] == "768"


# ── cache analytics routes ───────────────────────────────────────────────────

def test_the_analytics_routes(store):
    from visualweaver import cache, routes_redis
    for i, q in enumerate(["what is a vector database", "how do I enable tls on redis",
                           "explain the semantic cache ttl"]):
        cache.cache_store(q, f"A{i}", None, "s")
    hit = _until("what is a vector database")
    st = _stats()
    assert st["count"] == 3 and len(st["entries"]) == 3
    assert {e["query"] for e in st["entries"]} == {"what is a vector database",
                                                   "how do i enable tls on redis",
                                                   "explain the semantic cache ttl"}
    assert all(0 < e["ttl"] <= 3600 for e in st["entries"])
    assert set(st) == {"count", "entries"}
    assert routes_redis.api_delete_cache_entry(hit["entry_id"]) == {"ok": True}
    assert routes_redis.api_delete_cache_entry(hit["entry_id"]) == {"ok": False}
    assert routes_redis.api_delete_cache_entry("") == {"ok": False, "error": "entry_id required"}
    assert _stats()["count"] == 2
    assert _until("what is a vector database", lambda h: h is None) is None
    assert routes_redis.api_cache_clear() == {"deleted": "all"}
    assert _stats()["count"] == 0
    assert _lookup("how do i enable tls on redis") is None


def test_clearing_with_the_cache_disabled_still_empties_it(store, cfg):
    from visualweaver import cache, routes_redis
    cache.cache_store(Q, "X", None, "s")
    assert _until(Q)
    cfg["cache"]["enabled"] = False
    out = routes_redis.api_cache_clear()
    assert out == {"deleted": 1}
    assert _stats()["count"] == 0


# ── the cache follows the app-data store ─────────────────────────────────────

def test_the_cache_moves_with_the_app_data(both):
    """storage.app names the store; the cache object and its model marker belong to the
    store they were built on, even if nothing nulled the live object."""
    from visualweaver import cache, state
    both.use("redis")
    _ready(both)
    cache.cache_store(Q, "ON REDIS", None, "s")
    assert _until(Q)["response"] == "ON REDIS"
    both.use("mongodb")                           # state._semantic_cache NOT reset
    c = _ready(both)
    assert c.kind == "mongodb"
    assert _lookup(Q) is None, "the new store starts empty"
    cache.cache_store(Q, "ON MONGO", None, "s")
    assert _until(Q)["response"] == "ON MONGO"
    assert both.ep.col("vw_cache").count_documents({}) == 1
    both.use("redis")                             # and back, again without a reset
    assert _until(Q)["response"] == "ON REDIS", "the old store's entries were lost"
    assert getattr(state._semantic_cache, "kind", "redis") == "redis"


def test_a_redis_endpoint_for_app_data_moves_the_cache_too(redis_only, cfg, redis_client):
    """A further Redis connection chosen for app data: the cache uses its client, not the
    built-in one."""
    from visualweaver import cache, constants, redis_store
    cfg["redis_endpoints"].append({"name": "other-redis", "type": "redis", "host": REDIS_HOST,
                                   "port": REDIS_PORT, "db": REDIS_DB, "password": "", "ssl": False})
    cfg["storage"] = {"app": "other-redis"}
    redis_store.invalidate_redis_clients()
    kind, rc = cache._cache_backend()
    assert kind == "redis" and rc is redis_store.r_for("other-redis")
    assert rc is not redis_store.r()
    c = cache._get_semantic_cache()
    assert c is not None and c._redis_client is rc, "the cache was built on another client"
    cache.cache_store(Q, "OTHER", None, "s")
    assert _lookup(Q)["response"] == "OTHER"
    assert list(redis_client.scan_iter(f"{constants.CACHE_PREFIX}*"))


def test_no_ttl_is_reported_as_never_expiring(store, cfg):
    from visualweaver import cache
    cfg["cache"]["ttl"] = 0
    cache._get_semantic_cache()
    cache.cache_store(Q, "FOREVER", None, "s")
    assert _stats()["entries"][0]["ttl"] == -1
    assert _until(Q)["response"] == "FOREVER"
    assert _stats()["entries"][0]["ttl"] == -1, "a hit gave it an expiry"


def test_moving_to_a_store_cached_under_another_model_keeps_the_threshold(both, cfg):
    """That store's entries are stale and go; the threshold was set for the configured
    model, which did not change, and stays."""
    from visualweaver import appstore, cache, mongo_cache
    both.use("mongodb")
    _ready(both)
    cache.cache_store(Q, "STALE", None, "s")
    appstore.current().set(cache._CACHE_MODEL_KEY, "sentence-transformers/some-older-model")
    both.use("redis")
    _ready(both)
    cfg["cache"]["similarity_threshold"] = 0.91            # an explicit choice
    both.use("mongodb")
    _ready(both)
    assert cfg["cache"]["similarity_threshold"] == 0.91, "the user's threshold was reset"
    assert both.ep.col(mongo_cache.COLLECTION).count_documents({}) == 0, "stale entries kept"
    assert cache._stored_cache_model() == cache._cache_embedding_model()


def test_a_changed_model_still_resets_the_threshold(redis_only, cfg, monkeypatch):
    from visualweaver import cache, state
    _ready(redis_only)
    cfg["cache"]["similarity_threshold"] = 0.91
    cfg["embedding"]["model"] = OTHER_MODEL
    state._semantic_cache = None                  # a settings save
    cache._get_semantic_cache()
    assert cfg["cache"]["similarity_threshold"] is None


def test_the_default_connection_can_be_mongodb(cfg, monkeypatch):
    """A MongoDB-only install: the built-in "default" connection is MongoDB."""
    from visualweaver import appstore, cache, redis_store, state
    monkeypatch.setattr(state, "_config", {"storage": {"default_type": "mongodb", "app": "default"},
                                           "mongodb": {"uri": "mongodb://h:1", "database": "d"}})

    class _Store:
        kind, raw, name = "mongodb", object(), "default"
    monkeypatch.setattr(appstore, "current", lambda: _Store)
    monkeypatch.setattr(redis_store, "r", lambda: pytest.fail("the built-in Redis was used"))
    assert cache._cache_home() == ("default", "mongodb", "mongodb://h:1|d")
    assert cache._cache_backend() == ("mongodb", _Store.raw)


def test_a_mongodb_without_vector_search_disables_the_cache_quietly(both, monkeypatch):
    from visualweaver import cache, mongo_cache, mongo_store
    calls = []

    def _refuse(ep, dims):
        calls.append(dims)
        raise mongo_store.NoVectorSearch(mongo_store.NO_SEARCH_MSG)
    monkeypatch.setattr(mongo_cache, "ensure_indexes", _refuse)
    both.use("mongodb")
    assert cache._get_semantic_cache() is None
    assert _lookup(Q) is None
    cache.cache_store(Q, "X", None, "s")          # must not raise
    assert cache._get_semantic_cache() is None
    assert len(calls) == 1, "every question retried the build"
    monkeypatch.setattr(cache, "_BUILD_RETRY_S", 0.0)
    cache._get_semantic_cache()
    assert len(calls) == 2, "the build was never retried"


# ── parity ───────────────────────────────────────────────────────────────────

PARITY_STORED = [
    ("s1", "how long does a cached answer stay in the cache", "TTL"),
    ("s1", "how do I enable tls on redis", "TLS"),
    ("s1", "what is a vector database", "VDB"),
    ("s2", "what is a vector database", "VDB-S2"),
    ("v", "plot f(x) = sin(x)", "SIN"),
]
PARITY_QUERIES = [            # (scope, query, exact_only)
    ("s1", "How long does a cached answer stay in the cache?", False),
    ("s1", "how long is a cached answer kept", False),
    ("s1", "How do I enable TLS on Redis", False),
    ("s1", "how do I disable tls on redis", False),
    ("s1", "what is a vector database?", False),
    ("s1", "what is a graph database", False),
    ("s1", "what is the boiling point of water at sea level", False),
    ("s2", "What is a vector database", False),
    ("s2", "how do I enable tls on redis", False),
    ("s3", "what is a vector database", False),
    ("v", "plot f(x) = sin(x)", True),
    ("v", "plot f(x) = sin(2x)", True),
    ("", "what is a vector database", False),
]


def _decisions():
    out = []
    for scope, q, exact in PARITY_QUERIES:
        h = _lookup(q, scope=scope, exact=exact)
        out.append((scope, q, h["response"] if h else None, h["score"] if h else None))
    return out


def test_both_stores_make_the_same_decisions(both):
    from visualweaver import cache, state
    results = {}
    for kind in ("redis", "mongodb"):
        both.use(kind)
        state._semantic_cache = None
        _ready(both)
        for scope, q, resp in PARITY_STORED:
            cache.cache_store(q, resp, None, scope)
        # every stored entry visible before any miss is believed
        for scope, q, resp in PARITY_STORED:
            assert _until(q, lambda h, r=resp: bool(h) and h["response"] == r,
                          scope=scope, exact=scope == "v"), (kind, q)
        results[kind] = _decisions()
    for r, m in zip(results["redis"], results["mongodb"]):
        print(f"{r[0]:>3} {r[1][:50]:<50} redis={r[2]}/{r[3]} mongodb={m[2]}/{m[3]}")
        assert r[:3] == m[:3], f"redis {r} vs mongodb {m}"
        if r[3] is not None:
            assert abs(r[3] - m[3]) <= 1e-3, f"scores differ: redis {r} vs mongodb {m}"
    hits = sum(1 for r in results["redis"] if r[2])
    assert 0 < hits < len(PARITY_QUERIES), "the query set must contain both hits and misses"
