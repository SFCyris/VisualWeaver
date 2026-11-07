# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.mongo_cache — the answer cache on MongoDB.

Used by cache.py when the app's data store (``storage.app``) is a MongoDB connection.
``MongoSemanticCache`` has the part of redisvl's SemanticCache that cache.py uses —
check / store / clear, distance_threshold / set_threshold, ttl / set_ttl — with the
same semantics, so a question gets the same answer on either store:

  * entry id     redisvl's hashify(prompt, filters), so storing the same question under
                 the same scope replaces the entry, as on Redis
  * distance     cosine distance (1 − cos); a hit is the nearest entry within the
                 distance threshold. mongot reports (1 + cos) / 2, converted back here.
  * search       exact nearest neighbour (redisvl builds its cache index FLAT)
  * filters      scope and qhash by equality — the Redis Tag filters
  * TTL          ``exp`` per entry: a TTL index removes expired entries (about once a
                 minute) and every read filters on ``exp`` itself; a hit refreshes it,
                 as redisvl's check() does

One collection and one search index, in the connection's database:

  vw_cache         {_id: entry_id, entry_id, prompt, response, embedding: [float],
                    scope, qhash, metadata, inserted_at, updated_at, exp: date}
  vw_cache_vector  vectorSearch — embedding (cosine) + filters scope, qhash, exp
"""
from __future__ import annotations

import datetime as _dt
import logging
import threading
import time
from collections import OrderedDict

import numpy as np
from pymongo import WriteConcern
from pymongo.errors import OperationFailure, PyMongoError
from pymongo.operations import SearchIndexModel
from redisvl.redis.utils import hashify

from . import mongo_store

log = logging.getLogger(__name__)

COLLECTION = "vw_cache"
VEC_INDEX = "vw_cache_vector"
VEC_FIELD = "embedding"
# An entry stored with no TTL. A date rather than null: $vectorSearch filters compare it
# like any other, and the TTL monitor never reaches it.
NO_EXPIRY = _dt.datetime(9999, 12, 31, tzinfo=_dt.timezone.utc)


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _exp_for(ttl) -> _dt.datetime:
    return _now() + _dt.timedelta(seconds=int(ttl)) if ttl else NO_EXPIRY


def _aware(d: _dt.datetime) -> _dt.datetime:
    return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)


def _remaining(exp) -> int:
    """Seconds left, Redis TTL style: -1 for an entry that never expires."""
    if not isinstance(exp, _dt.datetime):
        return -1
    exp = _aware(exp)
    if exp >= NO_EXPIRY - _dt.timedelta(days=1):
        return -1
    return max(0, int((exp - _now()).total_seconds()))


# ── indexes ──────────────────────────────────────────────────────────────────

def vector_definition(dims: int) -> dict:
    return {"fields": [
        {"type": "vector", "path": VEC_FIELD, "numDimensions": int(dims), "similarity": "cosine"},
        {"type": "filter", "path": "scope"},
        {"type": "filter", "path": "qhash"},
        {"type": "filter", "path": "exp"},
    ]}


_ensure_lock = threading.Lock()
# (endpoint name, database) whose vector index is being recreated at another width in
# the background. Lookups there find nothing until it is back.
_rebuilding: set = set()


def _rebuild_key(ep) -> tuple:
    return (ep.name, ep.db.name)


def ensure_indexes(ep, dims: int) -> None:
    """Create the collection, its TTL index and the vector index for ``dims``.

    Idempotent, and remembered on the endpoint object: a settings save replaces every
    client, and the next build then checks the server again. An index built for another
    width (an embedding-model change) is dropped and recreated — the server rejects an
    in-place update of a vectorSearch definition. The drop completes asynchronously, so
    the recreate waits for it in a background thread instead of in the question that
    triggered it. Building is asynchronous too: until the index answers, lookups find
    nothing.
    """
    dims = int(dims)
    if getattr(ep, "_vw_cache_dims", None) == dims:
        return
    with _ensure_lock:
        if getattr(ep, "_vw_cache_dims", None) == dims:
            return
        col = ep.col(COLLECTION)
        try:
            if not col.database.list_collection_names(filter={"name": COLLECTION}):
                col.database.create_collection(COLLECTION)
        except OperationFailure as e:
            if e.code != 48:                       # NamespaceExists: created concurrently
                raise
        col.create_index("exp", expireAfterSeconds=0)
        try:
            existing = {i["name"]: i for i in col.list_search_indexes()}
            if VEC_INDEX in existing and _definition_dims(existing[VEC_INDEX]) != dims:
                if _rebuild_key(ep) not in _rebuilding:
                    col.drop_search_index(VEC_INDEX)
                    _rebuilding.add(_rebuild_key(ep))
                    threading.Thread(target=_recreate, args=(ep, dims), daemon=True,
                                     name="vw-cache-index").start()
                return
            if VEC_INDEX not in existing:
                col.create_search_index(SearchIndexModel(name=VEC_INDEX, type="vectorSearch",
                                                         definition=vector_definition(dims)))
        except OperationFailure as e:
            if mongo_store.is_no_search_error(e):
                raise mongo_store.NoVectorSearch(mongo_store.NO_SEARCH_MSG) from e
            raise
        ep._vw_cache_dims = dims


def _recreate(ep, dims: int, timeout: float = 120.0) -> None:
    """Wait for the dropped index's name to be free, then create it at ``dims``."""
    key = _rebuild_key(ep)
    try:
        col = ep.col(COLLECTION)
        t0 = time.time()
        while VEC_INDEX in {i["name"] for i in col.list_search_indexes()}:
            if time.time() - t0 > timeout:
                raise TimeoutError(f"the previous index was still being dropped after {timeout:.0f} s")
            time.sleep(0.25)
        col.create_search_index(SearchIndexModel(name=VEC_INDEX, type="vectorSearch",
                                                 definition=vector_definition(dims)))
        ep._vw_cache_dims = dims
        log.info(f"Mongo '{ep.name}': answer-cache index rebuilt for {dims} dims")
    except Exception as e:
        log.warning(f"Mongo '{ep.name}': could not rebuild the answer-cache index: {e}")
    finally:
        _rebuilding.discard(key)


