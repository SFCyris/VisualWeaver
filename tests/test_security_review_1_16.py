# SPDX-License-Identifier: AGPL-3.0-or-later
"""Security / storage review of 1.16.0: one test (or a few) per finding.

Requests go through TestClient with the connection's peer address set explicitly
(``_from``), the way uvicorn reports it with --no-proxy-headers.
"""
import ast
import asyncio
import io
import json
import os
import re
import time
import tokenize
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from conftest import KEY_PREFIX, REDIS_DB, REDIS_HOST, REDIS_PORT
from test_session_history import _conv, app_store  # noqa: F401  (Redis + MongoDB via mongo_uri)

ROOT_DIR = Path(__file__).resolve().parents[1]
PROXY = "10.9.0.2"
STRANGER = "10.9.0.77"
_ENV = ("VISUALWEAVER_AUTH", "VISUALWEAVER_AUTH_USER_HEADER", "VISUALWEAVER_AUTH_GROUPS_HEADER",
        "VISUALWEAVER_AUTH_NAME_HEADER", "VISUALWEAVER_TRUSTED_PROXIES", "VISUALWEAVER_ADMIN_USERS",
        "VISUALWEAVER_ADMIN_GROUPS", "VISUALWEAVER_AUTH_CASE_INSENSITIVE_NAMES")
PAGE = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"}


def _from(ip: str) -> TestClient:
    from visualweaver import appcore

    async def asgi(scope, receive, send):
        await appcore.app(dict(scope, client=(ip, 40001)), receive, send)
    return TestClient(asgi)


