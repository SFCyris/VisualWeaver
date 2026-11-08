# SPDX-License-Identifier: AGPL-3.0-or-later
"""The 1.16.0 review fixes on the server side.

Documents only:
  * a search that FAILED (storage unreachable, an index built with another embedding
    model) used to come back as an empty result, so Documents only answered "nothing in
    your documents matched" — telling the user their documents lack something that was
    never looked for. It is now a distinct "Search failed: …" answer (no model call, not
    cached), and every rag_context frame says when the search failed;
  * a follow-up ("shorten that", "explain point 2") retrieves nothing on its own wording
    and was refused; when the previous answer cited its sources, those sources are used
    again and the constrained model answers from them.

Also: the startup log names how many expiries a retention change lifted; the reranker
scores at full precision unless half precision is chosen; the conversation list with
sign-in on reads only the signed-in user's registry rows.
"""
import logging
import time

import numpy as np
import pytest

from conftest import KEY_PREFIX

from test_event_loop_responsiveness import _frames_until, chat   # noqa: F401  (fixture)
from test_documents_only import _KB, _answers, _turn, kb          # noqa: F401  (fixture)
from test_session_history import app_store                        # noqa: F401  (fixture)


# ── search failure is not "no hits" ──────────────────────────────────────────

class _Boom:
    def __init__(self, exc):
        self.exc = exc

    def query(self, *_a, **_k):
        raise self.exc


@pytest.fixture
def broken_index(monkeypatch):
    from visualweaver import mongo_store, rag

    def use(exc):
        monkeypatch.setattr(mongo_store, "is_mongo", lambda rc: False)
        monkeypatch.setattr(rag, "ensure_rag_index", lambda *a, **k: None)
        monkeypatch.setattr(rag, "_get_rag_index", lambda *a, **k: _Boom(exc))
        monkeypatch.setattr(rag, "_dim_mismatch_warned", set())
    return use


def test_search_rag_reports_an_unreachable_store_instead_of_an_empty_result(broken_index):
    import redis
    from visualweaver import rag
    broken_index(redis.exceptions.ConnectionError("Error 61 connecting to 127.0.0.1:6391. Connection refused."))
    errs: list = []
    out = rag.search_rag("kbx", "q", 5, 0.0, rc=object(), query_vec=np.zeros(4, np.float32), errors=errs)
    assert out == []
    assert errs == [{"instance": "kbx", "reason": "its storage could not be reached"}]
    # without the list nothing changes for the existing callers
    assert rag.search_rag("kbx", "q", 5, 0.0, rc=object(), query_vec=np.zeros(4, np.float32)) == []


def test_search_rag_names_an_embedding_dimension_mismatch(broken_index):
    from visualweaver import rag
    broken_index(Exception("query vector blob size (3072) does not match index's expected size (1536)"))
    errs: list = []
    rag.search_rag("kbx", "q", rc=object(), query_vec=np.zeros(4, np.float32), errors=errs)
    assert errs[0]["instance"] == "kbx" and "different embedding model" in errs[0]["reason"]


def test_a_knowledge_base_on_an_offline_store_is_a_failure_in_parallel_search(monkeypatch):
    import asyncio
    from visualweaver import rag, rag_admin, state

    async def meta(name):
        return {"enabled": True}, "far"
    monkeypatch.setattr(rag_admin, "_rag_meta_cached_async", meta)
    monkeypatch.setitem(state._endpoint_health, "far", False)
    errs: list = []
    out = asyncio.run(rag.search_rag_parallel(["a", "b"], "q", 5, 0.0, True,
                                              np.zeros(4, np.float32), "", errors=errs))
    assert out == [] and [e["instance"] for e in errs] == ["a", "b"]
    assert all(e["reason"] == "its storage could not be reached" for e in errs)


def _failing_search(kb, monkeypatch, reason="its storage could not be reached"):
    from visualweaver import rag

    def search(inst, *a, errors=None, **kw):
        if errors is not None:
            errors.append({"instance": inst, "reason": reason})
        return []
    monkeypatch.setattr(rag, "search_rag", search)


def test_documents_only_says_the_search_failed_instead_of_not_found(chat, kb, monkeypatch):
    """Old: the fixed "nothing in your documents matched" answer."""
    from visualweaver import rag, state
    _failing_search(kb, monkeypatch)
    sid = f"{KEY_PREFIX}fail_docs"
    with chat.websocket_connect(f"/ws/chat/{sid}") as ws:
        frames = _turn(ws, content="what is the warranty?", documents_only=True)
    tok = [f for f in frames if f["type"] == "token"]
    assert len(tok) == 1 and tok[0]["done"] is True
    text = tok[0]["content"]
    assert text.startswith("Search failed:") and "“kb”" in text and "could not be reached" in text
    assert text != rag.DOCS_ONLY_NOT_FOUND
    ctx = next(f for f in frames if f["type"] == "rag_context")
    assert ctx["search_failed"] is True and not ctx.get("not_found")
    assert chat.model_calls == [] and kb.stores == []
    meta = state._sessions[sid][1]["meta"]
    assert meta["search_failed"] is True and "not_found" not in meta


