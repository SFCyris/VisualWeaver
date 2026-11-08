# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.instance_registry — the set of RAG instance names held by one connection.

Listing the instances used to find the ones that exist only as metadata (created, never
ingested) with ``SCAN MATCH rag_meta:*`` — a walk over the WHOLE keyspace, every chunk
key included, on every listing (the UI lists every 30 s and on each page load). With 50
instances of 1,000 chunks that is ~250 round trips. The names are kept in a registry
instead, written wherever an instance comes into being or goes away:

  Redis    SET ``visualweaver:rag_instances`` — the names, plus the member ``:built``
           once the set is known to be complete
  MongoDB  collection ``vw_instances`` — {_id: name}, plus {_id: ":built"}

``:`` cannot occur in an instance name, so the marker never collides with one. The
marker lives INSIDE the registry, so removing the registry removes it too and the next
listing rebuilds it from a scan — which is also how an install from before the registry
gets one, once.

Consistency: every metadata write goes through rag_admin.meta_set / meta_delete, every
Redis index is created by rag.ensure_rag_index and every MongoDB chunk write by
mongo_store.add_chunks / import_rows; each of those registers or unregisters the name.
A Redis listing still reads FT._LIST (a server-wide list of indexes, not a keyspace walk)
and registers any index it finds missing, and drops a name with neither metadata nor an
index — so the registry heals itself if a write ever slips past it. A MongoDB listing
does not show a name with neither metadata nor chunks (an instance reset away), and
leaves it in the registry: removing it there could race an ingest that is writing that
instance's first chunks.
"""
import logging

log = logging.getLogger(__name__)

REDIS_KEY = "visualweaver:rag_instances"
MONGO_COLLECTION = "vw_instances"
BUILT = ":built"


def _decode(v) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


# ── Redis ────────────────────────────────────────────────────────────────────

def redis_add(rc, *names: str) -> None:
    names = [n for n in names if n]
    if names:
        rc.sadd(REDIS_KEY, *names)


def redis_remove(rc, *names: str) -> None:
    names = [n for n in names if n]
    if names:
        rc.srem(REDIS_KEY, *names)


def _scan_meta_names(rc) -> set:
    found = {_decode(k)[len("rag_meta:"):] for k in rc.scan_iter("rag_meta:*", count=500)}
    found.discard("")
    return found


def redis_names(rc) -> set:
    """The registered instance names on this connection (one SMEMBERS). Where the store
    has never had a complete registry, it is built from one scan of the metadata keys
    and marked complete; names registered meanwhile are kept.

    A store the registry cannot be read from or written to (another type under the key,
    a read-only replica) is answered from the scan, as before the registry existed."""
    try:
        members = {_decode(m) for m in rc.smembers(REDIS_KEY)}
    except Exception as e:
        log.debug(f"instance registry unreadable, scanning instead: {e}")
        return _scan_meta_names(rc)
    if BUILT not in members:
        found = _scan_meta_names(rc)
        try:
            rc.sadd(REDIS_KEY, *found, BUILT)
            log.info(f"Instance registry built from a scan: {len(found)} instance(s)")
        except Exception as e:
            log.debug(f"instance registry could not be written: {e}")
        members |= found
    members.discard(BUILT)
    return members


# ── MongoDB ──────────────────────────────────────────────────────────────────

def mongo_add(ep, *names: str) -> None:
    from pymongo.operations import UpdateOne  # noqa: PLC0415
    ops = [UpdateOne({"_id": n}, {"$setOnInsert": {"registered": True}}, upsert=True)
           for n in names if n]
    if ops:
        ep.col(MONGO_COLLECTION).bulk_write(ops, ordered=False)


def mongo_remove(ep, *names: str) -> None:
    names = [n for n in names if n]
    if names:
        ep.col(MONGO_COLLECTION).delete_many({"_id": {"$in": names}})


def mongo_names(ep, chunks: str, meta: str) -> set:
    """The registered instance names on this MongoDB database, building the registry
    from the ``chunks`` and ``meta`` collections the first time."""
    col = ep.col(MONGO_COLLECTION)
    members = {d["_id"] for d in col.find({}, {"_id": 1})}
    if BUILT not in members:
        found = set(ep.col(chunks).distinct("instance"))
        found |= {d["_id"] for d in ep.col(meta).find({}, {"_id": 1})}
        found.discard("")
        try:
            mongo_add(ep, *found, BUILT)
            log.info(f"Instance registry built on MongoDB '{ep.name}': {len(found)} instance(s)")
        except Exception as e:                    # no write access: list from the scan
            log.debug(f"instance registry on MongoDB '{ep.name}' could not be written: {e}")
        members |= found
    members.discard(BUILT)
    return members
