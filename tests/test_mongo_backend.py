# SPDX-License-Identifier: AGPL-3.0-or-later
"""RAG instances stored on MongoDB, against a real server with vector search.

The server is either the one named by VISUALWEAVER_TEST_MONGO_URI, or a throwaway copy
of the built-in MongoDB (visualweaver.local_mongo: the official Community server and
search images, set up exactly as install.sh does) removed with its data afterwards. Without either the
module is skipped — unless VISUALWEAVER_REQUIRE_MONGO_TESTS=1, which turns that into a
failure so CI cannot pass by silently not testing.

Route handlers are called directly (as test_workflow_backend does) so the app stays
pointed at the suite's private Redis for its own state.
"""
import asyncio
import io
import json
import os
import time

import pytest

from conftest import KEY_PREFIX, REDIS_DB, REDIS_HOST, REDIS_PORT

EP = "mongo-test"


# mongo_uri (a MongoDB with vector search, or skip) is in conftest.py, shared by every
# module that needs one.


@pytest.fixture
def mongo(app_module, cfg, clean_redis, mongo_uri):
    """The app with its own state on the test Redis and one MongoDB connection, EP.

    Searches record retrieval stats in the process-wide state._rag_stats, which the
    preset advice calibrates from — so they are restored afterwards, as the config is."""
    import copy
    from visualweaver import mongo_store, redis_store, rag_admin, state
    stats_before = copy.deepcopy(state._rag_stats)
    health_before = (dict(state._endpoint_health), dict(state._search_available))
    db = f"vwtest_{os.getpid()}_{int(time.time() * 1000) % 10**8}"
    cfg["redis"] = {"host": REDIS_HOST, "port": REDIS_PORT, "db": REDIS_DB, "password": "", "ssl": False}
    cfg["redis_endpoints"] = [{"name": EP, "type": "mongodb", "uri": mongo_uri, "database": db,
                               "username": "", "password": ""}]
    redis_store.invalidate_redis_clients()
    rag_admin.invalidate_rag_meta()
    ep = redis_store.r_for(EP)
    assert mongo_store.is_mongo(ep)
    yield ep
    try:
        ep.client.drop_database(db)
    finally:
        state._rag_stats.clear()
        state._rag_stats.update(stats_before)
        for d, before in zip((state._endpoint_health, state._search_available), health_before):
            d.clear(); d.update(before)
        redis_store.invalidate_redis_clients()
        rag_admin.invalidate_rag_meta()


def _create(name: str, endpoint: str = EP) -> dict:
    from visualweaver import routes_instances
    return asyncio.run(routes_instances.api_create_instance({"name": name, "endpoint": endpoint}))


def _ingest(inst: str, text: str, source: str) -> dict:
    from visualweaver import routes_ingestion
    return asyncio.run(routes_ingestion.api_ingest_text(inst, {"text": text, "source": source}))


def _search(inst: str, q: str, **kw) -> list:
    from visualweaver import rag, rag_admin
    return rag.search_rag(inst, q, top_k=kw.pop("top_k", 5), threshold=kw.pop("threshold", 0.0),
                          rc=rag_admin.rc_for_instance(inst), **kw)


def _until_legs_return(ep, inst: str, q_emb, field: str, keywords: list, source: str,
                       timeout: float = 120.0) -> None:
    """Poll a REAL query until it returns a document this test inserted — on the vector
    leg and, when ``keywords`` are given, the text leg too.

    Not the indexes' ``queryable`` flag: mongot reports it ~30 s after the index already
    answers, so waiting on it made every test sit idle — and it says nothing about the
    query the test runs next. These are the legs retrieval itself calls."""
    from pymongo.errors import OperationFailure
    from visualweaver import mongo_store
    got_vec = got_text = False
    last = None
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            vec, text = mongo_store.search_legs(ep, inst, q_emb, keywords, 10, field, bool(keywords))
        except OperationFailure as e:     # an index mid-rebuild can refuse outright: not ready
            last, vec, text = e, [], []
        got_vec = got_vec or any(r["source"] == source for r in vec)
        got_text = got_text or not keywords or any(r["source"] == source for r in text)
        if got_vec and got_text:
            return
        time.sleep(0.25)
    pytest.fail(f"'{inst}': no query returned {source!r} within {timeout:.0f}s "
                f"(vector index answered: {got_vec}, text index answered: {got_text}; "
                f"last error: {last})")


