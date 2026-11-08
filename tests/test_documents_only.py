# SPDX-License-Identifier: AGPL-3.0-or-later
"""Documents only: answers from the retrieved chunks (or an attached document), or a plain
"not in your documents" — never from general knowledge.

Before 1.16.0 there was no such mode. Every grounded prompt explicitly ALLOWED the general-
knowledge fallback ("answer the question from general knowledge"), and when retrieval came
back empty the model was told to answer from general knowledge anyway — so a reader had no
way to ask for an answer that is either from their documents or says it is not.

What must hold:
  * the per-turn prompt in this mode carries no general-knowledge fallback, and the system
    prompt (base instruction + template) is byte-identical in both modes;
  * with nothing retrieved and nothing attached, the fixed answer is given and NO model is
    called (asked to "answer only from the documents" with none in front of it, a model
    still improvises);
  * a documents-only answer and a normal answer never share a cache entry, and the scope of
    a normal question is exactly what it was before the mode existed (no stored entry lost);
  * the mode is recorded on each stored turn.
"""
import hashlib
import time

import pytest

from conftest import KEY_PREFIX

from test_event_loop_responsiveness import _frames_until, chat   # noqa: F401  (fixture)


# ── the prompt ───────────────────────────────────────────────────────────────

def _chunks():
    return [{"source": "notes.md", "text": "Streams are append-only.", "score": 0.81, "n": 1},
            {"source": "https://ex.org/a", "text": "XADD appends an entry.", "relevance": 0.6, "n": 2}]


def test_documents_only_prompt_has_no_general_knowledge_fallback():
    """The normal prompt tells the model to answer from general knowledge when the context
    is irrelevant; that sentence must not reach a documents-only turn."""
    from visualweaver import rag
    p = rag.build_documents_only_prompt(_chunks())
    assert "Answer ONLY from this" in p
    assert "never from general" in p and "knowledge" in p
    assert "answer the question from general knowledge" not in p
    assert "do not contain it, and stop" in p
    assert "[1] (source: notes.md, match 81%)" in p and "[2] (source: https://ex.org/a, match 60%)" in p
    assert "square brackets" in p
    normal = rag.build_context_prompt(_chunks())
    assert "answer the question from general knowledge" in normal     # unchanged for normal turns


def test_documents_only_prompt_without_chunks_restricts_to_the_attachment():
    from visualweaver import rag
    p = rag.build_documents_only_prompt([])
    assert "ONLY from the attached document" in p and "general knowledge" in p
    assert "answer from general knowledge" not in p


@pytest.mark.parametrize("rag_used,chunks,files,expect", [
    (True, [], [], "NOT_FOUND"),
    (False, [], [], "NO_KB"),
    (True, [{"text": "x"}], [], None),
    (True, [], [{"name": "a.txt", "text": "body"}], None),          # an attachment is a document
    (False, [], [{"name": "a.txt", "text": "body"}], None),
    (True, [], [{"name": "a.txt", "text": ""}], "NOT_FOUND"),       # an empty one is not
    (True, [], ["junk"], "NOT_FOUND"),
])
def test_refusal_is_given_only_when_there_is_nothing_to_answer_from(rag_used, chunks, files, expect):
    from visualweaver import rag
    got = rag.documents_only_refusal(rag_used, chunks, files)
    assert got == (getattr(rag, f"DOCS_ONLY_{expect}") if expect else None)


# ── the cache partition ──────────────────────────────────────────────────────

