# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.mongo_store — RAG instance storage on MongoDB (self-hosted or Atlas).

This module holds the RAG instances on a MongoDB connection — the built-in MongoDB or
an added one. The app's own data (appstore.py) and the answer cache (mongo_cache.py)
use the same connection when ``storage.app`` names it. Every per-instance operation in
rag / rag_admin / ingest / the routes checks ``is_mongo(rc)`` first and comes here, so
the Redis code paths are unchanged.

Layout, in the connection's database (all instances share one set of collections, so
a cluster needs only two search indexes in total — Atlas's free tier allows three):

  vw_chunks    {_id: "<inst>:<chunk_id>", instance, chunk_id, text, source, doc_id,
                ingested_at, pos, emb_model, embedding_<dim>: [float, ...]}
  vw_meta      {_id: "<inst>", color, tags, created, enabled, redis_endpoint}
  vw_hashes    {_id: "<inst>\\x00<sha256>", instance}   per-source dedup hashes
  vw_urls      {_id: "<inst>\\x00<url>", instance, url}  crawler skip-list
  vw_counters  {_id: "<inst>", n}                        chunk-id allocator

Search indexes on vw_chunks:
  vw_vector  vectorSearch — embedding_<dim> (cosine) + filters on instance, source
  vw_text    search       — text (lucene.standard) + instance, source as tokens

$vectorSearch needs MongoDB Atlas, or MongoDB 8.3+ with the mongot search process
(Atlas Local / Community with search). A server without it is reported as such and
cannot host an instance.
"""
import base64
import hashlib
import logging
import re
import threading
import time
from urllib.parse import quote, unquote, urlsplit

import numpy as np
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import (BulkWriteError, ConfigurationError, OperationFailure, PyMongoError,
                            ServerSelectionTimeoutError)
from pymongo.operations import SearchIndexModel, UpdateOne

log = logging.getLogger(__name__)

CHUNKS, META, HASHES, URLS, COUNTERS = "vw_chunks", "vw_meta", "vw_hashes", "vw_urls", "vw_counters"
VEC_INDEX, TEXT_INDEX = "vw_vector", "vw_text"
DEFAULT_DB = "visualweaver"
# Code a mongod without the search process returns for any search-index command.
_NO_SEARCH_CODES = {31082}
NO_SEARCH_MSG = ("This MongoDB has no vector search, so it cannot hold a RAG instance. "
                 "Use MongoDB Atlas, or MongoDB 8.3+ with the search process (mongot), such as "
                 "the built-in MongoDB.")


class NoVectorSearch(Exception):
    """The connection's server cannot build search indexes (no mongot / not Atlas)."""


def is_no_search_error(e: Exception) -> bool:
    return isinstance(e, OperationFailure) and (
        e.code in _NO_SEARCH_CODES or "requires additional configuration" in str(e))


# A database name MongoDB will accept (see "Naming Restrictions" in the MongoDB manual).
_BAD_DB_CHARS = set('/\\. "$*<>:|?\x00')


def valid_database_name(name: str) -> bool:
    return bool(name) and len(name.encode()) < 64 and not (set(name) & _BAD_DB_CHARS)


# ── connection ───────────────────────────────────────────────────────────────

def split_uri_credentials(uri: str) -> tuple[str, str, str]:
    """Return (uri_without_credentials, username, password).

    A password pasted inside the connection string is moved out so it can be stored
    and redacted like every other secret; the stored URI never carries one.
    """
    uri = (uri or "").strip()
    # Greedy up to the LAST "@" that is followed by a host list: a password with an
    # unencoded "@" (p@ss) must not leave half of itself in the stored URI.
    m = re.match(r"^(mongodb(?:\+srv)?://)(.*)@([^@/?]+(?:[/?].*)?)$", uri)
    if not m:
        return uri, "", ""
    user, _, pw = m.group(2).partition(":")
    return m.group(1) + m.group(3), unquote(user), unquote(pw)