def _until_searchable(ep, inst: str, query: str, source: str, keyword_query: str = "",
                      keyword_source: str = "") -> None:
    """_until_legs_return for an ingested instance: the vector leg must return ``source``
    for ``query``, and the text leg ``keyword_source`` for ``keyword_query``."""
    import numpy as np
    from visualweaver import embeddings, textutil
    field = embeddings.vector_field_for()
    q = embeddings.embed(query, is_query=True).astype(np.float32)
    _until_legs_return(ep, inst, q, field, [], source)
    if keyword_query:
        _until_legs_return(ep, inst, q, field, textutil._keywords_for_bm25(keyword_query),
                           keyword_source or source)


def _search_until(inst: str, q: str, pred, timeout: float = 30.0, **kw) -> list:
    """mongot indexes asynchronously; poll until the expected hit shows up."""
    t0, res = time.time(), []
    while time.time() - t0 < timeout:
        res = _search(inst, q, **kw)
        if pred(res):
            return res
        time.sleep(0.5)
    return res


DOC_A = ("The semantic cache keeps an answer for cache.ttl seconds, one hour by default, "
         "after which the entry is evicted and the question is answered again.")
DOC_B = ("Replacement valve part number QX-7731 fits the 2019 boiler. Order it through the "
         "service portal; the older QX-7700 is discontinued.")


def test_probe_reports_vector_search(mongo, mongo_uri):
    from visualweaver import mongo_store
    p = mongo_store.probe({"uri": mongo_uri})
    assert p["ok"] and p["vector_search"] and p["version"], p


def test_an_instance_round_trips_on_mongodb(mongo):
    """Create → ingest → hybrid search (semantic and keyword-only) → documents →
    delete one document → re-ingest it → reset → delete instance."""
    from visualweaver import mongo_store, rag_admin, routes_instances, routes_sources
    inst = f"{KEY_PREFIX}mongo_rt"
    made = _create(inst)
    assert made["redis_endpoint"] == EP
    listed = [i for i in rag_admin.list_rag_instances() if i["name"] == inst]
    assert listed and listed[0]["endpoint_type"] == "mongodb" and listed[0]["redis_endpoint"] == EP

    a = _ingest(inst, DOC_A, "notes/cache.md")
    b = _ingest(inst, DOC_B, "parts/valves.pdf")
    assert a["chunks"] >= 1 and b["chunks"] >= 1
    assert _ingest(inst, DOC_A, "notes/cache.md")["duplicate"] is True      # dedup per source

    # Both legs answer a real query with these documents (not the lagging flag).
    _until_searchable(mongo, inst, "how long does a stored response stay before it expires?",
                      "notes/cache.md", "QX-7731", "parts/valves.pdf")
    sem = _search_until(inst, "how long does a stored response stay before it expires?",
                        lambda r: r and r[0]["source"] == "notes/cache.md")
    assert sem and sem[0]["source"] == "notes/cache.md", sem
    assert 0.0 < sem[0]["score"] <= 1.0                                    # plain cosine scale

    kw = _search_until(inst, "QX-7731", lambda r: any(c["source"] == "parts/valves.pdf" and c["lexical"] for c in r))
    hit = [c for c in kw if c["source"] == "parts/valves.pdf"]
    assert hit and hit[0]["lexical"] and hit[0]["score"] > 0, kw           # keyword leg + cosine backfill

    scoped = _search(inst, "boiler", source_filter="valves")
    assert scoped and {c["source"] for c in scoped} == {"parts/valves.pdf"}, scoped
    assert _search(inst, "boiler", source_filter="no-such-source") == []

    docs = routes_sources.api_rag_documents(inst)
    assert {d["source"] for d in docs["documents"]} == {"notes/cache.md", "parts/valves.pdf"}
    assert routes_sources.api_rag_sources(inst) == ["notes/cache.md", "parts/valves.pdf"]

    gone = routes_sources.api_delete_document(inst, "parts/valves.pdf")
    assert gone["deleted"] == b["chunks"]
    assert routes_sources.api_rag_sources(inst) == ["notes/cache.md"]
    again = _ingest(inst, DOC_B, "parts/valves.pdf")                        # hashes were released
    assert again["chunks"] == b["chunks"] and again["duplicate"] is False

    page = routes_instances.api_rag_chunks(inst, limit=10, offset=0)
    assert page and all("text" in c for c in page)
    models = routes_instances.api_instance_embedding_models(inst)
    assert models["models"] and models["models"][0]["chunks"] >= 2

    routes_instances.api_toggle_rag(inst, {"enabled": False})
    rag_admin.invalidate_rag_meta(inst)
    assert rag_admin.meta_get(mongo, inst)["enabled"] is False

    routes_instances.api_reset_rag(inst)
    assert mongo.col(mongo_store.CHUNKS).count_documents({"instance": inst}) == 0
    assert mongo.col(mongo_store.HASHES).count_documents({"instance": inst}) == 0
    routes_instances.api_delete_instance(inst)
    assert rag_admin.meta_get(mongo, inst) is None


