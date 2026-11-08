# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.routes_storage — Settings → Storage: the storage connections and the
one that holds the app's own data.

  GET  /api/storage       every connection: type, built-in or added, status, instances
  POST /api/storage/app   move the app data (and the answer cache) to another connection
  GET  /api/components    VisualWeaver, Python, each connection's server and search
                          versions, the storage client libraries

Moving copies conversations (with their remaining lifetime), the usage tally, the
retrieval stats and the watched-folder signatures; the old store keeps its copy. The
answer cache starts empty on the new store. Writes of app data wait while the move
runs (appstore.gate), so none lands on the old store after it was copied.
"""
import asyncio
import hashlib
import importlib.metadata
import logging
import platform
import threading
import time

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from fastapi import HTTPException

from . import (appcore, appstore, cache, config, constants, local_mongo, mongo_store, rag_admin, redis_store,
               projects, routes_settings, sessions, state, ws)

log = logging.getLogger(__name__)

_BUILTIN_LABELS = {"redis": "Built-in Redis", "mongodb": "Built-in MongoDB"}
_COPY_BATCH = 500
_switch_lock = threading.Lock()


def _storage_connections() -> list[tuple[str, dict]]:
    """(name, config) for every connection: the built-in default, the other built-in
    ones, then the added ones (each group in config order)."""
    rest = [(ep["name"], ep) for ep in state._config.get("redis_endpoints") or []
            if isinstance(ep, dict) and ep.get("name") and ep["name"] != "default"]
    return ([("default", redis_store.endpoint_cfg("default"))]
            + [c for c in rest if c[1].get("builtin")] + [c for c in rest if not c[1].get("builtin")])


def _storage_label(name: str, cfg: dict) -> str:
    builtin = name == "default" or bool(cfg.get("builtin"))
    return _BUILTIN_LABELS.get(cfg.get("type") or "redis", name) if builtin else name


def _storage_host(cfg: dict) -> str:
    if cfg.get("type") == "mongodb":
        return mongo_store.display_host(cfg.get("uri", ""))
    return f"{cfg.get('host') or 'localhost'}:{int(cfg.get('port') or 6379)}"


def _storage_probe(name: str, cfg: dict) -> tuple[bool, bool | None, str]:
    """Connect now: (ok, vector_search, error). Updates the health caches; never raises.
    vector_search is None when the server could not be reached."""
    try:
        rc = redis_store.r_for(name)
    except Exception as e:
        state._endpoint_health[name] = False
        return False, None, str(e)[:300]
    if mongo_store.is_mongo(rc):
        p = mongo_store.probe(cfg, ep=rc)
        state._endpoint_health[name] = bool(p["ok"])
        # Unreachable says nothing about search: unknown, so the next check probes again.
        vs = bool(p.get("vector_search")) if p["ok"] else None
        state._search_available[name] = vs
        return bool(p["ok"]), vs, p.get("error") or ""
    try:
        redis_store._ping_once(rc)     # a dead server answers now, not after the retries
    except Exception as e:
        state._endpoint_health[name] = False
        return False, None, str(e)[:300]
    state._endpoint_health[name] = True
    return True, routes_settings.probe_search(rc, name), ""


def _storage_status(name: str, cfg: dict) -> tuple[bool | None, bool | None]:
    """(ok, vector_search) from the health caches, probing only what is not known yet."""
    ok = state._endpoint_health.get(name)
    vs = state._search_available.get(name)
    if ok is None or (ok and vs is None):
        ok, vs, _ = _storage_probe(name, cfg)
    return ok, (vs if ok else None)


# ── components: what each connection runs ───────────────────────────────────

_COMPONENT_TTL = 60.0        # seconds a connection's versions are reused
_RUNTIME_TTL = 300.0         # local_mongo.runtime() shells out to the container CLI
_PROBE_TIMEOUT_MS = 2500
_component_cache: dict[tuple, tuple[float, dict]] = {}
_runtime_cache: dict = {}
_LIBRARIES = (("redis-py", "redis"), ("redisvl", "redisvl"), ("pymongo", "pymongo"))


def _module_version(ver) -> str | None:
    """A Redis module's integer version (80800) as text ("8.8.0")."""
    try:
        v = int(ver)
    except (TypeError, ValueError):
        return None
    return f"{v // 10000}.{v // 100 % 100}.{v % 100}"


