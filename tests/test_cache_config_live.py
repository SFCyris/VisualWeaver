# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cache settings have to reach the cache.

The SemanticCache object is memoised for the life of the process, so everything read
at build time was frozen there. Changing the cache threshold or TTL in Settings did
nothing until a restart, with nothing to say so — and it was worse than inert: redisvl
discards candidates beyond the BUILD-time distance, so lowering the threshold could not
widen what matched, and every new entry kept the old TTL. Entries in a real deployment
showed both TTLs side by side.
"""
import pytest

from visualweaver import cache, state


class _FakeCache:
    """Stands in for redisvl's SemanticCache: same two setters, same two properties."""

    def __init__(self, distance_threshold=0.08, ttl=3600):
        self.distance_threshold = distance_threshold
        self.ttl = ttl
        self.calls: list = []

    def set_threshold(self, v):
        self.calls.append(("threshold", v)); self.distance_threshold = v

    def set_ttl(self, v):
        self.calls.append(("ttl", v)); self.ttl = v


@pytest.fixture
def cfg(monkeypatch):
    conf: dict = {"cache": {"enabled": True, "similarity_threshold": 0.92, "ttl": 3600}}
    monkeypatch.setattr(state, "_config", conf)
    return conf


def test_a_changed_threshold_reaches_the_live_cache(cfg):
    c = _FakeCache()
    cfg["cache"]["similarity_threshold"] = 0.81
    cache._apply_cache_config(c)
    assert c.distance_threshold == pytest.approx(0.19)


def test_a_changed_ttl_reaches_the_live_cache(cfg):
    c = _FakeCache()
    cfg["cache"]["ttl"] = 800_000
    cache._apply_cache_config(c)
    assert c.ttl == 800_000


def test_the_memoised_cache_is_reconfigured_on_every_lookup(monkeypatch, cfg):
    """The setters are on the return path, not only after a build — otherwise the fix
    only helps the process that happened to build the object."""
    c = _FakeCache()
    monkeypatch.setattr(state, "_semantic_cache", c)
    cfg["cache"].update(similarity_threshold=0.70, ttl=1234)
    got = cache._get_semantic_cache()
    assert got is c
    assert c.distance_threshold == pytest.approx(0.30) and c.ttl == 1234


def test_an_unchanged_setting_costs_no_write(cfg):
    """Called on every lookup, so it must be a comparison, not two round trips."""
    c = _FakeCache(distance_threshold=0.08, ttl=3600)
    cache._apply_cache_config(c)
    assert c.calls == []


def test_an_unset_threshold_resolves_to_the_models_calibration(cfg):
    """There is no fixed default any more: the models do not share a scale, so the
    number comes from whichever embedding model is configured."""
    from visualweaver import embeddings
    cfg["cache"] = {"enabled": True}
    cfg["embedding"] = {"model": "all-MiniLM-L6-v2"}
    c = _FakeCache(distance_threshold=0.5, ttl=1)
    cache._apply_cache_config(c)
    assert c.distance_threshold == pytest.approx(1 - 0.93)
    assert c.ttl == 3600

    cfg["embedding"] = {"model": "intfloat/multilingual-e5-base"}
    cache._apply_cache_config(c)
    assert c.distance_threshold == pytest.approx(1 - 0.98)
    assert embeddings.cache_threshold_for("intfloat/multilingual-e5-base") == 0.98


def test_a_setter_that_raises_does_not_fail_the_turn(cfg, caplog):
    """A cache is disposable; a chat turn is not."""
    class _Angry(_FakeCache):
        def set_threshold(self, v): raise RuntimeError("nope")
        def set_ttl(self, v): raise RuntimeError("nope")
    cfg["cache"].update(similarity_threshold=0.5, ttl=99)
    cache._apply_cache_config(_Angry())          # must not raise


def test_a_disabled_cache_is_not_reconfigured(monkeypatch, cfg):
    c = _FakeCache()
    monkeypatch.setattr(state, "_semantic_cache", c)
    cfg["cache"]["enabled"] = False
    assert cache._get_semantic_cache() is None
    assert c.calls == []


# ── a changed embedding model invalidates the cached vectors ─────────────────
class _FakeRedis:
    def __init__(self, model=None, keys=()):
        self.kv = {b"visualweaver:semcache_model": model.encode()} if model else {}
        self.keys_ = [k.encode() for k in keys]
        self.commands: list = []
        self.deleted: list = []

    def get(self, k): return self.kv.get(k.encode() if isinstance(k, str) else k)

    def set(self, k, v):
        self.kv[k.encode() if isinstance(k, str) else k] = v.encode() if isinstance(v, str) else v
        return True

    def delete(self, *ks):
        for k in ks:
            kb = k.encode() if isinstance(k, str) else k
            self.kv.pop(kb, None); self.deleted.append(kb)
        return len(ks)

    def scan_iter(self, match=None, count=None): return iter(list(self.keys_))

    def execute_command(self, *a):
        self.commands.append(a); return "OK"


