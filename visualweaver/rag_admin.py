# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.rag_admin — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import asyncio
import json
import re
import threading
import time
from datetime import datetime, timezone
import redis
from fastapi import HTTPException
from . import appstore, config, instance_registry, mongo_store, rag, redis_store, state

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# RAG INSTANCE MANAGEMENT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def meta_get(rc, instance: str) -> dict | None:
    """An instance's metadata on one connection (Redis key rag_meta:<i>, or vw_meta)."""
    if mongo_store.is_mongo(rc):
        return mongo_store.get_meta(rc, instance)
    raw = rc.get(f"rag_meta:{instance}")
    return json.loads(raw) if raw else None


def meta_set(rc, instance: str, meta: dict) -> None:
    """Write an instance's metadata, and its name to the instance registry."""
    if mongo_store.is_mongo(rc):
        mongo_store.set_meta(rc, instance, meta)
    else:
        rc.set(f"rag_meta:{instance}", json.dumps(meta))
        _registry_write(instance_registry.redis_add, rc, instance)


def meta_delete(rc, instance: str) -> None:
    """Remove an instance's metadata and its registry entry."""
    if mongo_store.is_mongo(rc):
        mongo_store.delete_meta(rc, instance)
    else:
        rc.delete(f"rag_meta:{instance}")
        _registry_write(instance_registry.redis_remove, rc, instance)


def _registry_write(fn, rc, instance: str) -> None:
    """A registry update after a metadata write. Best effort: the metadata is the
    record, and a listing repairs the registry (see instance_registry)."""
    try:
        fn(rc, instance)
    except Exception as e:
        log.debug(f"instance registry not updated for '{instance}': {e}")


