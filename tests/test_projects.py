# SPDX-License-Identifier: AGPL-3.0-or-later
"""Projects: a set of knowledge bases, a prompt template, a default model and the Documents
only default that conversations belong to.

Before 1.16.0 the knowledge-base selection was one browser preference for every
conversation: nothing tied a conversation to the knowledge bases it was about, and
reopening it (or opening it in another browser) searched whatever that browser had
selected last.

What must hold:
  * projects are stored in the app store (Redis and MongoDB) and managed through a CRUD
    API that validates what it stores;
  * with sign-in on, projects are managed by administrators and visible to every signed-in
    user, who may file their OWN conversations under one;
  * a conversation belongs to at most one project — set on its first turn, moved only by
    PATCH — and remembers its own knowledge-base selection and template, server-side;
  * deleting a project never deletes a conversation (anyone's): they lose the project and
    keep their remaining lifetime;
  * the answer cache is scoped by what changes an answer (knowledge bases searched,
    template/system prompt, model, Documents only) — the project id itself is not one of
    them, so two projects or a project and a project-less chat share an answer only when
    all of those are identical, and a #mention scopes by the knowledge bases it searched;
  * retrieval advice can be read for one project's knowledge bases.

Store cases run on the suite's private Redis and on the throwaway MongoDB (conftest).
"""
import json
import time

import pytest
from fastapi.testclient import TestClient

from conftest import KEY_PREFIX

from test_session_history import _conv, _patch, _rows, app_store   # noqa: F401  (fixture)
from test_event_loop_responsiveness import _frames_until, chat      # noqa: F401  (fixture)
from test_documents_only import kb                                  # noqa: F401  (fixture)
from test_signin import ALICE, BOB, ROOT, _from, no_auth_env, signin   # noqa: F401  (fixtures)


@pytest.fixture
def store(app_store, monkeypatch):
    """The app store (both kinds) with the projects hash under KEY_PREFIX."""
    from visualweaver import projects
    monkeypatch.setattr(projects, "_KEY", f"{KEY_PREFIX}projects")
    yield app_store
    try:
        app_store.delete(projects._KEY)
    except Exception:
        pass


def _client():
    from visualweaver import appcore
    return TestClient(appcore.app)


# ── CRUD ─────────────────────────────────────────────────────────────────────

def test_projects_are_created_listed_changed_and_deleted(store):
    c = _client()
    r = c.post("/api/projects", json={"name": "  Boiler   manuals ", "color": "#AA3366", "icon": "🔥",
                                      "kbs": ["manuals", "faq", "manuals"], "template": "Concise",
                                      "provider": "ollama", "model": "llama3:latest",
                                      "documents_only": True})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["id"].startswith("p_") and len(p["id"]) == 14
    assert (p["name"], p["color"], p["icon"], p["kbs"], p["template"], p["provider"], p["model"],
            p["documents_only"]) == ("Boiler manuals", "#aa3366", "🔥", ["manuals", "faq"], "Concise",
                                     "ollama", "llama3:latest", True)
    q = c.post("/api/projects", json={"name": "Alpha"}).json()
    assert (q["kbs"], q["template"], q["model"], q["documents_only"], q["color"]) == ([], "", "", None, "")
    # stored in the app store itself, listed by name
    assert set(store.hgetall(f"{KEY_PREFIX}projects")) == {p["id"], q["id"]}
    assert [x["name"] for x in c.get("/api/projects").json()] == ["Alpha", "Boiler manuals"]

    r = c.patch(f"/api/projects/{p['id']}", json={"kbs": ["faq"], "documents_only": None, "name": "Boilers"})
    assert r.status_code == 200 and r.json()["kbs"] == ["faq"] and r.json()["documents_only"] is None
    got = {x["id"]: x for x in c.get("/api/projects").json()}[p["id"]]
    assert (got["name"], got["kbs"], got["template"], got["icon"]) == ("Boilers", ["faq"], "Concise", "🔥")

    assert c.delete(f"/api/projects/{q['id']}").json() == {"ok": True, "unfiled": 0}
    assert [x["id"] for x in c.get("/api/projects").json()] == [p["id"]]
    assert c.delete(f"/api/projects/{q['id']}").status_code == 404
    assert c.patch("/api/projects/p_000000000000", json={"name": "x"}).status_code == 404


