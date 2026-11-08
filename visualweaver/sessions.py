# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.sessions — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import json
import re
import threading
import time
from . import appstore, auth, state

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SESSION PERSISTENCE (the app store: Redis or MongoDB, see appstore)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_SESSION_PREFIX = "session:"
# The registry the conversation list is served from: one hash, field = session id,
# value = that conversation's summary row (JSON). Every save writes the row (before the
# conversation, so a stored conversation always has one), a delete removes it, and the
# listing prunes the rows of expired conversations. Listing reads the registry and the
# remaining lifetimes — it never scans the keyspace. The field _INDEX_META_FIELD of the
# same hash notes that the registry is complete ("v") and the retention it last applied
# ("ttl"): it lives INSIDE the registry, so a registry that is lost loses the note too
# and the next listing rebuilds it from a scan.
_INDEX_KEY = "session_index"
_INDEX_META_KEY = "session_index_meta"      # where 1.16 pre-releases kept the note
_INDEX_META_FIELD = ":registry"             # never a listed conversation id
_INDEX_VERSION = 1
_index_lock = threading.Lock()
# A row younger than this whose conversation is not stored (yet) is left alone by the
# listing's pruning: its save writes the row first and the conversation right after.
_PRUNE_GRACE = 120
# The shortest retention limit accepted (0 = forever is always accepted): see
# config.MIN_SESSION_TTL.
MIN_RETENTION = 3600

# The conversation's own fields live in the meta of its first user turn (the title
# always has); session_meta() carries them over when that turn is replaced.
_SESSION_FIELDS = ("title", "title_user", "pinned", "created", "owner",
                   "project", "kb_select", "template")
TITLE_MAX = 120


def _session_key(sid: str) -> str:
    return f"{_SESSION_PREFIX}{sid}"


# ── Per-user conversations (sign-in on, see auth) ────────────────────────────
# With sign-in on, a conversation is stored under "u.<user tag>.<id the browser chose>":
# every route that takes a conversation id maps it through scoped_sid() first, so one
# user cannot address another's conversation at all — the same id from someone else is
# simply a different (empty) conversation, answered exactly like an unknown id. The
# owner's name is recorded in the first user turn's meta as well. Conversations stored
# without a tag (before sign-in was turned on) have no owner; administrators see them.
_SCOPED_RE = re.compile(r"^u\.[0-9a-f]{16}\.")


def user_prefix(owner: str) -> str:
    return f"u.{auth.partition(owner)}."


def _exists(store, sids: list) -> list:
    return [t != 0 for t in store.ttls([_session_key(s) for s in sids])]


def scoped_sid(sid: str) -> str:
    """The id the signed-in user's conversation ``sid`` is stored under (``sid`` itself
    when sign-in is off). An administrator who names an untagged conversation from before
    sign-in reaches that one, unless one of their own has the same id."""
    owner = auth.owner_id()
    if owner is None:
        return sid
    mine = user_prefix(owner) + sid
    if not auth.is_admin() or _SCOPED_RE.match(sid):
        return mine
    if mine in state._sessions:
        return mine
    if sid in state._sessions:
        return sid
    try:
        own, legacy = _exists(appstore.current(), [mine, sid])
    except Exception:
        return mine
    return sid if legacy and not own else mine


def public_sid(sid: str) -> str:
    """The id the browser knows a stored conversation by (scoped_sid reversed)."""
    owner = auth.owner_id()
    if owner is not None:
        pfx = user_prefix(owner)
        if sid.startswith(pfx):
            return sid[len(pfx):]
    return sid


def visible(sid: str) -> bool:
    """Is this stored conversation the signed-in user's (or untagged, for an admin)?"""
    owner = auth.owner_id()
    if owner is None:
        return True
    if sid.startswith(user_prefix(owner)):
        return True
    return auth.is_admin() and not _SCOPED_RE.match(sid)


