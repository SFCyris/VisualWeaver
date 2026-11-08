# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.routes_misc — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import asyncio
import copy
import json
import time
import uuid
from datetime import datetime, timezone
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from . import appcore, auth, config, constants, ingest, projects, sessions, state

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES — FEEDBACK
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_MAX_FEEDBACK_ENTRY = 64_000     # characters of one stored rating, as JSON
_MAX_FEEDBACK_DEPTH = 5          # {"sources": [{"source": …}]} is 3


def _depth(v, limit: int = 64) -> int:
    """Nesting depth of a JSON value (1 for a scalar); stops counting past ``limit``."""
    if limit <= 0 or not isinstance(v, (dict, list)):
        return 1
    items = v.values() if isinstance(v, dict) else v
    return 1 + max((_depth(x, limit - 1) for x in items), default=0)


@appcore.app.post("/api/feedback")
async def api_feedback(payload: dict):
    # The whole store is rewritten per rating, so an unbounded body permanently
    # raises the cost of every later write. Truncate rather than reject: a long
    # answer should still be reviewable.
    entry = {
        k: (v[:config._MAX_FEEDBACK_FIELD] if isinstance(v, str) else v)
        for k, v in payload.items()
    }
    # Nested values (the answer's sources) are kept as sent, so they are bounded as a
    # whole: depth and size of the stored entry.
    if _depth(entry) > _MAX_FEEDBACK_DEPTH or \
            len(json.dumps(entry, ensure_ascii=False)) > _MAX_FEEDBACK_ENTRY:
        raise HTTPException(413, f"A rating may be at most {_MAX_FEEDBACK_ENTRY // 1000} KB "
                                 f"and {_MAX_FEEDBACK_DEPTH} levels deep")
    entry["ts"] = datetime.now(timezone.utc).isoformat()
    if auth.owner_id() is not None:
        entry["user"] = auth.owner_id()      # who rated it (sign-in on); not client-settable
    state._feedback.append(entry)
    del state._feedback[:-config._MAX_FEEDBACK]        # bound memory as well as the file
    # Up to 2,000 entries rewritten per rating: in a thread, and atomically.
    await asyncio.to_thread(config.save_feedback)
    return {"ok": True}


@appcore.app.get("/api/feedback")
def api_feedback_list(limit: int = 200, value: str | None = None):
    """Return stored ratings, newest first.

    The store was previously write-only — nothing read it back, so thumbs-down
    ratings could not be reviewed or turned into a regression set. ``value``
    filters to one rating (e.g. ``down``).
    """
    # Accept the words the docstring advertises as well as the raw 1/-1 the client
    # actually posts; "?value=down" previously matched nothing at all.
    _ALIASES = {"down": {"-1"}, "up": {"1"}, "negative": {"-1"}, "positive": {"1"}}
    wanted = _ALIASES.get((value or "").strip().lower(), {str(value)})
    items = state._feedback if not value else [f for f in state._feedback if str(f.get("value")) in wanted]
    # max(0, limit) was a bypass: items[-0:] is items[0:], i.e. everything.
    n = max(1, min(int(limit or 0) or 1, config._MAX_FEEDBACK))
    return {"total": len(items), "items": list(reversed(items[-n:]))}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES — SESSIONS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@appcore.app.get("/api/me")
def api_me():
    """Who is signed in. With sign-in off: {"auth": false, "user": null, "admin": true}."""
    u = auth.current()
    return {"auth": auth.enabled(), "user": u.name if u else None, "id": u.id if u else None,
            "admin": auth.is_admin()}


# Every route below that takes a conversation id maps it through sessions.scoped_sid:
# with sign-in on, the caller can only ever reach their own conversations, and another
# user's id is answered exactly like an id nobody has used.

@appcore.app.get("/api/sessions")
def api_sessions():
    # The conversations open in memory and the stored ones (from the registry, not a
    # scan of the keyspace), newest first, each with title, times, model, knowledge
    # bases, pin and expiry. Plain JSON already: sent as is, without FastAPI's per-value
    # encoder (which took longer than reading the list from the store).
    rows = sessions.list_sessions()
    # A conversation filed under a project that no longer exists (deleted while the
    # conversation could not be rewritten) is listed without one.
    if any(r.get("project") for r in rows):
        known = projects.ids()
        for r in rows:
            if r.get("project") and r["project"] not in known:
                r.pop("project")
    return JSONResponse(rows)

@appcore.app.get("/api/sessions/{sid}")
def api_session(sid: str):
    sid = sessions.scoped_sid(sid)
    if sid in state._sessions:
        return state._sessions[sid]
    # Try Redis
    msgs = sessions.load_session(sid)
    if msgs:
        state._sessions[sid] = msgs   # cache in memory
    return msgs