@pytest.mark.parametrize("body,why", [
    ({}, "name is required"),
    ({"name": "   "}, "name is required"),
    ({"name": "x" * 61}, "60 characters"),
    ({"name": "a", "color": "red"}, "#rrggbb"),
    ({"name": "a", "color": "#12345"}, "#rrggbb"),
    ({"name": "a", "kbs": "manuals"}, "list"),
    ({"name": "a", "kbs": ["ok", "bad*name"]}, "not a knowledge-base name"),
    ({"name": "a", "kbs": [""]}, "not a knowledge-base name"),
    ({"name": "a", "kbs": [f"k{i}" for i in range(51)]}, "at most 50"),
    ({"name": "a", "provider": "skynet"}, "provider must be one of"),
    ({"name": "a", "documents_only": "yes"}, "documents_only"),
    ({"name": "a", "icon": "<script>"}, "icon"),
    ({"name": "a", "model": 3}, "model"),
])
def test_a_project_that_cannot_be_stored_is_refused(store, body, why):
    r = _client().post("/api/projects", json=body)
    assert r.status_code == 400 and why in r.json()["detail"], r.text
    assert store.hgetall(f"{KEY_PREFIX}projects") == {}


def test_project_names_are_unique_ignoring_case(store):
    c = _client()
    a = c.post("/api/projects", json={"name": "Manuals"}).json()
    assert c.post("/api/projects", json={"name": "manuals"}).status_code == 409
    b = c.post("/api/projects", json={"name": "Other"}).json()
    assert c.patch(f"/api/projects/{b['id']}", json={"name": "MANUALS"}).status_code == 409
    assert c.patch(f"/api/projects/{a['id']}", json={"name": "manuals"}).status_code == 200   # its own name


def test_a_project_name_is_stored_as_text(store):
    """Escaping is the page's job; the API must hand the name back exactly."""
    c = _client()
    name = '<img src=x onerror="alert(1)">'
    p = c.post("/api/projects", json={"name": name}).json()
    assert c.get("/api/projects").json()[0]["name"] == name == p["name"]


# ── conversations in a project ───────────────────────────────────────────────

def test_a_conversation_remembers_its_project_selection_and_template(store):
    """Stored with the conversation (both stores): reopening it — in any browser — reads
    them back from the server, not from the browser's own preference."""
    from visualweaver import sessions, state
    p = _client().post("/api/projects", json={"name": "P", "kbs": ["manuals"]}).json()
    msgs = _conv()
    sessions.set_conversation_fields(msgs, project=p["id"], kb_select=["manuals", "faq"], template="Concise")
    sessions.save_session("c1", msgs)
    state._sessions.clear()                                  # another browser / a restart
    row = _rows()["c1"]
    assert (row["project"], row["kb_select"], row["template"]) == (p["id"], ["manuals", "faq"], "Concise")
    first = sessions.load_session("c1")[0]["meta"]
    assert (first["project"], first["kb_select"], first["template"]) == (p["id"], ["manuals", "faq"], "Concise")
    # a project-less conversation lists no such fields (today's behaviour)
    sessions.save_session("c2", _conv())
    assert not {"project", "kb_select", "template"} & set(_rows()["c2"])


def test_patch_files_a_conversation_under_a_project_and_changes_its_selection(store):
    from visualweaver import sessions, state
    from fastapi import HTTPException
    p = _client().post("/api/projects", json={"name": "P"}).json()
    sessions.save_session("c1", _conv())
    row = _patch("c1", {"project": p["id"], "kb_select": ["faq"]})
    assert row["project"] == p["id"] and row["kb_select"] == ["faq"]
    state._sessions.clear()
    assert _rows()["c1"]["project"] == p["id"]
    assert _patch("c1", {"kb_select": "all"})["kb_select"] == "all"
    row = _patch("c1", {"project": None})
    assert "project" not in row and row["kb_select"] == "all"
    state._sessions.clear()
    assert "project" not in _rows()["c1"]
    for bad in ({"project": "p_000000000000"}, {"project": "nope"}, {"kb_select": []},
                {"kb_select": "some"}, {"kb_select": ["a:b"]}):
        with pytest.raises(HTTPException) as e:
            _patch("c1", bad)
        assert e.value.status_code == 400, bad