def _arun(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def no_auth_env(monkeypatch):
    for v in _ENV:
        monkeypatch.delenv(v, raising=False)


@pytest.fixture
def signin(app_module, cfg, no_auth_env):
    cfg["auth"] = {"mode": "header", "trusted_proxies": [PROXY],
                   "admin_users": ["root"], "admin_groups": ["vw-admins"]}
    return _from(PROXY)


@pytest.fixture
def as_user(app_module):
    """Run code as a signed-in user (the ContextVar the AuthGuard sets)."""
    from visualweaver import auth
    tokens = []

    def _as(uid, admin=False):
        tokens.append(auth._current.set(auth.User(id=uid, name=uid, admin=admin)))
    yield _as
    for t in reversed(tokens):
        auth._current.reset(t)


# ── 1. DNS rebinding: X-Forwarded-Host and the host allowlist ───────────────

def test_x_forwarded_host_from_a_browser_does_not_pass_the_host_check(app_module, cfg, no_auth_env):
    """A rebinding page (Host: the attacker's name) adds X-Forwarded-Host: localhost."""
    cfg.pop("auth", None)
    rebind = {**PAGE, "Host": "rebind.attacker.example:8420", "X-Forwarded-Host": "localhost"}
    assert _from("127.0.0.1").get("/api/config", headers=rebind).status_code == 403
    assert _from(STRANGER).get("/api/config", headers=rebind).status_code == 403


def test_x_forwarded_host_with_loopback_trust_cannot_reach_admin_routes(app_module, cfg, no_auth_env):
    """Sign-in on, loopback the trusted proxy: a browser on the same machine on a rebinding
    page sends X-Forwarded-Host: localhost AND Remote-User: root."""
    cfg["auth"] = {"mode": "header", "admin_users": ["root"]}          # trusted: loopback (default)
    h = {**PAGE, "Host": "rebind.attacker.example:8420", "X-Forwarded-Host": "localhost",
         "Remote-User": "root"}
    r = _from("127.0.0.1").get("/api/config/export", headers=h)
    assert r.status_code == 403 and "VISUALWEAVER_ALLOWED_HOSTS" in r.text


def test_x_forwarded_host_is_honoured_only_from_a_trusted_proxy(app_module, cfg, no_auth_env, monkeypatch):
    """A proxy that rewrites Host to the upstream address forwards the public name in
    X-Forwarded-Host: from that proxy it is checked and used for the Origin test; from
    anyone else it is ignored."""
    from visualweaver import appcore
    cfg.pop("auth", None)
    monkeypatch.setattr(appcore, "_allowed_hosts", {"vw.corp.example"})
    monkeypatch.setenv("VISUALWEAVER_TRUSTED_PROXIES", PROXY)
    h = {"Host": "127.0.0.1:8420", "X-Forwarded-Host": "vw.corp.example",
         "Origin": "https://vw.corp.example", "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"}
    assert _from(PROXY).delete("/api/sessions/x", headers=h).status_code == 200
    # the same headers from a client that is not the proxy: Origin does not match Host
    assert _from(STRANGER).delete("/api/sessions/x", headers=h).status_code == 403
    # an X-Forwarded-Host that is not an allowed name is refused even from the proxy
    bad = {**h, "X-Forwarded-Host": "rebind.attacker.example", "Origin": "https://rebind.attacker.example"}
    assert _from(PROXY).delete("/api/sessions/x", headers=bad).status_code == 403


def test_names_anyone_on_the_network_can_claim_are_not_allowed_by_default(app_module, cfg, no_auth_env,
                                                                         monkeypatch):
    """mDNS (.local), router DNS (.lan, .home.arpa, .internal) and single-label names can be
    answered by any device on the LAN. This machine's own names and listed ones still work."""
    from visualweaver import appcore
    cfg.pop("auth", None)
    monkeypatch.setattr(appcore, "_allowed_hosts", set())
    monkeypatch.setattr(appcore, "_MACHINE_NAMES", {"localhost", "my-mac", "my-mac.local"})
    c = _from(STRANGER)
    for host in ("evil.local:8420", "box.lan:8420", "vw.home.arpa", "x.internal", "evilbox:8420"):
        assert c.get("/api/health", headers={**PAGE, "Host": host}).status_code == 403, host
    for host in ("my-mac.local:8420", "my-mac:8420", "localhost:8420", "app.localhost",
                 "192.168.1.20:8420", "[::1]:8420"):
        assert c.get("/api/health", headers={**PAGE, "Host": host}).status_code == 200, host
    monkeypatch.setattr(appcore, "_allowed_hosts", {"nas.lan"})
    assert c.get("/api/health", headers={**PAGE, "Host": "nas.lan:8420"}).status_code == 200


# ── 2 / 7. A config.json that cannot be read must not turn sign-in off ───────

def _load_into_state(app_module, path, monkeypatch):
    from visualweaver import config, constants, state
    monkeypatch.setattr(constants, "CONFIG_PATH", path)
    loaded = config.load_config()
    state._config.clear()
    state._config.update(loaded)
    return loaded


def test_an_unparsable_config_refuses_every_request_until_fixed(app_module, cfg, no_auth_env, data_dir,
                                                                monkeypatch):
    from visualweaver import state
    path = data_dir / "config.json"
    path.write_text('{"auth": {"mode": "header", "admin_users": ["root"]}, "redis": {', encoding="utf-8")
    loaded = _load_into_state(app_module, path, monkeypatch)
    try:
        c = _from("127.0.0.1")
        assert c.get("/api/config").status_code == 503
        assert c.get("/api/health").json()["status"] == "misconfigured"
        # a restart after the bad file was quarantined: still refused
        loaded = _load_into_state(app_module, path, monkeypatch)
        assert c.get("/api/config").status_code == 503
        # the first save after a failed load keeps sign-in undecided rather than "off"
        from visualweaver import config
        config.save_config(loaded)
        assert json.loads(path.read_text())["auth"]["mode"] not in ("off", "", None)
        # stating it explicitly in the environment is the way to run without sign-in
        monkeypatch.setenv("VISUALWEAVER_AUTH", "off")
        assert c.get("/api/config").status_code == 200
    finally:
        state._config_load_error = ""


def test_an_unreadable_config_refuses_every_request(app_module, cfg, no_auth_env, data_dir, monkeypatch):
    from visualweaver import state
    path = data_dir / "config.json"
    path.write_text(json.dumps({"auth": {"mode": "header"}}), encoding="utf-8")
    os.chmod(path, 0o000)
    try:
        _load_into_state(app_module, path, monkeypatch)
        assert _from("127.0.0.1").get("/api/config").status_code == 503
    finally:
        os.chmod(path, 0o644)
        state._config_load_error = ""


@pytest.mark.parametrize("bad", ["header", ["header"], 1, True])
def test_an_auth_value_that_is_not_an_object_is_a_config_error(app_module, cfg, no_auth_env, bad):
    cfg["auth"] = bad
    c = _from("127.0.0.1")
    assert c.get("/api/health").json()["status"] == "misconfigured"
    assert c.get("/api/config").status_code == 503


# ── 3. Cross-site navigations reach only the page ────────────────────────────

NAV = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}