def _legacy_scope(rag_instances, provider, model, source_filter, system_prompt, visual):
    """_cache_scope as it was before the mode existed (1.15.0), written out."""
    payload = "|".join([",".join(sorted(rag_instances or [])), provider or "", model or "",
                        source_filter or "",
                        hashlib.sha256((system_prompt or "").encode()).hexdigest()[:16],
                        "v" if visual else "t"])
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def test_a_normal_scope_is_unchanged_and_documents_only_has_its_own():
    """Changing the normal scope would orphan every stored cache entry on upgrade; sharing
    it would replay a general-knowledge answer under Documents only (or a "not in your
    documents" answer to a normal question)."""
    from visualweaver import cache
    for visual in (False, True):
        args = (["b", "a"], "ollama", "m", "src", "SYS", visual)
        normal = cache._cache_scope(*args[:5], visual=visual)
        assert normal == _legacy_scope(*args)
        assert cache._cache_scope(*args[:5], visual=visual, documents_only=False) == normal
        docs = cache._cache_scope(*args[:5], visual=visual, documents_only=True)
        assert docs != normal
        assert docs == cache._cache_scope(["a", "b"], "ollama", "m", "src", "SYS",
                                          visual=visual, documents_only=True)


# ── through the chat WebSocket and the REST route ────────────────────────────

@pytest.fixture
def kb(chat, monkeypatch):
    """A knowledge base 'kb' whose search returns whatever `kb.hits` holds, plus recorders
    for the cache lookups and stores."""
    from visualweaver import cache, embeddings, rag, rag_admin, state

    class KB:
        hits: list = []
        lookups: list = []
        stores: list = []
    k = KB()
    k.hits, k.lookups, k.stores = [], [], []

    async def meta(name):
        return {"enabled": True}, "default"
    monkeypatch.setattr(rag_admin, "_rag_meta_cached_async", meta)
    monkeypatch.setattr(rag_admin, "rc_for_instance", lambda name: None)
    monkeypatch.setattr(rag_admin, "save_rag_stats", lambda: None)
    monkeypatch.setattr(rag, "search_rag", lambda *a, **kw: [dict(c) for c in k.hits])
    monkeypatch.setattr(embeddings, "rerank_chunks", lambda q, c, n: c)
    monkeypatch.setattr(state, "record_citations", lambda *a, **kw: None)
    monkeypatch.setattr(cache, "cache_lookup",
                        lambda q, t, scope, *a, **kw: k.lookups.append(scope) or None)
    monkeypatch.setattr(cache, "cache_store",
                        lambda q, r, c, scope, *a, **kw: k.stores.append((q, r, scope)))
    chat.kb = k
    return k


_KB = {"provider": "ollama", "model": "fake", "rag_instance": "kb"}


def _answers(chat):
    """The model calls that produced answers (the auto-title call is a model call too)."""
    return [c for c in chat.model_calls if not c[-1].startswith("Reply with ONLY a short title")]


def _turn(ws, **extra):
    ws.send_json({**_KB, **extra})
    return _frames_until(ws, "stream_end")


def test_no_match_under_documents_only_answers_without_calling_the_model(chat, kb):
    """Old: with nothing retrieved the model was told to answer from general knowledge."""
    from visualweaver import rag, state
    sid = f"{KEY_PREFIX}docs_nomatch"
    with chat.websocket_connect(f"/ws/chat/{sid}") as ws:
        frames = _turn(ws, content="what is the warranty period?", documents_only=True)
        _frames_until(ws, "session_title")
    assert chat.model_calls == [], "a model was called for a documents-only turn with no documents"
    tokens = [f for f in frames if f["type"] == "token"]
    assert tokens == [{"type": "token", "content": rag.DOCS_ONLY_NOT_FOUND, "done": True}]
    ctx = next(f for f in frames if f["type"] == "rag_context")
    assert ctx["rag_used"] is True and ctx["docs_only"] is True and ctx["not_found"] is True
    assert [f["type"] for f in frames].index("stream_start") < [f["type"] for f in frames].index("rag_context")
    assert kb.stores == [], "a not-found answer was cached"
    user, ans = state._sessions[sid]
    assert ans["content"] == rag.DOCS_ONLY_NOT_FOUND
    assert (ans["meta"]["docs_only"], ans["meta"]["rag_used"], ans["meta"]["not_found"]) == (True, True, True)
    assert ans["meta"]["kbs"] == ["kb"]
    assert user["meta"].get("title") == "what is the warranty period?"