def server_uri(value: str) -> str:
    """The connection string for what the Server field holds.

    A full mongodb:// or mongodb+srv:// string is kept as it is. A bare server is built
    into one: an Atlas cluster host (*.mongodb.net, no port) gets mongodb+srv://, as in
    the string Atlas shows; anything else (host, host:port, a host list) gets mongodb://.
    """
    v = (value or "").strip()
    if not v or "://" in v:
        return v
    hosts, sep, rest = v.partition("/")
    tail = (sep + rest) if sep else "/"
    first = hosts.rpartition("@")[2]
    srv = "," not in first and ":" not in first and first.lower().endswith(".mongodb.net")
    return ("mongodb+srv://" if srv else "mongodb://") + hosts + tail


def is_password_placeholder(pw: str) -> bool:
    """True for the <db_password> placeholder Atlas puts in the connection string it shows."""
    return bool(re.fullmatch(r"<[^<>]*>", (pw or "").strip()))


PLACEHOLDER_MSG = ("The connection string still holds the <db_password> placeholder. "
                   "Type the database user's password in the Password field.")


def friendly_error(e: Exception, uri: str = "") -> str:
    """A connection error in words a user can act on; the driver's text otherwise."""
    msg = str(e).split(", full error")[0]
    atlas = "mongodb.net" in (uri or "")
    if isinstance(e, OperationFailure) and (e.code in (18, 8000) or "auth" in msg.lower()):
        return ("Login failed: check the username and password"
                + (" (Atlas: Security → Database Access)." if atlas else "."))
    if isinstance(e, ConfigurationError) and ("DNS" in msg or "_mongodb._tcp" in msg):
        return f"Could not find the cluster address ({display_host(uri) or 'host'}). Check the connection string."
    if isinstance(e, ServerSelectionTimeoutError):
        return ("Could not reach the cluster in time. In Atlas, Network Access must allow "
                "this machine's IP address." if atlas else
                f"Could not reach {display_host(uri) or 'the server'} in time.")
    return msg[:300]


def display_host(uri: str) -> str:
    """Host part of a connection string, for listings (no scheme, no credentials)."""
    try:
        rest = split_uri_credentials(uri)[0].split("://", 1)[1]
        return rest.split("/", 1)[0].split("?", 1)[0]
    except Exception:
        return ""


def _client_kwargs(cfg: dict) -> dict:
    kw = dict(serverSelectionTimeoutMS=5000, connectTimeoutMS=5000,
              socketTimeoutMS=30000, appname="VisualWeaver")
    uri = cfg.get("uri") or ""
    if cfg.get("username"):
        kw["username"] = cfg["username"]
    if cfg.get("password"):
        kw["password"] = cfg["password"]
    # A plain single-host mongodb:// string (a local container, a lone server) is a
    # direct connection; without it the driver tries to reach the replica-set member
    # names the server reports, which only resolve inside its own network.
    try:
        parts = urlsplit(uri)
        if (parts.scheme == "mongodb" and "," not in parts.netloc
                and not re.search(r"(replicaSet|directConnection)=", parts.query or "")):
            kw["directConnection"] = True
    except Exception:
        pass
    return kw


class MongoEndpoint:
    """One configured MongoDB connection. Passed where the Redis code passes `rc`."""
    is_mongo = True

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.cfg = dict(cfg)
        self.client = MongoClient(cfg.get("uri") or "mongodb://localhost:27017", **_client_kwargs(cfg))
        self.db = self.client[(cfg.get("database") or DEFAULT_DB).strip() or DEFAULT_DB]

    def col(self, name: str):
        return self.db[name]

    def ping(self):
        self.client.admin.command("ping")
        return True

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass


def is_mongo(rc) -> bool:
    return bool(getattr(rc, "is_mongo", False))