def test_a_fork_and_a_regenerated_first_answer_keep_the_project(store):
    from visualweaver import routes_chat, routes_misc, sessions, state
    p = _client().post("/api/projects", json={"name": "P"}).json()
    msgs = _conv()
    sessions.set_conversation_fields(msgs, project=p["id"], kb_select=["manuals"])
    state._sessions["c1"] = msgs
    sessions.save_session("c1", msgs)
    fork = routes_misc.api_fork_session("c1", {"role": "assistant", "content_prefix": "About", "occurrence": 1})
    assert _rows()[fork["id"]]["project"] == p["id"]
    carried = routes_chat._cut_for_regenerate("c1", 1)
    assert carried["project"] == p["id"] and carried["kb_select"] == ["manuals"]


def test_deleting_a_project_unfiles_every_conversation_and_keeps_them(store, cfg):
    """Never deletes a conversation; every one filed under it — any user's, open or only
    stored — loses the project and keeps the lifetime it had left."""
    from visualweaver import projects, sessions, state
    cfg["sessions"]["ttl"] = 3600
    c = _client()
    p = c.post("/api/projects", json={"name": "P"}).json()
    other = c.post("/api/projects", json={"name": "Q"}).json()
    for sid, pid in (("a", p["id"]), ("u.0123456789abcdef.b", p["id"]), ("c", other["id"]), ("d", None)):
        msgs = _conv()
        sessions.set_conversation_fields(msgs, project=pid)
        sessions.save_session(sid, msgs)
    key = sessions._session_key("u.0123456789abcdef.b")
    store.set(key, store.get(key), 600)                      # less left than the retention
    state._sessions["a"] = sessions.load_session("a")        # one is open in memory
    state._sessions["a"][0]["meta"]["kb_select"] = ["kept"]

    r = c.delete(f"/api/projects/{p['id']}")
    assert r.json() == {"ok": True, "unfiled": 2}, r.text    # "a" (open and stored) and "b", once each
    assert projects.get(p["id"]) is None and projects.get(other["id"]) is not None
    for sid in ("a", "u.0123456789abcdef.b"):
        stored = sessions.load_session(sid)
        assert stored and stored[1]["content"] == "About 1.5 bar."
        assert "project" not in stored[0]["meta"]
    assert "project" not in state._sessions["a"][0]["meta"]
    assert state._sessions["a"][0]["meta"]["kb_select"] == ["kept"]
    left = store.ttl(key)
    assert left is not None and 500 <= left <= 600, left      # not counted again from now
    assert sessions.load_session("c")[0]["meta"]["project"] == other["id"]
    state._sessions.clear()
    rows = _rows()
    assert {"a", "c", "d"} <= set(rows)
    assert "project" not in rows["a"] and rows["c"]["project"] == other["id"]
    raw = json.loads(store.hgetall(sessions._INDEX_KEY)["u.0123456789abcdef.b"])
    assert "project" not in raw


def test_a_conversation_under_a_vanished_project_is_listed_without_one(store):
    from visualweaver import sessions
    msgs = _conv()
    sessions.set_conversation_fields(msgs, project="p_abcdefabcdef")
    sessions.save_session("c1", msgs)
    assert "project" not in _rows()["c1"]


# ── sign-in: admins manage projects, everyone sees them ──────────────────────

def test_with_sign_in_projects_are_admin_managed_and_visible_to_all(store, signin):
    from visualweaver import sessions
    r = signin.post("/api/projects", json={"name": "Shared"}, headers=BOB)
    assert r.status_code == 403
    p = signin.post("/api/projects", json={"name": "Shared", "kbs": ["manuals"]}, headers=ROOT)
    assert p.status_code == 200, p.text
    pid = p.json()["id"]
    for who in (ALICE, BOB, ROOT):
        assert [x["id"] for x in signin.get("/api/projects", headers=who).json()] == [pid]
    assert signin.patch(f"/api/projects/{pid}", json={"name": "Mine"}, headers=BOB).status_code == 403
    assert signin.delete(f"/api/projects/{pid}", headers=ALICE).status_code == 403
    assert signin.get("/api/projects", headers=ALICE).json()[0]["name"] == "Shared"
    # a user files their own conversation under it ...
    sessions.save_session("u." + _tag("bob") + ".s1", _conv())
    r = signin.patch("/api/sessions/s1", json={"project": pid}, headers=BOB)
    assert r.status_code == 200 and r.json()["project"] == pid
    # ... and only their own: the same id from alice is a different, unknown conversation
    assert signin.patch("/api/sessions/s1", json={"project": pid}, headers=ALICE).status_code == 404
    # an admin deleting the project unfiles bob's conversation, which stays bob's
    assert signin.delete(f"/api/projects/{pid}", headers=ROOT).json() == {"ok": True, "unfiled": 1}
    rows = signin.get("/api/sessions", headers=BOB).json()
    assert [(r["id"], "project" in r) for r in rows] == [("s1", False)]
    assert signin.get("/api/sessions", headers=ALICE).json() == []