def test_export_from_mongodb_imports_into_redis_and_back(mongo):
    """An export is backend-neutral: Mongo → Redis and Redis → Mongo both stay searchable."""
    from fastapi import UploadFile
    from visualweaver import mongo_store, routes_ingestion
    src_inst, red_inst, back_inst = (f"{KEY_PREFIX}mx_src", f"{KEY_PREFIX}mx_redis", f"{KEY_PREFIX}mx_back")
    _create(src_inst)
    _ingest(src_inst, DOC_A, "notes/cache.md")
    _ingest(src_inst, DOC_B, "parts/valves.pdf")

    def _export(inst):
        resp = routes_ingestion.api_export_rag_stream(inst)
        async def body():
            return "".join([c async for c in resp.body_iterator])
        return asyncio.run(body())

    nd = _export(src_inst)
    lines = [json.loads(l) for l in nd.splitlines() if l.strip()]
    chunks = [l for l in lines if l["_t"] == "chunk"]
    assert len(chunks) >= 2 and all(c["embedding_b64"] for c in chunks)

    _create(red_inst, endpoint="default")
    up = UploadFile(filename="x.jsonl", file=io.BytesIO(nd.encode()))
    asyncio.run(routes_ingestion.api_import_rag(red_inst, up, endpoint="default"))
    res = _search(red_inst, "QX-7731")
    assert any(c["source"] == "parts/valves.pdf" for c in res), res

    nd2 = _export(red_inst)
    _create(back_inst)
    up2 = UploadFile(filename="y.jsonl", file=io.BytesIO(nd2.encode()))
    asyncio.run(routes_ingestion.api_import_rag(back_inst, up2, endpoint=EP))
    assert mongo.col(mongo_store.CHUNKS).count_documents({"instance": back_inst}) == len(chunks)
    res2 = _search_until(back_inst, "how long is an answer cached?", lambda r: bool(r))
    assert res2 and res2[0]["source"] == "notes/cache.md", res2


def test_scores_match_the_redis_backend(mongo):
    """The same corpus scores the same on both backends: Atlas reports cosine as
    (1 + cos) / 2 and is converted back, so thresholds mean the same everywhere."""
    from visualweaver import mongo_store
    m_inst, r_inst = f"{KEY_PREFIX}par_m", f"{KEY_PREFIX}par_r"
    _create(m_inst)
    _create(r_inst, endpoint="default")
    for inst in (m_inst, r_inst):
        _ingest(inst, DOC_A, "notes/cache.md")
        _ingest(inst, DOC_B, "parts/valves.pdf")
    q = "how long does a cached answer live?"
    _until_searchable(mongo, m_inst, q, "notes/cache.md")      # vector only: hybrid=False below
    m = _search_until(m_inst, q, lambda r: len(r) >= 2, hybrid=False)
    r = _search(r_inst, q, hybrid=False)
    assert [c["source"] for c in m] == [c["source"] for c in r]
    for a, b in zip(m, r):
        assert abs(a["score"] - b["score"]) < 0.01, (a, b)


def test_optimize_removes_repeated_text(mongo):
    from visualweaver import mongo_store, routes_ingestion
    inst = f"{KEY_PREFIX}mongo_opt"
    _create(inst)
    _ingest(inst, DOC_A, "a.md")
    _ingest(inst, DOC_A, "b.md")          # same text, another source: kept by ingest dedup
    before = mongo.col(mongo_store.CHUNKS).count_documents({"instance": inst})
    out = routes_ingestion.api_optimize_rag(inst)
    assert out["total_before"] == before and out["removed"] == before // 2
    assert mongo.col(mongo_store.CHUNKS).count_documents({"instance": inst}) == out["remaining"]