def _redis_component(cfg: dict, out: dict) -> None:
    rc = redis.Redis(host=cfg.get("host") or "localhost", port=int(cfg.get("port") or 6379),
                     db=int(cfg.get("db") or 0), password=cfg.get("password") or None,
                     ssl=bool(cfg.get("ssl") or False), socket_connect_timeout=_PROBE_TIMEOUT_MS / 1000,
                     socket_timeout=_PROBE_TIMEOUT_MS / 1000 + 1,
                     retry=Retry(NoBackoff(), 0))   # one attempt: a probe, not a workload
    try:
        out["version"] = rc.info("server").get("redis_version")
        out["ok"] = True
        search = None
        try:
            for m in rc.module_list() or []:
                m = {(k.decode() if isinstance(k, bytes) else k): v for k, v in m.items()}
                name = m.get("name")
                name = (name.decode() if isinstance(name, bytes) else str(name or "")).lower()
                if name in ("search", "ft"):
                    ver = _module_version(m.get("ver"))
                    search = f"Redis Query Engine {ver}" if ver else "Redis Query Engine"
        except Exception:
            pass                     # MODULE LIST can be denied (managed Redis, ACLs)
        if search is None:
            try:
                rc.execute_command("FT._LIST")
                search = "Redis Query Engine"
            except Exception:
                search = "not available"
        out["search"] = search
    finally:
        rc.close()


def _mongo_component(cfg: dict, out: dict) -> None:
    from pymongo import MongoClient
    from pymongo.errors import OperationFailure
    uri = cfg.get("uri") or "mongodb://localhost:27017"
    atlas = "mongodb.net" in uri
    builtin = bool(cfg.get("builtin")) and not atlas
    out["product"] = "MongoDB Atlas" if atlas else ("MongoDB Community" if builtin else "MongoDB")
    kw = {**mongo_store._client_kwargs(cfg), "serverSelectionTimeoutMS": _PROBE_TIMEOUT_MS,
          "connectTimeoutMS": _PROBE_TIMEOUT_MS, "socketTimeoutMS": _PROBE_TIMEOUT_MS * 2}
    client = MongoClient(uri, **kw)
    try:
        out["version"] = client.server_info().get("version")
        out["ok"] = True
        db = client[(cfg.get("database") or mongo_store.DEFAULT_DB).strip() or mongo_store.DEFAULT_DB]
        try:
            list(db[mongo_store.CHUNKS].list_search_indexes())
            has = True
        except OperationFailure:
            has = False
        if not has:
            out["search"] = "not available"
        elif atlas:
            out["search"] = "Atlas Vector Search"
        elif builtin and out["version"] == local_mongo.SERVER_VERSION:
            # The search process has no version query; the built-in MongoDB's server
            # version identifies the image pair it was installed with.
            out["search"] = f"MongoDB Search {local_mongo.SEARCH_VERSION}"
        elif builtin:
            out["search"] = "MongoDB Search"
        else:
            out["search"] = "available"
    except Exception as e:
        out["ok"] = False
        out["error"] = mongo_store.friendly_error(e, uri)
    finally:
        client.close()


def _component_key(name: str, cfg: dict) -> tuple:
    # Everything that decides what is reached; the password only as a digest, so a
    # corrected password is probed afresh.
    pw = hashlib.sha256((cfg.get("password") or "").encode()).hexdigest()[:16]
    return (name, cfg.get("type") or "redis", cfg.get("host"), cfg.get("port"), cfg.get("db"),
            cfg.get("uri"), cfg.get("database"), cfg.get("username"), pw)


def component_info(name: str, cfg: dict, max_age: float = _COMPONENT_TTL) -> dict:
    """One connection's entry for /api/components: server product and version, search
    capability and version. Short timeouts; reused for max_age seconds. Never raises."""
    key = _component_key(name, cfg)
    hit = _component_cache.get(key)
    if hit and time.time() - hit[0] < max_age:
        return dict(hit[1])
    typ = cfg.get("type") or "redis"
    out = {"connection": name, "label": _storage_label(name, cfg), "type": typ,
           "builtin": name == "default" or bool(cfg.get("builtin")), "ok": False,
           "product": "Redis" if typ == "redis" else "MongoDB", "version": None, "search": None}
    try:
        (_mongo_component if typ == "mongodb" else _redis_component)(cfg, out)
    except Exception as e:
        out["ok"] = False
        out["error"] = str(e)[:300]
    if not out["ok"]:
        out["version"] = out["search"] = None
        out.setdefault("error", "Could not connect")
    _component_cache[key] = (time.time(), out)
    return dict(out)


def libraries() -> list[dict]:
    out = []
    for label, dist in _LIBRARIES:
        try:
            v = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            v = None
        out.append({"name": label, "version": v})
    return out


