# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.routes_instances — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from fastapi import HTTPException
from . import appcore, embeddings, mongo_store, rag, rag_admin, redis_store, state

log = logging.getLogger(__name__)

# An instance name is interpolated into Redis keys and into SCAN MATCH patterns. The
# patterns are escaped at the point of use (rag.rag_chunk_glob), which is what actually
# contains the blast radius; this is the second layer — reject the characters outright so
# a name like "*" cannot exist in the first place. Only glob metacharacters and the key
# separator are excluded: spaces and non-ASCII letters carry no glob meaning, so "My Docs"
# and "ünïcode" are perfectly safe names and were needlessly rejected before.
_BAD_INSTANCE_CHARS = re.compile(r"[*?\[\]\\:]")


def _check_instance_name(name: str) -> str:
    """Validate a RAG instance name, raising HTTPException(400) if unusable.

    Shared by every route that can CREATE an instance — creation, import and ingest —
    because gating only POST /api/rag/instances left ``POST /api/rag/*/import`` able to
    make one called "*". Destructive routes are deliberately NOT gated: a legacy instance
    with an awkward name must stay deletable.
    """
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(400, "Instance name is required.")
    if len(name) > 64:
        raise HTTPException(400, "Instance name must be 64 characters or fewer.")
    if _BAD_INSTANCE_CHARS.search(name):
        raise HTTPException(
            400,
            r"Instance name must not contain any of  * ? [ ] \ :  — these are pattern "
            "characters in Redis keys and would make the instance overlap with others.",
        )
    return name


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES — RAG INSTANCES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@appcore.app.get("/api/embedding/models")
def api_embedding_models():
    """The model registry, plus which models each instance's chunks were built with.

    ``in_use`` is what makes a mixed-model corpus visible: it is the set of
    registry ids actually present in an index, so the UI can show when an
    instance holds vectors from more than one model.
    """
    out = []
    for mid, spec in embeddings.EMBEDDING_MODELS.items():
        out.append({"id": mid, **{k: v for k, v in spec.items()},
                    "field": embeddings.vector_field_for(spec["repo"]),
                    "active": mid == embeddings.embedding_id_for()})
    return {"models": out, "active_id": embeddings.embedding_id_for(),
            "default_id": embeddings.DEFAULT_EMBEDDING_ID}


@appcore.app.get("/api/rag/{instance}/embedding-models")
def api_instance_embedding_models(instance: str, endpoint: str | None = None):
    """Registry ids present in one instance's chunks, with counts."""
    rc = rag_admin._rc_for(instance, endpoint)
    if mongo_store.is_mongo(rc):
        models = [{"id": mid, "chunks": n, "label": embeddings.EMBEDDING_MODELS.get(mid, {}).get(
                       "label", "unknown model")} for mid, n in mongo_store.embedding_models(rc, instance)]
        return {"instance": instance, "models": sorted(models, key=lambda m: -m["chunks"]),
                "mixed": len(models) > 1}
    try:
        res = rc.execute_command(
            "FT.AGGREGATE", f"{rag.rag_prefix(instance)}:idx", "*",
            "GROUPBY", "1", "@emb_model",
            "REDUCE", "COUNT", "0", "AS", "n", "LIMIT", "0", "20")
    except Exception as e:
        # Indexes built before the emb_model field exists have no such property.
        # Those chunks predate per-chunk provenance; report the configured model
        # as unverified rather than erroring.
        if "emb_model" in str(e):
            return {"instance": instance, "mixed": False, "legacy": True,
                    "models": [{"id": -1, "chunks": -1,
                                "label": "pre-v4 index — model not recorded per chunk"}]}
        return {"instance": instance, "models": [], "error": str(e)}
    models = []
    for row in res[1:]:
        d = {rag._decode(row[i]): rag._decode(row[i + 1]) for i in range(0, len(row) - 1, 2)}
        try:
            mid = int(d.get("emb_model", -1))
        except (TypeError, ValueError):
            mid = -1
        models.append({
            "id": mid, "chunks": int(d.get("n", 0) or 0),
            "label": embeddings.EMBEDDING_MODELS.get(mid, {}).get(
                "label", "provenance not recorded (ingested before per-chunk tracking)"
                if mid == -1 else "unknown model"),
        })
    return {"instance": instance, "models": sorted(models, key=lambda m: -m["chunks"]),
            "mixed": len(models) > 1}


@appcore.app.get("/api/rag/instances")
def api_rag_instances():
    return rag_admin.list_rag_instances()