def _tag(user):
    from visualweaver import auth
    return auth.partition(user)


# ── the chat turn ────────────────────────────────────────────────────────────

_P1 = {"id": "p_111111111111", "name": "One", "kbs": ["kb"]}
_P2 = {"id": "p_222222222222", "name": "Two", "kbs": ["kb"]}


@pytest.fixture
def fake_projects(monkeypatch):
    from visualweaver import projects
    known = {_P1["id"]: _P1, _P2["id"]: _P2}
    monkeypatch.setattr(projects, "get", lambda pid: known.get(pid))
    return known


_ASK = {"provider": "ollama", "model": "fake"}


def _turn(ws, **extra):
    ws.send_json({**_ASK, **extra})
    return _frames_until(ws, "stream_end")


def test_a_chat_started_in_a_project_is_filed_under_it_on_its_first_turn(chat, kb, fake_projects):
    """The first turn files it; a later turn naming another project does not move it, and
    the selection / template follow the latest turn. A #mention restricts the search to
    the mentioned knowledge bases for that question only — the selection stays."""
    from visualweaver import state
    sid = f"{KEY_PREFIX}proj_bind"
    with chat.websocket_connect(f"/ws/chat/{sid}") as ws:
        _turn(ws, content="q1", rag_instances=["kb"], kb_select=["kb", "faq"], project=_P1["id"],
              template="Concise")
        _turn(ws, content="q2", rag_instances=["faq"], kb_select=["kb", "faq"], project=_P2["id"],
              template="")
    msgs = state._sessions[sid]
    first = msgs[0]["meta"]
    assert first["project"] == _P1["id"]
    assert first["kb_select"] == ["kb", "faq"]
    assert "template" not in first                           # "" = Default, the latest turn's
    assert msgs[1]["meta"]["kbs"] == ["kb"] and msgs[3]["meta"]["kbs"] == ["faq"]   # what each searched
    assert "project" not in msgs[2]["meta"]


def test_an_unknown_project_or_a_bad_selection_is_not_stored(chat, kb, fake_projects):
    from visualweaver import state
    sid = f"{KEY_PREFIX}proj_unknown"
    with chat.websocket_connect(f"/ws/chat/{sid}") as ws:
        _turn(ws, content="q1", rag_instances=["kb"], project="p_999999999999", kb_select="some")
    first = state._sessions[sid][0]["meta"]
    assert "project" not in first and "kb_select" not in first


def test_a_rest_chat_is_filed_under_its_project_too(chat, kb, fake_projects):
    from visualweaver import state
    r = chat.post("/api/chat", json={**_ASK, "content": "q", "session_id": f"{KEY_PREFIX}rest_p",
                                     "rag_instances": ["kb"], "project": _P2["id"], "kb_select": ["kb"]})
    assert r.status_code == 200, r.text
    first = state._sessions[f"{KEY_PREFIX}rest_p"][0]["meta"]
    assert first["project"] == _P2["id"] and first["kb_select"] == ["kb"]


