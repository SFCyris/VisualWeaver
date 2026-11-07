# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.appstore — where the app keeps its own data: Redis or MongoDB.

The app's own data — conversations, the usage tally, retrieval stats, watched-folder
signatures, small markers — goes through this module instead of a bare Redis client,
so it can live on the built-in Redis, the built-in MongoDB, or any storage connection
with vector search. ``storage.app`` in the config names the connection (default
"default"). By the storage contract (internal/PLAN-install-storage-choice.md §10) the
answer cache lives on the same connection; that part is cache.py's.

The interface is the small subset of Redis the app uses, with str values:

  get / set(ttl) / mget / delete / keys(prefix) / ttl        plain keys
  ttls(keys) / set_many([(key, value, ttl)])                 batched, for copying stores
                                                             (ttls: 0 for a key that is gone)
  hgetall / hset / hdel / hincrby                            hashes (field → str/int)

On MongoDB, in the connection's database:

  vw_kv    {_id: key, v: str, exp: date|None}                TTL index on exp
  vw_hash  {_id: "<key>\\x00<field>", k: key, f: field, v}   index on k

TTL deletion on MongoDB runs about once a minute, so reads also check ``exp`` — an
expired conversation is never returned, as on Redis.

Every write of app data runs inside ``gate.write()``; moving the app data to another
connection holds ``gate.move()``, which waits for writes in flight and holds new ones
until the move is done — so nothing is written to the old store after it was copied.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
import threading
import time
from contextlib import contextmanager

from . import state

log = logging.getLogger(__name__)

KV, HASH = "vw_kv", "vw_hash"


def _s(v) -> str | None:
    if v is None:
        return None
    return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)


class RedisAppStore:
    kind = "redis"

    def __init__(self, name: str, client):
        self.name, self.raw = name, client

    def ping(self) -> bool:
        return bool(self.raw.ping())

    def get(self, key: str) -> str | None:
        return _s(self.raw.get(key))

    def mget(self, keys: list[str]) -> list[str | None]:
        if not keys:
            return []
        # Pipelined GETs, not MGET: a sharded Redis (Enterprise, cluster) refuses an MGET
        # whose keys live in different slots.
        pipe = self.raw.pipeline(transaction=False)
        for k in keys:
            pipe.get(k)
        return [_s(v) for v in pipe.execute()]

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        if ttl:
            self.raw.setex(key, int(ttl), value)
        else:
            self.raw.set(key, value)

    def delete(self, *keys: str) -> int:
        return int(self.raw.delete(*keys)) if keys else 0

    def keys(self, prefix: str) -> list[str]:
        return [_s(k) for k in self.raw.scan_iter(f"{_glob_escape(prefix)}*", count=500)]

    def ttl(self, key: str) -> int | None:
        t = self.raw.ttl(key)
        return int(t) if t is not None and t >= 0 else None

    def ttls(self, keys: list[str]) -> list[int | None]:
        """ttl() of each key in one round trip."""
        if not keys:
            return []
        pipe = self.raw.pipeline(transaction=False)
        for k in keys:
            pipe.ttl(k)
        # -1: no expiry → None; -2: gone (expired since it was read) → 0.
        return [None if t is None or t == -1 else max(0, int(t)) for t in pipe.execute()]

    def set_many(self, items: list[tuple[str, str, int | None]]) -> None:
        """set() of each (key, value, ttl) in one round trip."""
        if not items:
            return
        pipe = self.raw.pipeline(transaction=False)
        for k, v, ttl in items:
            if ttl:
                pipe.setex(k, int(ttl), v)
            else:
                pipe.set(k, v)
        pipe.execute()

    def hgetall(self, key: str) -> dict[str, str]:
        return {_s(k): _s(v) for k, v in self.raw.hgetall(key).items()}

    def hset(self, key: str, mapping: dict) -> None:
        if mapping:
            self.raw.hset(key, mapping={k: str(v) for k, v in mapping.items()})

    def hdel(self, key: str, *fields: str) -> int:
        return int(self.raw.hdel(key, *fields)) if fields else 0

    def hincrby(self, key: str, mapping: dict) -> None:
        """Add integer amounts to several fields in one round trip."""
        if not mapping:
            return
        pipe = self.raw.pipeline(transaction=False)
        for f, n in mapping.items():
            pipe.hincrby(key, f, int(n))
        pipe.execute()