def _stamp_owner(sid: str, messages: list) -> None:
    """Record the owner in the first user turn of a conversation in their namespace."""
    owner = auth.owner_id()
    if owner is None or not sid.startswith(user_prefix(owner)):
        return
    first = _first_user(messages)
    if first is not None:
        first.setdefault("meta", {}).setdefault("owner", owner)


def retention_ttl() -> int:
    """How long a conversation is kept after its last turn, in seconds; 0 = forever.
    Never less than MIN_RETENTION: a shorter value, however it got into the config, is
    read as MIN_RETENTION."""
    try:
        t = max(0, int((state._config.get("sessions") or {}).get("ttl") or 0))
    except (TypeError, ValueError):
        return 0
    return MIN_RETENTION if 0 < t < MIN_RETENTION else t


def load_session(sid: str) -> list:
    """Load a session's message list from the app store. Returns [] if not found or persistence disabled."""
    if not state._config.get("sessions", {}).get("persist", True):
        return []
    try:
        raw = appstore.current().get(_session_key(sid))
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return []


# Chunk text stored alongside a session turn is capped so a long conversation
# cannot balloon the session record; the inspector shows the excerpt.
_SESSION_CHUNK_TEXT_MAX = 1200


def _turn_meta(chunks: list | None = None, latency: dict | None = None,
               provider: str = "", model: str = "", usage: dict | None = None,
               cache: dict | None = None, kbs: list | None = None,
               docs_only: bool | None = None, rag_used: bool | None = None,
               not_found: bool = False, search_failed: bool = False,
               reused_sources: bool = False) -> dict:
    """Metadata persisted with a stored turn.

    Sessions used to hold only {role, content}, so everything that makes an answer
    inspectable — its retrieved chunks, timings and the model that produced it —
    was lost the moment the page reloaded: no citations, no RAG inspector, and
    ratings/regenerate could no longer identify the turn they belonged to.

    Only fields the UI can render are kept, and chunk text is truncated.
    NOTE: prompt construction reads `role`/`content` explicitly, so this extra key
    is never sent to a provider.
    """
    meta: dict = {"ts": int(time.time())}
    if usage and "prompt" in usage:
        meta["usage"] = usage   # provider-reported token counts (real, not estimated)
    if provider:
        meta["provider"] = provider
    if model:
        meta["model"] = model
    if latency:
        meta["latency"] = latency
    if kbs:
        # The knowledge bases the question was asked against, listed per conversation.
        meta["kbs"] = [str(k) for k in kbs if k]
    if docs_only is not None:
        # The answer mode this turn ran under (Documents only or normal). The grounding
        # badge and the composer toggle of a reopened conversation read it.
        meta["docs_only"] = bool(docs_only)
    if rag_used is not None:
        # Whether a knowledge-base search ran at all: an answer with no chunks is either
        # "searched, nothing matched" or "no search", and only this tells them apart.
        meta["rag_used"] = bool(rag_used)
    if not_found:
        # A documents-only turn answered with the fixed "not in your documents" text.
        meta["not_found"] = True
    if search_failed:
        # The knowledge-base search itself failed (not "nothing matched").
        meta["search_failed"] = True
    if reused_sources:
        # A documents-only follow-up answered from the previous answer's cited sources.
        meta["reused_sources"] = True
    if cache:
        # How this answer was obtained. Without it a reopened conversation rendered every
        # turn as "no sufficiently similar question was cached" — including the ones that
        # came FROM the cache — asserting a lookup that never happened on this page.
        meta["cache"] = cache
    if chunks:
        meta["chunks"] = [{
            # The citation number this chunk was given in the prompt. Without it a
            # reopened conversation cannot line its [n] markers up with the inspector.
            "n":         c.get("n", i),
            "source":    c.get("source", ""),
            "score":     c.get("score", 0),
            "relevance": c.get("relevance", c.get("score", 0)),
            "lexical":   bool(c.get("lexical", False)),
            "instance":  c.get("instance", ""),
            "text":     (c.get("text", "") or "")[:_SESSION_CHUNK_TEXT_MAX],
            # The cross-encoder's score, when the reranker ran: shown on the source card.
            **({"reranker_score": c["reranker_score"]}
               if isinstance(c.get("reranker_score"), (int, float)) else {}),
        } for i, c in enumerate(chunks, 1)]
    return meta