def test_a_cross_site_navigation_cannot_start_a_crawl_or_any_other_route(app_module, cfg, no_auth_env):
    cfg.pop("auth", None)
    c = _from("127.0.0.1")
    r = c.get("/api/rag/__secfix_none__/ingest/url/stream", params={"url": "http://127.0.0.1:9/"},
              headers=NAV)
    assert r.status_code == 403 and r.json() == {"detail": "Cross-site request refused"}
    iframe = {**NAV, "Sec-Fetch-Dest": "iframe"}
    assert c.get("/api/rag/__secfix_none__/ingest/url/stream", params={"url": "http://127.0.0.1:9/"},
                 headers=iframe).status_code == 403
    for path in ("/api/config/export", "/api/health", "/api/sessions"):
        assert c.get(path, headers=NAV).status_code == 403, path
        assert c.get(path, headers={**NAV, "Sec-Fetch-Site": "same-site"}).status_code == 403, path
    # the page itself: a link from another site, and the sign-in service's redirect back
    assert c.get("/", headers=NAV).status_code == 200
    # typed in the address bar / a bookmark / the app's own page: unchanged
    assert c.get("/api/health", headers={**NAV, "Sec-Fetch-Site": "none"}).status_code == 200
    assert c.get("/api/health", headers={**NAV, "Sec-Fetch-Site": "same-origin"}).status_code == 200


# ── 4 / 11. The answer cache with sign-in on ─────────────────────────────────

def test_with_sign_in_on_even_a_first_question_is_cached_per_user(app_module, cfg, no_auth_env, as_user):
    from visualweaver import cache
    cfg["auth"] = {"mode": "header", "trusted_proxies": [PROXY]}

    def scope(history):
        return cache._cache_scope(["kb"], "ollama", "m", "", "sp",
                                  owner=cache.private_partition(history))
    single_user_era = cache._cache_scope(["kb"], "ollama", "m", "", "sp", owner="")
    as_user("alice")
    alice_first, alice_later = scope([]), scope([{"role": "user", "content": "x"}])
    as_user("bob")
    bob_first = scope([])
    assert alice_first != bob_first, "a first answer is shared between users"
    assert alice_first == alice_later
    assert single_user_era not in (alice_first, bob_first), "single-user-era entries serve every user"


def test_with_sign_in_off_the_cache_scope_is_unchanged(app_module, cfg, no_auth_env):
    from visualweaver import cache
    cfg.pop("auth", None)
    assert cache.private_partition([]) == "" and cache.private_partition([{"role": "user"}]) == ""


class _RecCache:
    kind = "redis"

    def __init__(self):
        self.calls = []

    def store(self, **kw):
        self.calls.append(kw)


def test_a_stored_answer_records_its_owner_and_another_users_rerun_cannot_overwrite_it(
        app_module, cfg, no_auth_env, as_user, monkeypatch):
    from redisvl.redis.utils import hashify
    from visualweaver import cache, state
    rec = _RecCache()
    monkeypatch.setattr(cache, "_get_semantic_cache", lambda: rec)
    monkeypatch.setattr(state, "_semantic_cache_ready", True)
    cfg.pop("auth", None)
    cache.cache_store("q", "a", None, "scope0")
    assert "owner" not in rec.calls[-1]["filters"], "sign-in off: entry ids must stay as they were"
    cfg["auth"] = {"mode": "header", "trusted_proxies": [PROXY]}
    from visualweaver import auth
    as_user("alice")
    cache.cache_store("q", "alice's answer", None, "scopeA")
    fa = rec.calls[-1]["filters"]
    assert fa["owner"] == auth.partition("alice")
    as_user("bob")
    cache.cache_store("q", "bob's answer", None, "scopeA")   # "Re-run fresh", same scope string
    fb = rec.calls[-1]["filters"]
    assert hashify(rec.calls[-1]["prompt"], fa) != hashify(rec.calls[-1]["prompt"], fb)


def _redis_cache_entry(rc, prefix, entry_id, owner):
    key = f"{prefix.rstrip(':')}:{entry_id}"
    m = {"prompt": "q", "response": "private"}
    if owner:
        m["owner"] = owner
    rc.hset(key, mapping=m)
    return key