def test_a_normal_answer_after_a_failed_search_is_marked(chat, kb, monkeypatch):
    _failing_search(kb, monkeypatch)
    sid = f"{KEY_PREFIX}fail_normal"
    with chat.websocket_connect(f"/ws/chat/{sid}") as ws:
        frames = _turn(ws, content="anything")
    assert len(_answers(chat)) == 1
    assert next(f for f in frames if f["type"] == "rag_context")["search_failed"] is True
    assert kb.stores == [], "an answer from a failed search was cached"


def test_rest_documents_only_search_failure(chat, kb, monkeypatch):
    _failing_search(kb, monkeypatch)
    sid = f"{KEY_PREFIX}fail_rest"
    body = chat.post("/api/chat", json={**_KB, "content": "q", "session_id": sid,
                                        "documents_only": True}).json()
    assert body["response"].startswith("Search failed:") and body["search_failed"] is True
    assert not body.get("not_found") and chat.model_calls == [] and kb.stores == []


# ── follow-ups reuse the previous answer's sources ───────────────────────────

@pytest.fixture
def citing_model(chat, monkeypatch):
    """A model that cites source [1] in every answer."""
    import asyncio
    from visualweaver import providers

    async def stream(messages, model, images=None, usage=None):
        chat.model_calls.append([m.get("content") for m in messages])
        await asyncio.sleep(0.01)
        yield "The warranty lasts two years [1].", False
        yield "", True
    monkeypatch.setattr(providers, "ollama_stream", stream)


_HIT = {"source": "manual.pdf", "text": "The warranty is two years from the date of purchase.", "score": 0.8}


def test_a_follow_up_with_no_hits_answers_from_the_previous_answers_sources(chat, kb, citing_model):
    """Old: "shorten that" retrieved nothing and got the fixed not-found answer."""
    from visualweaver import state
    kb.hits = [_HIT]
    sid = f"{KEY_PREFIX}follow"
    with chat.websocket_connect(f"/ws/chat/{sid}") as ws:
        _turn(ws, content="how long is the warranty?", documents_only=True)
        end = time.time() + 5                 # the first answer's cache store runs after stream_end
        while not kb.stores and time.time() < end:
            time.sleep(0.05)
        kb.hits = []
        stores_before = len(kb.stores)
        assert stores_before == 1
        frames = _turn(ws, content="shorten that", documents_only=True)
    calls = _answers(chat)
    assert len(calls) == 2, "the follow-up was refused instead of answered"
    assert "Documents only is on" in calls[1][-1] and "two years from the date of purchase" in calls[1][-1]
    assert "[1] (source: manual.pdf" in calls[1][-1]
    ctx = next(f for f in frames if f["type"] == "rag_context")
    assert ctx["reused_sources"] is True and [c["source"] for c in ctx["chunks"]] == ["manual.pdf"]
    meta = state._sessions[sid][3]["meta"]
    assert meta["reused_sources"] is True and meta["chunks"][0]["n"] == 1 and "not_found" not in meta
    time.sleep(0.3)
    assert len(kb.stores) == stores_before, "an answer from reused sources was cached"


def test_no_reuse_when_the_previous_answer_cited_nothing(chat, kb):
    """The default fake model writes no [n] markers: nothing was cited, nothing is reused."""
    from visualweaver import rag
    kb.hits = [_HIT]
    sid = f"{KEY_PREFIX}follow_uncited"
    with chat.websocket_connect(f"/ws/chat/{sid}") as ws:
        _turn(ws, content="warranty?", documents_only=True)
        kb.hits = []
        frames = _turn(ws, content="shorten that", documents_only=True)
    assert next(f for f in frames if f["type"] == "token")["content"] == rag.DOCS_ONLY_NOT_FOUND
    assert len(_answers(chat)) == 1