def test_documents_only_without_a_knowledge_base_says_so_without_the_model(chat, kb):
    from visualweaver import rag, state
    sid = f"{KEY_PREFIX}docs_nokb"
    with chat.websocket_connect(f"/ws/chat/{sid}") as ws:
        ws.send_json({"provider": "ollama", "model": "fake", "rag_instances": [],
                      "content": "hello", "documents_only": True})
        frames = _frames_until(ws, "stream_end")
    assert chat.model_calls == []
    assert next(f for f in frames if f["type"] == "token")["content"] == rag.DOCS_ONLY_NO_KB
    meta = state._sessions[sid][1]["meta"]
    assert meta["rag_used"] is False and meta["not_found"] is True


def test_documents_only_with_chunks_constrains_the_prompt_but_not_the_system_prompt(chat, kb):
    """The mode rides on the turn's context, so the system prefix stays byte-stable (provider
    prompt caching) and the base instruction is untouched."""
    from visualweaver import state
    kb.hits = [{"source": "manual.pdf", "text": "The warranty is two years.", "score": 0.7}]
    sid = f"{KEY_PREFIX}docs_hit"
    with chat.websocket_connect(f"/ws/chat/{sid}") as ws:
        _turn(ws, content="warranty?", documents_only=True)
        _turn(ws, content="warranty again?")
        end = time.time() + 5                 # the cache store runs after stream_end
        while len(kb.stores) < 2 and time.time() < end:
            time.sleep(0.05)
    docs_call, normal_call = _answers(chat)
    assert docs_call[0] == normal_call[0], "documents-only changed the system prompt"
    assert "Documents only is on" in docs_call[-1] and "Answer ONLY from this" in docs_call[-1]
    assert "[1] (source: manual.pdf, match 70%)" in docs_call[-1]
    assert "answer the question from general knowledge" not in docs_call[-1]
    assert "Documents only is on" not in normal_call[-1]
    assert "answer the question from general knowledge" in normal_call[-1]
    a1, a2 = state._sessions[sid][1]["meta"], state._sessions[sid][3]["meta"]
    assert (a1["docs_only"], a1["rag_used"], "not_found" in a1) == (True, True, False)
    assert (a2["docs_only"], a2["rag_used"]) == (False, True)
    # different partitions for lookup and store
    assert len(kb.lookups) == 2 and kb.lookups[0] != kb.lookups[1]
    assert [s[2] for s in kb.stores] == kb.lookups


def test_a_normal_no_match_still_calls_the_model_with_the_abstention_notice(chat, kb):
    sid = f"{KEY_PREFIX}docs_normal_nomatch"
    with chat.websocket_connect(f"/ws/chat/{sid}") as ws:
        _turn(ws, content="anything")
    assert len(_answers(chat)) == 1
    assert "No relevant context was found" in _answers(chat)[0][-1]


def test_an_attached_document_counts_as_a_document(chat, kb):
    sid = f"{KEY_PREFIX}docs_file"
    with chat.websocket_connect(f"/ws/chat/{sid}") as ws:
        _turn(ws, content="summarise", documents_only=True,
              file_context=[{"name": "a.txt", "text": "Alpha beta."}])
    assert len(_answers(chat)) == 1
    assert "ONLY from the attached document" in _answers(chat)[0][-1]
    assert "Alpha beta." in _answers(chat)[0][-1]