def _approx_tokens(content) -> int:
    """Cheap token estimate (chars/4) for a stored message. Multimodal list
    content (a vision turn) is measured by its text parts only — image data URIs
    are huge but are never re-sent from history, so counting them would evict real
    conversation. Never loads a tokenizer (this runs on every turn)."""
    if isinstance(content, str):
        return len(content) // 4
    if isinstance(content, list):
        return sum(len(p.get("text", "")) for p in content if isinstance(p, dict)) // 4
    return len(str(content)) // 4


def history_window(msgs: list, max_tokens: int = 3000, hard_cap: int = 20) -> list:
    """The most recent messages that fit an approximate token budget, in
    chronological order. A flat message-count window let a few verbose turns
    (tables, chart JSON) re-bill an unbounded prefix on every subsequent turn;
    this bounds the resent history by size. Always keeps at least the most recent
    message; `hard_cap` bounds the count as a secondary backstop."""
    if max_tokens <= 0:
        return msgs[-hard_cap:] if hard_cap else list(msgs)
    out: list = []
    used = 0
    window = msgs[-hard_cap:] if hard_cap else msgs
    for m in reversed(window):
        t = _approx_tokens(m.get("content", ""))
        if out and used + t > max_tokens:
            break
        out.append(m)
        used += t
    out.reverse()
    # The budget can truncate on an odd boundary, leaving an assistant turn first.
    # Anthropic and Gemini reject a message list whose first non-system turn is the
    # assistant/model role ("first message must use the user role"), so drop any
    # leading assistant turns — the window then starts on a user turn or is empty
    # (messages become [system, user], which is valid). The old flat [-10:] window
    # never hit this because sessions always end on an assistant turn at an even index.
    while out and out[0].get("role") == "assistant":
        out.pop(0)
    return out


def _first_user(msgs: list) -> dict | None:
    return next((m for m in msgs or [] if isinstance(m, dict) and m.get("role") == "user"), None)


def _first_meta(msgs: list) -> dict:
    return (_first_user(msgs) or {}).get("meta") or {}


def session_meta(msgs: list) -> dict:
    """The conversation's own fields — its title, whether the user gave it, its pin and
    its creation time — to carry over when the first user turn is replaced: regenerating
    the first answer deletes that turn and appends it again."""
    m = _first_meta(msgs)
    out = {k: m[k] for k in _SESSION_FIELDS if m.get(k) not in (None, "", False)}
    if "created" not in out and m.get("ts"):
        out["created"] = m["ts"]
    return out


def has_user_title(msgs: list) -> bool:
    """Was the conversation renamed by the user? Such a name is never auto-replaced."""
    return bool(_first_meta(msgs).get("title_user"))


def clean_title(title: str) -> str:
    """A title as stored: one line, single spaces, at most TITLE_MAX characters."""
    return re.sub(r"\s+", " ", title or "").strip()[:TITLE_MAX].strip()


def mark_fork(msgs: list) -> None:
    """A fork is a new conversation: not pinned, created now, named after the original
    with " (fork)" — the name the sidebar gives it, now also after a reload."""
    first = _first_user(msgs)
    if first is None:
        return
    meta = first.setdefault("meta", {})
    meta.pop("pinned", None)
    meta["created"] = int(time.time())
    if meta.get("title"):
        meta["title"] = clean_title(re.sub(r" \(fork\)$", "", meta["title"]) + " (fork)")


