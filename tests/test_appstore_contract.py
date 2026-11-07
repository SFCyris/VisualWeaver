# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.appstore: the Redis and MongoDB app stores give the same answers.

Every case runs on both: the suite's private Redis (keys under KEY_PREFIX) and the
throwaway MongoDB from conftest.mongo_uri (a database of its own, dropped afterwards).
"""
import os
import time

import pytest

from conftest import KEY_PREFIX


@pytest.fixture(params=["redis", "mongodb"])
def store(request):
    from visualweaver import appstore, mongo_store
    if request.param == "redis":
        rc = request.getfixturevalue("clean_redis")
        yield appstore.RedisAppStore("contract", rc)
        return
    uri = request.getfixturevalue("mongo_uri")
    db = f"vwtest_app_{os.getpid()}_{int(time.time() * 1000) % 10**8}"
    ep = mongo_store.MongoEndpoint("contract", {"uri": uri, "database": db})
    try:
        yield appstore.MongoAppStore("contract", ep)
    finally:
        ep.client.drop_database(db)
        ep.close()


def k(s: str) -> str:
    return f"{KEY_PREFIX}{s}"


def test_plain_keys_round_trip(store):
    assert store.ping() is True
    assert store.get(k("a")) is None
    store.set(k("a"), "1")
    store.set(k("b"), "ünïcode ✓")
    assert store.get(k("a")) == "1"
    assert store.mget([k("a"), k("missing"), k("b")]) == ["1", None, "ünïcode ✓"]
    assert store.mget([]) == []
    store.set(k("a"), "2")                                     # overwrite
    assert store.get(k("a")) == "2"
    assert store.delete(k("a"), k("missing")) == 1
    assert store.get(k("a")) is None
    assert store.delete() == 0


def test_ttl_and_expiry(store):
    store.set(k("t"), "x", ttl=100)
    store.set(k("p"), "y")
    t = store.ttl(k("t"))
    assert t is not None and 95 <= t <= 100, t
    assert store.ttl(k("p")) is None and store.ttl(k("missing")) is None
    # ttls: None = no expiry; 0 = gone (expired or deleted since it was read).
    assert [v if v in (None, 0) else 95 <= v <= 100 for v in store.ttls([k("t"), k("p"), k("missing")])] \
        == [True, None, 0]
    assert store.ttls([]) == []
    store.set(k("gone"), "z", ttl=1)
    time.sleep(2.1)
    # MongoDB's TTL monitor runs once a minute; reads must still never return it.
    assert store.get(k("gone")) is None
    assert store.mget([k("gone"), k("p")]) == [None, "y"]
    assert k("gone") not in store.keys(k(""))


def test_set_many(store):
    store.set_many([(k("m1"), "one", None), (k("m2"), "two", 500)])
    assert store.mget([k("m1"), k("m2")]) == ["one", "two"]
    assert store.ttl(k("m1")) is None
    assert 490 <= store.ttl(k("m2")) <= 500
    store.set_many([])


def test_keys_take_the_prefix_literally(store):
    """Glob characters in a prefix match only themselves (Redis SCAN MATCH would
    otherwise read * ? [ ] as patterns)."""
    weird = k("g*[x]?\\")
    for suffix in ("1", "2"):
        store.set(weird + suffix, suffix)
    store.set(k("gZZ[x]?\\3"), "decoy")        # matches g*[x]?\\ only as a glob
    store.hset(weird + "h", {"f": "v"})        # a hash under the prefix is a key too
    assert sorted(store.keys(weird)) == sorted([weird + "1", weird + "2", weird + "h"])


def test_hashes(store):
    h = k("hash")
    assert store.hgetall(h) == {}
    store.hset(h, {"a": "1", "b": 2, "c\x00d": "nul"})
    assert store.hgetall(h) == {"a": "1", "b": "2", "c\x00d": "nul"}
    store.hset(h, {"a": "one"})
    assert store.hgetall(h)["a"] == "one"
    assert store.hdel(h, "a", "missing") == 1
    assert store.hdel(h) == 0
    assert set(store.hgetall(h)) == {"b", "c\x00d"}
    assert store.delete(h) == 1
    assert store.hgetall(h) == {}


def test_hincrby_counts(store):
    h = k("counts")
    store.hincrby(h, {"x:in": 5, "x:out": 2})
    store.hincrby(h, {"x:in": 10})
    store.hincrby(h, {})
    assert store.hgetall(h) == {"x:in": "15", "x:out": "2"}