def list_rag_instances() -> list[dict]:
    """
    Return metadata for all RAG instances across all configured Redis endpoints.

    Each instance can live on a different endpoint (stored in its rag_meta).
    We scan every endpoint so the UI shows a unified list.
    """
    all_instances: dict[str, dict] = {}   # "endpoint:name" -> info

    # Build the list of endpoints to scan: default + any extras in config
    endpoints_to_scan = []
    try:
        endpoints_to_scan.append(("default", redis_store.r_for("default")))
    except Exception as e:           # e.g. a malformed built-in MongoDB connection string
        state._endpoint_health["default"] = False
        log.warning(f"Built-in storage connection unusable: {e}")
    for ep in state._config.get("redis_endpoints", []):
        ep_name = ep.get("name", "")
        if ep_name and ep_name != "default":
            try:
                endpoints_to_scan.append((ep_name, redis_store.r_for(ep_name)))
            except Exception:
                pass

    listed: set = set()        # endpoints whose names were read completely this time
    # RediSearch indexes are server-wide: FT._LIST answers the same on every logical
    # database, and an index can only be created in database 0. A connection to another
    # database of a server therefore listed every instance of that server's database 0 as
    # its own — duplicate cards, and a Remove dialog naming instances it does not hold.
    # FT._LIST is read only through a database-0 connection; rag_meta keys are per
    # database. A second connection to the same server AND database (another host name
    # for it) is the same store and is not listed twice — servers are told apart by run_id.
    stores_seen: set[tuple[str, int]] = set()

    for ep_name, rc in endpoints_to_scan:
        # Skip endpoints already known to be down — avoids hanging on socket_timeout
        if state._endpoint_health.get(ep_name, None) is False:
            continue
        if mongo_store.is_mongo(rc):
            try:
                for m in mongo_store.list_instances(rc):
                    all_instances[f"{ep_name}:{m['name']}"] = {
                        "count": m["chunks"], "ep": ep_name, "name": m["name"], "meta": m}
                listed.add(ep_name)
            except Exception as e:
                log.warning(f"Could not list instances on MongoDB '{ep_name}': {e}")
            continue
        try:
            # Enumerate this endpoint's RAG indexes (FT._LIST) and read each
            # instance's chunk count directly from the index (FT.SEARCH … LIMIT 0 0
            # returns the match total). O(instances) index reads instead of a scan
            # over every chunk key in the keyspace.
            try:
                db = int(rc.connection_pool.connection_kwargs.get("db", 0) or 0)
            except Exception:
                db = 0
            try:
                run_id = (rc.info("server") or {}).get("run_id")
            except Exception:
                run_id = None
            if run_id:
                if (run_id, db) in stores_seen:
                    continue
                stores_seen.add((run_id, db))
            idx_names = []
            if db == 0:
                try:
                    idx_names = [rag._decode(x) for x in rc.execute_command("FT._LIST")]
                except Exception:
                    idx_names = []
            indexed = []
            for idx_name in idx_names:
                if not (idx_name.startswith("rag:") and idx_name.endswith(":idx")):
                    continue
                inst = idx_name[len("rag:"):-len(":idx")]
                if inst:
                    indexed.append(inst)
            # Instances that exist only as metadata (created, never ingested, so no
            # index yet) come from the instance registry. They used to come from
            # SCAN MATCH rag_meta:* — a walk over every key, chunks included.
            registered = instance_registry.redis_names(rc)
            unregistered = [i for i in indexed if i not in registered]
            if unregistered:                          # heal: an index the registry missed
                try:
                    instance_registry.redis_add(rc, *unregistered)
                except Exception as e:                # a read-only replica: list anyway
                    log.debug(f"instance registry on '{ep_name}' not updated: {e}")
            meta_only = sorted(registered.difference(indexed))
            # One round trip for every count and every metadata check, instead of one
            # FT.SEARCH per index.
            pipe = rc.pipeline(transaction=False)
            for inst in indexed:
                pipe.execute_command("FT.SEARCH", f"rag:{inst}:idx", "*", "LIMIT", "0", "0")
            for inst in meta_only:
                pipe.exists(f"rag_meta:{inst}")
            replies = pipe.execute(raise_on_error=False)
            for inst, res in zip(indexed, replies):
                try:
                    count = int(res[0]) if res and not isinstance(res, Exception) else 0
                except Exception:
                    count = 0
                all_instances[f"{ep_name}:{inst}"] = {"count": count, "ep": ep_name, "name": inst}
            stale = []
            for inst, has_meta in zip(meta_only, replies[len(indexed):]):
                if isinstance(has_meta, Exception):
                    continue
                if has_meta:
                    all_instances.setdefault(f"{ep_name}:{inst}",
                                             {"count": 0, "ep": ep_name, "name": inst})
                else:
                    stale.append(inst)                # no metadata and no index left
            if stale:
                try:
                    instance_registry.redis_remove(rc, *stale)
                except Exception as e:
                    log.debug(f"instance registry on '{ep_name}' not updated: {e}")
            listed.add(ep_name)
        except Exception:
            pass
    for ep_name in listed:
        state._known_instance_names[ep_name] = {
            i["name"] for i in all_instances.values() if i["ep"] == ep_name}

    # Pipeline metadata fetches grouped by endpoint to avoid N round-trips
    ep_instances: dict[str, list[tuple[str, str]]] = {}  # ep_name → [(key, inst)]
    for key, info in all_instances.items():
        ep_instances.setdefault(info["ep"], []).append((key, info["name"]))

    meta_cache: dict[str, dict] = {}
    for ep_name, pairs in ep_instances.items():
        if redis_store.endpoint_type(ep_name) == "mongodb":
            for key, _ in pairs:
                m = dict(all_instances[key].get("meta") or {})
                m["redis_endpoint"] = ep_name
                meta_cache[key] = m
            continue
        try:
            rc = redis_store.r_for(ep_name)
            pipe = rc.pipeline(transaction=False)
            for _, inst in pairs:
                pipe.get(f"rag_meta:{inst}")
            raws = pipe.execute()
            for (key, _), raw in zip(pairs, raws):
                meta_cache[key] = json.loads(raw) if raw else {}
        except Exception:
            for key, _ in pairs:
                meta_cache[key] = {}

    result = []
    for key, info in all_instances.items():
        ep_name, inst = info["ep"], info["name"]
        meta = meta_cache.get(key, {})
        resolved_ep = meta.get("redis_endpoint", ep_name)
        # Unknown endpoints (not yet probed) are treated as reachable
        reachable = state._endpoint_health.get(resolved_ep, True)
        result.append({
            "name":            inst,
            "chunks":          info["count"],
            "color":           safe_color(meta.get("color")),
            "tags":            meta.get("tags",    []),
            "created":         meta.get("created", ""),
            "enabled":         meta.get("enabled", True),
            "redis_endpoint":  resolved_ep,
            "endpoint_type":   redis_store.endpoint_type(resolved_ep),
            "reachable":       reachable,
        })
    return result


