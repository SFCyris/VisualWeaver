# SPDX-License-Identifier: AGPL-3.0-or-later
"""Durable, organised conversation history, on both app stores.

Conversations are kept forever unless a retention limit is set; a change of the limit
applies to every stored conversation; pinned conversations never expire; titles,
model, knowledge bases and times are stored per conversation; rename and pin go
through PATCH /api/sessions/{sid}; and the list is served from a registry, not a scan
of the keyspace.

Every store case runs on the suite's private Redis and on the throwaway MongoDB from
conftest.mongo_uri (its own database, dropped afterwards; skipped without one).
"""
import asyncio
import json
import os
import time

import pytest
from fastapi import HTTPException

from conftest import KEY_PREFIX, REDIS_DB, REDIS_HOST, REDIS_PORT

DAY, MONTH = 86400, 30 * 86400
MONGO_EP = "history-mongo"


@pytest.fixture(params=["redis", "mongodb"])
def app_store(request, app_module, cfg, clean_redis, monkeypatch):
    """The app's own data on the given store, its session keys under KEY_PREFIX."""
    from visualweaver import appstore, mongo_store, redis_store, sessions, state
    monkeypatch.setattr(sessions, "_SESSION_PREFIX", f"{KEY_PREFIX}session:")
    monkeypatch.setattr(sessions, "_INDEX_KEY", f"{KEY_PREFIX}session_index")
    monkeypatch.setattr(sessions, "_INDEX_META_KEY", f"{KEY_PREFIX}session_index_meta")
    cfg["redis"] = {"host": REDIS_HOST, "port": REDIS_PORT, "db": REDIS_DB, "password": "", "ssl": False}
    cfg["sessions"] = {"persist": True, "ttl": 0}
    db = None
    if request.param == "redis":
        cfg["storage"] = {"default_type": "redis", "app": "default"}
    else:
        uri = request.getfixturevalue("mongo_uri")
        db = f"vwtest_hist_{os.getpid()}_{int(time.time() * 1000) % 10**8}"
        cfg["redis_endpoints"] = [{"name": MONGO_EP, "type": "mongodb", "uri": uri, "database": db,
                                   "username": "", "password": ""}]
        cfg["storage"] = {"default_type": "redis", "app": MONGO_EP}
    redis_store.invalidate_redis_clients()
    live_before = dict(state._sessions)
    state._sessions.clear()
    store = appstore.current()
    assert store.kind == request.param
    yield store
    state._sessions.clear()
    state._sessions.update(live_before)
    if db:
        try:
            redis_store.r_for(MONGO_EP).client.drop_database(db)
        except Exception:
            pass
    redis_store.invalidate_redis_clients()


def _conv(q="What is the boiler pressure?", a="About 1.5 bar.", ts=1_700_000_000, *, model="llama3:latest",
          kbs=("manuals",), title=None):
    user = {"role": "user", "content": q, "meta": {"ts": ts}}
    if title:
        user["meta"]["title"] = title
    ans = {"role": "assistant", "content": a,
           "meta": {"ts": ts + 3, "provider": "ollama", "model": model,
                    "chunks": [{"n": 1, "source": "boiler.pdf", "instance": kbs[0] if kbs else "", "text": "x"}]}}
    if kbs:
        ans["meta"]["kbs"] = list(kbs)
    return [user, ans]


def _rows():
    from visualweaver import routes_misc
    return {r["id"]: r for r in json.loads(routes_misc.api_sessions().body)}


def _patch(sid, body):
    from visualweaver import routes_misc
    return routes_misc.api_update_session(sid, body)


# ── retention ────────────────────────────────────────────────────────────────

def test_new_conversations_never_expire_by_default(app_store):
    from visualweaver import constants, sessions
    assert constants.DEFAULT_CONFIG["sessions"]["ttl"] == 0
    sessions.save_session("a", _conv())
    assert app_store.ttl(sessions._session_key("a")) is None
    assert _rows()["a"]["expires"] is None