@appcore.app.post("/api/rag/instances")
async def api_create_instance(payload: dict):
    """
    Create a new RAG instance with optional metadata.
    ``endpoint`` (or the older ``redis_endpoint``) names the storage connection —
    Redis or MongoDB — that holds the instance; it defaults to the built-in "default"
    connection (the built-in Redis or MongoDB).
    """
    color = payload.get("color") or rag_admin.DEFAULT_COLOR
    if rag_admin.safe_color(color) != color:
        raise HTTPException(400, "A colour must be a hex colour such as #6366f1")
    name     = _check_instance_name(payload.get("name") or f"rag_{uuid.uuid4().hex[:6]}")
    ep_name  = payload.get("endpoint") or payload.get("redis_endpoint") or "default"
    # Chat and every name-only route resolve an instance by NAME, built-in Redis first —
    # a second instance with the same name on another connection would never be searched.
    clash = next((i for i in await asyncio.to_thread(rag_admin.list_rag_instances) if i["name"] == name), None)
    if clash is None:
        down = rag_admin.names_on_unreachable_connections().get(name)
        if down:
            raise HTTPException(409, f"An instance named '{name}' already exists on '{down}', "
                                     f"which can't be reached right now")
    if clash:
        where = clash.get("redis_endpoint") or "default"
        if where == "default":
            where = "the " + redis_store.default_label().replace("Built-in", "built-in")
        raise HTTPException(409, f"An instance named '{name}' already exists (on {where})")
    if ep_name != "default" and redis_store.endpoint_cfg(ep_name) is None:
        # r_for would quietly fall back to the default connection — the instance
        # would land somewhere the user did not choose.
        raise HTTPException(400, f"Unknown storage connection '{ep_name}'")
    if redis_store.endpoint_type(ep_name) == "mongodb":
        probe = await asyncio.to_thread(mongo_store.probe, redis_store.endpoint_cfg(ep_name))
        state._endpoint_health[ep_name] = bool(probe.get("ok"))
        state._search_available[ep_name] = bool(probe.get("vector_search")) if probe.get("ok") else None
        if not probe.get("ok"):
            raise HTTPException(400, f"Cannot reach MongoDB '{ep_name}': {probe.get('error')}")
        if not probe.get("vector_search"):
            raise HTTPException(400, f"MongoDB '{ep_name}' has no vector search, so it cannot hold "
                                     "a RAG instance. Use MongoDB Atlas, or MongoDB 8.3+ with the search process (mongot).")
    rc       = redis_store.r_for(ep_name)
    meta = {
        "color":           color,
        "tags":            payload.get("tags", []),
        "created":         datetime.now(timezone.utc).isoformat(),
        "redis_endpoint":  ep_name,
    }
    await asyncio.to_thread(rag_admin.meta_set, rc, name, meta)
    rag_admin.invalidate_rag_meta(name)
    await asyncio.to_thread(rag.ensure_rag_index, name, rc)
    return {"name": name, **meta}


@appcore.app.delete("/api/rag/instances/{instance}")
def api_delete_instance(instance: str, endpoint: str | None = None):
    rc = rag_admin._rc_for(instance, endpoint)
    rag_admin.reset_rag(instance, rc)
    rag_admin.meta_delete(rc, instance)
    rag_admin.invalidate_rag_meta(instance)
    state.drop_rag_stats(instance)
    rag_admin.save_rag_stats()
    return {"ok": True}


@appcore.app.post("/api/rag/{instance}/toggle")
def api_toggle_rag(instance: str, payload: dict, endpoint: str | None = None):
    """Enable or disable a RAG instance without deleting its data."""
    rc       = rag_admin._rc_for(instance, endpoint)
    meta     = rag_admin.meta_get(rc, instance) or {}
    meta["enabled"] = bool(payload.get("enabled", True))
    rag_admin.meta_set(rc, instance, meta)
    rag_admin.invalidate_rag_meta(instance)
    return {"ok": True, "enabled": meta["enabled"]}


@appcore.app.post("/api/rag/{instance}/reset")
def api_reset_rag(instance: str, endpoint: str | None = None):
    rag_admin.reset_rag(instance, rag_admin._rc_for(instance, endpoint))
    return {"ok": True}


@appcore.app.get("/api/rag/{instance}/chunks")
def api_rag_chunks(instance: str, limit: int = 50, offset: int = 0, endpoint: str | None = None):
    """Return a page of stored chunks for inspection, ordered by chunk_id.

    One FT.SEARCH over the index (SORTBY the sortable chunk_id) returns the
    fields directly and in a deterministic order — instead of a scan whose
    order is arbitrary followed by an HGETALL per key. `offset` enables real
    pagination.
    """
    rc     = rag_admin._rc_for(instance, endpoint)
    if mongo_store.is_mongo(rc):
        return mongo_store.chunks_page(rc, instance, max(0, offset), max(0, limit))
    prefix = rag.rag_prefix(instance)
    idx_name = f"{prefix}:idx"
    try:
        res = rc.execute_command(
            "FT.SEARCH", idx_name, "*",
            "SORTBY", "chunk_id", "ASC",
            "RETURN", "3", "chunk_id", "text", "source",
            "LIMIT", str(max(0, offset)), str(max(0, limit)),
            "DIALECT", "2",
        )
        chunks = []
        items = res[1:]   # res[0] is the total match count
        for i in range(0, len(items), 2):
            fields = items[i + 1]
            d = {}
            for j in range(0, len(fields), 2):
                d[rag._decode(fields[j])] = rag._decode(fields[j + 1])
            chunks.append({
                "id":     d.get("chunk_id", "0"),
                "text":   d.get("text", "")[:200],
                "source": d.get("source", ""),
            })
        return chunks
    except Exception as e:
        # Index missing → fall back to a bounded scan (arbitrary order). Honour
        # offset by skipping that many keys before collecting up to `limit`.
        log.warning(f"api_rag_chunks: search failed for '{instance}', scanning instead: {e}")
        keys, skipped = [], 0
        for k in rc.scan_iter(rag.rag_chunk_glob(instance), count=500):
            if skipped < max(0, offset):
                skipped += 1
                continue
            keys.append(k)
            if len(keys) >= limit:
                break
        chunks = []
        for k in keys:
            d = rc.hgetall(k)
            chunks.append({
                "id":     d.get(b"chunk_id", b"0").decode(),
                "text":   d.get(b"text", b"").decode()[:200],
                "source": d.get(b"source", b"").decode(),
            })
        return chunks