def reset_rag(instance: str, rc: redis.Redis | None = None):
    """
    Delete all chunk keys and the FT index for a RAG instance.

    Uses FT.DROPINDEX with the DD (Delete Documents) flag which atomically
    drops the index AND removes all indexed HASH keys in a single command —
    far more efficient than the previous KEYS scan + bulk DELETE approach.
    The chunk counter key is cleaned up separately.
    """
    rc = rc or redis_store.r_for("default")
    if mongo_store.is_mongo(rc):
        mongo_store.reset_instance(rc, instance)
        config.append_log({"ts": datetime.now(timezone.utc).isoformat(), "instance": instance,
                           "source": "RESET", "chunks": 0, "status": "reset"})
        return
    prefix = rag.rag_prefix(instance)
    rag._index_ensured.discard(instance)   # force re-creation on next ingest
    rag._bm25_leg_warned.discard(instance)  # re-warn if the rebuilt index still has no text leg
    rag._emb_mismatch_warned.discard(instance)
    rag._dim_mismatch_warned.discard(instance)  # same: re-arm the dimension diagnostic
    try:
        rc.execute_command("FT.DROPINDEX", f"{prefix}:idx", "DD")
    except Exception:
        pass  # index may not exist yet — that's fine
    # Belt-and-suspenders: explicitly delete any remaining chunk HASH keys.
    # FT.DROPINDEX DD may silently fail (e.g. index never existed), leaving
    # chunk keys behind so the instance keeps reappearing in list scans.
    batch: list = []
    for k in rc.scan_iter(rag.rag_chunk_glob(instance), count=500):
        batch.append(k)
        if len(batch) >= 500:
            rc.delete(*batch)
            batch = []
    if batch:
        rc.delete(*batch)
    # Remove counter, chunk hash dedup set, URL skip list, and the schema-version
    # marker in one call — so a reset/delete leaves no orphan keys behind.
    rc.delete(f"rag:{instance}:chunk_counter", f"rag:{instance}:chunk_hashes",
              f"rag:{instance}:indexed_urls", f"rag:{instance}:schema_ver")
    config.append_log({
        "ts": datetime.now(timezone.utc).isoformat(),
        "instance": instance,
        "source": "RESET",
        "chunks": 0,
        "status": "reset",
    })


# ── rag_meta read-through cache ──────────────────────────────────────────────
# rag_meta:{instance} = {"enabled": bool, "redis_endpoint": str, ...}. It is read on
# every chat turn — to pick the owning endpoint and check the enabled flag — but written
# only when an instance is created, toggled or deleted. Reading it fresh each turn cost
# up to two Redis round trips per instance (rc_for_instance re-read the same key), which
# is pure latency against a remote Redis. This short-TTL in-process cache turns the hot
# path into a dict lookup; writes invalidate explicitly, so the TTL only bounds staleness
# in the (unexpected) event an invalidation is ever missed.
_RAG_META_TTL = 3.0                       # seconds
_rag_meta_cache: dict[str, tuple[float, tuple[dict | None, str]]] = {}
# The cache is read from the event loop AND from to_thread / FastAPI-threadpool workers,
# and writes to rag_meta invalidate it. A generation counter guarded by a lock closes the
# resolve-then-store race: if an invalidation lands while a resolve is in flight, the stale
# value is not cached (we re-resolve on the next read instead).
_rag_meta_lock = threading.Lock()

