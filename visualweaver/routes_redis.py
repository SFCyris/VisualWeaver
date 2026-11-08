# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.routes_redis — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import asyncio
import json
from datetime import datetime, timezone
import redis
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from . import appcore, appstore, auth, config, constants, mongo_store, projects, rag, rag_admin, rag_advice, redis_store, routes_settings, state
from . import mongo_cache
from . import cache as _ns_cache

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES — REDIS ENDPOINTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@appcore.app.get("/api/redis/endpoints")
def api_list_endpoints():
    """Return all storage connections (the built-in default — Redis or MongoDB — then
    the added ones), passwords redacted. Each carries ``type``, ``builtin`` and, for
    MongoDB, a display ``host``."""
    def _strip(ep: dict) -> dict:
        e = dict(ep)
        e["type"] = e.get("type") or "redis"
        e["builtin"] = bool(e.get("builtin"))
        if e.get("password"):
            e["password"] = config._SECRET_SENTINEL
        if e["type"] == "mongodb":
            e["host"] = mongo_store.display_host(e.get("uri", ""))
        return e
    primary = _strip({**redis_store.endpoint_cfg("default"), "primary": True})
    extras  = [_strip(e) for e in state._config.get("redis_endpoints", [])]
    return [primary] + extras


def _mongo_entry(payload: dict, existing: dict) -> dict:
    """Normalise a MongoDB connection from the form: credentials pasted inside the
    connection string are moved to username/password so the stored URI holds no secret."""
    # The Server field takes a bare server (built into a connection string) or a full one.
    uri, u_user, u_pw = mongo_store.split_uri_credentials(mongo_store.server_uri(payload.get("uri", "")))
    if not uri.startswith(("mongodb://", "mongodb+srv://")):
        raise HTTPException(400, "Enter a server (cluster0.xxxxx.mongodb.net, host:27017) "
                                 "or a connection string starting with mongodb:// or mongodb+srv://")
    # Atlas shows the string with a <db_password> placeholder; a typed password wins.
    password = payload.get("password", "") or u_pw
    if mongo_store.is_password_placeholder(password):
        raise HTTPException(400, mongo_store.PLACEHOLDER_MSG)
    username = (payload.get("username") or u_user or "").strip()
    if password == config._SECRET_SENTINEL:
        # Reuse the stored password only for the server it belongs to. Matching on the
        # connection name alone let a Test against any other address send it there.
        same = (mongo_store.split_uri_credentials(existing.get("uri", ""))[0] == uri
                and (existing.get("username") or "") == username)
        password = existing.get("password", "") if same else ""
    database = (payload.get("database") or mongo_store.DEFAULT_DB).strip()
    if not mongo_store.valid_database_name(database):
        raise HTTPException(400, "A MongoDB database name cannot be empty, longer than 63 bytes, "
                                 "or contain / \\ . space \" $ * < > : | ?")
    return {
        "name":     payload["name"],
        "type":     "mongodb",
        "uri":      uri,
        "username": username,
        "password": password,
        "database": database,
    }


def _drop_client(name: str) -> None:
    # The answer cache holds the app-data connection's client: re-saving that connection
    # (a new password, say) retires the client, so the cache must be rebuilt on it too.
    if name == appstore.app_connection():
        state._semantic_cache = None
    c = state._redis_clients.pop(name, None)
    if mongo_store.is_mongo(c):
        redis_store.retire_client(c)
    mongo_store.forget_indexes(name)
    state._endpoint_health.pop(name, None)
    state._search_available.pop(name, None)