@pytest.fixture
def fake_redis(monkeypatch):
    from visualweaver import redis_store
    r = _FakeRedis()
    monkeypatch.setattr(redis_store, "r", lambda *a, **k: r)
    monkeypatch.setattr(state, "_semantic_cache_model", None)
    monkeypatch.setattr(state, "_semantic_cache", None)
    return r


def test_the_same_model_keeps_the_cache(fake_redis, cfg):
    cfg["embedding"] = {"model": "all-MiniLM-L6-v2"}
    fake_redis.set("visualweaver:semcache_model", "sentence-transformers/all-MiniLM-L6-v2")
    assert cache._cache_model_changed() is False


def test_a_changed_model_is_detected(fake_redis, cfg):
    cfg["embedding"] = {"model": "intfloat/multilingual-e5-base"}
    fake_redis.set("visualweaver:semcache_model", "sentence-transformers/all-MiniLM-L6-v2")
    assert cache._cache_model_changed() is True


def test_a_bare_model_name_is_not_mistaken_for_a_change(fake_redis, cfg):
    """The vectorizer prefixes bare names with sentence-transformers/. Comparing the raw
    config value against the resolved one reported a change on every call."""
    cfg["embedding"] = {"model": "all-MiniLM-L6-v2"}
    fake_redis.set("visualweaver:semcache_model", "sentence-transformers/all-MiniLM-L6-v2")
    assert cache._cache_model_changed() is False


def test_nothing_is_discarded_before_a_cache_has_ever_been_built(fake_redis, cfg):
    cfg["embedding"] = {"model": "anything-else"}
    assert cache._cache_model_changed() is False


def test_the_marker_survives_the_config_post_that_nulls_the_cache(fake_redis, cfg, monkeypatch):
    """The defect this closes: routes_settings calls invalidate_redis_clients() on every
    POST /api/config, which nulls state._semantic_cache. A guard conditioned on that
    object being alive therefore never ran on the ONE path that triggers it — changing
    the embedding model in Settings."""
    from visualweaver import redis_store
    fake_redis.set("visualweaver:semcache_model", "sentence-transformers/all-MiniLM-L6-v2")
    cfg["embedding"] = {"model": "intfloat/multilingual-e5-base"}
    monkeypatch.setattr(state, "_semantic_cache", object())
    redis_store.invalidate_redis_clients()
    assert state._semantic_cache is None
    assert cache._cache_model_changed() is True, "the guard is unreachable again"


def test_the_marker_survives_a_process_restart(fake_redis, cfg):
    """An in-process marker is empty after a restart, which is exactly the state a real
    deployment is in: index built under one model, config since changed, server
    restarted, stale vectors still indexed at the old dimension."""
    fake_redis.set("visualweaver:semcache_model", "sentence-transformers/all-MiniLM-L6-v2")
    cfg["embedding"] = {"model": "intfloat/multilingual-e5-base"}
    assert state._semantic_cache_model is None
    assert cache._cache_model_changed() is True


def test_the_discard_removes_the_documents_not_only_the_index(fake_redis, cfg):
    """redisvl's overwrite path issues DROPINDEX WITHOUT DD, so the hashes survive as
    orphans carrying their old-model vectors and reappear as soon as a matching index
    exists again."""
    fake_redis.keys_ = [b"semcache:a", b"semcache:b", b"semcache:c"]
    cache._discard_stale_cache()
    assert ("FT.DROPINDEX", "semcache", "DD") in fake_redis.commands
    assert {b"semcache:a", b"semcache:b", b"semcache:c"} <= set(fake_redis.deleted)
    assert b"visualweaver:semcache_model" in fake_redis.deleted, "a stale marker re-fires forever"


def test_the_discard_is_survivable(monkeypatch, cfg):
    """A cache is disposable; a chat turn is not."""
    from visualweaver import redis_store
    class _Broken:
        def execute_command(self, *a): raise RuntimeError("down")
        def scan_iter(self, **k): raise RuntimeError("down")
        def delete(self, *a): raise RuntimeError("down")
    monkeypatch.setattr(redis_store, "r", lambda *a, **k: _Broken())
    cache._discard_stale_cache()          # must not raise


# ── why a turn was not cached has to reach the user ──────────────────────────
def test_a_chart_request_is_no_longer_excluded_from_the_cache():
    """It used to be, and that one flag gated the lookup AND the store, so asking the
    identical question twice cost two model calls and left a permanent hole — a later
    plain-text rewording could not match either, because nothing had been written.
    Drawing requests are cached now, in their own partition, matched exactly."""
    assert cache.skip_reason(None, None, "Visualize the theorem of Pythagoras") is None
    assert cache.wants_visual("Visualize the theorem of Pythagoras") is True


@pytest.mark.parametrize("q", ["what is a symbolic link?", "are there other similar fractals ?",
                               "explain knowledge graphs in Redis"])
def test_an_ordinary_question_is_cacheable(q):
    assert cache.skip_reason(None, None, q) is None


def test_a_render_request_is_recognised_so_it_can_be_matched_exactly():
    """"Sugar molecule in 2d and 3d" asks to see something. It is still recognised — that
    is what routes it to the exact-match partition — it is simply no longer excluded."""
    assert cache.wants_visual("Sugar molecule in 2d and 3d") is True
    assert cache.skip_reason(None, None, "Sugar molecule in 2d and 3d") is None