def _glob_escape(s: str) -> str:
    return re.sub(r"([*?\[\]\\])", r"\\\1", s)


class MongoAppStore:
    kind = "mongodb"

    def __init__(self, name: str, endpoint):
        self.name, self.raw = name, endpoint
        self._kv, self._h = endpoint.col(KV), endpoint.col(HASH)
        _ensure_indexes(endpoint)

    def ping(self) -> bool:
        return bool(self.raw.ping())

    @staticmethod
    def _live(now=None) -> dict:
        now = now or _dt.datetime.now(_dt.timezone.utc)
        return {"$or": [{"exp": None}, {"exp": {"$gt": now}}]}

    def get(self, key: str) -> str | None:
        d = self._kv.find_one({"_id": key, **self._live()}, {"v": 1})
        return d.get("v") if d else None

    def mget(self, keys: list[str]) -> list[str | None]:
        if not keys:
            return []
        found = {d["_id"]: d.get("v") for d in self._kv.find({"_id": {"$in": list(keys)}, **self._live()},
                                                             {"v": 1})}
        return [found.get(k) for k in keys]

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        exp = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=int(ttl))) if ttl else None
        self._kv.replace_one({"_id": key}, {"_id": key, "v": value, "exp": exp}, upsert=True)

    def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        n = self._kv.delete_many({"_id": {"$in": list(keys)}}).deleted_count
        hk = set(self._h.distinct("k", {"k": {"$in": list(keys)}}))
        if hk:
            self._h.delete_many({"k": {"$in": list(hk)}})
        return n + len(hk)

    def keys(self, prefix: str) -> list[str]:
        q = {"_id": {"$regex": "^" + re.escape(prefix)}, **self._live()}
        out = [d["_id"] for d in self._kv.find(q, {"_id": 1})]
        out += [k for k in self._h.distinct("k", {"k": {"$regex": "^" + re.escape(prefix)}})]
        return out

    def ttl(self, key: str) -> int | None:
        d = self._kv.find_one({"_id": key}, {"exp": 1})
        if not d or not d.get("exp"):
            return None
        exp = d["exp"]
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=_dt.timezone.utc)
        return max(0, int((exp - _dt.datetime.now(_dt.timezone.utc)).total_seconds()))

    def ttls(self, keys: list[str]) -> list[int | None]:
        if not keys:
            return []
        now = _dt.datetime.now(_dt.timezone.utc)
        exps = {d["_id"]: d.get("exp") for d in self._kv.find({"_id": {"$in": list(keys)}}, {"exp": 1})}
        out = []
        for k in keys:
            if k not in exps:
                out.append(0)                  # gone since it was read
                continue
            exp = exps[k]
            if exp is None:
                out.append(None)
                continue
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=_dt.timezone.utc)
            out.append(max(0, int((exp - now).total_seconds())))
        return out

    def set_many(self, items: list[tuple[str, str, int | None]]) -> None:
        if not items:
            return
        from pymongo import ReplaceOne
        now = _dt.datetime.now(_dt.timezone.utc)
        self._kv.bulk_write([ReplaceOne({"_id": k}, {"_id": k, "v": v, "exp": (now + _dt.timedelta(
            seconds=int(ttl))) if ttl else None}, upsert=True) for k, v, ttl in items], ordered=False)

    def hgetall(self, key: str) -> dict[str, str]:
        return {d["f"]: _s(d.get("v")) for d in self._h.find({"k": key}, {"f": 1, "v": 1})}

    def hset(self, key: str, mapping: dict) -> None:
        if not mapping:
            return
        from pymongo import ReplaceOne
        self._h.bulk_write([ReplaceOne({"_id": f"{key}\x00{f}"}, {"_id": f"{key}\x00{f}", "k": key, "f": f,
                                                                  "v": str(v)}, upsert=True)
                            for f, v in mapping.items()], ordered=False)

    def hdel(self, key: str, *fields: str) -> int:
        if not fields:
            return 0
        return self._h.delete_many({"_id": {"$in": [f"{key}\x00{f}" for f in fields]}}).deleted_count

    def hincrby(self, key: str, mapping: dict) -> None:
        if not mapping:
            return
        from pymongo import UpdateOne
        # Counters are stored as numbers so $inc is atomic; hgetall returns them as str.
        self._h.bulk_write([UpdateOne({"_id": f"{key}\x00{f}"},
                                      {"$inc": {"v": int(n)}, "$setOnInsert": {"k": key, "f": f}},
                                      upsert=True) for f, n in mapping.items()], ordered=False)