@appcore.app.post("/api/redis/endpoints")
async def api_add_endpoint(payload: dict):
    """
    Add (or replace) a named storage connection: ``type`` "redis" (host, port, db,
    password, ssl) or "mongodb" (uri, database, username, password).
    The name must be unique and must not be 'default'.
    """
    name = payload.get("name", "").strip()
    if not name or name == "default":
        raise HTTPException(400, "Connection name must be a non-empty string other than 'default'")
    if name in ("local-mongodb", "local-redis") and not (redis_store.endpoint_cfg(name) or {}).get("builtin"):
        raise HTTPException(400, f"'{name}' is reserved for the built-in storage the installer sets up; "
                                 f"choose another name")
    payload = {**payload, "name": name}
    endpoints = state._config.get("redis_endpoints", [])
    existing = next((e for e in endpoints if e.get("name") == name), {})
    new_type = payload.get("type") or "redis"
    if existing.get("builtin") and (existing.get("type") or "redis") != new_type:
        raise HTTPException(400, f"'{name}' is a built-in connection; its type cannot change")
    if existing and name == appstore.app_connection() and (existing.get("type") or "redis") != new_type:
        raise HTTPException(400, f"'{name}' holds the app data and answer cache; its type cannot "
                                 "change. Choose another store for them in Settings → Storage first")
    if existing and (existing.get("type") or "redis") != new_type:
        held = [i["name"] for i in await asyncio.to_thread(rag_admin.list_rag_instances)
                if (i.get("redis_endpoint") or "default") == name]
        if held:
            raise HTTPException(409, f"'{name}' is a {existing.get('type') or 'redis'} connection holding "
                                     f"{len(held)} instance(s); remove them, or use another name")
    if new_type == "mongodb":
        entry = _mongo_entry(payload, existing)
    else:
        # A sentinel password means "keep the existing one" (the UI never saw it).
        password = payload.get("password", "")
        if password == config._SECRET_SENTINEL:
            password = existing.get("password", "")
        entry = {
            "name":     name,
            "type":     "redis",
            "host":     payload.get("host", "localhost"),
            "port":     int(payload.get("port", 6379)),
            "db":       int(payload.get("db", 0)),
            "password": password,
            "ssl":      bool(payload.get("ssl", False)),
        }
    if existing.get("builtin"):
        entry["builtin"] = True      # set up by the installer: stays undeletable
    # Upsert: replace if name already exists
    endpoints = [e for e in endpoints if e.get("name") != name]
    endpoints.append(entry)
    state._config["redis_endpoints"] = endpoints
    await config.save_config_async(state._config)
    _drop_client(name)
    await asyncio.to_thread(redis_store._probe_endpoint, name)
    return {"ok": True}


@appcore.app.delete("/api/redis/endpoints/{name}")
def api_delete_endpoint(name: str):
    """Remove a named storage connection. Built-in connections ('default' and the ones
    the installer added) and the connection holding the app data cannot be removed."""
    if name == "default":
        raise HTTPException(400, "Cannot delete the default endpoint")
    ep = redis_store.endpoint_cfg(name) or {}
    if ep.get("builtin"):
        raise HTTPException(400, f"'{name}' is a built-in connection set up by the installer; "
                                 "it cannot be removed")
    if name == appstore.app_connection():
        raise HTTPException(400, f"'{name}' holds the app data and answer cache; "
                                 "choose another store for them in Settings → Storage first")
    endpoints = [e for e in state._config.get("redis_endpoints", []) if e.get("name") != name]
    state._config["redis_endpoints"] = endpoints
    config.save_config(state._config)
    _drop_client(name)
    rag_admin.invalidate_rag_meta()
    return {"ok": True}


@appcore.app.get("/api/redis/endpoints/{name}/discover")
def api_discover_endpoint(name: str):
    """
    Scan a named Redis endpoint for existing RAG instances.
    Returns a list of {name, chunks, has_meta} dicts so the UI can prompt
    the user to re-register instances found on that server.
    """
    try:
        rc = redis_store.r_for(name)
        rc.ping()  # fail fast if connection is broken
    except Exception as e:
        raise HTTPException(400, f"Cannot connect to endpoint '{name}': {e}")
    if mongo_store.is_mongo(rc):
        try:
            metas = {m["_id"] for m in rc.col(mongo_store.META).find({}, {"_id": 1})}
            return [{"name": m["name"], "chunks": m["chunks"], "has_meta": m["name"] in metas}
                    for m in mongo_store.list_instances(rc)]
        except Exception as e:
            raise HTTPException(500, f"Discovery failed: {e}")

    discovered: dict[str, dict] = {}
    try:
        # SCAN can return the same key more than once under concurrent writes, so count
        # DISTINCT chunk keys per instance rather than incrementing on every hit.
        seen_chunks: dict[str, set] = {}
        for k in rc.scan_iter("rag:*:chunk:*", count=500):   # SCAN, not KEYS (O(N) blocks Redis)
            parts = k.decode().split(":")
            if len(parts) >= 3:
                inst = parts[1]
                seen_chunks.setdefault(inst, set()).add(k)
        for inst, keys in seen_chunks.items():
            discovered.setdefault(inst, {"name": inst, "chunks": 0, "has_meta": False})
            discovered[inst]["chunks"] = len(keys)
        for mk in rc.scan_iter("rag_meta:*", count=500):     # SCAN, not KEYS
            inst = mk.decode().replace("rag_meta:", "")
            discovered.setdefault(inst, {"name": inst, "chunks": 0, "has_meta": False})
            discovered[inst]["has_meta"] = True
    except Exception as e:
        raise HTTPException(500, f"Discovery scan failed: {e}")

    return sorted(discovered.values(), key=lambda x: x["name"])