def test_a_server_without_vector_search_cannot_hold_an_instance(mongo, monkeypatch):
    from fastapi import HTTPException
    from visualweaver import mongo_store
    monkeypatch.setattr(mongo_store, "probe", lambda cfg: {
        "ok": True, "version": "8.3.0", "vector_search": False, "atlas": False,
        "error": "This server has no vector search."})
    with pytest.raises(HTTPException) as e:
        _create(f"{KEY_PREFIX}novec")
    assert e.value.status_code == 400 and "vector search" in e.value.detail


def test_an_unknown_connection_is_refused_not_redirected(mongo):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        _create(f"{KEY_PREFIX}lost", endpoint="no-such-connection")
    assert e.value.status_code == 400


def test_a_password_in_the_connection_string_is_stored_as_a_secret(mongo, mongo_uri, monkeypatch):
    from visualweaver import config, routes_redis, state
    monkeypatch.setattr(config, "save_config", lambda c: None)
    from visualweaver import mongo_store
    uri = mongo_store.split_uri_credentials(mongo_uri)[0].replace("mongodb://", "mongodb://alice:s3cr%40t@")
    asyncio.run(routes_redis.api_add_endpoint({"name": "atlas", "type": "mongodb", "uri": uri, "database": "x"}))
    stored = next(e for e in state._config["redis_endpoints"] if e["name"] == "atlas")
    assert "s3cr" not in stored["uri"] and stored["username"] == "alice" and stored["password"] == "s3cr@t"
    listed = next(e for e in routes_redis.api_list_endpoints() if e["name"] == "atlas")
    assert listed["password"] == config._SECRET_SENTINEL and "s3cr" not in json.dumps(listed)
    assert listed["type"] == "mongodb" and listed["host"]
    red = config._redact_secrets(state._config)
    assert "s3cr" not in json.dumps(red)
    # saving the form again with the sentinel keeps the password
    asyncio.run(routes_redis.api_add_endpoint({"name": "atlas", "type": "mongodb", "uri": stored["uri"],
                                               "username": "alice", "password": config._SECRET_SENTINEL,
                                               "database": "x"}))
    assert next(e for e in state._config["redis_endpoints"] if e["name"] == "atlas")["password"] == "s3cr@t"


def test_vector_search_unavailable_is_recognised():
    """A mongod without mongot fails search-index commands with code 31082."""
    from pymongo.errors import OperationFailure
    from visualweaver import mongo_store

    class _Col:
        def list_search_indexes(self):
            raise OperationFailure("Using Atlas Search Database Commands and the $listSearchIndexes "
                                   "aggregation stage requires additional configuration.", code=31082)

    class _EP:
        def __init__(self, name, cfg):
            self.client = type("C", (), {"server_info": lambda s: {"version": "8.3.13"},
                                         "close": lambda s: None})()
        def col(self, n):
            return _Col()
        def close(self):
            pass

    orig = mongo_store.MongoEndpoint
    mongo_store.MongoEndpoint = _EP
    try:
        p = mongo_store.probe({"uri": "mongodb://x"})
    finally:
        mongo_store.MongoEndpoint = orig
    assert p["ok"] and p["vector_search"] is False and "no vector search" in p["error"]


def test_a_search_index_that_is_not_built_yet_answers_with_no_rows():
    """Self-managed mongot fails a query against an index it has not built (Atlas returns
    nothing). Each wording seen on 1.78.0 is "no rows yet"; FAILED and other errors raise."""
    from pymongo.errors import OperationFailure
    from visualweaver import mongo_store

    def fails_with(msg):
        class _Col:
            name = "vw_chunks"

            def aggregate(self, pipeline):
                raise OperationFailure(msg, code=8)
        return _Col()

    for msg in ["cannot query vector index 6aee (vector index vw_vector collection vw_chunks (x) "
                "in database visualweaver) while in state INITIAL_SYNC",
                "cannot query search index 6aee (index vw_text collection vw_chunks (x) in database "
                "visualweaver) while in state UNKNOWN",
                "Executor error during aggregate command on namespace: v.vw_cache :: caused by :: "
                "Index vw_cache_vector not initialized"]:
        assert mongo_store._aggregate(fails_with(msg), []) == [], msg
    for msg in ["cannot query vector index 6aee (vector index vw_vector …) while in state FAILED",
                "bad auth : authentication failed"]:
        with pytest.raises(OperationFailure):
            mongo_store._aggregate(fails_with(msg), [])