# ── Projects (see projects) ──────────────────────────────────────────────────
# A conversation's project, the knowledge-base selection it uses and its prompt template
# are conversation fields like its title: stored in the first user turn's meta, carried
# over by a regenerate of the first answer and by a fork, and listed in its registry row.
# kb_select is "all", "none" or a list of knowledge-base names — what the conversation
# searches by default, as opposed to a turn's own `kbs` (a question restricted with
# #mentions searches those only).
_UNSET = object()


def clean_kb_select(value):
    """A conversation's knowledge-base selection as stored, or None when it is not one."""
    if value in ("all", "none"):
        return value
    if isinstance(value, list) and value:
        from . import projects
        try:
            return projects.clean_kbs(value, what="kb_select")
        except ValueError:
            return None
    return None


def project_of(msgs: list) -> str | None:
    return _first_meta(msgs).get("project") or None


def set_conversation_fields(msgs: list, *, project=_UNSET, kb_select=_UNSET, template=_UNSET) -> bool:
    """Set (or, with None, remove) the conversation's project, knowledge-base selection and
    template on its first user turn. False when the conversation has no user turn yet."""
    first = _first_user(msgs)
    if first is None:
        return False
    meta = first.setdefault("meta", {})
    for key, val in (("project", project), ("kb_select", kb_select), ("template", template)):
        if val is _UNSET:
            continue
        if val is None or val == "":
            meta.pop(key, None)
        else:
            meta[key] = val
    return True


def unbind_project(pid: str, store=None) -> list[str]:
    """Every stored conversation filed under project ``pid`` — whoever owns it — loses
    that project. Each keeps its remaining lifetime (rewritten with what it had left, not
    the retention counted again). Returns the ids of the ones changed."""
    store = store or appstore.current()
    ensure_index(store)
    sids = []
    for sid, raw in _rows_of(store.hgetall(_INDEX_KEY)).items():
        try:
            if json.loads(raw).get("project") == pid:
                sids.append(sid)
        except (TypeError, ValueError, AttributeError):
            continue
    done: list[str] = []
    for sid in sids:
        key = _session_key(sid)
        with appstore.gate.write():
            store = appstore.current()          # the app data may have moved meanwhile
            raw, left = store.get(key), store.ttl(key)
            if not raw:
                continue
            try:
                msgs = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msgs, list) or project_of(msgs) != pid:
                continue
            set_conversation_fields(msgs, project=None)
            store.set(key, json.dumps(msgs), left)
            store.hset(_INDEX_KEY, {sid: json.dumps(_index_row(session_summary(sid, msgs)))})
            done.append(sid)
    return done


def _ttl_for(msgs: list) -> int | None:
    """The TTL a conversation is stored with: none while pinned or kept forever."""
    ttl = retention_ttl()
    return None if (not ttl or _first_meta(msgs).get("pinned")) else ttl


def _index_row(summary: dict) -> dict:
    return {k: v for k, v in summary.items() if k not in ("id", "expires")}


def _save(sid: str, messages: list) -> None:
    _stamp_owner(sid, messages)
    raw = json.dumps(messages)
    row = json.dumps(_index_row(session_summary(sid, messages)))
    ttl = _ttl_for(messages)
    with appstore.gate.write():
        store = appstore.current()
        # The row first: if the conversation write then fails, the listing finds a row
        # with nothing stored and drops it. The other order left a stored conversation
        # with no row — missing from the list for good, the registry being "complete".
        if sid != _INDEX_META_FIELD:
            store.hset(_INDEX_KEY, {sid: row})
        store.set(_session_key(sid), raw, ttl)


def save_session(sid: str, messages: list):
    """Persist a session to the app store — with the retention TTL, none when it is kept
    forever or pinned — and its row in the registry."""
    if not state._config.get("sessions", {}).get("persist", True):
        return
    try:
        _save(sid, messages)
    except Exception as e:
        log.warning(f"Session save failed for {sid}: {e}")


def delete_session_from_redis(sid: str):
    """Remove a session, and its registry row, from the app store."""
    try:
        with appstore.gate.write():
            store = appstore.current()
            store.delete(_session_key(sid))
            store.hdel(_INDEX_KEY, sid)
    except Exception:
        pass