_ensured: set = set()
_ensure_failed: dict = {}            # key → time before which no new attempt is made
_ENSURE_RETRY = 30.0


def _ensure_indexes(endpoint) -> None:
    """Create the TTL and hash-key indexes once per server. Idempotent and lock-free:
    a server that cannot be reached must not make every caller queue behind the
    first one's timeout, so a failure is remembered for _ENSURE_RETRY seconds and the
    operation itself then fails (or succeeds) on its own."""
    # The server too: "default" can be re-pointed (a settings save, a test) at another
    # MongoDB under the same connection name and database.
    key = (endpoint.name, (endpoint.cfg or {}).get("uri", ""), endpoint.db.name)
    if key in _ensured or _ensure_failed.get(key, 0) > time.time():
        return
    try:
        endpoint.col(KV).create_index("exp", expireAfterSeconds=0)
        endpoint.col(HASH).create_index("k")
        _ensured.add(key)
        _ensure_failed.pop(key, None)
    except Exception as e:
        _ensure_failed[key] = time.time() + _ENSURE_RETRY
        log.warning(f"App store '{endpoint.name}': could not create its indexes: {str(e)[:200]}")


# ── moving the app data ──────────────────────────────────────────────────────

class _MoveGate:
    """Shared by writes of app data, held alone while the app data moves."""

    def __init__(self):
        self._cond = threading.Condition()
        self._writers = 0
        self._moving = False

    @contextmanager
    def write(self):
        with self._cond:
            while self._moving:
                self._cond.wait()
            self._writers += 1
        try:
            yield
        finally:
            with self._cond:
                self._writers -= 1
                if not self._writers:
                    self._cond.notify_all()

    @contextmanager
    def move(self):
        with self._cond:
            while self._moving:
                self._cond.wait()
            self._moving = True                # new writes wait from here
            while self._writers:
                self._cond.wait()
        try:
            yield
        finally:
            with self._cond:
                self._moving = False
                self._cond.notify_all()


gate = _MoveGate()


# ── which store ──────────────────────────────────────────────────────────────

def app_connection() -> str:
    """The connection that holds the app's data and answer cache."""
    return ((state._config.get("storage") or {}).get("app") or "default").strip() or "default"


def connection_target(name: str | None = None) -> tuple[str, dict]:
    """(kind, cfg) for a connection — the app-data connection when name is None.

    kind is "redis" or "mongodb"; cfg is a copy of the effective connection config,
    credentials included: for "default" the built-in store per storage.default_type
    (config.redis or config.mongodb), otherwise the redis_endpoints entry. An unknown
    name resolves to "default", as redis_store.r_for does. The single place that
    answers "what is this connection" — callers must not read storage.default_type or
    the connection sections themselves."""
    from . import redis_store
    name = app_connection() if name is None else (name or "default")
    cfg = redis_store.endpoint_cfg(name)
    if cfg is None:
        log.warning(f"Storage connection '{name}' not found — using the built-in connection")
        cfg = redis_store.endpoint_cfg("default")
    kind = "mongodb" if cfg.get("type") == "mongodb" else "redis"
    return kind, {**cfg, "type": kind}


def for_connection(name: str):
    """An AppStore over a connection's cached client (redis_store.r_for; "default"
    through redis_store.r(), the built-in connection's client)."""
    from . import mongo_store, redis_store
    client = redis_store.r() if name == "default" else redis_store.r_for(name)
    return MongoAppStore(name, client) if mongo_store.is_mongo(client) else RedisAppStore(name, client)


def current():
    """The AppStore for ``storage.app``. Cheap: wraps the cached client each call, so a
    settings save (which rebuilds clients) is picked up without extra invalidation."""
    return for_connection(app_connection())