def test_connection_string_helpers():
    from visualweaver import mongo_store
    assert mongo_store.split_uri_credentials("mongodb+srv://bob:p%40ss@c0.ab.mongodb.net/?retryWrites=true") == \
        ("mongodb+srv://c0.ab.mongodb.net/?retryWrites=true", "bob", "p@ss")
    assert mongo_store.split_uri_credentials("mongodb://h:27017") == ("mongodb://h:27017", "", "")
    # an unencoded "@" in the password stays in the password, never in the stored URI
    assert mongo_store.split_uri_credentials("mongodb://bob:p@ss@127.0.0.1:27018/") == \
        ("mongodb://127.0.0.1:27018/", "bob", "p@ss")
    assert mongo_store.valid_database_name("visualweaver") and not mongo_store.valid_database_name("bad/db name")
    assert not mongo_store.valid_database_name("") and not mongo_store.valid_database_name("x" * 64)
    assert mongo_store.display_host("mongodb+srv://bob:x@c0.ab.mongodb.net/db?x=1") == "c0.ab.mongodb.net"
    assert mongo_store._client_kwargs({"uri": "mongodb://127.0.0.1:27018"}).get("directConnection") is True
    assert "directConnection" not in mongo_store._client_kwargs({"uri": "mongodb+srv://c0.mongodb.net"})
    assert "directConnection" not in mongo_store._client_kwargs({"uri": "mongodb://a:1,b:2/?replicaSet=rs"})


def test_an_atlas_connection_string_is_taken_as_atlas_shows_it():
    """Atlas's Connect → Drivers string: mongodb+srv://, user:<db_password>@, options."""
    from fastapi import HTTPException
    from pymongo.errors import ConfigurationError, OperationFailure, ServerSelectionTimeoutError
    from visualweaver import mongo_store, routes_redis
    atlas = "mongodb+srv://scyris_db_user:<db_password>@cluster0.clmrcbn.mongodb.net/?compressors=zlib"
    # the placeholder alone is refused, with the reason — never stored as the password
    with pytest.raises(HTTPException) as ei:
        routes_redis._mongo_entry({"name": "a", "uri": atlas}, {})
    assert ei.value.status_code == 400 and "<db_password>" in ei.value.detail
    # a typed password wins over the placeholder; the stored URI keeps scheme and options
    e = routes_redis._mongo_entry({"name": "a", "uri": atlas, "password": "real pw"}, {})
    assert e["uri"] == "mongodb+srv://cluster0.clmrcbn.mongodb.net/?compressors=zlib"
    assert (e["username"], e["password"]) == ("scyris_db_user", "real pw")
    assert "directConnection" not in mongo_store._client_kwargs(e)
    assert mongo_store.is_password_placeholder("<db_password>") and mongo_store.is_password_placeholder(" <password> ")
    assert not mongo_store.is_password_placeholder("p<a>ss") and not mongo_store.is_password_placeholder("")
    # driver errors in words a user can act on
    fe = mongo_store.friendly_error
    uri = e["uri"]
    assert fe(OperationFailure("bad auth : authentication failed", code=8000), uri).startswith("Login failed")
    assert "Database Access" in fe(OperationFailure("Authentication failed.", code=18), uri)
    assert "cluster0.clmrcbn.mongodb.net" in fe(ConfigurationError(
        "The DNS query name does not exist: _mongodb._tcp.cluster0.clmrcbn.mongodb.net."), uri)
    assert "Network Access" in fe(ServerSelectionTimeoutError("timed out"), uri)
    assert fe(ServerSelectionTimeoutError("timed out"), "mongodb://10.0.0.9:27017") == \
        "Could not reach 10.0.0.9:27017 in time."