def probe(cfg: dict, ep: "MongoEndpoint | None" = None) -> dict:
    """Connect and report what the server can do. Never raises.

    {ok, version, vector_search, atlas, error}: vector_search is False when the server
    has no search process (error 31082) — such a connection cannot host an instance.
    Pass ``ep`` to probe through an existing (cached) connection; otherwise a temporary
    client is opened and closed.
    """
    own = ep is None
    try:
        if own:
            ep = MongoEndpoint(cfg.get("name") or "probe", cfg)
        info = ep.client.server_info()
        out = {"ok": True, "version": info.get("version", "?"), "vector_search": False,
               "atlas": "mongodb.net" in (cfg.get("uri") or ""), "error": None}
        try:
            list(ep.col(CHUNKS).list_search_indexes())
            out["vector_search"] = True
        except OperationFailure as e:
            if is_no_search_error(e):
                out["error"] = NO_SEARCH_MSG
            else:
                out["error"] = f"Search indexes unavailable: {e.details.get('errmsg', e) if e.details else e}"
        return out
    except PyMongoError as e:
        return {"ok": False, "version": None, "vector_search": False, "atlas": False,
                "error": friendly_error(e, cfg.get("uri") or "")}
    except Exception as e:
        return {"ok": False, "version": None, "vector_search": False, "atlas": False, "error": str(e)[:300]}
    finally:
        if own and ep:
            ep.close()


# ── indexes ──────────────────────────────────────────────────────────────────

_ensured: dict[tuple, bool] = {}
_ensure_lock = threading.Lock()


def _vector_definition(field: str, dims: int) -> dict:
    return {"fields": [
        {"type": "vector", "path": field, "numDimensions": int(dims), "similarity": "cosine"},
        {"type": "filter", "path": "instance"},
        {"type": "filter", "path": "source"},
    ]}


_TEXT_DEFINITION = {"mappings": {"dynamic": False, "fields": {
    "text": {"type": "string", "analyzer": "lucene.standard"},
    "instance": {"type": "token"},
    "source": {"type": "token"},
}}}


def ensure_indexes(ep: MongoEndpoint, field: str, dims: int) -> None:
    """Create (or retarget) the collection's indexes for the current embedding model.

    Idempotent and cached per (endpoint, field). A model change moves the vector
    index to the new embedding_<dim> path; vectors stored under the old width stay in
    the documents but are no longer searched until the instance is re-ingested.
    Building is asynchronous on the server: search answers once it is queryable.
    """
    key = (ep.name, ep.db.name, field, int(dims))
    if _ensured.get(key):
        return
    with _ensure_lock:
        if _ensured.get(key):
            return
        col = ep.col(CHUNKS)
        try:
            col.create_index([("instance", 1), ("chunk_id", 1)])
            col.create_index([("instance", 1), ("source", 1)])
            ep.col(HASHES).create_index([("instance", 1)])
            ep.col(URLS).create_index([("instance", 1)])
        except PyMongoError as e:
            log.warning(f"Mongo '{ep.name}': could not create regular indexes: {e}")
        try:
            if not col.database.list_collection_names(filter={"name": CHUNKS}):
                col.database.create_collection(CHUNKS)
            existing = {i["name"]: i for i in col.list_search_indexes()}
            vec_def = _vector_definition(field, dims)
            if VEC_INDEX in existing:
                cur = (existing[VEC_INDEX].get("latestDefinition") or {}).get("fields") or []
                vec = next((f for f in cur if f.get("type") == "vector"), {})
                if vec.get("path") != field or int(vec.get("numDimensions") or 0) != int(dims):
                    # A model change: rebuild the vector index for the new embedding field.
                    # Dropped and recreated rather than updated — the server rejects an
                    # in-place update of a vectorSearch definition. The drop is asynchronous,
                    # so wait for the name to be free before creating it again.
                    col.drop_search_index(VEC_INDEX)
                    for _ in range(240):
                        if VEC_INDEX not in {i["name"] for i in col.list_search_indexes()}:
                            break
                        time.sleep(0.25)
                    existing.pop(VEC_INDEX, None)
                    log.info(f"Mongo '{ep.name}': vector index rebuilt for {field} ({dims} dims)")
            if VEC_INDEX not in existing:
                col.create_search_index(SearchIndexModel(name=VEC_INDEX, type="vectorSearch", definition=vec_def))
            if TEXT_INDEX not in existing:
                col.create_search_index(SearchIndexModel(name=TEXT_INDEX, type="search", definition=_TEXT_DEFINITION))
        except OperationFailure as e:
            if is_no_search_error(e):
                raise NoVectorSearch(NO_SEARCH_MSG) from e
            raise
        _ensured[key] = True