def test_a_cached_answer_records_the_mode_it_was_served_under(chat, kb, monkeypatch):
    from visualweaver import cache, state
    hit = {"response": "From cache [1].", "score": 0.99, "entry_id": "e",
           "chunks": [{"n": 1, "source": "s", "text": "t", "score": 0.9}]}
    monkeypatch.setattr(cache, "cache_lookup",
                        lambda q, t, scope, *a, **kw: kb.lookups.append(scope) or hit)
    sid = f"{KEY_PREFIX}docs_cachehit"
    with chat.websocket_connect(f"/ws/chat/{sid}") as ws:
        ws.send_json({**_KB, "content": "q", "documents_only": True})
        frames = _frames_until(ws, "rag_context")
    assert frames[-1]["docs_only"] is True and frames[-1]["rag_used"] is True
    end = time.time() + 5                     # the turn is stored just after the frame is sent
    while len(state._sessions.get(sid) or []) < 2 and time.time() < end:
        time.sleep(0.05)
    meta = state._sessions[sid][1]["meta"]
    assert meta["docs_only"] is True and meta["rag_used"] is True


def test_regenerating_a_not_found_answer_replaces_it(chat, kb):
    from visualweaver import rag, state
    sid = f"{KEY_PREFIX}docs_regen"
    with chat.websocket_connect(f"/ws/chat/{sid}") as ws:
        _turn(ws, content="q1")                                   # normal answer, model called
        _turn(ws, content="q2", documents_only=True)              # not found
        _turn(ws, content="q2", documents_only=True, replace_from=3, bypass_cache=True)
    msgs = state._sessions[sid]
    assert [m["content"] for m in msgs][::2] == ["q1", "q2"]
    assert msgs[3]["content"] == rag.DOCS_ONLY_NOT_FOUND and len(msgs) == 4
    assert len(_answers(chat)) == 1


def test_only_json_true_turns_the_mode_on(chat, kb):
    """A string "false" is truthy; a client sending it must not get the mode."""
    sid = f"{KEY_PREFIX}docs_strict"
    with chat.websocket_connect(f"/ws/chat/{sid}") as ws:
        _turn(ws, content="x", documents_only="false")
    assert len(_answers(chat)) == 1


def test_rest_no_match_under_documents_only_does_not_call_the_model(chat, kb):
    from visualweaver import rag, state
    sid = f"{KEY_PREFIX}docs_rest"
    r = chat.post("/api/chat", json={**_KB, "content": "q", "session_id": sid, "documents_only": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["response"] == rag.DOCS_ONLY_NOT_FOUND
    assert body["not_found"] is True and body["documents_only"] is True and body["rag_used"] is True
    assert chat.model_calls == [] and kb.stores == []
    assert state._sessions[sid][1]["meta"]["not_found"] is True
    # and with a match: the model is called with the constrained prompt
    kb.hits = [{"source": "m.pdf", "text": "Two years.", "score": 0.8}]
    r = chat.post("/api/chat", json={**_KB, "content": "q?", "session_id": sid, "documents_only": True})
    assert r.json()["documents_only"] is True and len(_answers(chat)) == 1
    assert "Documents only is on" in _answers(chat)[0][-1]
    assert kb.lookups[0] == kb.lookups[1] == kb.stores[0][2]
    chat.post("/api/chat", json={**_KB, "content": "q??", "session_id": sid})      # normal mode
    assert kb.lookups[2] != kb.lookups[0]
    assert "Documents only is on" not in _answers(chat)[1][-1]


def test_settings_default_is_off():
    from visualweaver import constants
    assert constants.DEFAULT_CONFIG["rag"]["documents_only"] is False


def test_a_stored_chunk_keeps_its_reranker_score_for_the_source_card():
    """Old: the stored chunk dropped reranker_score, so a reopened conversation's source
    cards could not show it although the live answer had it."""
    from visualweaver import sessions
    meta = sessions._turn_meta([{"n": 1, "source": "a", "text": "t", "score": 0.5, "reranker_score": 0.9312},
                                {"n": 2, "source": "b", "text": "u", "score": 0.4, "reranker_score": None}],
                               docs_only=False, rag_used=True)
    assert meta["chunks"][0]["reranker_score"] == 0.9312
    assert "reranker_score" not in meta["chunks"][1]
    assert (meta["docs_only"], meta["rag_used"], "not_found" in meta) == (False, True, False)
    assert "docs_only" not in sessions._turn_meta() and "rag_used" not in sessions._turn_meta()