def _container_runtime() -> str | None:
    """The container runtime the built-in MongoDB runs on ("docker (colima)"), cached."""
    hit = _runtime_cache.get("v")
    if hit and time.time() - hit[0] < _RUNTIME_TTL:
        return hit[1]
    try:
        cmd, desc = local_mongo.runtime()
        val = desc if cmd else None
    except Exception:
        val = None
    _runtime_cache["v"] = (time.time(), val)
    return val


def _has_builtin_mongodb() -> bool:
    return any((c.get("type") == "mongodb" and (n == "default" or c.get("builtin"))
                and "mongodb.net" not in (c.get("uri") or "")) for n, c in _storage_connections())


@appcore.app.get("/api/components")
async def api_components():
    """What this installation runs: VisualWeaver and Python, each storage connection's
    server and search versions (probed in parallel, reused for a minute), the storage
    client libraries, and the container runtime when a built-in MongoDB exists."""
    conns = _storage_connections()
    jobs = [asyncio.to_thread(component_info, n, c) for n, c in conns]
    builtin_mongo = _has_builtin_mongodb()
    if builtin_mongo:
        jobs.append(asyncio.to_thread(_container_runtime))
    res = await asyncio.gather(*jobs)
    out = {"app": {"name": "VisualWeaver", "version": constants.__version__,
                   "python": platform.python_version()},
           "storage": list(res[:len(conns)]),
           "libraries": libraries()}
    if builtin_mongo:
        out["runtime"] = {"containers": res[-1]}
    return out


def component_log_lines() -> list[str]:
    """The startup log's storage lines, one per connection (built-in first)."""
    from concurrent.futures import ThreadPoolExecutor
    conns = _storage_connections()
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(conns)))) as pool:
        comps = list(pool.map(lambda nc: component_info(nc[0], nc[1], max_age=0), conns))
    lines = []
    for (name, _cfg), c in zip(conns, comps):
        head = f"'{name}'" + (f" ({c['label']})" if c["label"] != name else "")
        if c["ok"]:
            lines.append(f"{head}: {c['product']} {c['version'] or '?'} · search: {c['search']}")
        else:
            lines.append(f"{head}: {c['product']} — unreachable ({c.get('error')})")
    return lines


@appcore.app.get("/api/storage")
async def api_storage():
    """The storage connections and which one holds the app data. ``ok`` /
    ``vector_search`` come from the last health check (probed when unknown);
    ``instances`` counts the RAG instances each connection holds."""
    conns = _storage_connections()
    status, comps = await asyncio.gather(
        asyncio.gather(*(asyncio.to_thread(_storage_status, n, c) for n, c in conns)),
        asyncio.gather(*(asyncio.to_thread(component_info, n, c) for n, c in conns)))
    counts: dict[str, int] = {}
    try:
        for inst in await asyncio.to_thread(rag_admin.list_rag_instances):
            ep = inst.get("redis_endpoint") or "default"
            counts[ep] = counts.get(ep, 0) + 1
    except Exception as e:                                        # pragma: no cover
        log.warning(f"Storage: could not count instances: {e}")
    app = appstore.app_connection()
    out = []
    for (name, cfg), (ok, vs), comp in zip(conns, status, comps):
        out.append({
            "name":          name,
            "type":          cfg.get("type") or "redis",
            "builtin":       name == "default" or bool(cfg.get("builtin")),
            "label":         _storage_label(name, cfg),
            "host":          _storage_host(cfg),
            "version":       comp.get("version"),
            "ok":            ok,
            "vector_search": vs,
            "is_app":        name == app,
            "instances":     counts.get(name, 0),
        })
    return {"app": app, "default_type": redis_store.default_type(), "connections": out}