def session_summary(sid: str, msgs: list) -> dict:
    """One sidebar row: the persisted title (set by the first answer, or given by the
    user), the times of the first and last turns, the model of the latest answer, the
    knowledge bases asked, and the pin — so a reloaded sidebar shows the same names in
    the same order and groups. `expires` (epoch seconds; None while the conversation is
    kept) is filled in by list_sessions."""
    preview = model = provider = ""
    kbs: list = []
    for m in msgs:
        if m.get("role") != "assistant":
            continue
        meta = m.get("meta") or {}
        asked = meta.get("kbs")
        if not isinstance(asked, list):        # a turn stored before `kbs` was recorded
            asked = [c.get("instance") for c in meta.get("chunks") or [] if isinstance(c, dict)]
        for k in asked:
            if k and k not in kbs:
                kbs.append(k)
        if meta.get("model"):
            model, provider = meta["model"], meta.get("provider") or ""
    for m in reversed(msgs):
        if m.get("role") == "assistant":
            c = m.get("content", "")
            preview = c[:60] if isinstance(c, str) else ""
            break
    first = _first_meta(msgs)
    times = [int((m.get("meta") or {}).get("ts") or 0) for m in msgs]
    updated = max(times or [0]) or None
    created = first.get("created") or first.get("ts") or min([t for t in times if t] or [0]) or None
    return {"id": sid, "messages": len(msgs), "preview": preview, "title": first.get("title") or None,
            "updated": updated, "created": created, "model": model, "provider": provider,
            "kbs": kbs, "pinned": bool(first.get("pinned")), "expires": None,
            **({"owner": first["owner"]} if first.get("owner") else {}),
            **{k: first[k] for k in ("project", "kb_select", "template") if first.get(k)}}


def set_title(sid: str, title: str) -> bool:
    """Keep a generated title with the conversation (the first user turn's meta). A name
    the user gave it is kept: returns False and changes nothing."""
    msgs = state._sessions.get(sid) or []
    first_user = _first_user(msgs)
    if first_user is None or not title:
        return False
    meta = first_user.setdefault("meta", {})
    if meta.get("title_user"):
        return False
    meta["title"] = title
    return True


def _expires(store, sid: str) -> int | None:
    left = store.ttl(_session_key(sid))
    return int(time.time()) + left if left else None


def update_session(sid: str, *, title: str | None = None, pinned: bool | None = None,
                   project=_UNSET, kb_select=_UNSET) -> dict | None:
    """Rename (a cleaned title, marked as the user's) and/or pin or unpin a conversation,
    and/or file it under a project (None: no project) or change its knowledge-base
    selection — stored at once; a pin also changes its expiry (see _ttl_for). Returns its
    row, or None when no conversation with a user turn has that id. Store errors are
    raised."""
    msgs = state._sessions.get(sid)
    if not msgs:
        msgs = load_session(sid)
    first = _first_user(msgs)
    if first is None:
        return None
    meta = first.setdefault("meta", {})
    if title is not None:
        meta["title"], meta["title_user"] = title, True
    if pinned is not None:
        if pinned:
            meta["pinned"] = True
        else:
            meta.pop("pinned", None)
    set_conversation_fields(msgs, project=project, kb_select=kb_select)
    row = session_summary(sid, msgs)
    if not state._config.get("sessions", {}).get("persist", True):
        return row
    _save(sid, msgs)
    row["expires"] = _expires(appstore.current(), sid)
    return row


# ── the registry ─────────────────────────────────────────────────────────────

def _index_meta(store, index: dict | None = None) -> dict | None:
    """The registry's note (complete + retention applied), or None when the registry
    must be built. ``index``: the registry hash already read (saves a round trip)."""
    if index is None:
        index = store.hgetall(_INDEX_KEY)
    try:
        meta = json.loads(index.get(_INDEX_META_FIELD) or "null")
    except (TypeError, ValueError):
        return None
    return meta if isinstance(meta, dict) and meta.get("v") == _INDEX_VERSION else None