def test_a_retention_limit_expires_conversations_and_rows_say_when(app_store, cfg):
    from visualweaver import sessions
    cfg["sessions"]["ttl"] = DAY
    sessions.apply_retention()
    sessions.save_session("a", _conv())
    t = app_store.ttl(sessions._session_key("a"))
    assert t is not None and DAY - 10 <= t <= DAY
    exp = _rows()["a"]["expires"]
    assert exp is not None and abs(exp - (time.time() + DAY)) < 15


def test_changing_retention_applies_to_existing_conversations(app_store, cfg):
    """Forever → 1 day → 30 days → forever, with conversations already stored: each
    change is applied at once; a pinned conversation keeps no expiry throughout."""
    from visualweaver import sessions
    cfg["sessions"]["ttl"] = DAY
    sessions.apply_retention()
    for sid in ("a", "b", "p"):
        sessions.save_session(sid, _conv())
    _patch("p", {"pinned": True})
    ttl = lambda sid: app_store.ttl(sessions._session_key(sid))   # noqa: E731
    assert DAY - 10 <= ttl("a") <= DAY and ttl("p") is None

    cfg["sessions"]["ttl"] = MONTH
    sessions.apply_retention()
    assert MONTH - 10 <= ttl("a") <= MONTH and MONTH - 10 <= ttl("b") <= MONTH
    assert ttl("p") is None
    rows = _rows()
    assert abs(rows["a"]["expires"] - (time.time() + MONTH)) < 15 and rows["p"]["expires"] is None

    # Shorter: nothing is deleted on the spot — at most the new limit from now, and a
    # conversation with less time left than that keeps it.
    app_store.set(sessions._session_key("b"), app_store.get(sessions._session_key("b")), 7200)
    cfg["sessions"]["ttl"] = DAY
    sessions.apply_retention()
    assert DAY - 10 <= ttl("a") <= DAY and 7190 <= ttl("b") <= 7200 and ttl("p") is None
    sessions.apply_retention()                                 # the same setting again: no change
    assert 7180 <= ttl("b") <= 7200
    # Longer than before: every conversation gets the new limit from now.
    cfg["sessions"]["ttl"] = MONTH
    sessions.apply_retention()
    assert MONTH - 10 <= ttl("b") <= MONTH

    cfg["sessions"]["ttl"] = 0
    sessions.apply_retention()
    assert ttl("a") is None and ttl("b") is None and ttl("p") is None
    assert all(r["expires"] is None for r in _rows().values())


def test_a_hand_edited_retention_is_applied_on_the_next_listing(app_store, cfg):
    """The registry remembers which retention it last applied: a config that changed
    without a settings save (config.json edited, an upgrade) is applied when the list
    is next read, so nothing keeps an expiry the setting no longer asks for."""
    from visualweaver import sessions
    cfg["sessions"]["ttl"] = DAY
    sessions.save_session("a", _conv())
    _rows()
    assert app_store.ttl(sessions._session_key("a")) is not None
    cfg["sessions"]["ttl"] = 0                   # no save, no apply_retention call
    assert _rows()["a"]["expires"] is None
    assert app_store.ttl(sessions._session_key("a")) is None


def test_expired_and_deleted_conversations_leave_the_list_and_the_registry(app_store, cfg):
    from visualweaver import routes_misc, sessions
    sessions.save_session("keep", _conv())
    sessions.save_session("del", _conv())
    sessions.save_session("gone", _conv())
    sessions.save_session("short", _conv())
    routes_misc.api_delete_session("del")
    app_store.delete(sessions._session_key("gone"))            # removed behind the app's back
    app_store.set(sessions._session_key("short"), app_store.get(sessions._session_key("short")), 1)
    time.sleep(2.1)
    rows = _rows()
    assert set(rows) == {"keep"}
    # pruned, so the registry does not grow with every conversation that ever expired
    assert set(sessions._rows_of(app_store.hgetall(sessions._INDEX_KEY))) == {"keep"}


# ── pins, rename, metadata ───────────────────────────────────────────────────