def test_a_server_is_built_into_a_connection_string():
    """The Server field: an Atlas cluster host gets mongodb+srv:// (as Atlas builds it);
    any other server gets mongodb://; a whole connection string is kept."""
    from visualweaver import mongo_store, routes_redis
    su = mongo_store.server_uri
    assert su("cluster0.clmrcbn.mongodb.net") == "mongodb+srv://cluster0.clmrcbn.mongodb.net/"
    assert su(" Cluster0.ABC.MongoDB.net/?compressors=zlib ") == "mongodb+srv://Cluster0.ABC.MongoDB.net/?compressors=zlib"
    assert su("ac-1-shard-00-00.ab.mongodb.net:27017") == "mongodb://ac-1-shard-00-00.ab.mongodb.net:27017/"
    assert su("10.0.0.9:27017") == "mongodb://10.0.0.9:27017/"
    assert su("localhost") == "mongodb://localhost/"
    assert su("a:1,b:2/?replicaSet=rs") == "mongodb://a:1,b:2/?replicaSet=rs"
    assert su("mongodb+srv://x.mongodb.net/?a=1") == "mongodb+srv://x.mongodb.net/?a=1"
    assert su("") == ""
    e = routes_redis._mongo_entry({"name": "a", "uri": "cluster0.clmrcbn.mongodb.net",
                                   "username": "scyris_db_user", "password": "pw"}, {})
    assert (e["uri"], e["username"], e["password"]) == \
        ("mongodb+srv://cluster0.clmrcbn.mongodb.net/", "scyris_db_user", "pw")
    assert "directConnection" not in mongo_store._client_kwargs(e)
    e = routes_redis._mongo_entry({"name": "a", "uri": "10.0.0.9:27017"}, {})
    assert e["uri"] == "mongodb://10.0.0.9:27017/" and mongo_store._client_kwargs(e)["directConnection"] is True



def test_a_model_change_rebuilds_the_vector_index(mongo):
    """The server refuses an in-place update of a vectorSearch definition, so a model
    change must drop and recreate the index — otherwise every search and ingest on the
    connection fails from then on."""
    from visualweaver import mongo_store
    mongo_store.ensure_indexes(mongo, "embedding_8", 8)
    mongo_store.ensure_indexes(mongo, "embedding_16", 16)
    idx = {i["name"]: i for i in mongo.col(mongo_store.CHUNKS).list_search_indexes()}
    vec = next(f for f in idx[mongo_store.VEC_INDEX]["latestDefinition"]["fields"] if f["type"] == "vector")
    assert vec["path"] == "embedding_16" and vec["numDimensions"] == 16
    # ...and the rebuilt index really answers at the new width: a 16-dim document is
    # found by a 16-dim query on both legs.
    import numpy as np
    inst = f"{KEY_PREFIX}rebuilt"
    v = np.ones(16, dtype=np.float32) / 4.0
    mongo_store.add_chunks(mongo, inst, [{"id": 1, "text": "zqxrebuiltprobe", "source": "probe.md"}],
                           "embedding_16", [v], 0, lambda src: "d")
    _until_legs_return(mongo, inst, v, "embedding_16", ["zqxrebuiltprobe"], "probe.md")


def test_naming_a_connection_without_vector_search_is_refused_everywhere(mongo, mongo_uri, cfg):
    """Ingest and import with ?endpoint= reached a server creation refuses — and left a
    dedup hash behind that made the text count as already stored."""
    from fastapi import HTTPException, UploadFile
    from visualweaver import mongo_store, routes_ingestion, state
    cfg["redis_endpoints"].append({"name": "novec", "type": "mongodb", "uri": mongo_uri,
                                   "database": mongo.db.name, "username": "", "password": ""})
    state._search_available["novec"] = False
    inst = f"{KEY_PREFIX}bypass"
    with pytest.raises(HTTPException) as e:
        asyncio.run(routes_ingestion.api_ingest_text(inst, {"text": DOC_A, "source": "a.md"}, endpoint="novec"))
    assert e.value.status_code == 400 and "vector search" in e.value.detail
    with pytest.raises(HTTPException) as e:
        asyncio.run(routes_ingestion.api_import_rag(
            inst, UploadFile(filename="x.jsonl", file=io.BytesIO(b'{"_t":"meta"}\n')), endpoint="novec"))
    assert e.value.status_code == 400
    for col in (mongo_store.HASHES, mongo_store.COUNTERS, mongo_store.META, mongo_store.CHUNKS):
        assert mongo.col(col).count_documents({"$or": [{"_id": inst}, {"instance": inst}]}) == 0, col