def forget_indexes(ep_name: str | None = None) -> None:
    """Drop the ensure cache (after an embedding-model change or a reconnect)."""
    for k in list(_ensured):
        if ep_name is None or k[0] == ep_name:
            _ensured.pop(k, None)


def indexes_ready(ep: MongoEndpoint) -> dict:
    """{vector: bool, text: bool} — whether each search index answers queries yet."""
    try:
        idx = {i["name"]: i for i in ep.col(CHUNKS).list_search_indexes()}
    except PyMongoError:
        return {"vector": False, "text": False}
    return {"vector": bool(idx.get(VEC_INDEX, {}).get("queryable")),
            "text": bool(idx.get(TEXT_INDEX, {}).get("queryable"))}


# ── instance metadata ────────────────────────────────────────────────────────

def get_meta(ep: MongoEndpoint, inst: str) -> dict | None:
    doc = ep.col(META).find_one({"_id": inst})
    if not doc:
        return None
    doc.pop("_id", None)
    return doc


def set_meta(ep: MongoEndpoint, inst: str, meta: dict) -> None:
    body = {k: v for k, v in meta.items() if k != "_id"}
    ep.col(META).replace_one({"_id": inst}, body, upsert=True)


def delete_meta(ep: MongoEndpoint, inst: str) -> None:
    ep.col(META).delete_one({"_id": inst})


def list_instances(ep: MongoEndpoint) -> list[dict]:
    """[{name, chunks, color, tags, created, enabled}] for every instance on this endpoint."""
    # distinct() and count_documents() on "instance" both answer from the
    # (instance, chunk_id) index; grouping the collection would read every chunk,
    # embeddings included, on each listing (the UI lists every 30 s).
    col = ep.col(CHUNKS)
    metas = {d["_id"]: d for d in ep.col(META).find({})}
    names = set(col.distinct("instance")) | set(metas)
    out = []
    for name in sorted(names):
        m = metas.get(name, {})
        out.append({"name": name, "chunks": int(col.count_documents({"instance": name})),
                    "color": m.get("color", "#6366f1"), "tags": m.get("tags", []),
                    "created": m.get("created", ""), "enabled": m.get("enabled", True)})
    return out


# ── writes ───────────────────────────────────────────────────────────────────

def reserve_ids(ep: MongoEndpoint, inst: str, n: int) -> int:
    """Reserve n sequential chunk ids; returns the first (same contract as INCRBY)."""
    doc = ep.col(COUNTERS).find_one_and_update(
        {"_id": inst}, {"$inc": {"n": int(n)}}, upsert=True, return_document=ReturnDocument.AFTER)
    return int(doc["n"]) - int(n)


def add_hashes(ep: MongoEndpoint, inst: str, hashes: list[str]) -> list[bool]:
    """Insert dedup hashes; returns, per hash, True if it was new (SADD's 1/0)."""
    if not hashes:
        return []
    docs = [{"_id": f"{inst}\x00{h}", "instance": inst} for h in hashes]
    failed: set[int] = set()
    try:
        ep.col(HASHES).insert_many(docs, ordered=False)
    except BulkWriteError as e:
        for we in e.details.get("writeErrors", []):
            if we.get("code") == 11000:
                failed.add(we["index"])
            else:
                raise
    return [i not in failed for i in range(len(hashes))]