def test_regenerating_a_follow_up_reuses_the_answer_before_it(chat, kb, citing_model):
    from visualweaver import state
    kb.hits = [_HIT]
    sid = f"{KEY_PREFIX}follow_regen"
    with chat.websocket_connect(f"/ws/chat/{sid}") as ws:
        _turn(ws, content="warranty?", documents_only=True)
        kb.hits = []
        _turn(ws, content="shorten that", documents_only=True)
        frames = _turn(ws, content="shorten that", documents_only=True, replace_from=3, bypass_cache=True)
    assert next(f for f in frames if f["type"] == "rag_context")["reused_sources"] is True
    assert len(state._sessions[sid]) == 4 and len(_answers(chat)) == 3


def test_rest_follow_up_reuses_the_previous_sources(chat, kb, citing_model):
    kb.hits = [_HIT]
    sid = f"{KEY_PREFIX}follow_rest"
    chat.post("/api/chat", json={**_KB, "content": "warranty?", "session_id": sid, "documents_only": True})
    kb.hits = []
    body = chat.post("/api/chat", json={**_KB, "content": "shorten that", "session_id": sid,
                                        "documents_only": True}).json()
    assert body.get("reused_sources") is True and not body.get("not_found")
    assert [c["source"] for c in body["chunks"]] == ["manual.pdf"] and len(_answers(chat)) == 2


# ── startup log: expiries lifted ─────────────────────────────────────────────

def test_applying_retention_logs_how_many_expiries_it_lifted(app_store, cfg, caplog):
    from visualweaver import sessions
    cfg["sessions"]["ttl"] = 86400
    sids = [f"{KEY_PREFIX}lift{i}" for i in range(3)]
    for s in sids:
        sessions.save_session(s, [{"role": "user", "content": "q", "meta": {}},
                                  {"role": "assistant", "content": "a", "meta": {}}])
    sessions.apply_retention(app_store)
    cfg["sessions"]["ttl"] = 0
    with caplog.at_level(logging.INFO, logger="visualweaver.sessions"):
        assert sessions.ensure_index(app_store) is True
    lines = [r.getMessage() for r in caplog.records if "expir" in r.getMessage()]
    assert any("3 expiries lifted" in line for line in lines), lines
    for s in sids:
        sessions.delete_session_from_redis(s)


# ── the conversation list with sign-in on reads only the user's own rows ─────

def _two_users(app_store, monkeypatch, admin=False):
    from visualweaver import auth, sessions
    turn = [{"role": "user", "content": "q", "meta": {}}, {"role": "assistant", "content": "a", "meta": {}}]
    a, b = sessions.user_prefix("alice"), sessions.user_prefix("bob")
    for sid in (a + "one", a + "two", b + "three", f"{KEY_PREFIX}legacy"):
        sessions.save_session(sid, [dict(m) for m in turn])
    reads = []
    cls = type(app_store)                 # appstore.current() hands out a new object per call
    real_all, real_pfx = cls.hgetall, cls.hgetall_prefix
    monkeypatch.setattr(cls, "hgetall", lambda self, key: reads.append(("all", key)) or real_all(self, key))
    monkeypatch.setattr(cls, "hgetall_prefix",
                        lambda self, key, pfx: reads.append(("prefix", key, pfx)) or real_pfx(self, key, pfx))
    monkeypatch.setattr(auth, "owner_id", lambda: "alice")
    monkeypatch.setattr(auth, "is_admin", lambda: admin)
    return reads


def test_a_signed_in_user_lists_only_their_rows_without_reading_everyones(app_store, monkeypatch):
    """Old: hgetall of the whole registry (every user's rows) on every listing."""
    from visualweaver import sessions
    reads = _two_users(app_store, monkeypatch)
    sessions.list_sessions()               # the first listing builds the registry (one full read)
    reads.clear()
    ids = sorted(r["id"] for r in sessions.list_sessions())
    assert ids == ["one", "two"]
    assert not [r for r in reads if r[0] == "all" and r[1] == sessions._INDEX_KEY], reads
    assert ("prefix", sessions._INDEX_KEY, sessions.user_prefix("alice")) in reads


def test_an_admin_still_lists_untagged_conversations(app_store, monkeypatch):
    from visualweaver import sessions
    _two_users(app_store, monkeypatch, admin=True)
    ids = sorted(r["id"] for r in sessions.list_sessions())
    assert ids == sorted(["one", "two", f"{KEY_PREFIX}legacy"])


def test_hgetall_prefix_matches_glob_characters_literally(app_store):
    app_store.hset(f"{KEY_PREFIX}h", {"u.a*.x": "1", "u.ab.y": "2", "u.a*": "3", "v.a*.z": "4"})
    try:
        assert app_store.hgetall_prefix(f"{KEY_PREFIX}h", "u.a*.") == {"u.a*.x": "1"}
        assert app_store.hgetall_prefix(f"{KEY_PREFIX}h", "") == app_store.hgetall(f"{KEY_PREFIX}h")
    finally:
        app_store.delete(f"{KEY_PREFIX}h")