def test_a_real_server_without_vector_search_fails_before_writing(monkeypatch):
    """Against a real mongod without mongot (VISUALWEAVER_TEST_MONGO_PLAIN_URI)."""
    from visualweaver import mongo_store
    uri = os.environ.get("VISUALWEAVER_TEST_MONGO_PLAIN_URI")
    if not uri:
        pytest.skip("set VISUALWEAVER_TEST_MONGO_PLAIN_URI to a mongod without search")
    db = f"vwtest_plain_{os.getpid()}"
    ep = mongo_store.MongoEndpoint("plain", {"uri": uri, "database": db})
    try:
        assert mongo_store.probe({"uri": uri})["vector_search"] is False
        with pytest.raises(mongo_store.NoVectorSearch):
            mongo_store.ensure_indexes(ep, "embedding_8", 8)
    finally:
        ep.client.drop_database(db)
        ep.close()


def test_a_settings_save_does_not_cut_off_running_work(mongo):
    """A crawl or export keeps the client it started with; rebuilding the client cache
    (every settings save does) used to close it under them."""
    from visualweaver import rag, redis_store
    held = redis_store.r_for(EP)
    redis_store.invalidate_redis_clients()
    assert rag.next_chunk_id(f"{KEY_PREFIX}held", 1, rc=held) >= 0      # still usable
    assert redis_store.r_for(EP) is not held                             # but no longer handed out


def test_the_stored_password_is_only_reused_for_its_own_server(mongo, mongo_uri, monkeypatch):
    from visualweaver import config, mongo_store, routes_redis, state
    state._config["redis_endpoints"].append({"name": "sec", "type": "mongodb", "uri": mongo_uri,
                                             "database": "x", "username": "alice", "password": "topsecret"})
    seen = []
    monkeypatch.setattr(mongo_store, "probe", lambda cfg, ep=None: seen.append(cfg) or
                        {"ok": False, "error": "x", "vector_search": False})
    for uri in (mongo_uri, "mongodb://attacker.example:27017"):
        asyncio.run(routes_redis.api_test_redis_adhoc({"type": "mongodb", "name": "sec", "uri": uri,
                                                       "username": "alice", "password": config._SECRET_SENTINEL}))
    assert seen[0]["password"] == "topsecret"          # same server: the saved password is used
    assert seen[1]["password"] == ""                   # another address never receives it


def test_connection_and_name_rules(mongo, mongo_uri, monkeypatch):
    from fastapi import HTTPException
    from visualweaver import config, routes_redis
    monkeypatch.setattr(config, "save_config", lambda c: None)
    with pytest.raises(HTTPException) as e:                       # an invalid database name
        asyncio.run(routes_redis.api_add_endpoint({"name": "bad", "type": "mongodb", "uri": mongo_uri,
                                                   "database": "bad/db name"}))
    assert e.value.status_code == 400
    inst = f"{KEY_PREFIX}dup"
    _create(inst)
    with pytest.raises(HTTPException) as e:                       # the same name on another connection
        _create(inst, endpoint="default")
    assert e.value.status_code == 409
    with pytest.raises(HTTPException) as e:                       # a connection holding instances changes kind
        asyncio.run(routes_redis.api_add_endpoint({"name": EP, "type": "redis", "host": "localhost"}))
    assert e.value.status_code == 409


def test_a_failed_stack_teardown_is_retried_and_reported(monkeypatch):
    """conftest's MongoDB fixture ignored the result of `compose down -v`, so a failed
    teardown left the stack and its volumes running unnoticed. It retries now, and says
    so — with the command to run — when the stack is still there."""
    import subprocess
    import time as _time
    import conftest
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        ok = len(calls) >= 3 if outcome == "late" else False
        return subprocess.CompletedProcess(argv, 0 if ok else 1, "", "" if ok else "daemon busy")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(_time, "sleep", lambda s: None)
    outcome = "late"
    assert conftest._compose_down(["docker", "compose"], "vwtest-x", conftest.Path("c.yml")) is True
    assert len(calls) == 3 and calls[0][-2:] == ["down", "-v"] and "vwtest-x" in calls[0]
    calls.clear()
    outcome = "never"
    with pytest.warns(UserWarning, match="vwtest-x.*NOT removed.*daemon busy"):
        assert conftest._compose_down(["docker", "compose"], "vwtest-x", conftest.Path("c.yml")) is False
    assert len(calls) == 3