def test_removing_a_cached_answer_reaches_only_your_own(app_module, cfg, no_auth_env, clean_redis,
                                                        monkeypatch):
    from visualweaver import auth, cache, constants, redis_store
    cfg["redis"] = {"host": REDIS_HOST, "port": REDIS_PORT, "db": REDIS_DB, "password": "", "ssl": False}
    cfg["storage"] = {"default_type": "redis", "app": "default"}
    redis_store.invalidate_redis_clients()
    prefix = f"{KEY_PREFIX}llmcache:"
    monkeypatch.setattr(constants, "CACHE_PREFIX", prefix)
    monkeypatch.setattr(cache, "_cache_backend", lambda: ("redis", clean_redis))
    cfg["auth"] = {"mode": "header", "trusted_proxies": [PROXY], "admin_users": ["root"]}
    k_alice = _redis_cache_entry(clean_redis, prefix, "ea", auth.partition("alice"))
    k_shared = _redis_cache_entry(clean_redis, prefix, "es", "")
    c = _from(PROXY)
    bob = {"Remote-User": "bob"}
    assert c.delete("/api/cache/entry", params={"entry_id": "ea"}, headers=bob).json()["ok"] is False
    assert c.delete("/api/cache/entry", params={"entry_id": "es"}, headers=bob).json()["ok"] is False
    assert clean_redis.exists(k_alice) and clean_redis.exists(k_shared)
    assert c.delete("/api/cache/entry", params={"entry_id": "ea"},
                    headers={"Remote-User": "alice"}).json()["ok"] is True
    assert not clean_redis.exists(k_alice)
    assert c.delete("/api/cache/entry", params={"entry_id": "es"},
                    headers={"Remote-User": "root"}).json()["ok"] is True
    redis_store.invalidate_redis_clients()


# ── 5. User names: no case or Unicode folding into someone else ──────────────

def test_a_look_alike_name_is_not_the_admin(signin):
    kelvin = "Kate"                       # KELVIN SIGN + "ate": .lower() == "kate"
    full = "ｒｏｏｔ"          # fullwidth "root": NFKC == "root"
    for name in (kelvin, full, "ro\u0301ot", "é"):
        r = signin.get("/api/me", headers=[(b"Remote-User", name.encode("utf-8"))])
        assert r.status_code == 400, (name, r.text)
    # names are compared exactly: another case is another user, not the admin
    assert signin.get("/api/me", headers={"Remote-User": "ROOT"}).json()["admin"] is False
    a = signin.get("/api/me", headers=[(b"Remote-User", "JÜRGEN".encode())]).json()
    b = signin.get("/api/me", headers=[(b"Remote-User", "jürgen".encode())]).json()
    assert a["id"] != b["id"]


def test_admin_names_and_groups_match_the_same_way(app_module, cfg, no_auth_env):
    from visualweaver import auth
    s = auth.parse_settings({"mode": "header", "admin_users": ["Kate", "Jürgen"],
                             "admin_groups": ["k-admins"], "case_insensitive_names": True})
    assert "kate" in s.admin_users and "Jürgen" in s.admin_users
    scope = {"type": "http", "client": ("127.0.0.1", 1), "headers": []}

    def who(name, groups=""):
        h = [(b"remote-user", name.encode())]
        if groups:
            h.append((b"remote-groups", groups.encode()))
        return auth.identify({**scope, "headers": h}, s)
    assert who("KATE")[0].admin is True
    assert who("jürgen")[0].admin is False
    assert who("Jürgen")[0].admin is True
    assert who("carol", "K-admins")[0].admin is False     # a look-alike group is not the group
    assert who("carol", "K-Admins")[0].admin is True


# ── 6. Images: only from the image directory ─────────────────────────────────

def test_the_image_route_serves_only_the_image_directory(app_module, cfg, no_auth_env, tmp_path, monkeypatch):
    from visualweaver import constants, routes_media
    cfg.pop("auth", None)
    img_dir = Path(routes_media.image_dir())
    img_dir.mkdir(parents=True, exist_ok=True)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    inside = img_dir / "secfix-ok.png"
    inside.write_bytes(png)
    outside = Path(constants.DATA_DIR) / "secfix-outside.png"
    outside.write_bytes(png)
    cwd_img = Path.cwd() / "secfix-cwd-probe.png"
    tmp_img = Path(os.environ.get("TMPDIR", "/tmp")) / "secfix-tmp-probe.png"
    link = img_dir / "secfix-link.png"
    try:
        cwd_img.write_bytes(png)
        tmp_img.write_bytes(png)
        link.unlink(missing_ok=True)
        link.symlink_to(outside)
        c = _from("127.0.0.1")
        r = c.get("/api/files/image", params={"path": str(inside)})
        assert r.status_code == 200 and r.content == png
        for p in (outside, cwd_img, tmp_img, link, img_dir / ".." / outside.name):
            assert c.get("/api/files/image", params={"path": str(p)}).status_code in (403, 404), p
    finally:
        for p in (inside, outside, cwd_img, tmp_img, link):
            try:
                p.unlink()
            except OSError:
                pass


