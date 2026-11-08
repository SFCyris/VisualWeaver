# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.redis_store — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import asyncio
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from . import mongo_store, rag, rag_admin, state

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# REDIS HELPERS — multi-endpoint aware
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_redis_client(cfg: dict) -> redis.Redis:
    """Build a redis.Redis client from a connection-config dict."""
    return redis.Redis(
        host=cfg.get("host") or "localhost",
        port=int(cfg.get("port") or 6379),    # or-default handles None (JSON null) safely
        db=int(cfg.get("db") or 0),
        password=cfg.get("password") or None,
        ssl=bool(cfg.get("ssl") or False),
        decode_responses=False,   # we handle bytes manually for embedding binary data
        socket_connect_timeout=5,  # fail fast if endpoint is unreachable
        socket_timeout=10,         # prevent blocking forever on slow endpoints
    )


def default_type() -> str:
    """What the built-in "default" connection is: "redis" (config.redis) or "mongodb"
    (config.mongodb), from ``storage.default_type`` (absent = "redis")."""
    st = state._config.get("storage")
    return "mongodb" if isinstance(st, dict) and st.get("default_type") == "mongodb" else "redis"


def default_cfg() -> dict:
    """The built-in default connection's stored config, with its ``type``."""
    if default_type() == "mongodb":
        return {**(state._config.get("mongodb") or {}), "type": "mongodb"}
    return {**(state._config.get("redis") or {}), "type": "redis"}


def default_label() -> str:
    return "Built-in MongoDB" if default_type() == "mongodb" else "Built-in Redis"


def get_redis(cfg: dict | None = None):
    """
    Return (and cache) the client for the built-in "default" connection: a redis.Redis,
    or a mongo_store.MongoEndpoint when ``storage.default_type`` is "mongodb".
    Pass cfg to temporarily use a different Redis connection config (e.g. for test).
    """
    if cfg is None and default_type() == "mongodb":
        client = mongo_store.MongoEndpoint("default", default_cfg())
    else:
        client = _build_redis_client(cfg or state._config.get("redis", {}))
    old = state._redis_clients.get("default")
    if mongo_store.is_mongo(old) and old is not client:
        retire_client(old)    # a MongoClient holds a pool and monitor threads
    state._redis_clients["default"] = client
    return client


def endpoint_cfg(endpoint_name: str) -> dict | None:
    """The stored config of a named connection (None for an unknown name). "default"
    is the built-in connection (config.redis or config.mongodb, see default_cfg)."""
    if endpoint_name == "default":
        return {**default_cfg(), "name": "default", "builtin": True}
    for ep in state._config.get("redis_endpoints", []) or []:
        if isinstance(ep, dict) and ep.get("name") == endpoint_name:
            return ep
    return None


def endpoint_type(endpoint_name: str) -> str:
    """"redis" or "mongodb"."""
    if endpoint_name == "default":
        return default_type()
    ep = endpoint_cfg(endpoint_name)
    return "mongodb" if ep and ep.get("type") == "mongodb" else "redis"


def r_for(endpoint_name: str = "default"):
    """
    Return the client for a named connection.
    "default" maps to the built-in connection (Redis or MongoDB, see get_redis).
    Other names are looked up in config["redis_endpoints"]; a MongoDB connection
    returns a mongo_store.MongoEndpoint, which every per-instance operation
    recognises with mongo_store.is_mongo().
    Clients are cached so we reuse connections.
    """
    if endpoint_name in state._redis_clients:
        return state._redis_clients[endpoint_name]

    if endpoint_name == "default":
        return get_redis()

    # Find the named endpoint in config
    for ep in state._config.get("redis_endpoints", []):
        if ep.get("name") == endpoint_name:
            client = (mongo_store.MongoEndpoint(endpoint_name, ep) if ep.get("type") == "mongodb"
                      else _build_redis_client(ep))
            state._redis_clients[endpoint_name] = client
            return client

    # Fallback to default if not found
    log.warning(f"Redis endpoint '{endpoint_name}' not found — falling back to default")
    return r()