def remove_hashes(ep: MongoEndpoint, inst: str, hashes: list[str]) -> None:
    if hashes:
        ep.col(HASHES).delete_many({"_id": {"$in": [f"{inst}\x00{h}" for h in hashes]}})


def get_urls(ep: MongoEndpoint, inst: str) -> set[str]:
    return {d["url"] for d in ep.col(URLS).find({"instance": inst}, {"url": 1})}


def add_urls(ep: MongoEndpoint, inst: str, urls) -> None:
    ops = [UpdateOne({"_id": f"{inst}\x00{u}"}, {"$set": {"instance": inst, "url": u}}, upsert=True)
           for u in urls]
    if ops:
        ep.col(URLS).bulk_write(ops, ordered=False)


def clear_urls(ep: MongoEndpoint, inst: str, url: str | None = None) -> None:
    if url is None:
        ep.col(URLS).delete_many({"instance": inst})
    else:
        ep.col(URLS).delete_one({"_id": f"{inst}\x00{url}"})


def add_chunks(ep: MongoEndpoint, inst: str, records: list[dict], field: str,
               embeddings, emb_model: int, doc_id_for) -> None:
    ops = []
    for ch, emb in zip(records, embeddings):
        src = ch.get("source", "")
        doc = {
            "instance": inst, "chunk_id": int(ch["id"]), "text": ch["text"], "source": src,
            "doc_id": doc_id_for(src), "ingested_at": int(ch.get("ingested_at", time.time())),
            "pos": int(ch.get("pos", 0)), "emb_model": int(emb_model),
            field: [float(x) for x in np.asarray(emb, dtype=np.float32)],
        }
        ops.append(UpdateOne({"_id": f"{inst}:{int(ch['id'])}"}, {"$set": doc}, upsert=True))
    if ops:
        ep.col(CHUNKS).bulk_write(ops, ordered=False)


def reset_instance(ep: MongoEndpoint, inst: str) -> int:
    n = ep.col(CHUNKS).delete_many({"instance": inst}).deleted_count
    ep.col(HASHES).delete_many({"instance": inst})
    ep.col(URLS).delete_many({"instance": inst})
    ep.col(COUNTERS).delete_one({"_id": inst})
    return n


# ── search ───────────────────────────────────────────────────────────────────

def _matching_sources(ep: MongoEndpoint, inst: str, fragment: str) -> list[str]:
    """Sources of this instance that CONTAIN fragment literally (the Redis infix filter)."""
    return ep.col(CHUNKS).distinct("source", {"instance": inst,
                                             "source": {"$regex": re.escape(fragment)}})


# A search index that cannot answer yet. Atlas answers a query against it with no
# results; MongoDB's self-managed search (mongot) fails it instead — "cannot query
# vector index … while in state INITIAL_SYNC", on a brand-new collection "… while in
# state UNKNOWN", and right after creation "Index <name> not initialized". Every such
# state means "not built yet" except FAILED, which will not recover and must surface.
_NOT_QUERYABLE = re.compile(r"cannot query (?:vector |search )?index .* while in state (\w+)"
                            r"|Index \S+ not initialized")


def _is_index_building(e: Exception) -> bool:
    if not isinstance(e, OperationFailure):
        return False
    m = _NOT_QUERYABLE.search(str(e))
    return bool(m) and m.group(1) != "FAILED"


def _aggregate(col, pipeline: list) -> list:
    """Run a search pipeline; an index still being built yields no rows, as on Atlas."""
    try:
        return list(col.aggregate(pipeline))
    except OperationFailure as e:
        if _is_index_building(e):
            log.debug(f"search index on {col.name} still building — no results yet")
            return []
        raise