def test_a_tool_image_outside_the_image_directory_is_not_linked(app_module, tmp_path):
    from visualweaver import providers, routes_media
    stray = tmp_path / "x.png"
    stray.write_bytes(b"\x89PNG\r\n\x1a\n")
    out = providers._tool_result_to_markdown([{"function": {"name": "draw", "arguments": {"p": str(stray)}}}])
    assert "/api/files/image" not in out
    d = Path(routes_media.image_dir())
    d.mkdir(parents=True, exist_ok=True)
    ok = d / "secfix-tool.png"
    ok.write_bytes(b"\x89PNG\r\n\x1a\n")
    try:
        out = providers._tool_result_to_markdown([{"function": {"name": "draw", "arguments": {"p": str(ok)}}}])
        assert "/api/files/image?path=" in out
    finally:
        ok.unlink()


# ── 12. GET /api/config for users who are not administrators ─────────────────

def test_users_get_the_config_without_admin_only_details(signin, cfg):
    cfg["web_sources"] = ["https://intranet.example/wiki"]
    cfg["scheduled_sources"] = [{"url": "https://intranet.example/x"}]
    cfg["watch_folders"] = {"enabled": True, "interval_minutes": 5, "folders": [{"path": "/srv/hr"}]}
    cfg["redis"] = {"host": "10.1.2.3", "port": 6390, "db": 0, "password": "pw", "ssl": False}
    cfg["redis_endpoints"] = [{"name": "atlas", "type": "mongodb", "uri": "mongodb+srv://c.example",
                               "username": "u", "password": "p"}]
    cfg["mongodb"] = {"uri": "mongodb://10.1.2.4:27017", "username": "vw", "password": "x"}
    cfg["provider"], cfg["rag"] = "ollama", {"top_k": 5}
    cfg["ollama"] = {"host": "http://10.1.2.5", "port": 11434, "model": "llama3"}
    cfg["claude"] = {"api_key": "", "model": "c", "base_url": "https://llm.intranet.example"}
    user = signin.get("/api/config", headers={"Remote-User": "bob"}).json()
    blob = json.dumps(user)
    for secret in ("intranet.example", "/srv/hr", "10.1.2.3", "10.1.2.4", "10.1.2.5", "c.example", PROXY,
                   "admin_users", "trusted_proxies"):
        assert secret not in blob, secret
    assert "auth" not in user
    assert user["redis_endpoints"] == [{"name": "atlas", "type": "mongodb"}]
    assert user["provider"] == "ollama" and user["rag"] == {"top_k": 5}      # what the chat page reads
    assert user["ollama"] == {"model": "llama3"} and user["claude"] == {"api_key": "", "model": "c"}
    admin = signin.get("/api/config", headers={"Remote-User": "root"}).json()
    assert admin["web_sources"] == ["https://intranet.example/wiki"] and admin["auth"]["mode"] == "header"


# ── 13. Trusted proxy addresses ──────────────────────────────────────────────

def test_an_ipv4_mapped_proxy_address_matches_the_proxy(app_module, cfg, no_auth_env):
    cfg["auth"] = {"mode": "header", "trusted_proxies": ["::ffff:" + PROXY]}
    for peer in (PROXY, "::ffff:" + PROXY):
        assert _from(peer).get("/api/me", headers={"Remote-User": "bob"}).status_code == 200, peer


@pytest.mark.parametrize("bad", [["10.0.0.5/8"], ["0.0.0.0/1", "128.0.0.0/1"], ["::/1", "8000::/1"]])
def test_proxy_ranges_with_host_bits_or_covering_everything_are_refused(app_module, cfg, no_auth_env, bad):
    from visualweaver import auth
    s = auth.parse_settings({"mode": "header", "trusted_proxies": bad})
    assert s.error, bad