def copy_app_data(src, dst) -> dict:
    """Copy the app data from one AppStore to another.

    Conversations: dst ends up with exactly src's, each keeping its remaining lifetime
    (one expiring this second is skipped). Conversations only on dst are copies left by
    an earlier move — removed, or a conversation deleted since would come back. The
    usage tally, retrieval stats and watched-folder signatures become exactly src's — a
    tally reset before the move stays reset. Batched: a round trip per
    _COPY_BATCH conversations, not per key. Returns the counts POST /api/storage/app
    reports."""
    keys = src.keys(sessions._SESSION_PREFIX)
    n_conv = 0
    for i in range(0, len(keys), _COPY_BATCH):
        batch = keys[i:i + _COPY_BATCH]
        items = [(k, v, t) for k, v, t in zip(batch, src.mget(batch), src.ttls(batch))
                 if v is not None and (t is None or t > 0)]
        try:
            dst.set_many(items)
            n_conv += len(items)
        except Exception as e:
            # One item the target refuses (a conversation over MongoDB's 16 MB document
            # limit) must not stop the others: retry the batch one by one.
            log.warning(f"Storage: batch write failed ({str(e)[:200]}); copying one by one")
            for k, v, t in items:
                try:
                    dst.set(k, v, t)
                    n_conv += 1
                except Exception as e1:
                    log.warning(f"Storage: conversation {k} not copied: {str(e1)[:200]}")

    gone = sorted(set(dst.keys(sessions._SESSION_PREFIX)) - set(keys))
    for i in range(0, len(gone), _COPY_BATCH):
        dst.delete(*gone[i:i + _COPY_BATCH])
    # The conversation registry there (from an earlier stay) no longer matches: drop
    # it, and the first listing indexes the copied conversations (sessions.ensure_index).
    dst.delete(sessions._INDEX_KEY, sessions._INDEX_META_KEY)

    usage = src.hgetall(sessions._USAGE_KEY)
    dst.delete(sessions._USAGE_KEY)
    if usage:
        # hincrby, not hset: on MongoDB the counters must stay numbers for later $inc.
        dst.hincrby(sessions._USAGE_KEY, {f: int(v) for f, v in usage.items()})

    stats = src.get(rag_admin._RAG_STATS_KEY)
    if stats:
        dst.set(rag_admin._RAG_STATS_KEY, stats)
    else:
        dst.delete(rag_admin._RAG_STATS_KEY)

    watch = src.hgetall(ws._WATCH_SEEN_KEY)
    dst.delete(ws._WATCH_SEEN_KEY)
    if watch:
        dst.hset(ws._WATCH_SEEN_KEY, watch)

    # Projects become exactly src's, like the conversations filed under them.
    proj = src.hgetall(projects._KEY)
    dst.delete(projects._KEY)
    if proj:
        dst.hset(projects._KEY, proj)

    return {"conversations": n_conv, "usage": bool(usage), "stats": bool(stats), "watch": len(watch)}


def _switch_app_store(name: str, copy: bool = True) -> dict | None:
    with _switch_lock:
        old = appstore.app_connection()
        if old == name:
            return None
        rag_admin.save_rag_stats()       # the counters still in memory go along
        with appstore.gate.move():       # app-data writes wait until the switch is done
            copied = None
            if copy:
                try:
                    copied = copy_app_data(appstore.for_connection(old), appstore.for_connection(name))
                except Exception as e:
                    log.warning(f"Storage: copying the app data from '{old}' to '{name}' failed: {e}")
                    raise HTTPException(400, f"Could not copy the app data from '{old}' to '{name}' "
                                             f"({str(e)[:200]}). Nothing was switched. To switch "
                                             "without copying, send \"copy\": false.")
            # The answer cache starts empty there: entries left from an earlier stay on
            # that store would otherwise be served again.
            try:
                cache.clear_cache_entries(appstore.for_connection(name))
            except Exception as e:
                log.warning(f"Storage: could not empty the answer cache on '{name}': {e}")
            storage = state._config.get("storage")
            storage = {**(storage if isinstance(storage, dict) else {}), "app": name}
            # Written before it takes effect: a failed save leaves the app where it was.
            config.save_config({**state._config, "storage": storage})
            state._config["storage"] = storage
            state._semantic_cache = None     # the answer cache reconnects on the new store
        log.info(f"Storage: app data moved from '{old}' to '{name}' ({copied or 'not copied'})")
        return copied


@appcore.app.post("/api/storage/app")
async def api_storage_set_app(payload: dict):
    """Keep the app data and answer cache on another connection: {"connection": name}.
    ``"copy": false`` switches without copying (for a previous store that is gone).

    Refused (400) for an unknown connection, one that cannot be reached, and one
    without vector search (the answer cache needs it)."""
    name = str((payload or {}).get("connection") or "").strip()
    do_copy = (payload or {}).get("copy", True) is not False
    cfg = redis_store.endpoint_cfg(name) if name else None
    if cfg is None:
        raise HTTPException(400, f"Unknown storage connection '{name}'" if name
                            else "Name the storage connection to keep the app data on")
    if name == appstore.app_connection():
        return {"ok": True, "app": name, "copied": None}
    label = _storage_label(name, cfg)
    ok, vs, err = await asyncio.to_thread(_storage_probe, name, cfg)
    if not ok:
        raise HTTPException(400, f"Can't connect to {label}" + (f": {err}" if err else "")
                            + ". Start it or fix the connection, then try again.")
    if not vs:
        raise HTTPException(400, f"{label} has no vector search, so it cannot hold the answer cache. "
                                 "Choose a Redis with the Query Engine, MongoDB Atlas, or MongoDB with "
                                 "the search process (mongot).")
    copied = await asyncio.to_thread(_switch_app_store, name, do_copy)
    return {"ok": True, "app": name, "copied": copied}