def search_legs(ep: MongoEndpoint, inst: str, q_emb, keywords: list[str], fetch_k: int,
                field: str, hybrid: bool, source_filter: str = "") -> tuple[list, list]:
    """Run the vector and (hybrid) keyword legs. Returns (vec_rows, text_rows) shaped like
    the Redis legs: {_key, text, source, _vec_score} and {_key, text, source, _vec_score, _bm25}.

    The vector score is converted back to plain cosine: Atlas reports (1 + cos) / 2.
    """
    pre: dict = {"instance": inst}
    sources = None
    if source_filter:
        sources = _matching_sources(ep, inst, source_filter.replace("\x00", ""))
        if not sources:
            return [], []
        pre = {"$and": [{"instance": inst}, {"source": {"$in": sources}}]}
    col = ep.col(CHUNKS)
    vec_rows = []
    for d in _aggregate(col, [
        {"$vectorSearch": {"index": VEC_INDEX, "path": field,
                           "queryVector": [float(x) for x in np.asarray(q_emb, dtype=np.float32)],
                           "numCandidates": max(100, fetch_k * 20), "limit": fetch_k, "filter": pre}},
        {"$project": {"text": 1, "source": 1, "score": {"$meta": "vectorSearchScore"}}},
    ]):
        vec_rows.append({"_key": d["_id"], "text": d.get("text", ""), "source": d.get("source", ""),
                         "_vec_score": round(2.0 * float(d.get("score", 0.5)) - 1.0, 4)})
    text_rows = []
    if hybrid and keywords:
        filt = [{"equals": {"path": "instance", "value": inst}}]
        if sources is not None:
            filt.append({"in": {"path": "source", "value": sources}})
        for d in _aggregate(col, [
            {"$search": {"index": TEXT_INDEX, "compound": {
                "filter": filt, "should": [{"text": {"query": keywords, "path": "text"}}],
                "minimumShouldMatch": 1}}},
            {"$limit": fetch_k},
            {"$project": {"text": 1, "source": 1, "score": {"$meta": "searchScore"}}},
        ]):
            text_rows.append({"_key": d["_id"], "text": d.get("text", ""), "source": d.get("source", ""),
                              "_vec_score": 0.0, "_bm25": float(d.get("score", 0.0))})
    return vec_rows, text_rows


def vectors_for(ep: MongoEndpoint, keys: list[str], field: str) -> dict:
    """{_id: np.ndarray} for the given chunk ids (cosine backfill of keyword-only hits)."""
    out = {}
    for d in ep.col(CHUNKS).find({"_id": {"$in": list(keys)}}, {field: 1}):
        v = d.get(field)
        if v:
            out[d["_id"]] = np.asarray(v, dtype=np.float32)
    return out


# ── listing / documents ──────────────────────────────────────────────────────

def sources(ep: MongoEndpoint, inst: str) -> list[str]:
    return sorted(ep.col(CHUNKS).distinct("source", {"instance": inst}))


def documents(ep: MongoEndpoint, inst: str) -> list[dict]:
    return [{"source": d["_id"], "chunks": int(d["n"]), "ingested_at": int(d.get("t") or 0)}
            for d in ep.col(CHUNKS).aggregate([
                {"$match": {"instance": inst}},
                {"$group": {"_id": "$source", "n": {"$sum": 1}, "t": {"$max": "$ingested_at"}}}])]


def delete_document(ep: MongoEndpoint, inst: str, source: str) -> tuple[int, list[str]]:
    """Delete one document's chunks. Returns (deleted, texts) so the caller can release
    the source-scoped dedup hashes exactly as the Redis path does."""
    texts = [d.get("text", "") for d in ep.col(CHUNKS).find({"instance": inst, "source": source}, {"text": 1})]
    n = ep.col(CHUNKS).delete_many({"instance": inst, "source": source}).deleted_count
    return n, texts