# ── the reranker scores at full precision unless half precision is chosen ────

def test_the_reranker_stays_in_full_precision_on_a_gpu_by_default(cfg):
    """Old: half precision on every GPU — scores moved by up to 6.5e-3 and a near-tie could
    swap places, so the same question could rank differently on a GPU and a CPU."""
    from test_perf_1_16 import _StubCE
    from visualweaver import embeddings
    cfg["reranker"] = {"enabled": True}
    for dev in ("cpu", "cuda", "mps"):
        stub = embeddings._prepare_reranker(_StubCE(dev))
        assert stub.precision == [] and not getattr(stub, "_vw_half", False), dev


def test_half_precision_is_an_opt_in_and_follows_the_setting(cfg, monkeypatch):
    from test_perf_1_16 import _StubCE
    from visualweaver import embeddings, state
    cfg["reranker"] = {"enabled": True, "half_precision": True}
    assert embeddings._prepare_reranker(_StubCE("mps")).precision == ["half"]
    assert embeddings._prepare_reranker(_StubCE("cpu")).precision == []
    # a loaded reranker follows a change of the setting without being reloaded
    stub = embeddings._prepare_reranker(_StubCE("cuda"))
    monkeypatch.setattr(state, "_reranker", stub)
    monkeypatch.setattr(state, "_reranker_model_name", "m")
    monkeypatch.setattr(embeddings, "HAS_CROSSENCODER", True)
    monkeypatch.setattr(embeddings, "_cross_encoder_cls", lambda: object)
    cfg["reranker"] = {"enabled": True, "model": "m", "half_precision": False}
    assert embeddings.get_reranker() is stub and stub.precision == ["half", "float"]
    assert not stub._vw_half


def test_the_sources_before_a_regenerated_follow_up_are_the_previous_answers(monkeypatch):
    """Regenerating a follow-up reuses the answer BEFORE the one being replaced, not the
    replaced answer's own (reused) sources."""
    from visualweaver import routes_chat, state
    x = {"n": 1, "source": "x.pdf", "text": "Ex.", "instance": "kb"}
    y = {"n": 1, "source": "y.pdf", "text": "Why.", "instance": "kb"}
    sid = f"{KEY_PREFIX}prev_src"
    monkeypatch.setitem(state._sessions, sid, [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "A [1].", "meta": {"chunks": [x], "kbs": ["kb"]}},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "B [1].", "meta": {"chunks": [y], "kbs": ["kb"]}}])
    prev = lambda replace_from: routes_chat._previous_cited_sources(sid, replace_from, "shorten that", ["kb"])
    assert [c["source"] for c in prev(None)] == ["y.pdf"]
    assert [c["source"] for c in prev(3)] == ["x.pdf"]
    # a not-found or failed answer before it has no sources to reuse
    state._sessions[sid][3]["meta"] = {"not_found": True}
    assert prev(None) == []


def test_a_connection_error_wrapped_by_the_search_library_still_reads_unreachable():
    """Found end to end: with Redis stopped, redisvl raises its own "Error while searching:
    Error 61 connecting to … Connection refused." around the ConnectionError, which was
    reported as a generic search error."""
    import redis
    from visualweaver import rag
    try:
        try:
            raise redis.exceptions.ConnectionError("Error 61 connecting to 127.0.0.1:18602. Connection refused.")
        except redis.exceptions.ConnectionError as inner:
            raise RuntimeError(f"Error while searching: {inner}") from inner
    except RuntimeError as wrapped:
        assert rag.search_failure_reason(wrapped) == rag.SEARCH_UNREACHABLE
    bare = RuntimeError("Error while searching: Error 61 connecting to 127.0.0.1:18602. Connection refused.")
    assert rag.search_failure_reason(bare) == rag.SEARCH_UNREACHABLE
    assert rag.search_failure_reason(RuntimeError("Syntax error at offset 3")) == rag.SEARCH_ERROR


def test_a_failed_search_beside_an_attached_document_is_still_marked(chat, kb, monkeypatch):
    """The attachment is answered from, but the turn still says its search failed."""
    _failing_search(kb, monkeypatch)
    sid = f"{KEY_PREFIX}fail_attach"
    with chat.websocket_connect(f"/ws/chat/{sid}") as ws:
        frames = _turn(ws, content="summarise", documents_only=True,
                       file_context=[{"name": "a.txt", "text": "Alpha beta."}])
    assert len(_answers(chat)) == 1
    assert next(f for f in frames if f["type"] == "rag_context").get("search_failed") is True
