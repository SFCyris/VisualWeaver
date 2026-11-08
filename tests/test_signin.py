# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign-in through a trusted reverse proxy, with per-user conversations.

Before 1.16 there was no identity at all: anyone who could reach the port could read,
rename and delete every stored conversation, change every setting and wipe the
knowledge bases. With ``auth.mode = "header"`` the user comes from the proxy's header —
only on a connection from a trusted proxy address — every route is classified (public /
any signed-in user / administrators, the default), and a conversation can only be
reached by the user who started it. With sign-in off nothing changes.

Requests are made through TestClient with the connection's peer address set explicitly
(``_from``), the way uvicorn reports it with --no-proxy-headers.
"""
import json
import os
import time

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import KEY_PREFIX, REDIS_DB, REDIS_HOST, REDIS_PORT

PROXY = "10.9.0.2"
STRANGER = "10.9.0.77"
MONGO_EP = "signin-mongo"
_ENV = ("VISUALWEAVER_AUTH", "VISUALWEAVER_AUTH_USER_HEADER", "VISUALWEAVER_AUTH_GROUPS_HEADER",
        "VISUALWEAVER_AUTH_NAME_HEADER", "VISUALWEAVER_TRUSTED_PROXIES", "VISUALWEAVER_ADMIN_USERS",
        "VISUALWEAVER_ADMIN_GROUPS", "VISUALWEAVER_AUTH_CASE_INSENSITIVE_NAMES")

ALICE = {"Remote-User": "alice"}
BOB = {"Remote-User": "bob"}
ROOT = {"Remote-User": "root"}
GROUP_ADMIN = {"Remote-User": "carol", "Remote-Groups": "staff, vw-admins"}


def _from(ip: str) -> TestClient:
    """A client whose connections come from ``ip`` (the ASGI scope's peer address)."""
    from visualweaver import appcore

    async def asgi(scope, receive, send):
        await appcore.app(dict(scope, client=(ip, 40001)), receive, send)
    return TestClient(asgi)


@pytest.fixture
def no_auth_env(monkeypatch):
    for v in _ENV:
        monkeypatch.delenv(v, raising=False)


@pytest.fixture
def signin(app_module, cfg, no_auth_env):
    """Sign-in on: the proxy at PROXY, admin user "root", admin group "vw-admins"."""
    cfg["auth"] = {"mode": "header", "trusted_proxies": [PROXY],
                   "admin_users": ["root"], "admin_groups": ["vw-admins"]}
    return _from(PROXY)


# ── off by default ───────────────────────────────────────────────────────────

def test_with_sign_in_off_nothing_changes(app_module, cfg, no_auth_env):
    """Off (no auth section): no user, every caller is treated as the one user, a header
    from anywhere is ignored, and conversation ids are the ones the browser chose."""
    from visualweaver import auth, sessions
    cfg.pop("auth", None)
    c = _from(STRANGER)
    assert c.get("/api/me").json() == {"auth": False, "user": None, "id": None, "admin": True}
    assert c.get("/api/me", headers=ROOT).json()["auth"] is False
    assert c.get("/api/sessions").status_code == 200
    assert auth.current() is None and sessions.scoped_sid("sess_1") == "sess_1"
    assert sessions.visible("u.0123456789abcdef.x") and sessions.public_sid("x") == "x"


# ── the identity header ──────────────────────────────────────────────────────

def test_the_header_from_an_untrusted_address_is_refused(signin):
    """A client that talks to the app directly and sends Remote-User: root must not become
    root — on any route, the WebSocket and the public health probe included."""
    spoof = _from(STRANGER)
    for method, path in (("GET", "/api/me"), ("GET", "/api/health"), ("POST", "/api/config"),
                         ("GET", "/api/sessions"), ("GET", "/")):
        r = spoof.request(method, path, headers=ROOT, json={} if method == "POST" else None)
        assert r.status_code == 403, (method, path, r.status_code)
    with pytest.raises(WebSocketDisconnect):
        with spoof.websocket_connect("/ws/chat/s1", headers=ROOT) as ws:
            ws.receive_json()
    # loopback is not trusted either unless listed
    assert _from("127.0.0.1").get("/api/me", headers=ROOT).status_code == 403


def test_requests_without_identity_are_401_except_the_health_probe(signin):
    for c in (signin, _from(STRANGER)):
        assert c.get("/api/me").status_code == 401
        assert c.get("/api/sessions").status_code == 401
        assert c.get("/").status_code == 401
        assert c.get("/api/health").status_code == 200       # Docker HEALTHCHECK, start.sh
    with pytest.raises(WebSocketDisconnect):
        with signin.websocket_connect("/ws/chat/s1") as ws:
            ws.receive_json()


def test_duplicate_or_malformed_identity_headers_are_refused(signin):
    """A proxy that appends instead of replacing would forward the client's own
    Remote-User next to its own; which one wins must not be left to chance."""
    r = signin.get("/api/me", headers=[("Remote-User", "bob"), ("Remote-User", "root")])
    assert r.status_code == 400
    assert signin.get("/api/me", headers={"Remote-User": "   "}).status_code == 400
    assert signin.get("/api/me", headers={"Remote-User": "x" * 300}).status_code == 400


def test_who_is_signed_in_and_who_is_an_admin(signin):
    me = signin.get("/api/me", headers={"Remote-User": "Bob", "Remote-Name": "Bob Builder"}).json()
    assert me == {"auth": True, "user": "Bob Builder", "id": "Bob", "admin": False}
    assert signin.get("/api/me", headers=ROOT).json()["admin"] is True
    assert signin.get("/api/me", headers={"Remote-User": "ROOT"}).json()["admin"] is False
    assert signin.get("/api/me", headers=GROUP_ADMIN).json()["admin"] is True
    # an IPv4-mapped IPv6 peer is the same proxy
    assert _from("::ffff:" + PROXY).get("/api/me", headers=BOB).status_code == 200


def test_environment_variables_turn_sign_in_on(app_module, cfg, monkeypatch, no_auth_env):
    cfg.pop("auth", None)
    monkeypatch.setenv("VISUALWEAVER_AUTH", "header")
    monkeypatch.setenv("VISUALWEAVER_TRUSTED_PROXIES", f"192.0.2.1, {PROXY}/32")
    monkeypatch.setenv("VISUALWEAVER_AUTH_USER_HEADER", "X-Forwarded-Email")
    monkeypatch.setenv("VISUALWEAVER_ADMIN_USERS", "Boss@Example.com")
    c = _from(PROXY)
    assert c.get("/api/me").status_code == 401
    assert c.get("/api/me", headers=BOB).status_code == 401              # not the configured header
    me = c.get("/api/me", headers={"X-Forwarded-Email": "Boss@Example.com"}).json()
    assert me["auth"] and me["admin"] and me["id"] == "Boss@Example.com"
    # names are compared exactly unless case-insensitive names are chosen
    assert c.get("/api/me", headers={"X-Forwarded-Email": "boss@example.com"}).json()["admin"] is False
    monkeypatch.setenv("VISUALWEAVER_AUTH_CASE_INSENSITIVE_NAMES", "1")
    me = c.get("/api/me", headers={"X-Forwarded-Email": "boss@example.com"}).json()
    assert me["admin"] and me["id"] == "boss@example.com"


@pytest.mark.parametrize("bad", [{"mode": "header", "trusted_proxies": ["0.0.0.0/0"]},
                                 {"mode": "header", "trusted_proxies": ["::/0"]},
                                 {"mode": "header", "trusted_proxies": ["proxy.lan"]},
                                 {"mode": "on"}])
def test_a_broken_sign_in_configuration_refuses_everything(app_module, cfg, no_auth_env, bad):
    """Never a silent fall-back to "off", and never "trust every address"."""
    cfg["auth"] = bad
    c = _from(PROXY)
    h = c.get("/api/health")         # the liveness probe alone answers, and says so
    assert h.status_code == 200 and h.json()["status"] == "misconfigured"
    assert c.get("/api/me", headers=ROOT).status_code == 503
    assert c.get("/api/config", headers=ROOT).status_code == 503


def test_the_cross_site_guard_still_answers_first(signin):
    """A signed-in user's browser on another site's page is still refused by the
    cross-site guard, and a rebinding host name by the host guard — before sign-in."""
    r = signin.post("/api/sessions/x/fork", headers={**BOB, "Origin": "http://evil.example"}, json={})
    assert r.status_code == 403 and r.json() == {"detail": "Cross-site request refused"}
    r = _from(STRANGER).get("/api/me", headers={**ROOT, "Host": "rebind.attacker.example",
                                                "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 403 and "VISUALWEAVER_ALLOWED_HOSTS" in r.text
    with pytest.raises(WebSocketDisconnect):
        with signin.websocket_connect("/ws/chat/s1", headers={**BOB, "Origin": "http://evil.example"}) as ws:
            ws.receive_json()


def test_startup_warnings_name_an_exposed_bind_address(app_module, cfg, no_auth_env):
    from visualweaver import auth
    cfg["auth"] = {"mode": "header", "admin_users": ["root"]}
    assert auth.startup_warnings("127.0.0.1") == []
    w = " ".join(auth.startup_warnings("0.0.0.0"))
    assert "only loopback is a trusted proxy" in w and "VISUALWEAVER_TRUSTED_PROXIES" in w
    cfg["auth"]["trusted_proxies"] = [PROXY]
    assert "reachable ONLY from the trusted proxy" in " ".join(auth.startup_warnings("0.0.0.0"))
    cfg["auth"] = {"mode": "header"}
    assert "nobody can change Settings" in " ".join(auth.startup_warnings("127.0.0.1"))
    cfg.pop("auth")
    assert auth.startup_warnings("0.0.0.0") == []


# ── the admin role ───────────────────────────────────────────────────────────

def _routes():
    from starlette.routing import WebSocketRoute
    from visualweaver import appcore
    out = []
    for r in appcore.app.router.routes:
        if isinstance(r, WebSocketRoute):
            out.append(("WS", r.path))
            continue
        for m in sorted((getattr(r, "methods", None) or {"GET"}) - {"HEAD"}):
            out.append((m, r.path))
    return out


def _fill(path: str) -> str:
    import re
    return re.sub(r"\{[^}]+\}", "x", path)


# Every route an ordinary user may change something through. Adding one is a decision
# about who may do what: it has to be made here as well as in auth.USER_ROUTES.
_USER_WRITES = {
    ("WS", "/ws/chat/{sid}"), ("POST", "/api/chat"), ("POST", "/api/chat/upload-file"),
    ("POST", "/api/feedback"), ("DELETE", "/api/cache/entry"),
    ("PATCH", "/api/sessions/{sid}"), ("DELETE", "/api/sessions/{sid}"), ("POST", "/api/sessions/{sid}/fork"),
}


def test_every_admin_route_refuses_a_non_admin(signin):
    """Enumerated from the app itself: a route added later is administrator-only until it
    is deliberately listed in auth.USER_ROUTES, and every one of them is refused to a
    signed-in user who is not an administrator — before its handler runs."""
    from visualweaver import auth
    routes = _routes()
    admin = [(m, p) for m, p in routes if auth.policy(m, p) == "admin"]
    for must in [("POST", "/api/config"), ("POST", "/api/config/import"), ("GET", "/api/config/export"),
                 ("POST", "/api/storage/app"), ("DELETE", "/api/rag/instances/{instance}"),
                 ("POST", "/api/rag/instances"), ("POST", "/api/rag/{instance}/ingest/files"),
                 ("DELETE", "/api/cache"), ("GET", "/api/feedback"), ("POST", "/api/redis/test"),
                 ("POST", "/api/status/claude"), ("POST", "/api/templates"), ("GET", "/openapi.json")]:
        assert must in admin, must
    for method, path in admin:
        assert method != "WS"
        r = signin.request(method, _fill(path), headers=BOB)
        assert r.status_code == 403 and r.json() == {"detail": "Administrators only."}, (method, path, r.text[:200])
    # what an ordinary user may write through is exactly the reviewed list
    writes = {(m, p) for m, p in routes if auth.policy(m, p) == "user" and m != "GET"}
    assert writes == _USER_WRITES
    # and nothing in the lists names a route that does not exist
    assert (auth.USER_ROUTES | auth.PUBLIC_ROUTES) <= set(routes)


def test_admins_by_name_or_group_reach_admin_routes(signin):
    for who in (ROOT, GROUP_ADMIN):
        r = signin.get("/api/config/export", headers=who)
        assert r.status_code == 200 and "attachment" in r.headers.get("content-disposition", "")


def test_a_settings_save_or_import_cannot_turn_sign_in_off(signin, monkeypatch):
    """The auth section comes from config.json / the environment only."""
    from visualweaver import config, embeddings, redis_store, state
    monkeypatch.setattr(embeddings, "get_embed_model", lambda *a, **k: None)
    monkeypatch.setattr(redis_store, "get_redis", lambda *a, **k: None)
    monkeypatch.setattr(config, "save_config", lambda *a, **k: None)
    before = json.dumps(state._config["auth"], sort_keys=True)
    r = signin.post("/api/config", headers=ROOT, json={"auth": {"mode": "off"}})
    assert r.status_code == 200
    assert json.dumps(state._config["auth"], sort_keys=True) == before
    r = signin.post("/api/config/import", headers=ROOT,
                    files={"file": ("c.json", json.dumps({"auth": {"mode": "off", "admin_users": ["bob"]}}))})
    assert r.status_code == 200
    assert json.dumps(state._config["auth"], sort_keys=True) == before
    assert signin.get("/api/me", headers=BOB).json() == {"auth": True, "user": "bob", "id": "bob", "admin": False}


def test_cached_questions_are_shown_only_to_the_user_who_asked_them(signin, monkeypatch):
    from visualweaver import auth, routes_redis
    monkeypatch.setattr(routes_redis, "_cache_stats", lambda: {"count": 2, "entries": [
        {"query": "my salary is 90k, ok?", "ttl": -1, "owner": auth.partition("bob")},
        {"query": "from before sign-in", "ttl": -1, "owner": ""}]})
    assert signin.get("/api/cache/stats", headers=BOB).json() == {
        "count": 1, "entries": [{"query": "my salary is 90k, ok?", "ttl": -1}]}
    admin = signin.get("/api/cache/stats", headers=ROOT).json()
    assert admin["entries"] == [] and admin["total"] == 2 and "salary" not in json.dumps(admin)


def test_a_rating_records_the_signed_in_user_not_a_claimed_one(signin, monkeypatch):
    from visualweaver import config, state
    monkeypatch.setattr(config, "save_feedback", lambda *a, **k: None)
    n = len(state._feedback)
    try:
        assert signin.post("/api/feedback", headers=BOB, json={"value": -1, "user": "root"}).status_code == 200
        assert state._feedback[-1]["user"] == "bob"
    finally:
        del state._feedback[n:]


# ── per-user conversations, on both app stores ───────────────────────────────

@pytest.fixture(params=["redis", "mongodb"])
def store(request, app_module, cfg, clean_redis, monkeypatch, no_auth_env):
    from visualweaver import appstore, redis_store, sessions, state
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
        db = f"vwtest_signin_{os.getpid()}_{int(time.time() * 1000) % 10**8}"
        cfg["redis_endpoints"] = [{"name": MONGO_EP, "type": "mongodb", "uri": uri, "database": db,
                                   "username": "", "password": ""}]
        cfg["storage"] = {"default_type": "redis", "app": MONGO_EP}
    redis_store.invalidate_redis_clients()
    live = dict(state._sessions)
    state._sessions.clear()
    s = appstore.current()
    assert s.kind == request.param
    yield s
    state._sessions.clear()
    state._sessions.update(live)
    if db:
        try:
            redis_store.r_for(MONGO_EP).client.drop_database(db)
        except Exception:
            pass
    redis_store.invalidate_redis_clients()


@pytest.fixture
def chat(store, cfg, monkeypatch):
    """Sign-in on, a fake model that records what it was sent, and the cache scopes each
    lookup used — over the real WebSocket, saving to the real store."""
    from visualweaver import cache, providers, sessions
    cfg["auth"] = {"mode": "header", "trusted_proxies": [PROXY], "admin_users": ["root"]}
    sent, scopes = [], []

    async def model(messages, model, images=None, usage=None):
        if "Just the title words" not in str(messages[-1].get("content")):
            sent.append([(m["role"], m["content"]) for m in messages if m["role"] != "system"])
        yield "An answer", False
        yield "", True

    monkeypatch.setattr(providers, "ollama_stream", model)
    monkeypatch.setattr(cache, "cache_lookup", lambda q, t, scope, *a, **kw: scopes.append(scope))
    monkeypatch.setattr(cache, "cache_store", lambda *a, **k: None)
    monkeypatch.setattr(sessions, "record_usage", lambda *a, **k: None)
    c = _from(PROXY)
    c.sent, c.scopes = sent, scopes
    return c


def _ask(c, who, sid, text):
    from test_event_loop_responsiveness import _frames_until
    with c.websocket_connect(f"/ws/chat/{sid}", headers=who) as ws:
        ws.send_json({"content": text, "provider": "ollama", "model": "fake", "rag_instances": []})
        frames = _frames_until(ws, "stream_end")
        if not any(f.get("type") == "session_title" for f in frames) and len(c.sent[-1]) == 1:
            _frames_until(ws, "session_title")        # the first turn: let its title land
    return frames


def _ids(c, who):
    r = c.get("/api/sessions", headers=who)
    assert r.status_code == 200
    return {row["id"]: row for row in r.json()}


def test_conversations_belong_to_the_user_who_started_them(chat, store):
    """Bob must not list, read, rename, pin, fork, delete or continue Alice's conversation
    — and each refusal looks exactly like a conversation id nobody has used."""
    from visualweaver import sessions, state
    _ask(chat, ALICE, "s1", "My salary is 90k. Remember it.")
    assert set(_ids(chat, ALICE)) == {"s1"}
    row = _ids(chat, ALICE)["s1"]
    assert row["owner"] == "alice" and row["messages"] == 2
    stored = [k for k in store.keys(sessions._SESSION_PREFIX)]
    assert len(stored) == 1 and stored[0].endswith(".s1") and stored[0] != sessions._session_key("s1")
    assert json.loads(store.get(stored[0]))[0]["meta"]["owner"] == "alice"

    assert _ids(chat, BOB) == {}
    unknown = chat.get("/api/sessions/never-used", headers=BOB)
    r = chat.get("/api/sessions/s1", headers=BOB)
    assert (r.status_code, r.json()) == (unknown.status_code, unknown.json()) == (200, [])
    for body in ({"title": "mine now"}, {"pinned": True}):
        assert chat.patch("/api/sessions/s1", headers=BOB, json=body).status_code == 404
    assert chat.post("/api/sessions/s1/fork", headers=BOB,
                     json={"role": "user", "content_prefix": "My", "occurrence": 1}).status_code == 404
    assert chat.delete("/api/sessions/s1", headers=BOB).json() == {"ok": True}
    state._sessions.clear()                                   # read back from the store
    alice = chat.get("/api/sessions/s1", headers=ALICE).json()
    assert len(alice) == 2 and alice[0]["content"].startswith("My salary")
    assert _ids(chat, ALICE)["s1"].get("title") != "mine now" and not _ids(chat, ALICE)["s1"]["pinned"]

    # the WebSocket: Bob "resuming" s1 starts his own conversation; the model never sees
    # Alice's turns, and her conversation is untouched
    _ask(chat, BOB, "s1", "What is my salary?")
    assert chat.sent[-1] == [("user", "What is my salary?")]
    assert len(chat.get("/api/sessions/s1", headers=ALICE).json()) == 2
    assert chat.get("/api/sessions/s1", headers=BOB).json()[0]["content"] == "What is my salary?"
    assert set(_ids(chat, BOB)) == {"s1"} and _ids(chat, BOB)["s1"]["owner"] == "bob"
    # and Alice continuing hers still has her own context
    _ask(chat, ALICE, "s1", "And what is it?")
    assert [m for _, m in chat.sent[-1]][:2] == ["My salary is 90k. Remember it.", "An answer"]


def test_owners_can_rename_pin_fork_and_delete_their_own(chat, store):
    _ask(chat, ALICE, "s1", "First question")
    r = chat.patch("/api/sessions/s1", headers=ALICE, json={"title": "Boiler", "pinned": True})
    assert r.status_code == 200 and r.json()["id"] == "s1" and r.json()["pinned"] is True
    f = chat.post("/api/sessions/s1/fork", headers=ALICE,
                  json={"role": "assistant", "content_prefix": "An answer", "occurrence": 1})
    assert f.status_code == 200
    fid = f.json()["id"]
    assert fid.endswith("_fork") and not fid.startswith("u.")
    rows = _ids(chat, ALICE)
    assert set(rows) == {"s1", fid} and rows[fid]["title"] == "Boiler (fork)"
    assert _ids(chat, BOB) == {}
    assert chat.delete(f"/api/sessions/{fid}", headers=ALICE).status_code == 200
    assert set(_ids(chat, ALICE)) == {"s1"}


def test_conversations_from_before_sign_in_are_shown_to_admins_only(chat, store):
    """A conversation stored while sign-in was off has no owner: the administrator (the
    single user it belonged to) keeps it; nobody else sees it."""
    from visualweaver import sessions
    sessions.save_session("legacy1", [{"role": "user", "content": "old q", "meta": {"ts": 1}},
                                      {"role": "assistant", "content": "old a", "meta": {"ts": 2}}])
    assert "legacy1" not in _ids(chat, BOB)
    assert chat.get("/api/sessions/legacy1", headers=BOB).json() == []
    assert "legacy1" in _ids(chat, ROOT)
    assert chat.get("/api/sessions/legacy1", headers=ROOT).json()[0]["content"] == "old q"
    # a tagged id is never taken for an untagged one
    _ask(chat, ALICE, "s9", "hello")
    tagged = sessions.user_prefix("alice") + "s9"
    assert chat.get(f"/api/sessions/{tagged}", headers=ROOT).json() == []


def test_answers_are_cached_per_user_first_answers_included(chat, store):
    """Every answer is cached for its user only: a follow-up's answer is built from the
    conversation so far, and a first question can carry private details as well, so
    neither may be replayed to another user (or come from the single-user-era cache)."""
    from visualweaver import cache
    _ask(chat, ALICE, "a1", "What is RAG?")
    _ask(chat, BOB, "b1", "What is RAG?")
    _ask(chat, ALICE, "a1", "Summarise what I told you")
    _ask(chat, BOB, "b1", "Summarise what I told you")
    first_a, first_b, follow_a, follow_b = chat.scopes
    assert first_a != first_b and follow_a != follow_b
    # with sign-in off the scope is the one every earlier release used
    assert cache._cache_scope(["kb"], "ollama", "m", "", "p") == cache._cache_scope(["kb"], "ollama", "m", "", "p", owner="")
    assert cache.private_partition([{"role": "user"}]) == ""                 # no user: shared


def test_rest_chat_keeps_conversations_per_user(chat, store, monkeypatch):
    from visualweaver import providers
    r = chat.post("/api/chat", headers=ALICE, json={"content": "Alice here", "session_id": "r1",
                                                     "provider": "ollama", "model": "fake", "rag_instances": []})
    assert r.status_code == 200 and r.json()["session_id"] == "r1"
    r = chat.post("/api/chat", headers=BOB, json={"content": "Bob here", "session_id": "r1",
                                                   "provider": "ollama", "model": "fake", "rag_instances": []})
    assert r.status_code == 200 and r.json()["session_id"] == "r1"
    assert [m["content"] for m in chat.get("/api/sessions/r1", headers=ALICE).json()][0] == "Alice here"
    assert [m["content"] for m in chat.get("/api/sessions/r1", headers=BOB).json()][0] == "Bob here"


# ── start.sh ─────────────────────────────────────────────────────────────────

from test_start_stop_storage import stack  # noqa: E402,F401  (the fake start.sh stack)


_HEALTHY = (200, '{"status": "ok", "app": "VisualWeaver", "version": "1.14.0"}')


def _fake_app(stack, me_status: str, log_line: str = "", health: tuple = _HEALTHY):
    """start.sh's fakes, with /api/me answering ``me_status``, /api/health answering
    ``health`` (status code, body) — as curl does: with -f a status of 400 or more prints
    nothing and exits 22 — and the app writing ``log_line`` to its log at start."""
    import shlex
    repo = stack.tmp / "repo"
    py = repo / "venv" / "bin" / "python"
    text = py.read_text()
    args = stack.tmp / "app.args"
    text = text.replace('env > "', f'echo "$@" > "{args}"; '
                        + (f"echo '{log_line}'; " if log_line else "") + 'env > "', 1)
    py.write_text(text)
    code, body = health
    fail = "exit 22" if code >= 400 else ":"
    curl = stack.tmp / "bin" / "curl"
    curl.write_text("#!/bin/sh\ncase \"$*\" in\n"
                    "  *api/me*) printf '%s' " + me_status + " ;;\n"
                    "  *api/health*) case \"$*\" in -f*|*\\ -f*) " + fail + " ;; esac; "
                    "printf '%s\\n' " + shlex.quote(body) + " ;;\n"
                    "  *) echo '{\"app\": \"VisualWeaver\", \"version\": \"1.14.0\"}' ;;\nesac\n")
    curl.chmod(0o755)
    return args


def test_start_sh_reports_sign_in_and_the_apps_warnings(stack):
    """With sign-in on, start.sh says so, repeats the app's sign-in warnings from this
    start's log, and no longer claims there is no auth; the server is started with
    --no-proxy-headers so the trusted-proxy check sees each connection's own address."""
    stack.install(redis=True)
    args = _fake_app(stack, "401", "WARNING:visualweaver.startup:  SIGN-IN WARNING: bound to 0.0.0.0 test line")
    rc, out, _ = stack.run("start.sh", {"VISUALWEAVER_HOST": "0.0.0.0"})
    assert rc == 0, out
    assert "Sign-in:  through your reverse proxy" in out
    assert "WARNING: bound to 0.0.0.0 test line" in out
    assert "NO built-in auth" not in out
    assert "--no-proxy-headers" in args.read_text()


def test_start_sh_without_sign_in_keeps_the_open_network_warning(stack):
    stack.install(redis=True)
    _fake_app(stack, "200")
    rc, out, _ = stack.run("start.sh", {"VISUALWEAVER_HOST": "0.0.0.0"})
    assert rc == 0, out
    assert "NO built-in auth" in out and "Sign-in:" not in out


@pytest.mark.parametrize("why", ["auth", "config"])
def test_start_sh_says_when_sign_in_is_misconfigured(stack, app_module, cfg, monkeypatch, why):
    """The fakes answer as the real app does while sign-in is misconfigured (taken from it,
    not written out): /api/me 503, /api/health 200 "misconfigured". Old: /api/health was
    503 too, so start.sh waited 60 s, said "did not become healthy" and exited 1 while the
    app kept running, and its MISCONFIGURED line was never reached."""
    from test_security_review_1_16 import _ENV, _from
    from visualweaver import state
    for v in _ENV:
        monkeypatch.delenv(v, raising=False)
    if why == "auth":
        cfg["auth"] = {"mode": "header", "trusted_proxies": ["not-an-address"]}
    else:                      # what config.load_config leaves for a config.json it could not load
        from visualweaver import auth
        cfg["auth"] = {"mode": auth.UNREADABLE_MODE}
        monkeypatch.setattr(state, "_config_load_error", "config.json could not be parsed")
    c = _from("127.0.0.1")
    me, health = c.get("/api/me"), c.get("/api/health")
    assert me.status_code == 503
    stack.install(redis=True)
    _fake_app(stack, str(me.status_code), health=(health.status_code, health.text))
    t0 = time.time()
    rc, out, _ = stack.run("start.sh")
    assert rc == 0, out
    assert "did not become healthy" not in out and time.time() - t0 < 30
    assert "Sign-in:  MISCONFIGURED" in out
    assert health.json()["detail"] in out