def _resolve_rag_meta(instance: str) -> tuple[dict | None, str]:
    """Find an instance's rag_meta across endpoints. Returns (meta_or_None, endpoint_name).
    One GET on the default endpoint in the common case; extra endpoints are consulted only
    when the instance is not on the default. Mirrors the old rc_for_instance search order."""
    try:
        meta = meta_get(redis_store.r_for("default"), instance)
        if meta:
            return meta, meta.get("redis_endpoint", "default")
    except Exception:
        pass
    for ep in state._config.get("redis_endpoints", []):
        ep_name = ep.get("name", "")
        if ep_name and ep_name != "default":
            try:
                meta = meta_get(redis_store.r_for(ep_name), instance)
                if meta is not None:
                    return meta, ep_name
            except Exception:
                pass
    return None, "default"

def _rag_meta_cached(instance: str) -> tuple[dict | None, str]:
    """Cached (meta, endpoint) for an instance. Cache hit performs no Redis I/O."""
    ent = _rag_meta_cache.get(instance)
    if ent and (time.time() - ent[0]) < _RAG_META_TTL:
        return ent[1]
    with _rag_meta_lock:
        gen_before = state._rag_meta_gen
    val = _resolve_rag_meta(instance)                 # Redis I/O outside the lock
    with _rag_meta_lock:
        if state._rag_meta_gen == gen_before:               # no invalidation raced us
            _rag_meta_cache[instance] = (time.time(), val)
    return val

async def _rag_meta_cached_async(instance: str) -> tuple[dict | None, str]:
    """Same as _rag_meta_cached, but a cache miss resolves off the event loop."""
    ent = _rag_meta_cache.get(instance)
    if ent and (time.time() - ent[0]) < _RAG_META_TTL:
        return ent[1]
    return await asyncio.to_thread(_rag_meta_cached, instance)

def invalidate_rag_meta(instance: str | None = None):
    """Drop cached rag_meta after a write so the next read reflects it immediately."""
    with _rag_meta_lock:
        state._rag_meta_gen += 1                             # cancels any in-flight resolve's store
        if instance is None:
            _rag_meta_cache.clear()
        else:
            _rag_meta_cache.pop(instance, None)

def rc_for_instance(instance: str) -> redis.Redis:
    """
    Return the Redis client that owns a specific RAG instance.
    Endpoint is resolved via the cached rag_meta (default endpoint first, then extras),
    falling back to the default client if the instance is not found anywhere.
    """
    _meta, ep = _rag_meta_cached(instance)
    return redis_store.r_for(ep)

def _rc_for(instance: str, endpoint: str | None = None) -> redis.Redis:
    """
    Return the Redis client for an instance.
    When ``endpoint`` is supplied explicitly (e.g. from a query parameter) it
    is used directly, bypassing the metadata lookup.  This lets callers target
    the correct server even when two instances share the same name on different
    endpoints.
    """
    if endpoint:
        if endpoint != "default" and redis_store.endpoint_cfg(endpoint) is None:
            raise HTTPException(400, f"Unknown storage connection '{endpoint}'")
        if redis_store.endpoint_type(endpoint) == "mongodb":
            # Naming a connection directly (ingest/import ?endpoint=) must not reach a
            # server that cannot hold an instance — the same rule creation enforces.
            ok = state._search_available.get(endpoint)
            if ok is None:
                p = mongo_store.probe(redis_store.endpoint_cfg(endpoint), ep=redis_store.r_for(endpoint))
                state._endpoint_health[endpoint] = bool(p["ok"])
                ok = state._search_available[endpoint] = bool(p.get("vector_search")) if p["ok"] else None
            if ok is False:
                raise HTTPException(400, f"Storage '{endpoint}': {mongo_store.NO_SEARCH_MSG}")
        return redis_store.r_for(endpoint)
    return rc_for_instance(instance)



# ── Retrieval-quality stats: persistence ──────────────────────────────────────
# _rag_stats lives in memory, so before this it emptied on every restart — and
# Settings is most often opened right after one. Anything driven by these counters
# (the measured advice under each control) would have shown "no data yet" almost
# every time it mattered.
_RAG_STATS_KEY = "rag_stats"
# Nothing may be flushed before the restore has run. The window between the server
# accepting traffic and _bg_init reaching load_rag_stats is seconds wide, and one chat
# turn inside it used to SET the whole key from a dict holding a single fresh row —
# overwriting every other instance's history permanently.
_stats_loaded = False
# The restore could not READ the store (not reachable yet — a MongoDB container still
# starting). Flushing stays blocked and the next flush retries the restore, or the first
# turn would overwrite the persisted history with a one-row snapshot.
_stats_load_failed = False