def _write_meta(store, ttl) -> None:
    store.hset(_INDEX_KEY, {_INDEX_META_FIELD: json.dumps({"v": _INDEX_VERSION, "ttl": ttl})})


def _rows_of(index: dict) -> dict:
    """The conversation rows of a registry hash (its note left out)."""
    return {k: v for k, v in index.items() if k != _INDEX_META_FIELD}


def _rebuild_index(store) -> int:
    """Index every stored conversation that has no registry row — the one scan of the
    keyspace, for conversations stored before the registry existed or copied in by a
    move of the app data — and note that the registry is complete."""
    keys = store.keys(_SESSION_PREFIX)
    have = set(store.hgetall(_INDEX_KEY))
    rows = {}
    for i in range(0, len(keys), 200):
        batch = keys[i:i + 200]
        for k, raw in zip(batch, store.mget(batch)):
            sid = k[len(_SESSION_PREFIX):]
            if raw is None or sid in have or sid == _INDEX_META_FIELD:
                continue
            try:
                msgs = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msgs, list):
                rows[sid] = json.dumps(_index_row(session_summary(sid, msgs)))
    with appstore.gate.write():
        # A conversation saved while this ran wrote its own, newer row: keep that one.
        fresh = set(store.hgetall(_INDEX_KEY)) - have
        rows = {sid: r for sid, r in rows.items() if sid not in fresh}
        if rows:
            store.hset(_INDEX_KEY, rows)
        _write_meta(store, None)
    log.info(f"Conversation registry on '{store.name}': {len(rows)} stored conversation(s) indexed")
    return len(rows)


def apply_retention(store=None) -> int:
    """Make the stored conversations follow the retention setting, at once.

    Forever (or a pinned conversation): no expiry. A limit: a conversation with no expiry,
    or with more time left than the limit, gets the limit counted from now; one with less
    left keeps it — unless the limit is longer than the one applied before, which counts
    every conversation's limit again from now. So shortening never deletes anything on
    the spot, lengthening extends everything, and applying the same setting twice (a move
    of the app data, a rebuilt registry) leaves the remaining lifetimes as they are. Each
    later turn counts the limit again from that turn (save_session). Builds the registry
    first if it is missing. Returns how many expiries it set or cleared, and logs both
    numbers (at startup this is how an upgrade's lifted one-day expiries are reported)."""
    store = store or appstore.current()
    with _index_lock:
        meta = _index_meta(store)
        before = meta.get("ttl") if meta else None
        if meta is None:
            _rebuild_index(store)
        ttl = retention_ttl()
        sids, pinned = [], set()
        seen = _rows_of(store.hgetall(_INDEX_KEY))
        for sid, raw in seen.items():
            sids.append(sid)
            if _row_pinned(raw):
                pinned.add(sid)
        longer = bool(ttl) and isinstance(before, int) and ttl > before
        items = []
        for sid, left in zip(sids, store.ttls([_session_key(s) for s in sids])):
            if left == 0:
                continue                                   # gone
            if sid in pinned or not ttl:
                if left is not None:
                    items.append((sid, None))
            elif left is None or left > ttl or longer:
                items.append((sid, ttl))
        with appstore.gate.write():
            # A conversation saved since its row was read (a pin, a new turn) set its own
            # expiry with that save: leave it. Its row changed, so it is skipped here; one
            # saved between this check and the write is set right again just after.
            current = _rows_of(store.hgetall(_INDEX_KEY))
            items = [(sid, t) for sid, t in items if current.get(sid) == seen.get(sid)]
            store.expire_many([(_session_key(sid), t) for sid, t in items])
            after = _rows_of(store.hgetall(_INDEX_KEY))
            fix = [(_session_key(sid), None) for sid, t in items
                   if t and after.get(sid) != seen.get(sid) and _row_pinned(after.get(sid))]
            store.expire_many(fix)
            _write_meta(store, ttl)
    lifted = sum(1 for _sid, t in items if not t)
    log.info(f"Retention ({'forever' if not ttl else f'{ttl} s'}) applied to {len(sids)} stored "
             f"conversation(s) on '{store.name}': {lifted} expir{'y' if lifted == 1 else 'ies'} lifted, {len(items) - lifted} set")
    return len(items)