# ── 1.16: concurrent retrieval legs, instance registry ─────────────────────────

def test_concurrent_legs_rank_exactly_as_the_sequential_ones(mongo):
    """search_legs now runs the keyword leg beside the vector leg instead of after it.
    The ranked ids must be the ones the two legs return run one after the other, and a
    full hybrid search must rank identically."""
    import numpy as np
    from visualweaver import embeddings, mongo_store, textutil
    inst = f"{KEY_PREFIX}mongo_legs"
    _create(inst)
    _ingest(inst, DOC_A, "notes/cache.md")
    _ingest(inst, DOC_B, "parts/valves.pdf")
    _until_searchable(mongo, inst, "how long does a stored response stay before it expires?",
                      "notes/cache.md", "QX-7731 boiler", "parts/valves.pdf")
    field = embeddings.vector_field_for()
    for q in ("how long does a stored response stay before it expires?", "QX-7731 boiler valve"):
        q_emb = embeddings.embed(q, is_query=True).astype(np.float32)
        kws = textutil._keywords_for_bm25(q)
        col = mongo.col(mongo_store.CHUNKS)
        seq = (mongo_store._vector_leg(col, field, q_emb, 10, {"instance": inst}),
               mongo_store._text_leg(col, inst, kws, 10, None))
        conc = mongo_store.search_legs(mongo, inst, q_emb, kws, 10, field, True)
        assert [[r["_key"] for r in leg] for leg in conc] == [[r["_key"] for r in leg] for leg in seq]
        assert conc[0], f"the vector leg returned nothing for {q!r}"
        if "QX-7731" in q:
            assert conc[1], "the keyword leg returned nothing for a part number"
        first = [c["text"] for c in _search(inst, q)]
        assert first and [c["text"] for c in _search(inst, q)] == first


def test_the_mongodb_listing_reads_the_registry_and_heals_it(mongo, monkeypatch):
    """The listing ran distinct("instance") over every chunk to find the instances. It
    reads the vw_instances registry now: built once (from the chunks and metadata) where
    none exists, kept by create / ingest / delete, and a name with nothing behind it is
    not listed."""
    from pymongo.collection import Collection
    from visualweaver import instance_registry, mongo_store, rag_admin, routes_instances
    made, fed = f"{KEY_PREFIX}mreg_made", f"{KEY_PREFIX}mreg_fed"
    _create(made)                                       # metadata only
    reg = mongo.col(instance_registry.MONGO_COLLECTION)
    assert reg.find_one({"_id": made})
    distinct_calls = []
    real_distinct = Collection.distinct

    def spy(self, key, *a, **kw):
        distinct_calls.append((self.name, key))
        return real_distinct(self, key, *a, **kw)

    monkeypatch.setattr(Collection, "distinct", spy)
    mongo_store.add_chunks(mongo, fed, [{"id": 0, "text": "x", "source": "f.md"}],
                           "embedding_384", [[0.1] * 384], 1, lambda s: "d")   # chunks only
    assert reg.find_one({"_id": fed})

    mine = lambda: {i["name"]: i["chunks"] for i in rag_admin.list_rag_instances()
                    if i["name"] in (made, fed)}
    distinct_calls.clear()
    assert mine() == {made: 0, fed: 1}
    assert (mongo_store.CHUNKS, "instance") not in distinct_calls, distinct_calls

    reg.drop()                                          # an install from before the registry
    assert mine() == {made: 0, fed: 1}
    assert (mongo_store.CHUNKS, "instance") in distinct_calls
    assert reg.find_one({"_id": instance_registry.BUILT})
    distinct_calls.clear()
    assert mine() == {made: 0, fed: 1} and distinct_calls == []

    mongo.col(mongo_store.CHUNKS).delete_many({"instance": fed})
    assert mine() == {made: 0}, "a name with nothing behind it is still listed"
    routes_instances.api_delete_instance(made, endpoint=EP)
    assert reg.find_one({"_id": made}) is None and mine() == {}