def save_rag_stats() -> None:
    """Flush the retrieval counters to the app store. Best-effort: never fail a request."""
    if not _stats_loaded and _stats_load_failed and state._rag_stats_dirty:
        load_rag_stats()                 # the store may be reachable now
    if not _stats_loaded or not state._rag_stats_dirty:
        return
    try:
        # Clear the dirty set BEFORE the round trip and snapshot in the same breath:
        # anything marked while the SET is in flight would otherwise be cleared without
        # having been written. A copy also stops "dictionary changed size during
        # iteration" when an executor thread adds an instance mid-serialise.
        pending = set(state._rag_stats_dirty)
        state._rag_stats_dirty -= pending
        snapshot = {k: dict(v) for k, v in list(state._rag_stats.items())}
        try:
            with appstore.gate.write():
                appstore.current().set(_RAG_STATS_KEY, json.dumps(snapshot))
        except Exception:
            state._rag_stats_dirty |= pending      # unwritten — try again next turn
            raise
    except Exception as e:                                        # pragma: no cover
        log.warning(f"Could not persist RAG stats: {e}")


def _clean_stats_row(s: dict) -> dict | None:
    """Validate one persisted row, or reject it.

    Every field is type-checked, not just ``queries``. A row that half-loads is worse
    than one that is dropped: a surviving ``queries`` beside a zeroed histogram reads as
    "200 queries, 0% clear this" — a confident red verdict built on nothing.
    """
    if not isinstance(s, dict) or not isinstance(s.get("queries"), int):
        return None
    row = dict(state.RAG_STATS_TEMPLATE)
    for k, default in state.RAG_STATS_TEMPLATE.items():
        v = s.get(k, default)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return None
        row[k] = type(default)(v)

    def _ints(key, size):
        v = s.get(key)
        if (isinstance(v, list) and len(v) == size
                and all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in v)):
            return list(v)
        return None

    hist = _ints("raw_hist", state.RAW_HIST_BUCKETS)
    cited = _ints("cited_hist", state.CITED_HIST_RANKS)
    # A rejected histogram invalidates the counter that depends on it, or the advice
    # reads a sample size it does not have the data for.
    if hist is None:
        hist, row["queries"], row["raw_score_sum"] = [0] * state.RAW_HIST_BUCKETS, 0, 0.0
        row["scored"] = 0     # the sample size has to fall with the data it counts
    row["raw_hist"] = hist
    row["cited_hist"] = cited if cited is not None else [0] * state.CITED_HIST_RANKS
    aw = s.get("answers_with_citations")
    row["answers_with_citations"] = (int(aw) if cited is not None and isinstance(aw, int)
                                     and not isinstance(aw, bool) and aw >= 0 else 0)
    # Which retrieval geometry the sample belongs to, and the parked ones. Dropping
    # these on load would silently re-pool the modes on the next restart — the exact
    # defect the split exists to prevent, and invisible because the numbers still look
    # plausible.
    # A row written before `scored` existed reports 0, which makes a corpus with real
    # history look unmeasured — and sent it down the histogram-only path, where a
    # legacy blob full of the pre-fix "empty retrieval scored 0.00" entries derived a
    # threshold of 0.00 and offered it as a one-click apply.
    if not s.get("scored"):
        row["scored"] = sum(row["raw_hist"]) or int(row.get("queries") or 0)
    reg = s.get("regime")
    row["regime"] = reg if isinstance(reg, str) and reg else state.UNKNOWN_REGIME
    alt_in = s.get("alt")
    alt_out: dict = {}
    if isinstance(alt_in, dict):
        for name, slot in list(alt_in.items())[:state.MAX_REGIMES]:
            if not isinstance(name, str) or not isinstance(slot, dict):
                continue
            vals = slot.get("s")
            if not (isinstance(vals, list)
                    and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                            for x in vals)):
                continue
            # "h" too. _switch_regime parks the histogram with the sample, but this
            # rebuilt the slot without it — so on the next restart the chart the release
            # added "so the reader can check the verdict" showed one search out of 306.
            h = slot.get("h")
            if not (isinstance(h, list) and len(h) == state.RAW_HIST_BUCKETS
                    and all(isinstance(x, int) and not isinstance(x, bool) and x >= 0
                            for x in h)):
                h = [0] * state.RAW_HIST_BUCKETS
            alt_out[name] = {
                "n": int(slot["n"]) if isinstance(slot.get("n"), int) else 0,
                "sum": float(slot["sum"]) if isinstance(slot.get("sum"), (int, float)) else 0.0,
                "s": [round(float(x), state.RAW_SAMPLE_DP)
                      for x in vals[:state.RAW_SAMPLE_SIZE]],
                "h": list(h),
            }
    row["alt"] = alt_out
    samp = s.get("raw_sample")
    # Rounded on the way in too, so a blob written before the reservoir rounded does not
    # keep paying full float repr on every flush until its entries happen to roll over.
    row["raw_sample"] = ([round(float(x), state.RAW_SAMPLE_DP)
                          for x in samp[:state.RAW_SAMPLE_SIZE]]
                         if isinstance(samp, list)
                         and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                                 for x in samp) else [])
    return row