def test_an_attachment_names_its_own_reason():
    assert "image" in cache.skip_reason(["data:..."], None, "what is this")
    assert "file" in cache.skip_reason(None, [{"name": "a.txt"}], "what is this")


def test_an_image_is_reported_before_a_chart_request():
    """Both are true for "chart this image"; the attachment is the more specific reason
    and the one the user can act on."""
    assert "image" in cache.skip_reason(["img"], None, "chart this")


def test_both_chat_paths_ask_the_same_question(monkeypatch):
    """They had the condition written out twice and could drift apart: one path would
    store a turn the other would have skipped."""
    import inspect
    from visualweaver import routes_chat
    src = inspect.getsource(routes_chat)
    assert src.count("cache.skip_reason(") == 2
    assert src.count("cache.skip_kind(") == 2
    # wants_visual IS called here now — it selects the exact-match partition rather than
    # excluding the turn — but only through the shared helper, never re-implemented.
    assert src.count("cache.wants_visual(") == 2


def test_the_lookup_path_discards_stale_vectors_before_using_them(monkeypatch, cfg):
    """Testing the predicate alone proved the rule was right while the code path never
    consulted it — which is exactly how the first version of this guard shipped dead."""
    from visualweaver import cache as C, redis_store
    r = _FakeRedis(model="sentence-transformers/all-MiniLM-L6-v2", keys=("semcache:a",))
    monkeypatch.setattr(redis_store, "r", lambda *a, **k: r)
    monkeypatch.setattr(state, "_semantic_cache", _FakeCache())
    monkeypatch.setattr(state, "_semantic_cache_model", None)
    cfg["embedding"] = {"model": "intfloat/multilingual-e5-base"}
    # the rebuild itself will fail (no real vectorizer here); the discard must precede it
    monkeypatch.setattr(C, "SemanticCache", None, raising=False)
    C._get_semantic_cache()
    assert ("FT.DROPINDEX", "semcache", "DD") in r.commands, "stale vectors were scored against"
    assert b"semcache:a" in r.deleted


# ── the upgrade itself is the case the guard exists for ──────────────────────
def test_a_cache_of_unknown_provenance_is_discarded_once(monkeypatch, cfg):
    """The marker is only written after a build, so no deployment that predates it has
    one — and the guard returned False for exactly the upgrade it was written for.

    Comparing vector widths is not enough either: two different models can share one.
    Swapping all-MiniLM-L6-v2 for multilingual-e5-small (both 384) collapsed every score
    to ~0.08, so the cache never hit again while the entry count kept reporting the
    entries, for the whole 9-day TTL. A cache is disposable and re-earned in a handful of
    queries; a silently dead one is not detectable from the outside.
    """
    from visualweaver import cache as C, redis_store
    monkeypatch.setattr(redis_store, "r", lambda *a, **k: _FakeRedis())   # no marker
    monkeypatch.setattr(state, "_semantic_cache_model", None)
    monkeypatch.setattr(C, "_cache_index_dim", lambda: 384)
    cfg["embedding"] = {"model": "all-MiniLM-L6-v2"}                      # also 384
    assert C._cache_model_changed() is True


def test_no_index_yet_is_not_a_change(monkeypatch, cfg):
    """A fresh install has nothing to discard, and must not log one."""
    from visualweaver import cache as C, redis_store
    monkeypatch.setattr(redis_store, "r", lambda *a, **k: _FakeRedis())
    monkeypatch.setattr(state, "_semantic_cache_model", None)
    monkeypatch.setattr(C, "_cache_index_dim", lambda: None)
    cfg["embedding"] = {"model": "intfloat/multilingual-e5-base"}
    assert C._cache_model_changed() is False


def test_the_question_is_only_asked_once(monkeypatch, cfg):
    """Once the marker is written, an unchanged model must not keep discarding."""
    from visualweaver import cache as C, redis_store
    r = _FakeRedis(model="sentence-transformers/all-MiniLM-L6-v2")
    monkeypatch.setattr(redis_store, "r", lambda *a, **k: r)
    monkeypatch.setattr(state, "_semantic_cache_model", None)
    monkeypatch.setattr(C, "_cache_index_dim", lambda: 384)
    cfg["embedding"] = {"model": "all-MiniLM-L6-v2"}
    assert C._cache_model_changed() is False


def test_an_explicit_marker_still_wins_over_the_width(monkeypatch, cfg):
    """Two models can share a width; the name is the stronger signal when present."""
    from visualweaver import cache as C, redis_store
    r = _FakeRedis(model="sentence-transformers/all-MiniLM-L6-v2")
    monkeypatch.setattr(redis_store, "r", lambda *a, **k: r)
    monkeypatch.setattr(state, "_semantic_cache_model", None)
    monkeypatch.setattr(C, "_cache_index_dim", lambda: 384)
    cfg["embedding"] = {"model": "BAAI/bge-small-en-v1.5"}            # also 384
    assert C._cache_model_changed() is True
