# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.routes_chat — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import asyncio
import json
import re
import time
import uuid
from typing import Any, Optional
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from . import appcore, cache, config, embeddings, hyde, projects, providers, rag, rag_admin, sessions, state
from . import ws as _ns_ws

log = logging.getLogger(__name__)


def valid_replace_index(value, n_messages: int) -> bool:
    """Is ``value`` a usable truncation point for a regenerate?

    A named predicate rather than an inline condition so a test can exercise the real
    rule. Written inline, the only thing a test could reach was the AST — which cannot
    see polarity, so a guard inverted to ``or isinstance(value, bool)`` still "passed".

    bool is a subclass of int, so a client-supplied JSON ``true`` used to satisfy the
    isinstance check as the integer 1, walk back to index 0, and delete the entire
    conversation — which was then persisted as an empty list.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return 0 <= value <= n_messages


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# WEBSOCKET — CHAT HANDLER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@appcore.app.websocket("/ws/chat/{sid}")
async def ws_chat(ws: WebSocket, sid: str):
    """One WebSocket per browser tab; each message is a full chat request.

    The streaming task runs as an asyncio background task so the receive loop
    can concurrently process an {"type":"abort"} message and cancel it.

    Everything here is per SOCKET, not per session. The UI resumes the last
    conversation, so two tabs routinely open the same session id — and with the state
    keyed by session, closing one tab cancelled the other tab's answer mid-stream, and a
    question sent from the second tab while the first was answering was dropped without
    a word. Now Stop and closing a tab touch only that tab's own turns, and every
    message is answered: turns on one conversation run one after another (_run_turn),
    and one that has to wait is told so.
    """
    # With sign-in on, the signed-in user's own conversation of that id (see
    # sessions.scoped_sid): another user's conversation cannot be resumed or added to.
    sid = await asyncio.to_thread(sessions.scoped_sid, sid)
    await _ns_ws.mgr.connect(ws, sid)
    try:
        if sid not in state._sessions:
            state._sessions[sid] = await asyncio.to_thread(sessions.load_session, sid)
        state.touch_session(sid)
        while True:
            raw = await ws.receive_text()
            # Parsed INSIDE the try: a malformed frame used to raise straight out of the
            # handler, skipping the cleanup below and leaving this socket's task running.
            try:
                msg = json.loads(raw)
            except ValueError:
                msg = None
            if not isinstance(msg, dict):
                await ws.send_json({"type": "error",
                                    "content": "Ignored a message that was not a valid chat request."})
                continue

            # ── Abort: cancel THIS socket's turns — the one answering and any queued
            # behind it (Stop means stop) — and nobody else's ─────────────────────
            if msg.get("type") == "abort":
                tasks = state._chat_tasks.pop(ws, [])
                for task in tasks:
                    if not task.done():
                        task.cancel()
                for task in tasks:
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                # Notify client that the stream is done (aborted)
                try:
                    await ws.send_json({"type": "stream_end", "aborted": True, "latency": {}})
                except Exception:
                    pass
                continue

            # ── New chat turn: start as a background task ────────────────────
            # If this socket's previous answer is still streaming (a regenerate clicked
            # mid-answer), the new turn queues behind it like one from another tab. It
            # used to be dropped without a word.
            state._chat_tasks.setdefault(ws, []).append(
                asyncio.create_task(_run_turn(ws, sid, msg)))

    except WebSocketDisconnect:
        pass
    finally:
        # Cancel only this socket's turns; another tab on the same conversation keeps
        # its answer.
        for task in state._chat_tasks.pop(ws, []):
            if not task.done():
                task.cancel()
        _ns_ws.mgr.disconnect(sid, ws)


def _transcript_mark(sid: str) -> tuple:
    """What a turn compares to tell whether it changed the transcript: its length and
    its last message — a regenerate can leave the length as it was."""
    msgs = state._sessions.get(sid) or []
    return len(msgs), id(msgs[-1]) if msgs else None


async def _run_turn(ws: WebSocket, sid: str, msg: dict):
    """Run one socket's chat turn once the conversation is free.

    A conversation's history is read when a turn starts and appended and saved whole
    when it ends, so two turns on it at once — the same conversation open in two tabs
    — would each save a transcript missing the other's exchange. A turn that finds the
    conversation busy waits its turn (first come, first served) and its tab is told it
    is queued. The claim ends at stream_end (handle_chat calls ``release``): the title
    and the cache store that follow do not hold up the next question.
    """
    turn = state._session_turns.setdefault(sid, {"lock": asyncio.Lock(), "turns": 0, "writers": []})
    lock = turn["lock"]
    turn["turns"] += 1
    me = asyncio.current_task()
    mark = len(turn["writers"])      # turns that change the transcript from here on
    held = False
    before = None
    released = False

    def release():
        nonlocal held, released
        if released:
            return
        released = True
        if held:
            held = False
            if _transcript_mark(sid) != before:
                turn["writers"].append(ws)            # this turn changed the transcript
            lock.release()
        turn["turns"] -= 1
        if turn["turns"] <= 0 and state._session_turns.get(sid) is turn:
            del state._session_turns[sid]
        # Free this socket's slot: Stop and closing the tab no longer reach this turn's
        # title and cache work, and the next message is not queued behind it.
        mine = state._chat_tasks.get(ws)
        if mine and me in mine:
            mine.remove(me)
            if not mine:
                state._chat_tasks.pop(ws, None)

    try:
        if lock.locked():
            # The UI keeps its typing indicator up on a status frame, so the question
            # visibly waits instead of seeming lost.
            await ws.send_json({"type": "status", "phase": "queued"})
        await lock.acquire()
        held = True
        before = _transcript_mark(sid)
        if (any(w is not ws for w in turn["writers"][mark:])
                and valid_replace_index(msg.get("replace_from"), before[0])):
            # A regenerate names a position in the transcript THIS tab saw. Another tab
            # added to it while this one waited, so the position no longer means what the
            # user clicked on — and truncating there would delete the other tab's answer.
            # (A turn of this same tab ahead of it is fine: the tab saw that one.)
            await ws.send_json({"type": "error", "content": (
                "This conversation changed in another tab — reload it, then regenerate.")})
            return
        await handle_chat(ws, sid, msg, release=release)
    except asyncio.CancelledError:
        raise
    except WebSocketDisconnect:
        pass                         # the tab went away mid-turn; nobody to tell
    except Exception as e:
        # Uncaught, this died as an unretrieved task exception: the tab kept its typing
        # indicator up forever with nothing in the log to say why.
        log.exception(f"chat turn failed for session {sid}")
        if not released:
            # After stream_end the answer is complete and the tab may already be
            # streaming its next one, which an error frame would cut short.
            try:
                await ws.send_json({"type": "error", "content": f"Could not answer: {e}"})
            except Exception:
                pass
    finally:
        release()


# Citation markers the model wrote, e.g. "[3]" or "[3, 4]". The grouped form counts
# too — a model citing two passages writes it either way, and matching only the lone
# form would under-report exactly the ranks that tend to appear together.
_CITE_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
# Answers about code are full of [0] and [1] that are subscripts, not citations. Left
# in, argv[1] in a fenced block votes for rank 1 and inflates the citation counters on
# exactly the corpora most likely to be technical documentation.
_CODE_RE = re.compile(r"```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z)|`[^`\n]*`", re.S)


def _record_answer_citations(answer: str, chunks: list[dict]) -> None:
    """Feed the per-rank citation counters from one finished answer."""
    if not answer or not chunks:
        return
    prose = _CODE_RE.sub(" ", answer)
    ranks = {int(n) for m in _CITE_RE.finditer(prose)
             for n in m.group(1).replace(" ", "").split(",")}
    if not ranks:
        return
    # Each chunk carries the instance it came from (rag.search_rag_parallel stamps it),
    # so a multi-instance answer attributes each citation to the right corpus.
    by_rank = {c.get("n"): c.get("instance") for c in chunks}
    per_inst: dict[str, list[int]] = {}
    for r in sorted(ranks):
        inst = by_rank.get(r)
        if inst:
            per_inst.setdefault(inst, []).append(r)
    # Ranks go to every corpus that supplied one, but the answer itself is counted once:
    # summing a per-instance "answers with citations" across corpora made a single
    # answer look like N, and unlocked the advice on a fraction of the evidence it
    # claims to need. It lands on whichever corpus supplied the most cited chunks.
    owner = max(per_inst, key=lambda i: (len(per_inst[i]), i)) if per_inst else None
    for inst, rs in per_inst.items():
        state.record_citations(inst, rs, count_answer=(inst == owner))


def _cut_for_regenerate(sid: str, replace_from) -> dict:
    """On a regenerate, truncate the stored transcript to the replaced answer's position.

    The user turn that produced it is dropped too, because the caller re-appends it —
    truncating at the assistant index alone would duplicate the question. Returns the
    conversation's own fields (title, a name the user gave, pin, creation time) when the
    first user turn goes, for the caller to carry over to its successor; else {}.
    """
    carried: dict = {}
    if valid_replace_index(replace_from, len(state._sessions[sid])):
        cut = replace_from
        if cut > 0 and state._sessions[sid][cut - 1].get("role") == "user":
            cut -= 1
        if cut == 0:
            carried = sessions.session_meta(state._sessions[sid])
        del state._sessions[sid][cut:]
    return carried


def _previous_cited_sources(sid: str, replace_from, query: str, instances: list,
                            source_filter: str = "") -> list[dict]:
    """The sources of the answer before this turn, for a documents-only follow-up that
    retrieved nothing on its own wording; [] unless all of these hold:

      * that answer cited them (rag.cited_sources) and was not a refusal;
      * it was asked against the same knowledge bases as this turn (its stored ``kbs``, as
        a set) — after switching the selection, a #mention or a project this turn must
        not be handed another knowledge base's passages;
      * this question is a follow-up on it (rag.is_follow_up), not one of its own.

    Only passages from this turn's knowledge bases (``instances``) whose source matches
    ``source_filter`` are returned. On a regenerate, "before this turn" is before the turn
    being replaced."""
    msgs = state._sessions.get(sid) or []
    end = len(msgs)
    if valid_replace_index(replace_from, len(msgs)):
        end = replace_from - 1 if replace_from > 0 and msgs[replace_from - 1].get("role") == "user" else replace_from
    for j in range(end - 1, -1, -1):
        m = msgs[j]
        if not (isinstance(m, dict) and m.get("role") == "assistant"):
            continue
        meta = m.get("meta") or {}
        if meta.get("not_found") or meta.get("search_failed"):
            return []
        wanted = {str(i) for i in instances or [] if i}
        prev_kbs = meta.get("kbs")
        if not wanted or not isinstance(prev_kbs, list) or {str(k) for k in prev_kbs} != wanted:
            return []
        cited = rag.cited_sources(m.get("content"), meta.get("chunks"))
        sf = (source_filter or "").casefold()
        cited = [c for c in cited if c.get("instance") in wanted
                 and (not sf or sf in str(c.get("source", "")).casefold())]
        if not cited:
            return []
        q_before = msgs[j - 1] if j > 0 and isinstance(msgs[j - 1], dict) else {}
        context = [q_before.get("content") if q_before.get("role") == "user" else "", m.get("content")]
        context += [str(c.get("text", "")) for c in cited] + [str(c.get("source", "")) for c in cited]
        return cited if rag.is_follow_up(query, context) else []
    return []


def _documents_only_outcome(sid: str, replace_from, rag_used: bool, chunks: list, file_context,
                            search_errors: list, query: str = "", instances: list | None = None,
                            source_filter: str = "") -> tuple:
    """What a documents-only turn with no retrieved chunk answers from. Returns
    (fixed answer or None, chunks, reused_sources, search_failed):

      * the search failed → the "Search failed: …" answer (not "not in your documents");
      * nothing retrieved, and this question is a follow-up on a previous answer that cited
        its sources from the same knowledge bases → those sources again, for the
        constrained model (a follow-up such as "shorten that" retrieves nothing on its own
        wording; see _previous_cited_sources);
      * otherwise → documents_only_refusal's answer (None when a document is attached).
    """
    if chunks or rag.documents_only_refusal(rag_used, chunks, file_context) is None:
        return None, chunks, False, bool(search_errors) and not chunks
    if search_errors:
        return rag.documents_only_search_failed(search_errors), [], False, True
    if rag_used:
        prev = _previous_cited_sources(sid, replace_from, query, instances or [], source_filter)
        if prev:
            for c in prev:
                c.pop("n", None)
            rag.number_chunks(prev)
            return None, prev, True, False
    return rag.documents_only_refusal(rag_used, chunks, file_context), chunks, False, False


def _conversation_fields(msg: dict, sid: str) -> dict:
    """The conversation's own fields a chat turn carries, to store with it (see
    sessions.set_conversation_fields): its knowledge-base selection (``kb_select``), its
    prompt template (``template``, by name; "" = Default) and — on its first turn only —
    the project it is started in (``project``, an existing project's id). Later turns do
    not move a conversation between projects: PATCH /api/sessions/{sid} does."""
    out: dict = {}
    ks = sessions.clean_kb_select(msg.get("kb_select"))
    if ks is not None:
        out["kb_select"] = ks
    t = msg.get("template")
    if isinstance(t, str) and len(t) <= 120:
        out["template"] = t.strip() or None
    first = not any(isinstance(m, dict) and m.get("role") == "user"
                    for m in state._sessions.get(sid) or [])
    if first:
        p = projects.resolve(msg.get("project"))
        if p is not None:
            out["project"] = p["id"]
    return out


async def _store_turn(sid: str, conv: dict | None) -> None:
    """Save the conversation with this turn, its conversation fields applied first. The
    project a first turn resolved when it started is checked again: deleted while the turn
    ran (projects.unbind_all found no turn in memory to unfile), it is not stored."""
    fields = dict(conv or {})
    if fields.get("project") and await asyncio.to_thread(projects.resolve, fields["project"]) is None:
        fields.pop("project")
    if fields:
        sessions.set_conversation_fields(state._sessions[sid], **fields)
    await asyncio.to_thread(sessions.save_session, sid, state._sessions[sid])


async def _answer_not_found(ws: WebSocket, sid: str, query: str, answer: str, chunks: list,
                            rag_used: bool, provider: str, model: str, rag_instances: list,
                            replace_from, latency: dict, release=None, conv=None,
                            search_failed: bool = False) -> None:
    """Deliver a documents-only turn's fixed "not in your documents" answer.

    Sent through the same frames as a streamed answer (stream_start, rag_context, one
    final token, stream_end) so the page renders, stores and regenerates it like any
    other; no model is called and nothing is cached. A first turn is titled after its
    question, as a cached first answer is.
    """
    outcome = {"search_failed": True} if search_failed else {"not_found": True}
    await ws.send_json({"type": "stream_start"})
    await ws.send_json({"type": "rag_context", "chunks": chunks, "rag_used": rag_used,
                        "docs_only": True, **outcome, "latency": latency})
    await ws.send_json({"type": "token", "content": answer, "done": True})
    carried = _cut_for_regenerate(sid, replace_from)
    state._sessions[sid].append({"role": "user", "content": query,
                                 "meta": {**sessions._turn_meta(), **carried}})
    state._sessions[sid].append({"role": "assistant", "content": answer,
                                 "meta": sessions._turn_meta(chunks, latency, provider, model,
                                                             kbs=rag_instances, docs_only=True,
                                                             rag_used=rag_used, not_found=not search_failed,
                                                             search_failed=search_failed)})
    title = None
    if len(state._sessions[sid]) == 2:
        title = re.sub(r"\s+", " ", query).strip()[:60] or None
        if not sessions.set_title(sid, title or ""):
            title = None                       # a name the user gave stays, and is not resent
    await _store_turn(sid, conv)
    await ws.send_json({"type": "stream_end", "latency": latency, "usage": None,
                        "title": None, "cache": None, **outcome})
    if release is not None:
        release()
    if title:
        try:
            await ws.send_json({"type": "session_title", "title": title})
        except Exception:
            pass


async def handle_chat(ws: WebSocket, sid: str, msg: dict, release=None):
    """
    Process one chat turn:
    1. Semantic cache lookup  — return immediately on hit
    2. RAG retrieval          — find relevant chunks from the knowledge base
    3. Build message history  — last 10 turns + system prompt with context
    4. Stream LLM response    — Ollama / Claude / OpenAI token-by-token
    5. Auto-title generation  — on first turn, generate a short session title
    6. Cache store            — save the response for future similar queries

    ``release``, when given (see _run_turn), is called right after stream_end: from
    there on this turn no longer holds the conversation or its socket.
    """
    query         = msg.get("content", "")
    provider      = msg.get("provider", state._config.get("provider", "ollama"))
    model         = msg.get("model", "")
    images        = msg.get("images", [])       # list of base64 data URIs
    # base_instruction (always) + the selected template's system prompt (additive)
    system_prompt = config.compose_system_prompt(msg.get("system_prompt"))
    source_filter = msg.get("source_filter", "")   # optional substring filter on chunk source
    bypass_cache  = msg.get("bypass_cache", False)  # skip cache lookup (re-run fresh)
    # Documents only: answer from the retrieved chunks (or an attached document) or say
    # they do not contain it — never from general knowledge. JSON true only.
    documents_only = msg.get("documents_only") is True
    # Regenerate: drop the answer being replaced and everything after it, so the
    # stored transcript matches the client's view. Without this the rejected answer
    # stays in Redis, is re-sent as context on the next turn, and reappears on reload.
    replace_from  = msg.get("replace_from")
    file_context  = msg.get("file_context", [])    # list of {name, text} uploaded documents
    # The conversation's project / knowledge-base selection / template, stored with it.
    conv          = await asyncio.to_thread(_conversation_fields, msg, sid)

    # ── RAG instance selection ───────────────────────────────────────────────
    # Accept either:
    #   rag_instances: ["inst1", "inst2"]  → parallel multi-instance query
    #   rag_instance:  "inst1"             → single-instance query (legacy/default)
    rag_instances_raw = msg.get("rag_instances")   # multi-instance list
    rag_inst          = msg.get("rag_instance", state._config.get("active_rag", "default"))
    # Normalise to a list for uniform handling below
    # An EXPLICIT empty list means "no RAG" (every instance disabled in the UI).
    # Treating it as falsy fell back to active_rag, so RAG could never be turned
    # off from the client and every answer carried the grounding instructions.
    if isinstance(rag_instances_raw, list):
        rag_instances = [i for i in rag_instances_raw if i]
    else:
        rag_instances = [rag_inst] if rag_inst else []
    parallel_mode = len(rag_instances) > 1
    t0            = time.time()

    # Fall back to default model for the active provider if none specified
    if not model:
        if provider == "claude":
            model = state._config.get("claude", {}).get("model", "claude-sonnet-4-6")
        elif provider == "openai":
            model = state._config.get("openai", {}).get("model", "gpt-4o")
        elif provider == "qwen":
            model = state._config.get("qwen", {}).get("model", "qwen-plus")
        elif provider == "mistral":
            model = state._config.get("mistral", {}).get("model", "mistral-small-latest")
        elif provider == "groq":
            model = state._config.get("groq", {}).get("model", "llama-3.3-70b-versatile")
        elif provider == "gemini":
            model = state._config.get("gemini", {}).get("model", "gemini-3-flash-preview")
        else:
            model = state._config.get("ollama", {}).get("model", "")

    if not model:
        await ws.send_json({"type": "error", "content": "No model selected."})
        return

    # ── 1. Semantic cache check ─────────────────────────────────────────────
    # Skip cache entirely for vision requests: same text + different image ≠ same answer.
    # Also skipped when an uploaded file is in context — that content is private to
    # this turn and must never seed an entry another question could match.
    await ws.send_json({"type": "status", "phase": "cache"})
    cache_threshold = cache.effective_threshold()
    effective_rag   = await cache._effective_rag_instances(rag_instances)
    # A request to draw something gets its own partition and is matched only on an exact
    # question hash: two genuinely different charts measure closer together than the
    # threshold accepts, so similarity there answers the wrong request.
    exact_only      = cache.wants_visual(query)
    cache_scope     = cache._cache_scope(effective_rag,
                                   provider, model, source_filter, system_prompt,
                                   visual=exact_only, documents_only=documents_only,
                                   owner=cache.private_partition(state._sessions[sid]))
    # Naming the reason, not just the fact. A user who repeats a question and sees
    # nothing cached has no way to tell "nothing similar was stored" from "this kind of
    # question is never cached" — and concludes, reasonably, that caching is broken.
    cache_kind      = cache.skip_kind(images, file_context, query)
    cache_skipped   = cache.skip_reason(images, file_context, query)
    cacheable       = cache_skipped is None
    t_cache_start   = time.time()
    # The question is embedded once for the whole turn, on first use (cache lookup,
    # retrieval, cache store all read this).
    qvec            = cache.query_vectors(query)
    # Off the event loop: while these run, no other session's tokens can flush and
    # the WS receive loop cannot read an {'type':'abort'} frame (Stop goes dead).
    hit             = (await asyncio.to_thread(cache.cache_lookup, query, cache_threshold,
                                              cache_scope, exact_only, qvec)
                       if cacheable and not bypass_cache else None)
    t_cache         = round(time.time() - t_cache_start, 3)

    if hit:
        # A replayed answer ran no retrieval, so it is absent from every number the
        # advice panel shows. Counted against the corpus that WOULD have been searched
        # (one owner, so the totals still equal the number of questions) purely so the
        # panel can state what share of traffic its numbers speak for.
        if effective_rag:
            state.record_cache_replay(sorted(effective_rag)[0])
            await asyncio.to_thread(rag_admin.save_rag_stats)
        await ws.send_json({
            "type":     "cache_hit",
            "content":  hit["response"],
            "score":    hit["score"],
            "entry_id": hit.get("entry_id", ""),
            "latency":  {"cache": t_cache, "total": round(time.time() - t0, 3)},
        })
        cached_chunks = hit.get("chunks") or []
        # rag_used: a cached answer was grounded iff it carries chunks. Without
        # this the client cannot tell "searched and found nothing" from "RAG was
        # never run", and shows a "no KB match" warning on both.
        await ws.send_json({"type": "rag_context", "chunks": cached_chunks,
                            "rag_used": bool(cached_chunks), "docs_only": documents_only,
                            "latency": {"cache": t_cache, "rag": 0}})
        # Persist the turn. Returning early used to skip this, so a cached answer
        # was missing from the stored transcript — breaking session restore,
        # regenerate-in-place, the version switcher and thumbs feedback at once.
        state._sessions[sid].append({"role": "user", "content": query, "meta": sessions._turn_meta()})
        state._sessions[sid].append({
            "role": "assistant", "content": hit["response"],
            "meta": sessions._turn_meta(cached_chunks,
                               {"cache": t_cache, "total": round(time.time() - t0, 3)},
                               provider, model,
                               cache={"hit": True, "score": hit.get("score")}, kbs=rag_instances,
                               docs_only=documents_only, rag_used=bool(cached_chunks)),
        })
        # A conversation that opens with a cached answer runs no generation, so no title
        # call either: name it after its question instead of leaving it untitled.
        cache_title = None
        if len(state._sessions[sid]) == 2:
            cache_title = re.sub(r"\s+", " ", query).strip()[:60] or None
            if not sessions.set_title(sid, cache_title or ""):
                cache_title = None             # a name the user gave stays, and is not resent
        await _store_turn(sid, conv)
        if cache_title:
            try:
                await ws.send_json({"type": "session_title", "title": cache_title})
            except Exception:
                pass
        return

    # ── 2. RAG retrieval ────────────────────────────────────────────────────
    await ws.send_json({"type": "status", "phase": "rag"})
    rag_cfg       = state._config.get("rag", {})
    rag_threshold = rag_cfg.get("similarity_threshold", 0.75)
    top_k         = rag_cfg.get("top_k", 5)
    hybrid_search = rag_cfg.get("hybrid_search", True)
    # ── HyDE: generate a hypothetical answer and use its embedding for search.
    # Timed on its own — it is an LLM generation, not a Redis/vector cost — so the
    # latency badge attributes it to HyDE instead of silently inflating "rag".
    t_hyde = 0.0
    search_vec: "np.ndarray | None" = None
    if rag_instances and state._config.get("hyde", {}).get("enabled", False):
        await ws.send_json({"type": "status", "phase": "hyde"})
        t_hyde_start = time.time()
        hypothesis = await hyde.hyde_generate(query, provider, model)
        if hypothesis:
            search_vec = (await asyncio.to_thread(embeddings.embed, hypothesis)).astype(np.float32)
            log.info(f"HyDE hypothesis ({len(hypothesis)} chars) embedded for RAG search")
        t_hyde = round(time.time() - t_hyde_start, 3)

    t_rag_start   = time.time()   # retrieval + rerank only; HyDE is timed above

    # With the reranker on, retrieve a WIDER candidate set than we intend to keep —
    # otherwise the cross-encoder is handed exactly the top_k it would have returned
    # anyway and can only permute them, never promote a better chunk from deeper down.
    fetch_k = embeddings._rerank_candidate_k(top_k)
    # A knowledge base that could not be searched (storage down, an index from another
    # embedding model) lands here — an empty result alone cannot tell it from "no match".
    search_errors: list = []

    if parallel_mode:
        # Multi-instance parallel query — search all requested instances simultaneously.
        # search_rag_parallel filters disabled instances internally.
        # source_filter is pushed into each instance's KNN + BM25 query (not post-filtered).
        chunks = await rag.search_rag_parallel(rag_instances, query, fetch_k, rag_threshold,
                                           hybrid_search,
                                           search_vec if search_vec is not None else qvec,
                                           source_filter, errors=search_errors)
        rag_used = True
    elif rag_instances:
        # Single-instance query (normal mode)
        rag_inst = rag_instances[0]
        meta, _ep = await rag_admin._rag_meta_cached_async(rag_inst)   # primes the cache off-loop
        rag_enabled = (meta or {}).get("enabled", True)
        chunks = (
            await asyncio.to_thread(rag.search_rag, rag_inst, query, fetch_k, rag_threshold,
                                    rag_admin.rc_for_instance(rag_inst), hybrid_search,
                                    search_vec if search_vec is not None else qvec, source_filter,
                                    errors=search_errors)
            if rag_enabled else []
        )
        # search_rag_parallel stamps this; the single-instance path did not, so citation
        # tracking recorded nothing at all on the default (single-corpus) topology.
        for _c in chunks:
            _c.setdefault("instance", rag_inst)
        # A disabled instance means RAG did not actually run.
        rag_used = rag_enabled
    else:
        chunks = []
        rag_used = False

    # ── Cross-encoder reranking (runs after fast retrieval, before LLM)
    if chunks:
        top_n = state._config.get("reranker", {}).get("top_n", top_k)
        chunks = await asyncio.to_thread(embeddings.rerank_chunks, query, chunks, top_n)
    # The candidate pool was widened for the reranker; trim it back whatever the
    # outcome. rerank_chunks returns its input UNCHANGED when the cross-encoder is
    # unavailable or raises, so gating this on reranker.enabled left the full
    # widened pool (40 chunks) in the prompt on exactly that failure path.
    if len(chunks) > top_k:
        chunks = chunks[:top_k]
    # Numbered once the list is final, so the stamp matches the prompt exactly.
    rag.number_chunks(chunks)

    t_rag = round(time.time() - t_rag_start, 3)

    search_failed = bool(search_errors) and not chunks
    reused_sources = False
    # Documents only, and nothing to answer from: the fixed answer, no model call — "not
    # in your documents", or "search failed" when the search itself failed. A follow-up
    # that retrieves nothing answers from the previous answer's cited sources instead.
    refusal = None
    if documents_only:
        refusal, chunks, reused_sources, search_failed = _documents_only_outcome(
            sid, replace_from, rag_used, chunks, file_context, search_errors,
            query, rag_instances, source_filter)
    if refusal:
        await _answer_not_found(ws, sid, query, refusal, chunks, rag_used, provider, model,
                                rag_instances, replace_from,
                                {"cache": t_cache, "hyde": t_hyde, "rag": t_rag, "llm": 0,
                                 "total": round(time.time() - t0, 3)}, release, conv,
                                search_failed=search_failed)
        return
    if reused_sources:
        # Answered from another turn's sources: not an answer to replay for this question.
        cacheable = False

    # Per-turn context (uploaded files + retrieved RAG) rides on the FINAL user
    # turn, NOT the system prompt. Keeping the system prefix byte-stable across a
    # conversation is what lets Claude cache it (cache_control) and OpenAI-compatible
    # providers auto-cache it; it also keeps this volatile, per-query content out of
    # the resent history. The turn is still STORED as the raw query (below), so
    # nothing here bloats history or the semantic cache.
    context_parts: list[str] = []
    if file_context:
        file_parts = [f"[File: {f['name']}]\n{f['text']}" for f in file_context if f.get("text")]
        if file_parts:
            context_parts.append("The user has attached the following document(s) — use them to answer:\n\n"
                                 + "\n\n---\n\n".join(file_parts))
    # Numbered context + citation instruction, or an explicit abstention notice
    # when a real search came back empty. Gated on rag_used rather than
    # rag_instances: the latter is never empty (an empty list falls back to
    # active_rag), so it would attach the abstention notice to every ungrounded
    # answer — including plain general-knowledge questions and sessions where the
    # user has disabled all instances.
    if documents_only:
        context_parts.append(rag.build_documents_only_prompt(chunks).lstrip())
    elif rag_used:
        context_parts.append(rag.build_context_prompt(chunks).lstrip())
    context_block = "\n\n".join(context_parts)

    # ── 3. Build message list ───────────────────────────────────────────────
    history  = sessions.history_window(state._sessions[sid],
                                       state._config.get("history_max_tokens", 3000))
    messages = [{"role": "system", "content": system_prompt}]
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})

    # Current turn's context precedes the question in the user turn. Vision: wrap
    # as a multi-modal list when images are attached.
    user_text: str = f"{context_block}\n\n{query}" if context_block else query
    user_content: Any = user_text
    if images:
        user_content = (
            [{"type": "text", "text": user_text}]
            + [{"type": "image_url", "image_url": {"url": img}} for img in images]
        )
    messages.append({"role": "user", "content": user_content})

    # ── 4. Stream LLM response ──────────────────────────────────────────────
    # stream_start MUST fire first so the frontend creates the message element
    # and sets currentAiMsgId before rag_context arrives.
    await ws.send_json({"type": "stream_start"})
    await ws.send_json({"type": "rag_context", "chunks": chunks, "rag_used": rag_used,
                        "docs_only": documents_only,
                        **({"search_failed": True} if search_failed else {}),
                        **({"reused_sources": True} if reused_sources else {}),
                        "latency": {"cache": t_cache, "hyde": t_hyde, "rag": t_rag}})

    full_response = ""
    stream_error  = False
    t_llm_start   = time.time()

    usage_sink: dict = {}   # filled by the provider stream when counts are reported
    try:
        # Route to the correct provider
        if provider == "claude":
            stream_gen = providers.claude_stream(messages, model, usage=usage_sink)
        elif provider == "openai":
            stream_gen = providers.openai_stream(messages, model, usage=usage_sink)
        elif provider == "qwen":
            stream_gen = providers.qwen_stream(messages, model, usage=usage_sink)
        elif provider == "mistral":
            stream_gen = providers.mistral_stream(messages, model, usage=usage_sink)
        elif provider == "groq":
            stream_gen = providers.groq_stream(messages, model, usage=usage_sink)
        elif provider == "gemini":
            stream_gen = providers.gemini_stream(messages, model, usage=usage_sink)
        else:
            stream_gen = providers.ollama_stream(messages, model, images or None,
                                                 usage=usage_sink)

        async for token, done in stream_gen:
            full_response += token
            await ws.send_json({"type": "token", "content": token, "done": done})
            if done:
                # Error tokens must not be cached
                if token and (token.startswith("Error:") or "error:" in token.lower()[:20]):
                    stream_error = True
                break

    except asyncio.CancelledError:
        # Client sent abort — clean exit, do not send stream_end (ws_chat handles it)
        raise
    except Exception as e:
        await ws.send_json({"type": "error", "content": str(e)})
        # Report the real phase timings we already have (cache/hyde/rag ran before the
        # LLM error); llm is however far the failed stream got.
        await ws.send_json({"type": "stream_end", "latency": {
            "cache": t_cache, "hyde": t_hyde, "rag": t_rag,
            "llm": round(time.time() - t_llm_start, 3),
            "total": round(time.time() - t0, 3)}, "title": None})
        return

    t_llm  = round(time.time() - t_llm_start, 3)
    total  = round(time.time() - t0, 3)

    # ── Store turn in session (memory + Redis) ──────────────────────────────
    # On a regenerate, truncate to the replaced answer's position first. The
    # user turn that produced it is dropped too, because it is re-appended just
    # below — truncating at the assistant index alone would duplicate the question.
    carried = _cut_for_regenerate(sid, replace_from)
    state._sessions[sid].append({"role": "user", "content": query, "meta": {**sessions._turn_meta(), **carried}})
    state._sessions[sid].append({"role": "assistant", "content": full_response,
                           "meta": sessions._turn_meta(chunks,
                                              {"cache": t_cache, "hyde": t_hyde, "rag": t_rag,
                                               "llm": t_llm, "total": total},
                                              provider, model, usage_sink, cache={"hit": False, "skipped": cache_skipped, "label": cache.skip_label(cache_kind),
                                                     "bypassed": bool(bypass_cache) and cacheable},
                                              kbs=rag_instances, docs_only=documents_only,
                                              rag_used=rag_used, search_failed=search_failed,
                                              reused_sources=reused_sources)})
    await _store_turn(sid, conv)
    if usage_sink.get("prompt") is not None:
        await asyncio.to_thread(sessions.record_usage, provider, model, usage_sink)

    # ── 5. Release the client FIRST ─────────────────────────────────────────
    # stream_end unlocks the composer. Auto-titling is a second, full LLM call
    # (measured at ~423 ms on local gemma4, seconds on a large model) and the cache
    # store touches Redis — neither is something the user should wait behind while
    # staring at a finished answer. Send stream_end now; the title arrives later as
    # its own event.
    await ws.send_json({
        "type":    "stream_end",
        "latency": {"cache": t_cache, "hyde": t_hyde, "rag": t_rag, "llm": t_llm, "total": total},
        "usage":   usage_sink if usage_sink.get("prompt") is not None else None,
        "title":   None,
        "cache":   {"skipped": cache_skipped,
                    "label": cache.skip_label(cache_kind),
                    "bypassed": bool(bypass_cache) and cacheable},
    })
    # The answer is complete and saved: a question queued in another tab can start, and
    # this tab can send its next one (or Stop) without touching the title call below.
    if release is not None:
        release()

    # ── 6. Cache store — off the critical path ──────────────────────────────
    # Tagged with the scope it was produced under, so it can only be replayed for
    # the same corpus/provider/model/prompt. `cacheable` also excludes turns that
    # had an uploaded file in context.
    if not stream_error and cacheable and cache.worth_caching(effective_rag, chunks):
        await asyncio.to_thread(cache.cache_store, query, full_response, chunks, cache_scope, qvec)

    # Which chunks did the answer actually use? The app knows how many it SENT;
    # without this it cannot know how many were read, so "is top_k too high" has no
    # answer but a guess. Ranks are the [n] markers the model wrote, which are the
    # numbers rag.number_chunks stamped onto the prompt.
    if not stream_error and chunks and not reused_sources:
        _record_answer_citations(full_response, chunks)
        await asyncio.to_thread(rag_admin.save_rag_stats)

    # ── 7. Auto-title (first turn only), delivered as a follow-up event ─────
    title_msg = None
    # Not over a name the user gave it (a regenerated first answer carries that name).
    if len(state._sessions[sid]) == 2 and not sessions.has_user_title(state._sessions[sid]):
        try:
            t_payload = [{"role": "user", "content": (
                "Reply with ONLY a short title of 2-5 words for this query. "
                "No punctuation, no explanation, no numbering, no quotes. "
                f"Just the title words. Query: {query}"
            )}]
            title_chunks = ""
            # The title is a real billed LLM call — tally it (it has no chat turn
            # of its own, so it lands only in the cumulative usage counter).
            title_usage: dict = {}

            # Use the same provider that answered the question
            if provider == "claude":
                async for tok, done in providers.claude_stream(t_payload, model, usage=title_usage):
                    title_chunks += tok
                    if done:
                        break
            elif provider == "openai":
                async for tok, done in providers.openai_stream(t_payload, model, usage=title_usage):
                    title_chunks += tok
                    if done:
                        break
            elif provider == "qwen":
                async for tok, done in providers.qwen_stream(t_payload, model, usage=title_usage):
                    title_chunks += tok
                    if done:
                        break
            elif provider == "mistral":
                async for tok, done in providers.mistral_stream(t_payload, model, usage=title_usage):
                    title_chunks += tok
                    if done:
                        break
            elif provider == "groq":
                async for tok, done in providers.groq_stream(t_payload, model, usage=title_usage):
                    title_chunks += tok
                    if done:
                        break
            elif provider == "gemini":
                async for tok, done in providers.gemini_stream(t_payload, model, usage=title_usage):
                    title_chunks += tok
                    if done:
                        break
            else:
                async for tok, done in providers.ollama_stream(t_payload, model, usage=title_usage):
                    title_chunks += tok
                    if done:
                        break
            if title_usage.get("prompt") is not None:
                await asyncio.to_thread(sessions.record_usage, provider, model, title_usage)

            # Clean the response: take first non-empty line, strip list markers
            raw_title  = title_chunks.strip()
            first_line = next((l.strip() for l in raw_title.splitlines() if l.strip()), raw_title)
            first_line = re.sub(r'^[\d]+[.)]\s*|^[-*•]\s*', '', first_line).strip()
            first_line = first_line.strip('"\'').rstrip('.:,;')
            title_msg  = first_line[:60] if first_line else None

        except Exception:
            pass   # title generation is non-critical

    if title_msg:
        if sessions.set_title(sid, title_msg):
            await asyncio.to_thread(sessions.save_session, sid, state._sessions[sid])
        try:
            await ws.send_json({"type": "session_title", "title": title_msg})
        except Exception:
            pass   # client may have navigated away; the title is non-critical

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES — BATCH CHAT (non-streaming REST)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@appcore.app.post("/api/chat")
async def api_chat(payload: dict):
    """
    Non-streaming chat endpoint — returns the full LLM response as JSON.

    Same semantics as the WebSocket handler but suitable for scripting,
    batch pipelines, or any client that does not support WebSockets.

    Request body fields (all optional except ``content``):
      content        — the user message
      session_id     — reuse an existing session (creates new if omitted)
      provider       — "ollama" | "claude" | "openai" | "qwen"
      model          — model name (uses config default if omitted)
      system_prompt  — overrides the default system prompt
      rag_instance   — single RAG instance to query
      rag_instances  — list of RAG instances for parallel multi-instance query
      source_filter  — substring filter applied to chunk sources
      use_cache      — bool (default true); skip semantic cache when false

    Response:
      session_id, response, chunks (list of RAG chunks used)
    """
    query         = payload.get("content", "")
    provider      = payload.get("provider", state._config.get("provider", "ollama"))
    model         = payload.get("model", "")
    # base_instruction (always) + the selected template's system prompt (additive)
    system_prompt = config.compose_system_prompt(payload.get("system_prompt"))
    source_filter = payload.get("source_filter", "")
    images        = payload.get("images", [])       # list of base64 data URIs
    file_context  = payload.get("file_context", []) # list of {name, text} uploaded documents
    use_cache     = bool(payload.get("use_cache", True))
    documents_only = payload.get("documents_only") is True     # see handle_chat
    sid = payload.get("session_id") or f"rest_{uuid.uuid4().hex[:8]}"
    sid = await asyncio.to_thread(sessions.scoped_sid, sid)    # the caller's own (sign-in on)

    # Ensure session exists in memory
    if sid not in state._sessions:
        state._sessions[sid] = await asyncio.to_thread(sessions.load_session, sid)
    conv = await asyncio.to_thread(_conversation_fields, payload, sid)   # see handle_chat

    # Model fallback
    if not model:
        if provider == "claude":
            model = state._config.get("claude", {}).get("model", "claude-sonnet-4-6")
        elif provider == "openai":
            model = state._config.get("openai", {}).get("model", "gpt-4o")
        elif provider == "qwen":
            model = state._config.get("qwen", {}).get("model", "qwen-plus")
        elif provider == "mistral":
            model = state._config.get("mistral", {}).get("model", "mistral-small-latest")
        elif provider == "groq":
            model = state._config.get("groq", {}).get("model", "llama-3.3-70b-versatile")
        elif provider == "gemini":
            model = state._config.get("gemini", {}).get("model", "gemini-3-flash-preview")
        else:
            model = state._config.get("ollama", {}).get("model", "")
    if not model:
        raise HTTPException(400, "No model selected — configure a model in settings")

    # RAG instance resolution — must happen BEFORE the cache check, because the
    # instance set is part of the cache scope (an answer from one corpus must not
    # be replayed for a question asked against another).
    rag_instances_raw = payload.get("rag_instances")
    rag_inst          = payload.get("rag_instance", state._config.get("active_rag", "default"))
    # An explicit empty list means "no RAG" (see the WS path).
    if isinstance(rag_instances_raw, list):
        rag_instances = [i for i in rag_instances_raw if i]
    else:
        rag_instances = [rag_inst] if rag_inst else []
    parallel_mode = len(rag_instances) > 1

    # Semantic cache — skipped for attachments and chart requests (see cache.skip_reason),
    # and any turn carrying an uploaded file (that content is private to the turn).
    effective_rag = await cache._effective_rag_instances(rag_instances)
    exact_only = cache.wants_visual(query)      # see the websocket path
    cache_scope = cache._cache_scope(effective_rag,
                               provider, model, source_filter, system_prompt,
                               visual=exact_only, documents_only=documents_only,
                               owner=cache.private_partition(state._sessions[sid]))
    cache_kind    = cache.skip_kind(images, file_context, query)
    cache_skipped = cache.skip_reason(images, file_context, query)
    cacheable   = cache_skipped is None
    qvec        = cache.query_vectors(query)   # one embedding for the turn (see handle_chat)
    if use_cache and cacheable:
        cache_threshold = cache.effective_threshold()
        _t_cache_start  = time.time()
        hit = await asyncio.to_thread(cache.cache_lookup, query, cache_threshold,
                                      cache_scope, exact_only, qvec)
        if hit:
            t_cache = round(time.time() - _t_cache_start, 3)
            # see the websocket path: counted for coverage, never scored
            if effective_rag:
                state.record_cache_replay(sorted(effective_rag)[0])
                await asyncio.to_thread(rag_admin.save_rag_stats)
            # Persist the cached turn here too — the WS path does, and a
            # transcript that silently omits cached answers breaks restore,
            # regenerate and feedback exactly the same way.
            cached_chunks = hit.get("chunks", [])
            state._sessions[sid].append({"role": "user", "content": query, "meta": sessions._turn_meta()})
            state._sessions[sid].append({
                "role": "assistant", "content": hit["response"],
                "meta": sessions._turn_meta(cached_chunks, {"cache": t_cache}, provider, model,
                                            cache={"hit": True, "score": hit.get("score")},
                                            kbs=rag_instances, docs_only=documents_only,
                                            rag_used=bool(cached_chunks)),
            })
            await _store_turn(sid, conv)
            return {"session_id": sessions.public_sid(sid), "response": hit["response"], "chunks": cached_chunks,
                    "cache_hit": True, "cache_score": hit["score"], "rag_used": bool(cached_chunks),
                    "documents_only": documents_only}

    rag_cfg       = state._config.get("rag", {})
    rag_threshold = rag_cfg.get("similarity_threshold", 0.75)
    top_k         = rag_cfg.get("top_k", 5)
    hybrid_search = rag_cfg.get("hybrid_search", True)

    # HyDE
    search_vec: "np.ndarray | None" = None
    if rag_instances and state._config.get("hyde", {}).get("enabled", False):
        hypothesis = await hyde.hyde_generate(query, provider, model)
        if hypothesis:
            search_vec = (await asyncio.to_thread(embeddings.embed, hypothesis)).astype(np.float32)

    # RAG retrieval — source_filter is applied in-query (KNN + BM25 pre-filter).
    # fetch_k widens the candidate set when reranking is on (see _rerank_candidate_k).
    fetch_k = embeddings._rerank_candidate_k(top_k)
    search_errors: list = []           # see handle_chat
    if parallel_mode:
        chunks = await rag.search_rag_parallel(rag_instances, query, fetch_k, rag_threshold,
                                           hybrid_search,
                                           search_vec if search_vec is not None else qvec,
                                           source_filter, errors=search_errors)
        rag_used = True
    elif rag_instances:
        inst = rag_instances[0]
        meta, _ep = await rag_admin._rag_meta_cached_async(inst)       # primes the cache off-loop
        rag_enabled = (meta or {}).get("enabled", True)
        chunks = (
            await asyncio.to_thread(rag.search_rag, inst, query, fetch_k, rag_threshold,
                                    rag_admin.rc_for_instance(inst), hybrid_search,
                                    search_vec if search_vec is not None else qvec, source_filter,
                                    errors=search_errors)
            if rag_enabled else []
        )
        # Same stamp as the streaming route.
        for _c in chunks:
            _c.setdefault("instance", inst)
        rag_used = rag_enabled
    else:
        chunks = []
        rag_used = False

    top_n = state._config.get("reranker", {}).get("top_n", top_k)
    chunks = await asyncio.to_thread(embeddings.rerank_chunks, query, chunks, top_n)
    # See handle_chat: trim unconditionally, the reranker may be a no-op.
    if len(chunks) > top_k:
        chunks = chunks[:top_k]
    # Numbered once the list is final, so the stamp matches the prompt exactly.
    rag.number_chunks(chunks)

    # Documents only, and nothing to answer from: the fixed answer, no model call (see
    # handle_chat: "search failed", or the previous answer's cited sources for a follow-up).
    search_failed = bool(search_errors) and not chunks
    reused_sources = False
    refusal = None
    if documents_only:
        refusal, chunks, reused_sources, search_failed = _documents_only_outcome(
            sid, None, rag_used, chunks, file_context, search_errors,
            query, rag_instances, source_filter)
    if refusal:
        state._sessions[sid].append({"role": "user", "content": query, "meta": sessions._turn_meta()})
        state._sessions[sid].append({"role": "assistant", "content": refusal,
                                     "meta": sessions._turn_meta(chunks, None, provider, model,
                                                                 kbs=rag_instances, docs_only=True,
                                                                 rag_used=rag_used, not_found=not search_failed,
                                                                 search_failed=search_failed)})
        await _store_turn(sid, conv)
        outcome = {"search_failed": True} if search_failed else {"not_found": True}
        return {"session_id": sessions.public_sid(sid), "response": refusal, "chunks": [], "cache_hit": False,
                "rag_used": rag_used, "documents_only": True, **outcome,
                "cache": None, "usage": None}
    if reused_sources:
        cacheable = False              # answered from another turn's sources

    # Per-turn context rides on the final user turn, not the system prompt — keeps
    # the system prefix stable for provider prompt-caching and out of resent history.
    # See handle_chat for the full rationale. Turn is stored as the raw query below.
    context_parts: list[str] = []
    if file_context:
        file_parts = [f"[File: {f['name']}]\n{f['text']}" for f in file_context if f.get("text")]
        if file_parts:
            context_parts.append("The user has attached the following document(s) — use them to answer:\n\n"
                                 + "\n\n---\n\n".join(file_parts))
    # Only when RAG actually ran. `rag_instances` is never empty (an empty list
    # falls back to active_rag), so gating on it alone attached the abstention
    # notice to every ungrounded answer — including plain general-knowledge
    # questions and sessions where the user had disabled all instances.
    if documents_only:
        context_parts.append(rag.build_documents_only_prompt(chunks).lstrip())
    elif rag_used:
        context_parts.append(rag.build_context_prompt(chunks).lstrip())
    context_block = "\n\n".join(context_parts)

    history  = sessions.history_window(state._sessions[sid],
                                       state._config.get("history_max_tokens", 3000))
    messages = [{"role": "system", "content": system_prompt}]
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})

    # Current turn's context precedes the question in the user turn. Vision: wrap
    # as a multi-modal list when images are attached (mirrors handle_chat) so the
    # provider adapters (_to_ollama_messages / _to_claude_content / the OpenAI-style
    # handlers) forward the image. A plain-text turn silently drops the image and
    # returns a non-vision answer with no error.
    user_text: str = f"{context_block}\n\n{query}" if context_block else query
    user_content: Any = user_text
    if images:
        user_content = (
            [{"type": "text", "text": user_text}]
            + [{"type": "image_url", "image_url": {"url": img}} for img in images]
        )
    messages.append({"role": "user", "content": user_content})

    full_response = ""
    stream_error  = False
    usage_sink: dict = {}
    try:
        if provider == "claude":
            async for tok, done in providers.claude_stream(messages, model, usage=usage_sink):
                full_response += tok
                if done: break
        elif provider == "openai":
            async for tok, done in providers.openai_stream(messages, model, usage=usage_sink):
                full_response += tok
                if done: break
        elif provider == "qwen":
            async for tok, done in providers.qwen_stream(messages, model, usage=usage_sink):
                full_response += tok
                if done: break
        elif provider == "mistral":
            async for tok, done in providers.mistral_stream(messages, model, usage=usage_sink):
                full_response += tok
                if done: break
        elif provider == "groq":
            async for tok, done in providers.groq_stream(messages, model, usage=usage_sink):
                full_response += tok
                if done: break
        elif provider == "gemini":
            async for tok, done in providers.gemini_stream(messages, model, usage=usage_sink):
                full_response += tok
                if done: break
        else:
            async for tok, done in providers.ollama_stream(messages, model, usage=usage_sink):
                full_response += tok
                if done: break
        if full_response.startswith("Error:") or "error:" in full_response.lower()[:20]:
            stream_error = True
    except Exception as e:
        raise HTTPException(500, str(e))

    state._sessions[sid].append({"role": "user", "content": query, "meta": sessions._turn_meta()})
    state._sessions[sid].append({"role": "assistant", "content": full_response,
                           "meta": sessions._turn_meta(chunks, None, provider, model, usage_sink,
                                                      cache={"hit": False, "skipped": cache_skipped,
                                                             "label": cache.skip_label(cache_kind),
                                                             "bypassed": bool(not use_cache) and cacheable},
                                                      kbs=rag_instances, docs_only=documents_only,
                                                      rag_used=rag_used, search_failed=search_failed,
                                                      reused_sources=reused_sources)})
    await _store_turn(sid, conv)
    if usage_sink.get("prompt") is not None:
        await asyncio.to_thread(sessions.record_usage, provider, model, usage_sink)

    if not stream_error and cacheable and cache.worth_caching(effective_rag, chunks):
        await asyncio.to_thread(cache.cache_store, query, full_response, chunks, cache_scope, qvec)

    # Which chunks did the answer actually use? The app knows how many it SENT;
    # without this it cannot know how many were read, so "is top_k too high" has no
    # answer but a guess. Ranks are the [n] markers the model wrote, which are the
    # numbers rag.number_chunks stamped onto the prompt.
    if not stream_error and chunks and not reused_sources:
        _record_answer_citations(full_response, chunks)
        await asyncio.to_thread(rag_admin.save_rag_stats)

    return {"session_id": sessions.public_sid(sid), "response": full_response, "chunks": chunks, "cache_hit": False,
            "rag_used": rag_used, "documents_only": documents_only,
            **({"search_failed": True} if search_failed else {}),
            **({"reused_sources": True} if reused_sources else {}),
            # Same truth the websocket path reports: a client that gets no hit needs to
            # know whether nothing matched or whether this kind of turn is never cached.
            "cache": {"skipped": cache_skipped, "label": cache.skip_label(cache_kind),
                      "bypassed": bool(not use_cache) and cacheable},
            "usage": usage_sink if usage_sink.get("prompt") is not None else None}