def test_broad_proxy_ranges_are_warned_about(app_module, cfg, no_auth_env):
    from visualweaver import auth
    s = auth.parse_settings({"mode": "header", "trusted_proxies": ["10.0.0.0/8", "2001:db8::/32"],
                             "admin_users": ["root"]})
    assert not s.error
    assert any("10.0.0.0/8" in w for w in s.warnings) and any("2001:db8::/32" in w for w in s.warnings)
    s = auth.parse_settings({"mode": "header", "trusted_proxies": ["10.0.0.0/24", "2001:db8::/64"],
                             "admin_users": ["root"]})
    assert not s.error and not s.warnings


# ── 14. Ratings are bounded ──────────────────────────────────────────────────

def test_a_rating_with_a_huge_or_deeply_nested_body_is_refused(app_module, cfg, no_auth_env, monkeypatch):
    from visualweaver import config, state
    cfg.pop("auth", None)
    monkeypatch.setattr(config, "save_feedback", lambda: None)
    n = len(state._feedback)
    c = _from("127.0.0.1")
    deep: object = "x"
    for _ in range(40):
        deep = [deep]
    try:
        assert c.post("/api/feedback", json={"msg_id": "a", "value": 1, "x": deep}).status_code == 413
        big = {"msg_id": "a", "value": 1, "sources": [{"source": "s" * 900, "score": 0.5}] * 2000}
        assert c.post("/api/feedback", json=big).status_code == 413
        assert len(state._feedback) == n
        ui = {"msg_id": "m1", "value": -1, "session": "s", "query": "q" * 2000, "answer": "a" * 4000,
              "provider": "ollama", "model": "m", "rag_instances": ["kb"],
              "sources": [{"source": "doc.pdf", "score": 0.8}] * 50}
        assert c.post("/api/feedback", json=ui).status_code == 200
        assert len(state._feedback) == n + 1
    finally:
        del state._feedback[n:]


# ── 15. Provider ids in inline handlers ──────────────────────────────────────

def test_the_provider_in_a_settings_button_is_one_of_the_known_ids():
    import html as _html
    import shutil
    from _jsrun import extract_js_function, run_node
    if not shutil.which("node"):
        pytest.skip("node not available")
    page = (ROOT_DIR / "visualweaver" / "index.html").read_text(encoding="utf-8")
    fns = ["escHtml", "_isAdmin", "_safeProv", "_frRow", "_frHidden", "_renderFirstRunChecklist", "_hsItem",
           "_renderHealthStrip"]
    parts = []
    for n in fns:
        try:
            parts.append(extract_js_function(page, n))
        except ValueError:
            pass                              # a helper this version does not have
    src = "\n".join(parts)
    prelude = """
const _FR_KIND={ok:['✓','Done'],bad:['✗','Needs attention'],todo:['○','To do'],wait:['…','Checking'],blocked:['–','Waiting']};
let _healthPrev=null; function announce(){}
const els={}; const document={getElementById:id=>(els[id]=els[id]||{id,dataset:{},hidden:false,innerHTML:''})};
const localStorage={getItem:()=>null};
function _healthModel(){return {storage:{ok:true,label:'Built-in Redis'},
  model:{ok:false,text:'No key',label:'X',fix:''},knowledge:{ok:true,text:'ok'},
  socket:{state:'open',queued:false,net:true}};}
let S={provider:"x');alert(1);//",me:{auth:false,user:null,admin:true}};
"""
    body = """
_renderFirstRunChecklist(); _renderHealthStrip();
console.log(JSON.stringify([els['welcome-checklist'].innerHTML, els['health-strip'].innerHTML]));
"""
    r = run_node(prelude + src + body)
    assert r.returncode == 0, r.stderr
    handlers = []
    for chunk in json.loads(r.stdout):
        handlers += [_html.unescape(m) for m in re.findall(r'onclick="([^"]*)"', chunk)]
    calls = [h for h in handlers if h.startswith("openSettings(")]
    assert calls, handlers
    for h in calls:
        assert re.fullmatch(r"openSettings\('(status|ollama|claude|openai|qwen|mistral|groq|gemini)'\)", h), h


# ── 16. Sign-in deploy examples ──────────────────────────────────────────────