def forget_indexes(ep) -> None:
    """Check the indexes again on the next build (after the entries were discarded)."""
    ep._vw_cache_dims = None


def _definition_dims(idx: dict) -> int | None:
    fields = (idx.get("latestDefinition") or idx.get("definition") or {}).get("fields") or []
    vec = next((f for f in fields if f.get("type") == "vector"), None)
    try:
        return int(vec["numDimensions"]) if vec else None
    except (TypeError, ValueError, KeyError):
        return None


def index_dims(ep) -> int | None:
    """The vector width of the existing cache index, or None if there is none."""
    try:
        for i in ep.col(COLLECTION).list_search_indexes(VEC_INDEX):
            return _definition_dims(i)
    except PyMongoError:
        return None
    return None


def index_ready(ep) -> bool:
    try:
        return any(i.get("queryable") for i in ep.col(COLLECTION).list_search_indexes(VEC_INDEX))
    except PyMongoError:
        return False


# ── the cache object ─────────────────────────────────────────────────────────

class MongoSemanticCache:
    """The subset of redisvl's SemanticCache that cache.py uses, on a MongoDB connection."""
    kind = "mongodb"

    def __init__(self, ep, vectorizer, distance_threshold: float = 0.1, ttl: int | None = None):
        self.ep = ep
        self.col = ep.col(COLLECTION)
        self._vectorizer = vectorizer
        self.set_threshold(distance_threshold)
        self._ttl = int(ttl) if ttl else None
        self._vectors: OrderedDict = OrderedDict()
        self._vlock = threading.Lock()
        ensure_indexes(ep, int(vectorizer.dims))

    # settings that can change on a live object (cache._apply_cache_config)
    @property
    def distance_threshold(self) -> float:
        return self._distance_threshold

    def set_threshold(self, distance_threshold: float) -> None:
        if not 0 <= float(distance_threshold) <= 2:
            raise ValueError(f"Distance must be between 0 and 2, got {distance_threshold}")
        self._distance_threshold = float(distance_threshold)

    @property
    def ttl(self) -> int | None:
        return self._ttl

    def set_ttl(self, ttl: int | None = None) -> None:
        if ttl and not isinstance(ttl, int):
            raise ValueError(f"TTL must be an integer value, got {ttl}")
        self._ttl = int(ttl) if ttl else None

    def _vectorize(self, prompt: str) -> list[float]:
        """The prompt's vector. A miss is followed by a store of the same prompt, so the
        last few are kept rather than embedded a second time."""
        with self._vlock:
            v = self._vectors.get(prompt)
            if v is not None:
                self._vectors.move_to_end(prompt)
                return v
        v = [float(x) for x in np.asarray(self._vectorizer.embed(prompt), dtype=np.float32)]
        with self._vlock:
            self._vectors[prompt] = v
            while len(self._vectors) > 256:
                self._vectors.popitem(last=False)
        return v

    def check(self, prompt: str, num_results: int = 1, filters: dict | None = None,
              distance_threshold: float | None = None) -> list[dict]:
        """Entries nearest to ``prompt`` within the distance threshold, nearest first.

        One aggregate: exact nearest neighbour, pre-filtered on the filters and on expiry,
        and expiry checked again on the documents themselves.
        Hits are shaped like redisvl's (entry_id, prompt, response, vector_distance,
        inserted_at, updated_at, metadata, the filter fields, key).
        """
        dist_max = distance_threshold or self._distance_threshold
        vec = self._vectorize(prompt)
        if _rebuild_key(self.ep) in _rebuilding:
            return []
        now = _now()
        pre = [{k: {"$eq": v}} for k, v in (filters or {}).items()] + [{"exp": {"$gt": now}}]
        rows = mongo_store._aggregate(self.col, [
            {"$vectorSearch": {"index": VEC_INDEX, "path": VEC_FIELD, "queryVector": vec,
                               # one spare: the nearest may turn out expired below
                               "exact": True, "limit": int(num_results) + 1,
                               "filter": {"$and": pre}}},
            {"$project": {VEC_FIELD: 0, "score": {"$meta": "vectorSearchScore"}}},
        ])
        hits = []
        for d in rows:
            dist = 2.0 - 2.0 * float(d.pop("score", 0.0))       # (1 + cos) / 2  →  1 − cos
            # The index's copy of exp can lag the document; the document's own is the truth.
            exp = d.pop("exp", None)
            if dist > dist_max or not isinstance(exp, _dt.datetime) or _aware(exp) <= now:
                continue
            eid = d.pop("_id")
            hit = {k: v for k, v in d.items() if v is not None}
            hit.update(entry_id=d.get("entry_id") or eid, vector_distance=max(0.0, dist), key=eid)
            hits.append(hit)
        hits = hits[:int(num_results)]
        if hits and self._ttl:
            # A hit keeps the entry alive, as redisvl's check() does. Unacknowledged, so
            # the lookup stays one round trip; losing it only means an earlier expiry.
            try:
                self.col.with_options(write_concern=WriteConcern(w=0)).update_many(
                    {"_id": {"$in": [h["key"] for h in hits]}, "exp": {"$gt": _now()}},
                    {"$set": {"exp": _exp_for(self._ttl)}})
            except PyMongoError as e:                             # pragma: no cover
                log.debug(f"cache TTL refresh failed: {e}")
        return hits

    def store(self, prompt: str, response: str, metadata: dict | None = None,
              filters: dict | None = None, ttl: int | None = None) -> str:
        """Store (or replace) the entry for ``prompt`` under ``filters``; returns its id."""
        if metadata is not None and not isinstance(metadata, dict):
            raise TypeError("Metadata must be a dictionary.")
        vec = self._vectorize(prompt)
        entry_id = hashify(prompt, filters)
        t = time.time()
        doc = {"_id": entry_id, "entry_id": entry_id, "prompt": prompt, "response": response,
               VEC_FIELD: vec, "inserted_at": t, "updated_at": t, "exp": _exp_for(ttl or self._ttl)}
        if metadata is not None:
            doc["metadata"] = metadata
        for k, v in (filters or {}).items():
            if k not in doc:
                doc[k] = v
        self.col.replace_one({"_id": entry_id}, doc, upsert=True)
        return entry_id

    def clear(self) -> None:
        """Remove every entry; the index stays."""
        self.col.delete_many({})


# ── what the cache analytics routes need ─────────────────────────────────────

def entries(ep) -> list[dict]:
    """[{query, ttl}] for every live entry, newest first (ttl −1 = never expires)."""
    out = []
    for d in (ep.col(COLLECTION).find({"exp": {"$gt": _now()}}, {"prompt": 1, "exp": 1, "inserted_at": 1})
              .sort("inserted_at", -1)):
        out.append({"query": d.get("prompt") or "", "ttl": _remaining(d.get("exp"))})
    return out


def delete_entry(ep, entry_id: str) -> bool:
    """True when a live entry was removed — an expired one counts as already gone, as on
    Redis (it is removed all the same)."""
    col = ep.col(COLLECTION)
    if col.delete_one({"_id": entry_id, "exp": {"$gt": _now()}}).deleted_count:
        return True
    col.delete_one({"_id": entry_id})
    return False


def clear(ep) -> int:
    """Remove every entry; returns how many were live."""
    col = ep.col(COLLECTION)
    n = int(col.delete_many({"exp": {"$gt": _now()}}).deleted_count)
    col.delete_many({})
    return n