@appcore.app.patch("/api/sessions/{sid}")
def api_update_session(sid: str, payload: dict):
    """Rename, pin, file under a project and/or set the knowledge-base selection of a
    conversation: {"title": str}, {"pinned": bool}, {"project": "<project id>" | null},
    {"kb_select": "all" | "none" | [names]}. Returns its row as /api/sessions lists it.
    A pinned conversation never expires."""
    if not isinstance(payload, dict) or not ({"title", "pinned", "project", "kb_select"} & set(payload)):
        raise HTTPException(400, "title, pinned, project or kb_select is required")
    title = pinned = None
    extra: dict = {}
    if "project" in payload:
        pid = payload["project"]
        if pid is not None and projects.resolve(pid) is None:
            raise HTTPException(400, "no such project")
        extra["project"] = pid
    if "kb_select" in payload:
        ks = sessions.clean_kb_select(payload["kb_select"])
        if ks is None:
            raise HTTPException(400, 'kb_select must be "all", "none" or a list of knowledge-base names')
        extra["kb_select"] = ks
    if "title" in payload:
        title = sessions.clean_title(payload["title"]) if isinstance(payload["title"], str) else ""
        if not title:
            raise HTTPException(400, "title must be a non-empty string")
    if "pinned" in payload:
        if not isinstance(payload["pinned"], bool):
            raise HTTPException(400, "pinned must be true or false")
        pinned = payload["pinned"]
    sid = sessions.scoped_sid(sid)
    try:
        row = sessions.update_session(sid, title=title, pinned=pinned, **extra)
    except Exception as e:
        raise HTTPException(503, f"The conversation could not be saved ({str(e)[:200]})")
    if row is None:
        raise HTTPException(404, "session not found")
    row["id"] = sessions.public_sid(row["id"])
    return row


@appcore.app.delete("/api/sessions/{sid}")
def api_delete_session(sid: str):
    sid = sessions.scoped_sid(sid)
    state._sessions.pop(sid, None)
    sessions.delete_session_from_redis(sid)
    return {"ok": True}


@appcore.app.post("/api/sessions/{sid}/fork")
def api_fork_session(sid: str, payload: dict):
    """Fork a conversation: a new session holding the messages up to and
    including the anchored one. The original is untouched; the fork continues
    independently from that point.

    The anchor is {role, content_prefix, occurrence} rather than a bare index:
    the client's message array can run AHEAD of the stored one (aborted and
    errored streams are kept client-side but never persisted), so a client
    index would silently fork at the wrong message after any Stop. Content is
    identical for every turn both sides persisted, and turns only the client
    has can never match, so occurrence counting stays aligned."""
    sid = sessions.scoped_sid(sid)
    msgs = state._sessions.get(sid) or sessions.load_session(sid)
    if not msgs:
        raise HTTPException(404, "session not found")
    role = payload.get("role")
    prefix = payload.get("content_prefix")
    occurrence = payload.get("occurrence", 1)
    if role not in ("user", "assistant") or not isinstance(prefix, str) or not prefix:
        raise HTTPException(400, "role and content_prefix are required")
    if not isinstance(occurrence, int) or occurrence < 1:
        raise HTTPException(400, "occurrence must be a positive int")
    at, seen = None, 0
    for i, m in enumerate(msgs):
        c = m.get("content")
        c = c if isinstance(c, str) else str(c)
        if m.get("role") == role and c.startswith(prefix):
            seen += 1
            if seen == occurrence:
                at = i
                break
    if at is None:
        raise HTTPException(409, "message not found in the stored session "
                                 "(it may not have been persisted — e.g. an aborted answer)")
    new_sid = sessions.scoped_sid(f"sess_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}_fork")
    state._sessions[new_sid] = [copy.deepcopy(m) for m in msgs[:at + 1]]
    sessions.mark_fork(state._sessions[new_sid])     # not pinned; "<title> (fork)"
    state.touch_session(new_sid)
    sessions.save_session(new_sid, state._sessions[new_sid])
    return {"id": sessions.public_sid(new_sid), "messages": len(state._sessions[new_sid])}


@appcore.app.get("/api/usage")
def api_usage():
    """All-time provider-reported token usage, per provider:model."""
    return sessions.usage_totals()


@appcore.app.delete("/api/usage")
def api_usage_clear():
    """Reset the all-time token-usage tally (Analytics → Token Usage → Reset)."""
    sessions.clear_usage()
    return {"ok": True}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ROUTES — PROMPT TEMPLATES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@appcore.app.get("/api/templates")
def api_templates():
    return state._config.get("prompt_templates", [])

@appcore.app.post("/api/templates")
async def api_save_templates(payload: list):
    state._config["prompt_templates"] = payload
    await config.save_config_async(state._config)
    return {"ok": True}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FILE UPLOAD — CHAT CONTEXT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@appcore.app.post("/api/chat/upload-file")
async def api_chat_upload_file(file: UploadFile = File(...)):
    """Extract plain text from an uploaded document (TXT, MD, CSV, PDF, DOCX, XLSX).

    The extracted text is returned to the browser and included in the next chat
    message as inline context — similar to pasting the document into the prompt,
    but with server-side format conversion.  Files are NOT stored on disk or
    indexed into Redis; they exist only for the duration of the browser session.
    """
    data = await file.read()
    if len(data) > ingest._CHAT_FILE_MAX_BYTES:
        raise HTTPException(413, f"File too large (max {ingest._CHAT_FILE_MAX_BYTES // (1024*1024)} MB)")
    try:
        text = await asyncio.to_thread(ingest.extract_file_text, file.filename or "", data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(422, f"Could not parse file: {e}")
    truncated = len(text) > ingest._CHAT_FILE_MAX_CHARS
    if truncated:
        text = text[:ingest._CHAT_FILE_MAX_CHARS]
    return {
        "filename": file.filename,
        "chars":    len(text),
        "truncated": truncated,
        "text":     text,
    }