def test_the_cache_scope_follows_what_changes_the_answer_not_the_project(chat, kb, fake_projects):
    """Two projects with the same knowledge bases, template and model ask under ONE scope
    (the answer would be the same); a project-less chat with those inputs too. A different
    knowledge base, template, model or Documents only is a different scope, and a #mention
    is scoped by the knowledge bases it searched — not by the conversation's selection."""
    from visualweaver import cache, config
    sp = config.compose_system_prompt("Be brief.")
    asks = [
        dict(project=_P1["id"], rag_instances=["kb"], kb_select=["kb"], system_prompt="Be brief."),
        dict(project=_P2["id"], rag_instances=["kb"], kb_select=["kb"], system_prompt="Be brief."),
        dict(rag_instances=["kb"], system_prompt="Be brief."),
        dict(project=_P1["id"], rag_instances=["kb"], kb_select=["kb"], system_prompt="Other."),
        dict(project=_P1["id"], rag_instances=["faq"], kb_select=["kb"], system_prompt="Be brief."),
        dict(project=_P1["id"], rag_instances=["kb"], kb_select=["kb"], system_prompt="Be brief.",
             documents_only=True),
        dict(project=_P1["id"], rag_instances=["kb"], kb_select=["kb"], system_prompt="Be brief.",
             model="fake2"),
    ]
    for i, a in enumerate(asks):
        with chat.websocket_connect(f"/ws/chat/{KEY_PREFIX}proj_scope{i}") as ws:   # first turns
            _turn(ws, content="what is the warranty?", **a)
    s = kb.lookups
    assert len(s) == len(asks)
    assert s[0] == s[1] == s[2], "the project itself changed the scope"
    assert s[0] == cache._cache_scope(["kb"], "ollama", "fake", "", sp)
    assert len({s[0], s[3], s[4], s[5], s[6]}) == 5
    assert s[4] == cache._cache_scope(["faq"], "ollama", "fake", "", sp)      # the mention's KB


# ── retrieval advice for a project ───────────────────────────────────────────

def _stats_row(scores):
    from visualweaver import state
    hist = [0] * state.RAW_HIST_BUCKETS
    for x in scores:
        hist[min(int(x * state.RAW_HIST_BUCKETS), state.RAW_HIST_BUCKETS - 1)] += 1
    return {"queries": len(scores), "hits": len(scores), "raw_sample": list(scores), "raw_hist": hist,
            "raw_score_sum": sum(scores), "scored": len(scores), "errors": 0,
            "answers_with_citations": 0, "cited_hist": [0] * state.CITED_HIST_RANKS}


def test_retrieval_advice_can_be_read_for_one_projects_knowledge_bases(store, monkeypatch):
    from visualweaver import rag_advice, state
    stats = {"hi": _stats_row([0.80 + i * 0.004 for i in range(40)]),
             "lo": _stats_row([0.40 + i * 0.004 for i in range(40)]),
             "other": _stats_row([0.60 + i * 0.004 for i in range(40)])}
    monkeypatch.setattr(state, "_rag_stats", stats)
    c = _client()
    p = c.post("/api/projects", json={"name": "High", "kbs": ["hi", "missing"]}).json()
    whole = c.get("/api/rag/advice").json()
    assert {r["id"] for r in whole["advice"]["corpora"]} == {"hi", "lo", "other"}
    scoped = c.get(f"/api/rag/advice?project={p['id']}").json()
    assert [r["id"] for r in scoped["advice"]["corpora"]] == ["hi"]
    assert scoped["project"] == {"id": p["id"], "name": "High", "kbs": ["hi"]}
    assert scoped["advice"]["samples"] == 40 and whole["advice"]["samples"] == 120
    want = rag_advice.build_advice(stats={"hi": stats["hi"]})
    assert scoped["advice"]["consensus"]["suggested"] == want["consensus"]["suggested"]
    assert scoped["advice"]["consensus"]["suggested"] != whole["advice"]["consensus"]["suggested"]
    # the presets too are read from the project's knowledge bases, not every one
    assert scoped["presets"] != whole["presets"]
    pre = {x["id"]: x for x in scoped["presets"] if x}
    for pid, d in pre.items():
        assert d == rag_advice.preset_diff(pid, stats={"hi": stats["hi"]})
    # a project with no knowledge bases of its own speaks for all of them
    q = c.post("/api/projects", json={"name": "Any"}).json()
    assert {r["id"] for r in c.get(f"/api/rag/advice?project={q['id']}").json()["advice"]["corpora"]} \
        == {"hi", "lo", "other"}
    assert c.get("/api/rag/advice?project=p_000000000000").status_code == 404