def r():
    """Shorthand: the client of the built-in "default" connection, creating it if
    needed. A MongoEndpoint when the built-in store is MongoDB — app data goes through
    appstore.current(), RAG paths dispatch on mongo_store.is_mongo()."""
    if "default" not in state._redis_clients:
        return get_redis()
    return state._redis_clients["default"]


async def _reset_index_markers_for_embedding_change():
    """Drop every index schema marker so all indexes rebuild at the new dimension.

    Shared by the Settings save guard and by config import — a model change through
    either route leaves every stored vector at the old dimension, which makes
    retrieval return nothing at all rather than failing loudly.
    """
    for inst in await asyncio.to_thread(rag_admin.list_rag_instances):
        name = inst.get("name", "")
        if not name:
            continue
        try:
            rc_i = rag_admin.rc_for_instance(name)
            if mongo_store.is_mongo(rc_i):
                mongo_store.forget_indexes(rc_i.name)   # retargets the vector index on next use
            else:
                await asyncio.to_thread(rc_i.delete, f"rag:{name}:schema_ver")
            rag._index_ensured.discard(name)
        except Exception as e:
            log.warning(f"  could not reset index marker for '{name}': {e}")
    rag._dim_mismatch_warned.clear()   # allow the diagnostic to fire again if needed


def retire_client(c, delay: float = 600.0) -> None:
    """Stop handing out a MongoDB client without cutting off work still holding it.

    A crawl, watched-folder scan, export or import keeps the client it started with;
    closing it on the spot (any settings save rebuilds the cache) failed those jobs
    mid-way with "Cannot use MongoClient after close". The client is closed later.
    """
    import threading
    t = threading.Timer(delay, c.close)
    t.daemon = True
    t.start()


def invalidate_redis_clients():
    """
    Clear the client cache so they're rebuilt on next use.
    Also resets the SemanticCache so it reconnects with the new client.
    Called after config changes that affect Redis connections.
    """
    for c in list(state._redis_clients.values()):
        if mongo_store.is_mongo(c):
            retire_client(c)
    state._redis_clients.clear()
    mongo_store.forget_indexes()
    state._semantic_cache = None


_probe_errors: dict[str, str] = {}     # last health-probe failure per connection


def _ping_once(rc) -> None:
    """PING once, without the client's retries. A redis.Redis client retries a
    refused connection three times with backoff (~5.7 s per call), so a health
    probe of a stopped server took seconds to say "down"; this one connection
    answers in milliseconds. MongoDB connections keep their own ping."""
    if not isinstance(rc, redis.Redis):
        rc.ping()
        return
    pool = rc.connection_pool
    conn = pool.connection_class(**{**pool.connection_kwargs, "retry": Retry(NoBackoff(), 0)})
    try:
        conn.connect()
        conn.send_command("PING")
        conn.read_response()
    finally:
        conn.disconnect()


def _probe_endpoint(ep_name: str) -> bool:
    """
    Return True if the named Redis endpoint responds to PING within the
    socket_connect_timeout.  Updates _endpoint_health in place.
    Never raises.
    """
    try:
        rc = r_for(ep_name)
        _ping_once(rc)
        state._endpoint_health[ep_name] = True
        _probe_errors.pop(ep_name, None)
        return True
    except Exception as e:
        state._endpoint_health[ep_name] = False
        _probe_errors[ep_name] = str(e)[:300]
        log.warning(f"Storage connection '{ep_name}' is unreachable — RAG instances on it will be marked offline")
        return False


def refresh_endpoint_health() -> dict[str, bool]:
    """
    Probe all configured Redis endpoints (default + extras) and return the
    updated health dict.  Called at startup and periodically in background.
    """
    _probe_endpoint("default")
    for ep in state._config.get("redis_endpoints", []):
        ep_name = ep.get("name", "")
        if ep_name and ep_name != "default":
            _probe_endpoint(ep_name)
    return dict(state._endpoint_health)