def _row_pinned(raw) -> bool:
    try:
        return bool(json.loads(raw).get("pinned"))
    except (TypeError, ValueError, AttributeError):
        return False


def ensure_index(store=None, index: dict | None = None) -> bool:
    """The registry is complete and the stored expiries follow the retention setting: one
    read when both hold. Otherwise — no registry yet (an upgraded install, a move of the
    app data) or a retention that changed without a settings save (config.json edited,
    the old one-day default read as forever) — apply_retention does the rest. ``index``:
    the registry hash already read. True when the registry was rebuilt or re-applied."""
    store = store or appstore.current()
    meta = _index_meta(store, index)
    if meta is None or meta.get("ttl") != retention_ttl():
        apply_retention(store)
        return True
    return False


def _prune(store, sids: list) -> None:
    """Remove the rows of conversations that expired or were deleted behind the app's
    back — checked again first, so one saved since the listing read it keeps its row."""
    if not sids:
        return
    dead = [s for s, t in zip(sids, store.ttls([_session_key(s) for s in sids])) if t == 0]
    if dead:
        # A row whose conversation was saved moments ago may be read between the save's
        # two writes (row first): keep it while it is young.
        fresh = int(time.time()) - _PRUNE_GRACE
        index = store.hgetall(_INDEX_KEY)
        dead = [s for s in dead if not _row_recent(index.get(s), fresh)]
    if dead:
        with appstore.gate.write():
            store.hdel(_INDEX_KEY, *dead)


def _row_recent(raw, since: int) -> bool:
    try:
        return int(json.loads(raw).get("updated") or 0) >= since
    except (TypeError, ValueError, AttributeError):
        return False


def _visible_rows(store) -> dict:
    """The registry rows the caller may see, plus the registry's note. With sign-in on, a
    user who is not an administrator reads only the rows under their own prefix (one
    prefix scan of the hash, not every user's rows); an administrator also sees untagged
    conversations, so reads them all."""
    owner = auth.owner_id()
    if owner is None or auth.is_admin():
        return store.hgetall(_INDEX_KEY)
    rows = store.hgetall_prefix(_INDEX_KEY, user_prefix(owner))
    note = store.hgetall_prefix(_INDEX_KEY, _INDEX_META_FIELD).get(_INDEX_META_FIELD)
    if note is not None:
        rows[_INDEX_META_FIELD] = note
    return rows