@appcore.app.post("/api/redis/endpoints/{name}/register")
def api_register_discovered(name: str, payload: dict):
    """
    Register a list of discovered RAG instances on a named endpoint.
    Ensures each instance has a rag_meta key that references this endpoint
    and that its search index exists.
    """
    instances = payload.get("instances", [])
    if not instances:
        return {"ok": True, "registered": 0}
    try:
        rc = redis_store.r_for(name)
    except Exception as e:
        raise HTTPException(400, f"Cannot connect to endpoint '{name}': {e}")

    registered = 0
    for inst_name in instances:
        try:
            meta = rag_admin.meta_get(rc, inst_name) or {}
            meta["redis_endpoint"] = name
            meta.setdefault("created", datetime.now(timezone.utc).isoformat())
            meta.setdefault("color", "#6366f1")
            meta.setdefault("enabled", True)
            rag_admin.meta_set(rc, inst_name, meta)
            rag_admin.invalidate_rag_meta(inst_name)
            rag.ensure_rag_index(inst_name, rc)
            registered += 1
        except Exception as e:
            log.warning(f"Failed to register '{inst_name}' on endpoint '{name}': {e}")

    return {"ok": True, "registered": registered}


@appcore.app.post("/api/redis/test")
async def api_test_redis_adhoc(payload: dict):
    """
    Test a Redis connection using parameters supplied in the request body.
    Does NOT use saved config — validates form-field values before saving.
    Body: {host, port, db, password, ssl}, or {type:"mongodb", name?, uri, database,
    username, password} for a MongoDB connection.
    """
    if payload.get("type") == "mongodb":
        existing = redis_store.endpoint_cfg(payload.get("name", "")) or {}
        try:
            cfg = _mongo_entry({**payload, "name": payload.get("name") or "probe"}, existing)
        except HTTPException as he:
            return {"ok": False, "error": he.detail}
        return _mongo_test_result(await asyncio.to_thread(mongo_store.probe, cfg))
    try:
        host = payload.get("host", "localhost")
        port = int(payload.get("port", 6379))
        password = payload.get("password", "")
        # The UI never receives real passwords (they're redacted to a sentinel).
        # If it echoes the sentinel back, resolve it against the stored config
        # for the matching host:port so "Test" works without re-typing the key.
        if password == config._SECRET_SENTINEL:
            candidates = [state._config.get("redis", {})] + list(state._config.get("redis_endpoints", []))
            match = next(
                (e for e in candidates
                 if e.get("host") == host and int(e.get("port", 6379)) == port),
                {},
            )
            password = match.get("password", "")
        rc = redis.Redis(
            host=host,
            port=port,
            db=int(payload.get("db", 0)),
            password=password or None,
            ssl=bool(payload.get("ssl", False)),
            socket_connect_timeout=3,
            socket_timeout=3,
            decode_responses=False,
        )
        info = await asyncio.to_thread(rc.info)   # keep the sync client off the event loop
        # Use a unique key so the probe result doesn't pollute the saved-endpoint cache
        probe_key = f"__adhoc_{payload.get('host')}:{payload.get('port')}"
        search_ok = await asyncio.to_thread(routes_settings.probe_search, rc, probe_key)
        # Don't persist this ad-hoc probe result permanently
        state._search_available.pop(probe_key, None)
        return {
            "ok":               True,
            "version":          info.get("redis_version"),
            "memory_used":      info.get("used_memory_human"),
            "connected_clients":info.get("connected_clients"),
            "mode":             info.get("redis_mode", "standalone"),
            "search_available": search_ok,
            **_db_note(int(payload.get("db", 0) or 0), search_ok),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _db_note(db: int, search_ok: bool) -> dict:
    """Why a Redis with the Search module still cannot hold instances: its database."""
    if db and not search_ok:
        return {"search_note": f"Redis search works only in database 0; this connection uses "
                               f"database {db}. Use database 0 to keep RAG instances or app data here."}
    return {}


def _mongo_test_result(p: dict) -> dict:
    return {"ok": p["ok"], "type": "mongodb", "version": p.get("version"),
            "search_available": p.get("vector_search", False), "atlas": p.get("atlas", False),
            "error": p.get("error")}


@appcore.app.post("/api/redis/endpoints/{name}/test")
async def api_test_endpoint(name: str):
    """Test connectivity to a named storage connection (uses saved config)."""
    if redis_store.endpoint_type(name) == "mongodb":
        ep = await asyncio.to_thread(redis_store.r_for, name)   # a mongodb+srv client resolves DNS
        p = await asyncio.to_thread(mongo_store.probe, redis_store.endpoint_cfg(name), ep)
        state._endpoint_health[name] = bool(p["ok"])
        state._search_available[name] = bool(p.get("vector_search")) if p["ok"] else None
        return _mongo_test_result(p)
    try:
        rc = redis_store.r_for(name)
        info = await asyncio.to_thread(rc.info)
        search_ok = await asyncio.to_thread(routes_settings.probe_search, rc, name, True)
        return {
            "ok":               True,
            "version":          info.get("redis_version"),
            "memory":           info.get("used_memory_human"),
            "search_available": search_ok,
            **_db_note(int((redis_store.endpoint_cfg(name) or {}).get("db") or 0), search_ok),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES — CACHE ANALYTICS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@appcore.app.get("/api/cache/stats")
def api_cache_stats():
    """
    Return a summary of entries in the SemanticCache, on whichever store holds the app's
    data: {"count", "entries": [{"query", "ttl"}]} (ttl -1 = never expires).
    redisvl stores the prompt in a field named 'prompt' (previously 'query').

    With sign-in on, every answer is cached for its user only (cache.private_partition)
    and a cached question is that user's own words: everyone — administrators included —
    gets only their own entries and their count. Administrators also get ``total`` and
    ``by_user`` ([{"user": partition tag, "count"}]; "" = cached before sign-in), counts
    only, never another user's questions.
    """
    out = _cache_stats()
    rows = out.get("entries") or []
    me = auth.current()
    if me is None:
        return {**out, "entries": [{k: v for k, v in e.items() if k != "owner"} for e in rows]}
    mine = auth.partition(me.id)
    own = [{k: v for k, v in e.items() if k != "owner"} for e in rows if e.get("owner") == mine]
    res = {**out, "count": len(own), "entries": own}
    if me.admin:
        per: dict = {}
        for e in rows:
            per[e.get("owner") or ""] = per.get(e.get("owner") or "", 0) + 1
        res["total"] = out.get("count", len(rows))
        res["by_user"] = [{"user": u, "count": n} for u, n in sorted(per.items(), key=lambda x: -x[1])]
    return res


def _cache_stats() -> dict:
    try:
        kind, rc = _ns_cache._cache_backend()
        if kind == "mongodb":
            entries = mongo_cache.entries(rc)
            return {"count": len(entries), "entries": entries}
        keys = list(rc.scan_iter(f"{constants.CACHE_PREFIX}*", count=200))
        entries = []
        pipe = rc.pipeline(transaction=False)
        for k in keys:
            pipe.hget(k, "prompt")
            pipe.ttl(k)
            pipe.hget(k, "owner")       # the user it was cached for (sign-in on), else none
        results = pipe.execute()
        for i in range(0, len(results), 3):
            prompt_raw = results[i]
            ttl        = results[i + 1]
            owner_raw  = results[i + 2]
            prompt     = prompt_raw.decode() if isinstance(prompt_raw, bytes) else (prompt_raw or "")
            owner      = owner_raw.decode() if isinstance(owner_raw, bytes) else (owner_raw or "")
            entries.append({"query": prompt, "ttl": ttl, "owner": owner})
        return {"count": len(keys), "entries": entries}
    except Exception as e:
        return {"count": 0, "entries": [], "error": str(e)}


@appcore.app.delete("/api/cache")
def api_cache_clear():
    """Clear all semantic cache entries. Uses SemanticCache.clear() when available."""
    cache = _ns_cache._get_semantic_cache()
    if cache is not None:
        try:
            cache.clear()
            return {"deleted": "all"}
        except Exception:
            pass  # fall through to manual key deletion
    kind, rc = _ns_cache._cache_backend()
    if kind == "mongodb":
        deleted = mongo_cache.clear(rc)
        state._semantic_cache = None
        return {"deleted": deleted}
    deleted = 0
    batch: list = []
    for k in rc.scan_iter(f"{constants.CACHE_PREFIX}*", count=500):   # SCAN, not KEYS (O(N) blocks Redis)
        batch.append(k)
        if len(batch) >= 500:
            rc.delete(*batch); deleted += len(batch); batch = []
    if batch:
        rc.delete(*batch); deleted += len(batch)
    state._semantic_cache = None  # reset so it re-indexes on next use
    return {"deleted": deleted}


@appcore.app.delete("/api/cache/entry")
def api_delete_cache_entry(entry_id: str):
    """
    Delete a single cache entry by its entry_id.
    redisvl SemanticCache stores entries as HASH keys named "{name}:{entry_id}";
    on MongoDB the entry_id is the document's _id.
    """
    if not entry_id:
        return {"ok": False, "error": "entry_id required"}
    # With sign-in on, a user removes only answers cached for them (their partition, see
    # cache.private_partition); an administrator any. Someone else's entry is answered
    # like one that does not exist.
    owner = None if auth.is_admin() else auth.cache_partition()
    try:
        kind, rc = _ns_cache._cache_backend()
        if kind == "mongodb":
            return {"ok": mongo_cache.delete_entry(rc, entry_id, owner=owner)}
        # SemanticCache name is CACHE_PREFIX stripped of the trailing ":"
        key = f"{constants.CACHE_PREFIX.rstrip(':')}:{entry_id}"
        if owner is not None:
            got = rc.hget(key, "owner")
            got = got.decode() if isinstance(got, bytes) else got
            if not owner or got != owner:
                return {"ok": False}
        deleted = rc.delete(key)
        return {"ok": bool(deleted)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@appcore.app.get("/api/rag/advice")
def api_rag_advice(project: str = ""):
    """Per-setting verdicts for the RAG controls, measured from this deployment.

    Everything here comes from observed retrieval behaviour — the distribution of
    pre-threshold scores and which chunk ranks answers actually cited. A setting with
    no measurement behind it is returned as ``unknown`` or ``static`` so the UI can
    show no colour rather than a guess.

    ``?project=<id>``: measured on that project's knowledge bases only (404 when there
    is no such project); the response then names the project and its knowledge bases.
    """
    if not project:
        return {"advice": rag_advice.build_advice(),
                "presets": [rag_advice.preset_diff(p["id"]) for p in rag_advice.RAG_PRESETS]}
    stats = projects.advice_stats(project)
    if stats is None:
        raise HTTPException(404, "project not found")
    p = projects.get(project) or {}
    return {"advice": rag_advice.build_advice(stats=stats),
            "presets": [rag_advice.preset_diff(x["id"], stats=stats) for x in rag_advice.RAG_PRESETS],
            "project": {"id": project, "name": p.get("name", ""), "kbs": sorted(stats)}}


@appcore.app.get("/api/rag/stats")
def api_rag_stats():
    """
    Return per-instance query statistics. They are persisted, so they accumulate
    across restarts rather than starting empty each time.

    Metrics:
      queries            — total number of times this instance was searched
      hits               — queries that returned ≥1 chunk above the threshold
      hit_rate           — hits / queries  (0.0–1.0)
      avg_top_score      — mean cosine similarity of the top-1 chunk on hit queries.
                           Serves as an accuracy proxy: higher = better semantic match.
      avg_best_raw_score — mean cosine similarity of the top-1 KNN result *before*
                           threshold filtering, across all queries.  If this is high
                           but hit_rate is low, your similarity_threshold is too strict.
      avg_chunks         — mean number of chunks returned per query (including misses)
    """
    result = []
    # A snapshot: these counters are mutated from executor threads, and iterating the
    # live dict raises "dictionary changed size during iteration" the moment a new
    # instance's first query lands mid-request.
    for inst, s in list(state._rag_stats.items()):
        q = s.get("queries") or 0
        h = s.get("hits") or 0
        result.append({
            "name":               inst,
            "queries":            q,
            "hits":               h,
            "misses":             q - h,
            "hit_rate":           round(h / q, 4)                          if q else 0.0,
            "avg_top_score":      round(s.get("score_sum", 0.0) / h, 4)     if h else 0.0,
            # Divided by SCORED searches, not by every query. raw_score_sum is parked
            # with its retrieval regime while `queries` is lifetime, so after any
            # embedding-model or HyDE change this collapsed to a fraction of the truth —
            # 0.0108 against a real 0.30 — and DOCS points users at it as the diagnostic
            # for a too-strict threshold. Searches that retrieved nothing are excluded
            # for the same reason: they contribute no score to the sum.
            "avg_best_raw_score": (round(s.get("raw_score_sum", 0.0) / sc, 4)
                                   if (sc := int(s.get("scored") or 0)) else 0.0),
            "avg_chunks":         round(s.get("chunks_total", 0) / q, 2)    if q else 0.0,
        })
    result.sort(key=lambda x: x["queries"], reverse=True)
    return result


@appcore.app.delete("/api/rag/stats")
def api_rag_stats_reset():
    """Reset all per-instance RAG statistics."""
    rag_admin.clear_rag_stats()
    return {"ok": True}