def chunks_page(ep: MongoEndpoint, inst: str, offset: int, limit: int) -> list[dict]:
    return [{"id": d.get("chunk_id"), "text": (d.get("text") or "")[:200], "source": d.get("source", "")}
            for d in ep.col(CHUNKS).find({"instance": inst}, {"chunk_id": 1, "text": 1, "source": 1})
            .sort("chunk_id", 1).skip(int(offset)).limit(int(limit))]


def embedding_models(ep: MongoEndpoint, inst: str) -> list[tuple[int, int]]:
    return [(int(d["_id"] if d["_id"] is not None else -1), int(d["n"])) for d in ep.col(CHUNKS).aggregate([
        {"$match": {"instance": inst}}, {"$group": {"_id": "$emb_model", "n": {"$sum": 1}}}])]


def optimize(ep: MongoEndpoint, inst: str) -> tuple[int, int, int]:
    """Remove chunks whose normalised text repeats one already kept (same global hash the
    Redis path uses). Returns (removed, remaining, total_before)."""
    seen, dup = set(), []
    total = 0
    for d in ep.col(CHUNKS).find({"instance": inst}, {"text": 1}).sort("chunk_id", 1):
        total += 1
        h = hashlib.sha256(" ".join((d.get("text") or "").lower().split()).encode()).hexdigest()
        if h in seen:
            dup.append(d["_id"])
        else:
            seen.add(h)
    for i in range(0, len(dup), 500):
        ep.col(CHUNKS).delete_many({"_id": {"$in": dup[i:i + 500]}})
    return len(dup), total - len(dup), total


def iter_chunks(ep: MongoEndpoint, inst: str, field: str):
    """Export rows in the Redis export format: {id, text, source, embedding_b64}."""
    for d in ep.col(CHUNKS).find({"instance": inst}).sort("chunk_id", 1):
        v = d.get(field)
        if v is None:
            v = next((d[k] for k in d if k.startswith("embedding")), None)
        yield {"id": d.get("chunk_id"), "text": d.get("text", ""), "source": d.get("source", ""),
               "embedding_b64": base64.b64encode(np.asarray(v, dtype=np.float32).tobytes()).decode()
               if v is not None else ""}


def import_rows(ep: MongoEndpoint, inst: str, rows: list[dict], field: str, emb_model: int, doc_id_for) -> int:
    """Write imported chunks: [{id, text, source, vector(np.ndarray)}]. Returns max id."""
    ops, max_id = [], -1
    now = int(time.time())
    for r in rows:
        cid = int(r["id"])
        max_id = max(max_id, cid)
        ops.append(UpdateOne({"_id": f"{inst}:{cid}"}, {"$set": {
            "instance": inst, "chunk_id": cid, "text": r.get("text", ""), "source": r.get("source", ""),
            "doc_id": doc_id_for(r.get("source", "")), "ingested_at": now, "pos": 0, "emb_model": int(emb_model),
            field: [float(x) for x in np.asarray(r["vector"], dtype=np.float32)]}}, upsert=True))
        if len(ops) >= 500:
            ep.col(CHUNKS).bulk_write(ops, ordered=False); ops = []
    if ops:
        ep.col(CHUNKS).bulk_write(ops, ordered=False)
    if max_id >= 0:
        ep.col(COUNTERS).update_one({"_id": inst}, {"$max": {"n": max_id + 1}}, upsert=True)
    return max_id


def stats(ep: MongoEndpoint) -> dict:
    s = ep.db.command("dbStats")
    return {"data_size": int(s.get("dataSize", 0)), "storage_size": int(s.get("storageSize", 0)),
            "objects": int(s.get("objects", 0))}


def credentials_for_display(uri: str, username: str) -> str:
    """mongodb+srv://user@host — what a listing shows (never a password)."""
    base, _, _ = split_uri_credentials(uri)
    if not username:
        return base
    scheme, rest = base.split("://", 1)
    return f"{scheme}://{quote(username)}@{rest}"