def test_a_pin_is_stored_with_the_conversation_and_survives_a_restart(app_store, cfg):
    from visualweaver import sessions, state
    cfg["sessions"]["ttl"] = DAY
    sessions.apply_retention()
    sessions.save_session("a", _conv())
    row = _patch("a", {"pinned": True})
    assert row["pinned"] is True and row["expires"] is None
    assert app_store.ttl(sessions._session_key("a")) is None          # pinned: kept
    state._sessions.clear()                                           # a restart
    assert _rows()["a"]["pinned"] is True
    assert sessions.load_session("a")[0]["meta"]["pinned"] is True    # in the stored copy itself
    row = _patch("a", {"pinned": False})
    assert row["pinned"] is False
    t = app_store.ttl(sessions._session_key("a"))
    assert t is not None and DAY - 10 <= t <= DAY                     # back on the retention clock


def test_rename_is_stored_and_never_overwritten_by_an_auto_title(app_store):
    from visualweaver import sessions, state
    sessions.save_session("a", _conv(title="Boiler pressure"))
    row = _patch("a", {"title": "  My   boiler\nnotes  "})
    assert row["title"] == "My boiler notes"
    state._sessions["a"] = sessions.load_session("a")
    assert sessions.set_title("a", "Generated title") is False       # the user's name stays
    assert sessions.has_user_title(state._sessions["a"]) is True
    state._sessions.clear()
    assert _rows()["a"]["title"] == "My boiler notes"
    assert len(_patch("a", {"title": "x" * 500})["title"]) == sessions.TITLE_MAX


@pytest.mark.parametrize("body", [{"title": "   "}, {"title": 5}, {"pinned": "yes"}, {}, {"title": None}])
def test_a_bad_update_is_a_400(app_store, body):
    from visualweaver import sessions
    sessions.save_session("a", _conv())
    with pytest.raises(HTTPException) as e:
        _patch("a", body)
    assert e.value.status_code == 400


def test_updating_a_conversation_that_is_not_stored_is_a_404(app_store):
    with pytest.raises(HTTPException) as e:
        _patch("nope", {"pinned": True})
    assert e.value.status_code == 404


def test_rows_carry_title_model_knowledge_bases_and_times(app_store):
    from visualweaver import sessions
    msgs = _conv(ts=1_000, title="Boiler", model="old-model", kbs=("manuals",)) \
        + _conv(q="And the valve?", a="Half a turn.", ts=2_000, model="llama3:latest", kbs=("manuals", "wiki"))
    msgs.append({"role": "user", "content": "q", "meta": {"ts": 3_000}})
    msgs.append({"role": "assistant", "content": "a",           # a turn stored before `kbs` existed
                 "meta": {"ts": 3_001, "model": "llama3:latest", "chunks": [{"instance": "legacy"}]}})
    sessions.save_session("a", msgs)
    r = _rows()["a"]
    assert (r["title"], r["created"], r["updated"]) == ("Boiler", 1_000, 3_001)
    assert (r["model"], r["provider"]) == ("llama3:latest", "")
    assert r["kbs"] == ["manuals", "wiki", "legacy"]
    assert r["messages"] == 6 and r["preview"] == "a" and r["pinned"] is False


def test_turn_meta_records_the_knowledge_bases_asked(app_module):
    from visualweaver import sessions
    assert sessions._turn_meta(kbs=["a", "b"])["kbs"] == ["a", "b"]
    assert "kbs" not in sessions._turn_meta()


def test_a_regenerated_first_answer_keeps_the_conversations_own_fields(app_module):
    from visualweaver import sessions
    msgs = _conv(title="Mine")
    msgs[0]["meta"].update(title_user=True, pinned=True)
    kept = sessions.session_meta(msgs)
    assert kept == {"title": "Mine", "title_user": True, "pinned": True, "created": msgs[0]["meta"]["ts"]}