def test_the_sign_in_compose_files_keep_the_proxy_address_for_the_proxy():
    import yaml
    for name, proxy in (("docker-compose.signin.yml", "caddy"), ("docker-compose.oauth2-proxy.yml", "oauth2-proxy")):
        doc = yaml.safe_load((ROOT_DIR / "deploy" / "signin" / name).read_text())
        import ipaddress
        ip = ipaddress.ip_address(doc["services"][proxy]["networks"]["signin"]["ipv4_address"])
        pool = doc["networks"]["signin"]["ipam"]["config"][0]
        assert ip in ipaddress.ip_network(pool["subnet"])
        assert "ip_range" in pool and ip not in ipaddress.ip_network(pool["ip_range"]), name
        image = doc["services"]["visualweaver"]["image"]
        tag = image.rsplit(":", 1)[1]
        assert tag != "latest" and tuple(int(x) for x in tag.split(".")[:2]) >= (1, 16), image
    authentik = (ROOT_DIR / "deploy" / "signin" / "Caddyfile.authentik").read_text()
    assert not re.search(r"^\s*trusted_proxies", authentik, re.M)


# ── 17. Conversation retention has a floor ───────────────────────────────────

def test_a_retention_of_seconds_is_refused(app_module, cfg, no_auth_env, monkeypatch):
    from fastapi import HTTPException
    from visualweaver import config, routes_settings
    cfg.pop("auth", None)
    monkeypatch.setattr(config, "save_config", lambda c: None)
    monkeypatch.setattr(routes_settings.sessions, "apply_retention", lambda *a, **k: 0)
    for bad in (1, 59, 3599):
        with pytest.raises(HTTPException) as ei:
            _arun(routes_settings.api_save_config({"sessions": {"ttl": bad}}))
        assert ei.value.status_code == 400, bad


# ── 18. Test-suite rules ─────────────────────────────────────────────────────

def test_no_test_file_flushes_a_redis_database():
    """Keys are removed by prefix only; a FLUSHDB/FLUSHALL is never safe in this suite."""
    words = {"flush" + "db", "flush" + "all"}
    hits = []
    for f in sorted((ROOT_DIR / "tests").glob("*.py")):
        src = f.read_text(encoding="utf-8")
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.NAME and tok.string.lower() in words:
                hits.append(f"{f.name}:{tok.start[0]}")
            elif tok.type == tokenize.STRING:
                try:
                    val = ast.literal_eval(tok.string)
                except Exception:
                    continue
                if isinstance(val, str) and val.strip().lower() in words:
                    hits.append(f"{f.name}:{tok.start[0]}")
    assert not hits, f"FLUSHDB/FLUSHALL in tests: {hits}"


def test_an_expect_test_that_times_out_is_not_a_kill(tmp_path):
    import sys
    import mutation_sweep as ms
    root = tmp_path / "r"
    (root / "tests").mkdir(parents=True)
    (root / "pytest.ini").write_text("[pytest]\n")
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "mod.py").write_text("def f():\n    return 1\n")
    (root / "tests" / "test_mod.py").write_text(
        "import time\nfrom pkg.mod import f\n\ndef test_f():\n    if f() != 1:\n        time.sleep(60)\n")
    s = ms.Settings(root=root, python=Path(sys.executable), index=ms.index_tests(root), timeout=8)
    r = ms.run_one({"id": "MX", "file": "pkg/mod.py", "defect": "d", "find": "return 1",
                    "replace": "return 2", "expect": "test_f"}, s)
    assert r["verdict"] == "TIMEOUT", r
    assert "TIMEOUT" in ms.FAILING


# ── 8 / 9 / 10. The conversation registry ────────────────────────────────────

DAY = 86400


def _rows():
    from visualweaver import sessions
    return {r["id"]: r for r in sessions.list_sessions()}


def test_a_lost_registry_is_rebuilt_even_when_its_marker_survived(app_store):
    from visualweaver import sessions
    sessions.save_session("c1", _conv(ts=1_000))
    sessions.save_session("c2", _conv(ts=2_000))
    assert set(_rows()) == {"c1", "c2"}
    app_store.delete(sessions._INDEX_KEY)           # the hash is gone; anything else kept
    assert set(_rows()) == {"c1", "c2"}, "the listing stayed empty after the registry was lost"


def test_a_conversation_is_never_stored_without_its_registry_row(app_store, monkeypatch):
    from visualweaver import sessions
    sessions.save_session("c0", _conv(ts=1_000))
    _rows()
    real = type(app_store).hset

    def broken(self, key, mapping):
        if key == sessions._INDEX_KEY:
            raise ConnectionError("store went away")
        return real(self, key, mapping)
    monkeypatch.setattr(type(app_store), "hset", broken)
    sessions.save_session("c1", _conv(ts=2_000))     # logged, not raised
    monkeypatch.setattr(type(app_store), "hset", real)
    stored = app_store.get(sessions._session_key("c1")) is not None
    assert not (stored and "c1" not in _rows()), "a stored conversation is missing from the list"


