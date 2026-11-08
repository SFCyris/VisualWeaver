# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.projects — projects that conversations belong to.

A project is a name (with an optional colour and icon), a subset of the knowledge bases,
an optional prompt template (by name, as the composer's template picker lists it), an
optional default provider/model and a Documents only default. A conversation belongs to at
most one project: its id is stored with the conversation's own fields (the first user
turn's meta, see sessions), and starting a chat inside a project applies the project's
settings to that conversation.

Projects live in the app store (Redis or MongoDB, see appstore) as one hash: field =
project id, value = the project as JSON. With sign-in on they are managed by
administrators and visible to every signed-in user (the knowledge bases and templates
they name are shared in the same way); anyone may file their own conversations under one.

Deleting a project never deletes a conversation: every conversation filed under it —
whoever owns it — becomes a conversation without a project, keeping its remaining
lifetime.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time

from . import appstore, sessions, state

log = logging.getLogger(__name__)

_KEY = "projects"
_lock = threading.Lock()

MAX_PROJECTS = 200
NAME_MAX = 60
MAX_KBS = 50
_ID_RE = re.compile(r"^p_[0-9a-f]{12}$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# The characters an instance name may not hold (routes_instances._BAD_INSTANCE_CHARS).
_BAD_KB_CHARS = re.compile(r"[*?\[\]\\:]")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
PROVIDERS = ("ollama", "claude", "openai", "qwen", "mistral", "groq", "gemini")
FIELDS = ("name", "color", "icon", "kbs", "template", "provider", "model", "documents_only")


class Invalid(ValueError):
    """A project body that cannot be stored (the message says why)."""


class Conflict(ValueError):
    """Another project already has that name, or the project limit is reached."""


def valid_id(pid) -> bool:
    return isinstance(pid, str) and bool(_ID_RE.match(pid))


def clean_kbs(value, *, what: str = "kbs") -> list[str]:
    """A list of knowledge-base names, in order, without repeats."""
    if not isinstance(value, list):
        raise Invalid(f"{what} must be a list of knowledge-base names")
    out: list[str] = []
    for k in value:
        if not isinstance(k, str) or not k.strip() or len(k) > 64 or _BAD_KB_CHARS.search(k) \
                or _CTRL_RE.search(k):
            raise Invalid(f"{what}: {str(k)[:40]!r} is not a knowledge-base name")
        if k not in out:
            out.append(k)
    if len(out) > MAX_KBS:
        raise Invalid(f"{what}: at most {MAX_KBS} knowledge bases")
    return out


def _clean(body: dict, *, partial: bool) -> dict:
    if not isinstance(body, dict):
        raise Invalid("a JSON object is required")
    out: dict = {}
    if "name" in body or not partial:
        name = body.get("name")
        name = re.sub(r"\s+", " ", name).strip() if isinstance(name, str) else ""
        if not name:
            raise Invalid("name is required")
        if len(name) > NAME_MAX:
            raise Invalid(f"name must be {NAME_MAX} characters or fewer")
        out["name"] = name
    if "color" in body:
        c = body["color"] if body["color"] is not None else ""
        if not isinstance(c, str) or (c and not _COLOR_RE.match(c)):
            raise Invalid("color must be #rrggbb")
        out["color"] = c.lower()
    if "icon" in body:
        ic = body["icon"] if body["icon"] is not None else ""
        if not isinstance(ic, str) or len(ic.strip()) > 8 or _CTRL_RE.search(ic) \
                or re.search(r"[\s<>&\"'`]", ic.strip()):
            raise Invalid("icon must be a short symbol (an emoji), at most 8 characters")
        out["icon"] = ic.strip()
    if "kbs" in body:
        out["kbs"] = clean_kbs(body["kbs"] if body["kbs"] is not None else [])
    if "template" in body:
        t = body["template"] if body["template"] is not None else ""
        if not isinstance(t, str) or len(t) > 120 or _CTRL_RE.search(t):
            raise Invalid("template must be a template name")
        out["template"] = t.strip()
    if "provider" in body:
        p = body["provider"] if body["provider"] is not None else ""
        if p not in ("", *PROVIDERS):
            raise Invalid(f"provider must be one of {', '.join(PROVIDERS)}")
        out["provider"] = p
    if "model" in body:
        m = body["model"] if body["model"] is not None else ""
        if not isinstance(m, str) or len(m) > 200 or _CTRL_RE.search(m):
            raise Invalid("model must be a model name")
        out["model"] = m.strip()
    if "documents_only" in body:
        d = body["documents_only"]
        if d is not None and not isinstance(d, bool):
            raise Invalid("documents_only must be true, false or null (the Settings default)")
        out["documents_only"] = d
    return out


def _defaults() -> dict:
    return {"color": "", "icon": "", "kbs": [], "template": "", "provider": "", "model": "",
            "documents_only": None}


def _read_all(store) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for pid, raw in store.hgetall(_KEY).items():
        try:
            p = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(p, dict) and valid_id(pid) and isinstance(p.get("name"), str):
            out[pid] = {**_defaults(), **{k: p[k] for k in (*FIELDS, "created", "updated") if k in p},
                        "id": pid}
    return out


def list_projects() -> list[dict]:
    """Every project, by name (case-insensitive)."""
    return sorted(_read_all(appstore.current()).values(), key=lambda p: (p["name"].lower(), p["id"]))


def ids() -> set[str]:
    """The ids of the projects that exist (one read)."""
    try:
        return {pid for pid in appstore.current().hgetall(_KEY) if valid_id(pid)}
    except Exception:
        return set()


def get(pid: str) -> dict | None:
    if not valid_id(pid):
        return None
    return _read_all(appstore.current()).get(pid)


def _name_taken(projects: dict, name: str, except_id: str | None = None) -> bool:
    return any(p["name"].lower() == name.lower() and pid != except_id for pid, p in projects.items())


def create(body: dict) -> dict:
    fields = _clean(body, partial=False)
    with _lock:
        store = appstore.current()
        have = _read_all(store)
        if len(have) >= MAX_PROJECTS:
            raise Conflict(f"at most {MAX_PROJECTS} projects")
        if _name_taken(have, fields["name"]):
            raise Conflict(f"a project called {fields['name']!r} already exists")
        pid = "p_" + secrets.token_hex(6)
        now = int(time.time())
        p = {**_defaults(), **fields, "created": now, "updated": now}
        with appstore.gate.write():
            store.hset(_KEY, {pid: json.dumps(p)})
    return {**p, "id": pid}


def update(pid: str, body: dict) -> dict | None:
    """Change some of a project's fields. None when there is no such project."""
    fields = _clean(body, partial=True)
    with _lock:
        store = appstore.current()
        have = _read_all(store)
        if pid not in have:
            return None
        if "name" in fields and _name_taken(have, fields["name"], except_id=pid):
            raise Conflict(f"a project called {fields['name']!r} already exists")
        p = {k: v for k, v in {**have[pid], **fields}.items() if k != "id"}
        p["updated"] = int(time.time())
        with appstore.gate.write():
            store.hset(_KEY, {pid: json.dumps(p)})
    return {**p, "id": pid}


def delete(pid: str) -> int | None:
    """Delete a project; its conversations stay, without a project. Returns how many
    conversations were unfiled, or None when there is no such project."""
    with _lock:
        store = appstore.current()
        if pid not in _read_all(store):
            return None
        with appstore.gate.write():
            store.hdel(_KEY, pid)
    return unbind_all(pid)


def unbind_all(pid: str) -> int:
    """Take every stored conversation — every user's — out of project ``pid``, keeping each
    one's remaining lifetime (sessions.unbind_project). Open conversations in memory too.
    Returns how many conversations were taken out of it."""
    done: set[str] = set()
    for sid, msgs in list(state._sessions.items()):
        if sessions.project_of(msgs) == pid:
            sessions.set_conversation_fields(msgs, project=None)
            done.add(sid)
    try:
        done.update(sessions.unbind_project(pid))
    except Exception as e:
        log.warning(f"Project {pid}: conversations could not all be unfiled ({str(e)[:200]})")
    return len(done)


def resolve(pid) -> dict | None:
    """The project a chat turn names, when it exists (an unknown or deleted one: None)."""
    if not valid_id(pid):
        return None
    try:
        return get(pid)
    except Exception:
        return None


def advice_stats(pid: str, stats: dict | None = None) -> dict | None:
    """The retrieval stats of the project's knowledge bases only (None: no such project).
    A project with no knowledge bases of its own speaks for all of them."""
    p = get(pid)
    if p is None:
        return None
    stats = stats if stats is not None else state._rag_stats
    if not p["kbs"]:
        return dict(stats or {})
    keep = set(p["kbs"])
    return {k: v for k, v in list((stats or {}).items()) if k in keep}