def test_a_fork_is_not_pinned_and_is_named_as_a_fork(app_store):
    from visualweaver import routes_misc, sessions
    sessions.save_session("a", _conv(title="Boiler"))
    _patch("a", {"pinned": True})
    sessions.load_session("a")
    res = routes_misc.api_fork_session("a", {"role": "assistant", "content_prefix": "About", "occurrence": 1})
    row = _rows()[res["id"]]
    assert row["pinned"] is False and row["title"] == "Boiler (fork)"
    assert row["created"] >= int(time.time()) - 5


# ── the registry ─────────────────────────────────────────────────────────────

def _no_scan(monkeypatch):
    from visualweaver import appstore

    def boom(self, prefix):
        raise AssertionError(f"the session list scanned the keyspace ({prefix!r})")
    monkeypatch.setattr(appstore.RedisAppStore, "keys", boom)
    monkeypatch.setattr(appstore.MongoAppStore, "keys", boom)


def test_the_list_is_served_from_the_registry_without_a_scan(app_store, monkeypatch):
    from visualweaver import sessions
    for i in range(5):
        sessions.save_session(f"c{i}", _conv(ts=1_000 + i))
    _rows()                                                    # registry built (and noted) once
    _no_scan(monkeypatch)
    rows = [r["id"] for r in sessions.list_sessions()]
    assert rows == [f"c{i}" for i in reversed(range(5))]
    sessions.save_session("c5", _conv(ts=2_000))             # saved after: listed from its row
    sessions.delete_session_from_redis("c0")
    assert [r["id"] for r in sessions.list_sessions()] == ["c5", "c4", "c3", "c2", "c1"]
    app_store.hset(sessions._INDEX_KEY, {"c1": "not json"})    # an unreadable row is summarised again
    assert next(r for r in sessions.list_sessions() if r["id"] == "c1")["updated"] == 1_004
    assert json.loads(app_store.hgetall(sessions._INDEX_KEY)["c1"])["updated"] == 1_004


def test_conversations_stored_before_the_registry_are_indexed_once(app_store, monkeypatch, cfg):
    """An install upgraded from 1.15: blobs only, a one-day TTL each, no registry. The
    first listing indexes them (and applies the retention, here forever); later
    listings do not scan again."""
    from visualweaver import sessions
    for i in range(3):
        app_store.set(sessions._session_key(f"old{i}"), json.dumps(_conv(ts=500 + i, title=f"Old {i}")), DAY)
    assert app_store.hgetall(sessions._INDEX_KEY) == {}
    rows = _rows()
    assert {f"old{i}" for i in range(3)} <= set(rows)
    assert rows["old2"]["title"] == "Old 2" and rows["old2"]["expires"] is None
    assert app_store.ttl(sessions._session_key("old0")) is None   # retention (forever) applied
    _no_scan(monkeypatch)
    assert {f"old{i}" for i in range(3)} <= set(_rows())


# ── the shipped default and existing configs ─────────────────────────────────

@pytest.mark.parametrize("stored, expected", [
    (None, 0),                                                  # no sessions section: new default
    ({"persist": True, "ttl": 86400}, 0),                       # the old shipped default
    ({"persist": True, "ttl": 86400, "retention_chosen": True}, 86400),   # chosen in Settings
    ({"persist": True, "ttl": 3600}, 3600),                     # any other value was hand-set
    ({"persist": False}, 0),
])
def test_the_old_one_day_default_is_read_as_forever(app_module, monkeypatch, tmp_path, stored, expected):
    from visualweaver import config, constants
    p = tmp_path / "config.json"
    p.write_text(json.dumps({} if stored is None else {"sessions": stored}))
    monkeypatch.setattr(constants, "CONFIG_PATH", p)
    loaded = config.load_config()
    assert loaded["sessions"]["ttl"] == expected
    if stored and "persist" in stored:
        assert loaded["sessions"]["persist"] is stored["persist"]