def test_a_row_written_just_before_its_conversation_is_not_pruned(app_store):
    from visualweaver import sessions
    sessions.save_session("c0", _conv(ts=1_000))
    _rows()
    msgs = _conv(ts=int(time.time()))
    app_store.hset(sessions._INDEX_KEY, {"fresh": json.dumps(sessions._index_row(
        sessions.session_summary("fresh", msgs)))})
    _rows()                                           # lands between the two writes
    app_store.set(sessions._session_key("fresh"), json.dumps(msgs))
    assert "fresh" in _rows()


def test_pinning_while_retention_is_applied_is_not_undone(app_store, cfg, monkeypatch):
    from visualweaver import sessions
    for sid in ("a", "b"):
        sessions.save_session(sid, _conv(ts=1_000))
    _rows()
    cfg["sessions"] = {"persist": True, "ttl": DAY}
    real = type(app_store).ttls
    fired = []

    def ttls(self, keys):
        out = real(self, keys)
        if not fired and sessions._session_key("a") in keys:
            fired.append(1)
            sessions.update_session("a", pinned=True)          # the user pins it right now
        return out
    monkeypatch.setattr(type(app_store), "ttls", ttls)
    sessions.apply_retention(app_store)
    monkeypatch.setattr(type(app_store), "ttls", real)
    assert fired
    assert app_store.ttl(sessions._session_key("a")) is None, "a conversation pinned meanwhile got an expiry"
    assert app_store.ttl(sessions._session_key("b")) is not None


def test_an_open_conversation_whose_stored_copy_expired_is_not_listed_as_kept(app_store, cfg):
    from visualweaver import sessions, state
    cfg["sessions"] = {"persist": True, "ttl": DAY}
    state._sessions["old"] = _conv(ts=1_700_000_000)         # last turn long past the limit
    now = int(time.time())
    state._sessions["new"] = _conv(ts=now)                   # its save has not landed yet
    rows = _rows()
    assert "old" not in rows or rows["old"]["expires"] is not None
    assert "old" not in rows
    assert rows["new"]["expires"] is not None and abs(rows["new"]["expires"] - (now + 3 + DAY)) < 60


def test_removing_a_cached_answer_on_mongodb_reaches_only_your_own(app_store):
    """The same rule as on Redis, through mongo_cache.delete_entry."""
    import datetime as dt
    from visualweaver import mongo_cache, redis_store
    if app_store.kind != "mongodb":
        pytest.skip("the Redis case is test_removing_a_cached_answer_reaches_only_your_own")
    ep = app_store.raw
    col = ep.col(mongo_cache.COLLECTION)
    exp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
    col.insert_many([{"_id": "ea", "prompt": "q", "response": "private", "owner": "aaaa", "exp": exp},
                     {"_id": "es", "prompt": "q", "response": "shared", "exp": exp}])
    try:
        assert mongo_cache.delete_entry(ep, "ea", owner="bbbb") is False
        assert mongo_cache.delete_entry(ep, "es", owner="bbbb") is False
        assert mongo_cache.delete_entry(ep, "ea", owner="") is False
        assert col.count_documents({}) == 2
        assert mongo_cache.delete_entry(ep, "ea", owner="aaaa") is True
        assert mongo_cache.delete_entry(ep, "es") is True                  # an administrator
        assert col.count_documents({}) == 0
    finally:
        col.delete_many({"_id": {"$in": ["ea", "es"]}})


def test_a_damaged_config_that_never_set_up_sign_in_keeps_the_app_running(app_module, cfg, no_auth_env,
                                                                         data_dir, monkeypatch):
    """Sign-in off (the default) and a damaged config.json: the app runs on the defaults
    (health degraded) as before, also after a restart with only the quarantined copy."""
    from visualweaver import state
    path = data_dir / "config.json"
    path.write_text('{"redis": {"host": "127.0.0.1", "port": 6389', encoding="utf-8")
    try:
        _load_into_state(app_module, path, monkeypatch)
        c = _from("127.0.0.1")
        assert c.get("/api/config").status_code == 200
        assert state._config_load_error
        _load_into_state(app_module, path, monkeypatch)      # restart: only the copy is left
        assert c.get("/api/config").status_code == 200
    finally:
        state._config_load_error = ""
        for p in data_dir.glob("config.json.corrupt-*"):
            p.unlink()