def list_sessions() -> list[dict]:
    """Every conversation, newest first: those open in memory (newer than their stored
    copy) and the registry's rows for the rest, each with its expiry. Two reads of the
    store — the registry and the remaining lifetimes — and no scan of the keyspace."""
    rows = {sid: session_summary(sid, msgs) for sid, msgs in list(state._sessions.items())
            if visible(sid)}
    try:
        store = appstore.current()
        index = _visible_rows(store)
        if ensure_index(store, index):
            index = _visible_rows(store)
        index = {sid: row for sid, row in _rows_of(index).items() if visible(sid)}
        sids = list(dict.fromkeys([*rows, *index]))
        now = int(time.time())
        gone, broken = [], []
        persist = state._config.get("sessions", {}).get("persist", True)
        for sid, left in zip(sids, store.ttls([_session_key(s) for s in sids])):
            if sid in rows:
                if left == 0 and persist and rows[sid]["messages"]:
                    # Open here, but its stored copy is gone: expired (its last turn is
                    # older than the limit) — not listed — or its save has not landed yet.
                    ttl = _ttl_for(state._sessions.get(sid) or [])
                    last = rows[sid].get("updated") or now
                    if ttl and last + ttl <= now:
                        del rows[sid]
                    else:
                        rows[sid]["expires"] = last + ttl if ttl else None
                    continue
                rows[sid]["expires"] = now + left if left else None
                continue
            if left == 0:                      # expired, or removed behind the app's back
                gone.append(sid)
                continue
            try:
                row = json.loads(index[sid])
            except (TypeError, ValueError):
                row = None
            if not isinstance(row, dict):
                broken.append(sid)
                continue
            rows[sid] = {"title": None, "kbs": [], "pinned": False, **row,
                         "id": sid, "expires": now + left if left else None}
        _prune(store, gone)
        for sid, raw in zip(broken, store.mget([_session_key(s) for s in broken])):
            try:                               # an unreadable row: summarised again
                s = session_summary(sid, json.loads(raw))
            except (TypeError, ValueError, AttributeError):
                continue
            with appstore.gate.write():
                store.hset(_INDEX_KEY, {sid: json.dumps(_index_row(s))})
            rows[sid] = {**s, "expires": _expires(store, sid)}
    except Exception as e:
        log.debug(f"session list: the app store could not be read ({e})")
    out = sorted(rows.values(), key=lambda r: r.get("updated") or 0, reverse=True)
    if auth.owner_id() is not None:
        for r in out:
            r["id"] = public_sid(r["id"])
    return out


def list_sessions_from_redis() -> list[dict]:
    """The stored conversations not open in memory (see list_sessions)."""
    in_memory = {public_sid(s) for s in list(state._sessions) if visible(s)}
    return [r for r in list_sessions() if r["id"] not in in_memory]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TOKEN USAGE — cumulative tally
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Per-turn usage lives in each turn's meta (above). This is the all-time counter
# across every session, so the user can see what a provider/model has consumed
# overall. One hash in the app store, fields "<provider>:<model>:in|out|cached|cache_write".

_USAGE_KEY = "usage:cumulative"


def record_usage(provider: str, model: str, usage: dict) -> None:
    """Add one turn's provider-reported counts to the all-time tally."""
    try:
        pfx = f"{provider}:{model}"
        inc = {f"{pfx}:in": int(usage.get("prompt", 0)), f"{pfx}:out": int(usage.get("completion", 0))}
        if usage.get("cached"):
            inc[f"{pfx}:cached"] = int(usage["cached"])
        if usage.get("cache_write"):   # Claude cache-creation tokens, billed ~1.25x input
            inc[f"{pfx}:cache_write"] = int(usage["cache_write"])
        with appstore.gate.write():
            appstore.current().hincrby(_USAGE_KEY, inc)
    except Exception as e:
        log.debug(f"usage tally skipped: {e}")


def usage_totals() -> dict:
    """The all-time tally as {"provider:model": {"in", "out", "cached", "cache_write"}}.

    The four counts are disjoint, as the providers report them: ``in`` is fresh input,
    ``cached`` prompt-cache reads, ``cache_write`` cache creation, ``out`` generated.
    ``cached``/``cache_write`` are absent for providers that do not report them.

    Split on the LAST colon: a model id carries colons of its own (``qwen2.5:7b``),
    so partitioning on the first would truncate it and merge two models into one row.
    """
    out: dict = {}
    try:
        for k, v in appstore.current().hgetall(_USAGE_KEY).items():
            pfx, _, kind = k.rpartition(":")
            out.setdefault(pfx, {})[kind] = int(v)
    except Exception:
        pass
    return out


def clear_usage() -> None:
    """Reset the all-time tally. Per-turn usage already stored in session
    history is untouched — this only zeroes the cumulative counter."""
    try:
        with appstore.gate.write():
            appstore.current().delete(_USAGE_KEY)
    except Exception as e:
        log.debug(f"usage clear skipped: {e}")