def load_rag_stats() -> None:
    """Restore the counters at startup. A malformed or partial record is discarded
    rather than half-loaded — a counter that is wrong is worse than one that is absent,
    because the advice built on it reads as measurement either way."""
    global _stats_load_failed
    try:
        try:
            raw = appstore.current().get(_RAG_STATS_KEY)
        except Exception as e:
            _stats_load_failed = True
            log.warning(f"Could not read the persisted RAG stats yet ({str(e)[:200]}); will retry")
            return
        _stats_load_failed = False
        if not raw:
            return
        data = json.loads(raw)
        if not isinstance(data, dict):
            return
        clean: dict = {}
        for inst, row in data.items():
            try:
                cleaned = _clean_stats_row(row)
            except Exception:
                cleaned = None            # one bad row must not discard the others
            if cleaned is not None:
                clean[inst] = cleaned
        # Do not clobber a row already accumulating in memory: a turn may have landed
        # before the restore ran.
        for inst, row in clean.items():
            state._rag_stats.setdefault(inst, row)
        log.info(f"Restored retrieval stats for {len(clean)} instance(s)")
    except Exception as e:                                        # pragma: no cover
        log.warning(f"Could not restore RAG stats: {e}")
    finally:
        # Flushing is unblocked either way: a corrupt or missing key must not wedge
        # persistence for the life of the process. Only an unreadable store keeps it
        # blocked (see _stats_load_failed).
        global _stats_loaded
        _stats_loaded = not _stats_load_failed


def clear_rag_stats() -> None:
    """Zero the counters, in memory and in the app store."""
    global _stats_loaded, _stats_load_failed
    _stats_loaded, _stats_load_failed = True, False
    state._rag_stats.clear()
    state._rag_stats_dirty.clear()
    try:
        with appstore.gate.write():
            appstore.current().delete(_RAG_STATS_KEY)
    except Exception as e:                                        # pragma: no cover
        log.warning(f"Could not clear persisted RAG stats: {e}")


def names_on_unreachable_connections() -> dict[str, str]:
    """{instance name: connection} last listed on connections that are down now — the
    instances list_rag_instances cannot show, which a new name must not repeat."""
    out: dict[str, str] = {}
    for ep_name, names in list(state._known_instance_names.items()):
        if state._endpoint_health.get(ep_name) is False:
            for n in names:
                out.setdefault(n, ep_name)
    return out


_COLOR_RE = re.compile(r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")
DEFAULT_COLOR = "#6366f1"


def safe_color(value) -> str:
    """An instance colour as the UI may use it: a #hex colour, else the default. The
    colour is written into style attributes, so anything else (a value that closes the
    attribute and adds markup) must never reach the page — whatever stored it."""
    return value if isinstance(value, str) and _COLOR_RE.fullmatch(value) else DEFAULT_COLOR