def test_a_settings_save_records_the_choice_and_applies_it(app_store, cfg, monkeypatch, tmp_path):
    from visualweaver import config, constants, routes_settings, sessions
    monkeypatch.setattr(constants, "CONFIG_PATH", tmp_path / "config.json")
    sessions.save_session("a", _conv())
    asyncio.run(routes_settings.api_save_config({"sessions": {"persist": True, "ttl": DAY}}))
    saved = json.loads((tmp_path / "config.json").read_text())["sessions"]
    assert saved == {"persist": True, "ttl": DAY, "retention_chosen": True}
    t = app_store.ttl(sessions._session_key("a"))
    assert t is not None and DAY - 10 <= t <= DAY
    # read back after a restart, the chosen day is a day — not the old default
    assert config.load_config()["sessions"]["ttl"] == DAY
    with pytest.raises(HTTPException) as e:
        asyncio.run(routes_settings.api_save_config({"sessions": {"persist": True, "ttl": -5}}))
    assert e.value.status_code == 400


# ── through the chat WebSocket (fake model; see test_event_loop_responsiveness) ──

from test_event_loop_responsiveness import _ASK, _frames_until, chat   # noqa: E402,F401


def test_regenerating_the_first_answer_keeps_the_name_and_pin_and_skips_the_title_call(chat):
    """↻ on the first answer deletes the first user turn and appends it again — the turn
    that carries the conversation's own fields. They are carried over; and a name the
    user gave is not replaced by a generated one (no title call is made at all)."""
    from visualweaver import state
    sid = f"{KEY_PREFIX}regen_first"
    with chat.websocket_connect(f"/ws/chat/{sid}") as a:
        a.send_json({"content": "q1", **_ASK})
        _frames_until(a, "stream_end")
        _frames_until(a, "session_title")                   # the first turn is titled
        first = state._sessions[sid][0]["meta"]
        first.update(title="My name", title_user=True, pinned=True)
        created = first["ts"]
        n_calls = len(chat.model_calls)
        time.sleep(1.1)                                      # a new turn gets a later ts
        a.send_json({"content": "q1", "replace_from": 1, "bypass_cache": True, **_ASK})
        _frames_until(a, "stream_end")
        time.sleep(0.3)
    meta = state._sessions[sid][0]["meta"]
    assert (meta.get("title"), meta.get("title_user"), meta.get("pinned")) == ("My name", True, True)
    assert meta.get("created") == created
    assert len(chat.model_calls) == n_calls + 1, "a title was generated over the user's name"


def test_an_answer_records_the_knowledge_bases_it_was_asked_against(app_module, cfg, monkeypatch):
    """Through POST /api/chat (fake retrieval and model, as test_workflow_backend does):
    the stored answer names the knowledge base, so the conversation's row can list it."""
    from fastapi.testclient import TestClient
    from visualweaver import appcore, cache, providers, rag, sessions, state

    async def fake_stream(*a, **kw):
        yield "an answer [1]", False
        yield "", True

    monkeypatch.setattr(rag, "search_rag", lambda *a, **kw: [{"text": "t", "source": "s.md", "relevance": .9,
                                                              "instance": "default"}])
    monkeypatch.setattr(providers, "ollama_stream", fake_stream)
    monkeypatch.setattr(cache, "cache_lookup", lambda *a, **kw: None)
    monkeypatch.setattr(cache, "cache_store", lambda *a, **kw: None)
    monkeypatch.setattr(sessions, "save_session", lambda *a, **kw: None)
    monkeypatch.setattr(sessions, "record_usage", lambda *a, **kw: None)
    monkeypatch.setattr(app_module.embeddings, "rerank_chunks", lambda q, chunks, top_n=5: chunks)
    cfg["active_rag"] = "default"
    sid = f"{KEY_PREFIX}kbs"
    with TestClient(appcore.app) as client:
        r = client.post("/api/chat", json={"message": "q", "session_id": sid, "provider": "ollama",
                                           "model": "llama3", "use_cache": False})
    assert r.status_code == 200, r.text
    try:
        assert state._sessions[sid][1]["meta"]["kbs"] == ["default"]
        assert sessions.session_summary(sid, state._sessions[sid])["kbs"] == ["default"]
    finally:
        state._sessions.pop(sid, None)
