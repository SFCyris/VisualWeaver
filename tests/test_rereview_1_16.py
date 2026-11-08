# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixes from the second review of 1.16.0 (server side).

Documents only — a follow-up that retrieves nothing reused the previous answer's cited
passages whatever this turn searched: after switching knowledge base (a selection,
a #mention, a project) the model was handed passages from a knowledge base the user had
not selected, and any unrelated question with no hits got them too, so "no model call
when nothing matches" no longer held. Reuse now needs the same knowledge bases as the
previous answer, only keeps passages from this turn's knowledge bases (and source
filter), and only for a question that brings in no subject of its own.

The rest is listed per section below.
"""
import time

import pytest

from conftest import KEY_PREFIX

from test_event_loop_responsiveness import _frames_until, chat   # noqa: F401  (fixture)
from test_documents_only import _KB, _answers, _turn, kb          # noqa: F401  (fixture)
from test_uxfix_backend import _HIT, citing_model                 # noqa: F401  (fixture)


# ── M1: documents-only follow-ups reuse only what this turn may see ──────────

def _first_answer(ws, kb, **extra):
    kb.hits = [dict(_HIT)]
    _turn(ws, content="how long is the warranty?", documents_only=True, **extra)
    kb.hits = []


def _refused(frames):
    from visualweaver import rag
    toks = [f["content"] for f in frames if f["type"] == "token"]
    ctx = next(f for f in frames if f["type"] == "rag_context")
    return toks == [rag.DOCS_ONLY_NOT_FOUND] and not ctx.get("reused_sources") and ctx["chunks"] == []


def test_a_follow_up_after_switching_knowledge_base_does_not_get_the_old_passages(chat, kb, citing_model):
    """Old: "shorten that" asked against KB "other" was answered from KB "kb"'s passages."""
    sid = f"{KEY_PREFIX}m1_switch"
    with chat.websocket_connect(f"/ws/chat/{sid}") as ws:
        _first_answer(ws, kb)
        frames = _turn(ws, content="shorten that", documents_only=True, rag_instance="other")
    assert _refused(frames), "passages of a knowledge base this turn did not search were reused"
    assert len(_answers(chat)) == 1, "the model was called although nothing matched"


def test_a_follow_up_with_a_mention_of_another_knowledge_base_is_refused(chat, kb, citing_model):
    """A #mention (or a project) widens the selection: not the previous answer's selection."""
    sid = f"{KEY_PREFIX}m1_mention"
    with chat.websocket_connect(f"/ws/chat/{sid}") as ws:
        _first_answer(ws, kb)
        frames = _turn(ws, content="shorten that", documents_only=True, rag_instances=["kb", "other"])
    assert _refused(frames) and len(_answers(chat)) == 1


def test_an_unrelated_question_with_no_hits_is_refused_without_the_model(chat, kb, citing_model):
    """Old: any no-hit question in the same conversation was answered from the old passages."""
    sid = f"{KEY_PREFIX}m1_unrelated"
    with chat.websocket_connect(f"/ws/chat/{sid}") as ws:
        _first_answer(ws, kb)
        frames = _turn(ws, content="What is the capital of France?", documents_only=True)
    assert _refused(frames)
    assert len(_answers(chat)) == 1, "an unrelated question with no match called the model"


def test_a_follow_up_with_a_source_filter_excluding_the_passages_is_refused(chat, kb, citing_model):
    sid = f"{KEY_PREFIX}m1_filter"
    with chat.websocket_connect(f"/ws/chat/{sid}") as ws:
        _first_answer(ws, kb)
        frames = _turn(ws, content="shorten that", documents_only=True, source_filter="other.pdf")
    assert _refused(frames) and len(_answers(chat)) == 1


@pytest.mark.parametrize("follow_up", ["shorten that", "Explain the warranty in more detail, please.",
                                       "Put that in a table", "what about the purchase date?"])
def test_a_real_follow_up_in_the_same_knowledge_base_still_reuses_the_sources(chat, kb, citing_model,
                                                                              follow_up):
    sid = f"{KEY_PREFIX}m1_legit"
    with chat.websocket_connect(f"/ws/chat/{sid}") as ws:
        _first_answer(ws, kb)
        frames = _turn(ws, content=follow_up, documents_only=True)
    ctx = next(f for f in frames if f["type"] == "rag_context")
    assert ctx.get("reused_sources") is True and [c["source"] for c in ctx["chunks"]] == ["manual.pdf"]
    assert len(_answers(chat)) == 2 and "two years from the date of purchase" in _answers(chat)[1][-1]


def test_rest_follow_up_after_switching_knowledge_base_is_refused(chat, kb, citing_model):
    sid = f"{KEY_PREFIX}m1_rest"
    kb.hits = [dict(_HIT)]
    chat.post("/api/chat", json={**_KB, "content": "warranty?", "session_id": sid, "documents_only": True})
    kb.hits = []
    body = chat.post("/api/chat", json={**_KB, "rag_instance": "other", "content": "shorten that",
                                        "session_id": sid, "documents_only": True}).json()
    assert body.get("not_found") is True and not body.get("reused_sources") and len(_answers(chat)) == 1


def test_only_passages_from_this_turns_knowledge_bases_are_reused(monkeypatch):
    """Even with the same selection recorded, a passage whose knowledge base is not searched
    this turn (or that names none, from an older turn) is never handed on."""
    from visualweaver import routes_chat, state
    a = {"n": 1, "source": "a.pdf", "text": "Alpha warranty.", "instance": "kb"}
    b = {"n": 2, "source": "b.pdf", "text": "Beta warranty.", "instance": "elsewhere"}
    c = {"n": 3, "source": "c.pdf", "text": "Gamma warranty."}
    sid = f"{KEY_PREFIX}m1_inst"
    monkeypatch.setitem(state._sessions, sid, [
        {"role": "user", "content": "warranty?"},
        {"role": "assistant", "content": "Two years [1][2][3].", "meta": {"chunks": [a, b, c], "kbs": ["kb"]}}])
    got = routes_chat._previous_cited_sources(sid, None, "shorten that", ["kb"])
    assert [x["source"] for x in got] == ["a.pdf"]
    # a turn stored without its knowledge bases cannot be shown to match: nothing reused
    state._sessions[sid][1]["meta"].pop("kbs")
    assert routes_chat._previous_cited_sources(sid, None, "shorten that", ["kb"]) == []


@pytest.mark.parametrize("query,expect", [
    ("shorten that", True), ("make it shorter", True), ("explain point 2", True),
    ("Can you summarise the warranty?", True), ("in bullet points please", True),
    ("What is the capital of France?", False), ("who won the 2022 world cup", False),
    ("法国的首都是什么", False), ("", False), ("2", False),
])
def test_what_counts_as_a_follow_up(query, expect):
    from visualweaver import rag
    ctx = ["how long is the warranty?", "The warranty lasts two years [1].",
           "The warranty is two years from the date of purchase.", "manual.pdf"]
    assert rag.is_follow_up(query, ctx) is expect


# ── M2 + minor 6: tool images in a folder whose path has a space ─────────────

from pathlib import Path                                                    # noqa: E402

from test_security_review_1_16 import _from, no_auth_env                    # noqa: E402,F401

_PNG = b"\x89PNG\r\n\x1a\n" + b"1" * 16


@pytest.fixture
def spaced_image_dir(tmp_path, monkeypatch):
    """The image folder under a path with a space, as the default macOS data directory
    ("~/Library/Application Support/VisualWeaver") has."""
    from visualweaver import routes_media
    d = tmp_path / "Application Support" / "VisualWeaver" / "images"
    d.mkdir(parents=True)
    monkeypatch.setattr(routes_media, "_ALLOWED_IMAGE_DIRS", [d])
    return d


def test_a_tool_image_in_a_folder_with_a_space_renders_and_is_served(app_module, cfg, no_auth_env,
                                                                       spaced_image_dir):
    """Old: "![…](/api/files/image?path=/…/Application Support/…)" — marked does not take a
    link destination with a space, so the answer showed the markdown as text."""
    import re
    import urllib.parse

    from test_math_pipeline_js import _render
    from visualweaver import providers
    cfg.pop("auth", None)
    img = spaced_image_dir / "chart #1 & more.png"
    img.write_bytes(_PNG)
    md = providers._tool_result_to_markdown([{"function": {"name": "draw", "arguments": {"p": str(img)}}}])
    html = _render(md, {"math": True})
    m = re.search(r'<img[^>]*\bsrc="([^"]+)"', html)
    assert m, f"the image markdown did not render as an image: {html[:300]}"
    src = m.group(1).replace("&amp;", "&")
    assert src.startswith("/api/files/image?path=")
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(src).query)["path"] == [str(img)]
    r = _from("127.0.0.1").get(src)
    assert r.status_code == 200 and r.content == _PNG


def test_the_image_route_does_not_tell_an_existing_file_outside_from_a_missing_one(
        app_module, cfg, no_auth_env, spaced_image_dir, tmp_path):
    """Old: 403 for a file that exists outside the image folder, 404 for one that does not —
    so any signed-in user could probe which files exist anywhere on the disk."""
    cfg.pop("auth", None)
    outside = tmp_path / "secret.png"
    outside.write_bytes(_PNG)
    c = _from("127.0.0.1")
    there = c.get("/api/files/image", params={"path": str(outside)})
    missing = c.get("/api/files/image", params={"path": str(tmp_path / "nothing.png")})
    assert (there.status_code, there.json()) == (missing.status_code, missing.json()) == (404, {"detail": "File not found"})
    inside = spaced_image_dir / "ok.png"
    inside.write_bytes(_PNG)
    assert c.get("/api/files/image", params={"path": str(inside)}).status_code == 200


# ── M3: /api/health answers while sign-in is misconfigured ───────────────────

_HEALTH_KEYS = {"status", "app", "detail", "license", "source"}


def _misconfigured_health():
    """GET /api/health from the real app while sign-in is misconfigured: (status, body)."""
    r = _from("127.0.0.1").get("/api/health")
    return r.status_code, r.text


@pytest.mark.parametrize("auth", [{"mode": "header", "trusted_proxies": ["not-an-address"]},
                                  "header"])
def test_health_says_misconfigured_with_200_and_nothing_else(app_module, cfg, no_auth_env, auth):
    """Old: 503 like every other route, so start.sh waited 60 s and gave up while the app
    kept running, and the Docker health check failed."""
    import json
    cfg["auth"] = auth
    cfg["provider"] = "claude"
    status, text = _misconfigured_health()
    body = json.loads(text)
    assert status == 200 and body["status"] == "misconfigured" and body["app"] == "VisualWeaver"
    assert set(body) == _HEALTH_KEYS, "the misconfigured health answer carries more than it should"
    assert "claude" not in text and "version" not in body and "services" not in body
    assert "misconfigured" in body["detail"].lower()
    c = _from("127.0.0.1")
    assert c.get("/api/config").status_code == 503 and c.get("/api/me").status_code == 503
    assert c.post("/api/health").status_code in (405, 503)
    assert c.get("/api/health/x").status_code == 503


def test_health_says_misconfigured_when_config_json_cannot_be_loaded(app_module, cfg, no_auth_env,
                                                                     data_dir, monkeypatch):
    import json
    from test_security_review_1_16 import _load_into_state
    from visualweaver import state
    path = data_dir / "config.json"
    path.write_text('{"auth": {"mode": "header"}, "redis": {', encoding="utf-8")
    try:
        _load_into_state(app_module, path, monkeypatch)
        status, text = _misconfigured_health()
        body = json.loads(text)
        assert status == 200 and body["status"] == "misconfigured" and set(body) == _HEALTH_KEYS
        assert "config.json" in body["detail"]
        assert _from("127.0.0.1").get("/api/config").status_code == 503
    finally:
        state._config_load_error = ""
        for p in data_dir.glob("config.json.corrupt-*"):
            p.unlink()


def test_health_with_sign_in_set_up_correctly_is_unchanged(app_module, cfg, no_auth_env):
    cfg["auth"] = {"mode": "header", "trusted_proxies": ["127.0.0.1"], "admin_users": ["root"]}
    r = _from("127.0.0.1").get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok" and "version" in r.json()


from test_docker_helper import repo, run as docker_run                      # noqa: E402,F401


def test_docker_sh_up_reports_misconfigured_sign_in(repo, app_module, cfg, no_auth_env):
    """The fake curl answers as the real app does while sign-in is misconfigured (taken
    from it): /api/health 200 "misconfigured", /api/components 503 (curl -f: exit 22)."""
    import shlex
    cfg["auth"] = {"mode": "header", "trusted_proxies": ["not-an-address"]}
    c = _from("127.0.0.1")
    health, comp = c.get("/api/health"), c.get("/api/components")
    assert comp.status_code == 503
    _, b, _ = repo
    (b / "curl").write_text("#!/bin/sh\ncase \"$*\" in\n"
                            f"  *api/health*) printf '%s\\n' {shlex.quote(health.text)} ;;\n"
                            "  *) exit 22 ;;\nesac\n")
    p, _, _ = docker_run(repo, "up", "redis")
    assert p.returncode == 0, p.stderr
    assert "Sign-in:  MISCONFIGURED" in p.stderr and health.json()["detail"] in p.stderr
    # and a healthy app is reported without it
    (b / "curl").write_text("#!/bin/sh\necho '{\"status\": \"ok\", \"app\": \"VisualWeaver\"}'\n")
    p, _, _ = docker_run(repo, "up", "redis")
    assert p.returncode == 0 and "MISCONFIGURED" not in p.stdout + p.stderr


# ── M4: user and group names are compared exactly ────────────────────────────

def _who(s, name, groups=""):
    from visualweaver import auth
    h = [(b"remote-user", name.encode())]
    if groups:
        h.append((b"remote-groups", groups.encode()))
    return auth.identify({"type": "http", "client": ("127.0.0.1", 1), "headers": h}, s)[0]


def test_a_name_differing_only_in_case_is_another_user_and_not_the_admin(app_module, cfg, no_auth_env):
    """Old: "Root" and "root" were one identity, so on a proxy whose user names are
    case-sensitive (with self-enrolment) anyone could sign up as "Root" and be the admin,
    and read root's conversations."""
    from visualweaver import auth
    s = auth.parse_settings({"mode": "header", "admin_users": ["root"], "admin_groups": ["vw-admins"]})
    assert _who(s, "root").admin is True
    for name in ("Root", "ROOT", "rOOt"):
        u = _who(s, name)
        assert u.admin is False and u.id == name, name
    assert _who(s, "carol", "vw-admins").admin is True
    assert _who(s, "carol", "VW-Admins").admin is False


def test_case_insensitive_names_are_an_explicit_choice(app_module, cfg, no_auth_env, monkeypatch):
    from visualweaver import auth
    s = auth.parse_settings({"mode": "header", "admin_users": ["Root"], "admin_groups": ["VW-Admins"],
                             "case_insensitive_names": True})
    assert _who(s, "ROOT").admin is True and _who(s, "ROOT").id == "root" == _who(s, "root").id
    assert _who(s, "carol", "vw-admins").admin is True
    # non-ASCII names stay exact even then
    assert _who(s, "JÜRGEN").id != _who(s, "jürgen").id
    # the environment can state it too; anything but a clear "on" leaves it off
    monkeypatch.setenv("VISUALWEAVER_AUTH", "header")
    for val, on in (("1", True), ("true", True), ("yes", True), ("0", False), ("off", False), ("maybe", False)):
        monkeypatch.setenv("VISUALWEAVER_AUTH_CASE_INSENSITIVE_NAMES", val)
        assert auth.settings().fold_case is on, val
    monkeypatch.delenv("VISUALWEAVER_AUTH_CASE_INSENSITIVE_NAMES")
    monkeypatch.delenv("VISUALWEAVER_AUTH")
    cfg["auth"] = {"mode": "header"}                                      # the default: exact
    assert auth.settings().fold_case is False
    cfg["auth"] = {"mode": "header", "case_insensitive_names": True}
    assert auth.settings().fold_case is True


def test_conversations_of_names_differing_in_case_are_kept_apart(app_module, cfg, no_auth_env):
    from test_security_review_1_16 import PROXY
    from visualweaver import sessions
    cfg["auth"] = {"mode": "header", "trusted_proxies": [PROXY], "admin_users": ["root"]}
    c = _from(PROXY)
    a = c.get("/api/me", headers={"Remote-User": "Alice"}).json()
    b = c.get("/api/me", headers={"Remote-User": "alice"}).json()
    assert a["id"] == "Alice" and b["id"] == "alice"
    assert sessions.user_prefix(a["id"]) != sessions.user_prefix(b["id"])


# ── minor 1: this machine's names, from its host name only ───────────────────

_ARPA = "1.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.ip6.arpa"


@pytest.mark.parametrize("hostname,fqdn,allowed,refused", [
    # macOS: getfqdn() answers a reverse-lookup name for ::1
    ("Studio.local", _ARPA, {"studio.local", "studio"}, {"1", "1.local", _ARPA}),
    ("nas", "nas.corp.example", {"nas", "nas.local", "nas.corp.example"}, {"corp.example", "nas.corp"}),
    ("1", "1.0.0.127.in-addr.arpa", set(), {"1", "1.local", "1.0.0.127.in-addr.arpa"}),
    ("box.lan", "box.lan", {"box.lan", "box", "box.local"}, {"lan"}),
])
def test_machine_names_come_from_the_host_name_not_a_reverse_lookup(monkeypatch, hostname, fqdn,
                                                                     allowed, refused):
    """Old: getfqdn()'s "…ip6.arpa" gave the allowed names "1" and "1.local" — a .local
    name any device on the network can claim over mDNS."""
    import socket as _s
    from visualweaver import appcore
    monkeypatch.setattr(_s, "gethostname", lambda: hostname)
    monkeypatch.setattr(_s, "getfqdn", lambda *a: fqdn)
    names = appcore._machine_names()
    assert allowed <= names, names
    assert not (refused & names), names
    assert all(not n.endswith(".arpa") and not n.split(".")[0].isdigit() for n in names), names


def test_docker_sh_records_no_names_from_a_reverse_lookup(repo):
    """./docker.sh records this machine's names for the container; `hostname -f` on macOS
    can print an "…ip6.arpa" name, whose first label "1" must not become "1" and "1.local"."""
    _, b, _ = repo
    (b / "hostname").write_text("#!/bin/sh\n[ \"$1\" = -f ] && echo " + _ARPA + " || echo Studio.local\n")
    (b / "hostname").chmod(0o755)
    p, _, env = docker_run(repo, "up", "redis")
    assert p.returncode == 0, p.stderr
    line = next(ln for ln in env.splitlines() if ln.startswith("VISUALWEAVER_ALLOWED_HOSTS="))
    names = set(line.split("=", 1)[1].split(","))
    assert names == {"studio.local", "studio"}, names


# ── minor 5: the Host is checked on every request ────────────────────────────

def test_a_rebinding_host_is_refused_without_origin_or_fetch_metadata(app_module, cfg, no_auth_env,
                                                                     monkeypatch):
    """Old: the host check ran only when Origin or Sec-Fetch-* was present; Safari before
    16.4 and Firefox before 90 send neither on a GET, so a page on a re-pointed domain
    read /api/config (200) through them."""
    from visualweaver import appcore
    cfg.pop("auth", None)
    c = _from("127.0.0.1")
    for path in ("/api/config", "/api/sessions", "/"):
        r = c.get(path, headers={"Host": "evil.example:8420"})
        assert r.status_code == 403, (path, r.status_code)
    # the liveness probe answers whatever the name (a container health check, a proxy)
    assert c.get("/api/health", headers={"Host": "evil.example:8420"}).status_code == 200
    # the names that were always allowed still are, for scripts too
    for host in ("127.0.0.1:8420", "localhost:8420", "[::1]:8420", "testserver"):
        assert c.get("/api/config", headers={"Host": host}).status_code == 200, host
    # "*" is the explicit way to turn the check off
    monkeypatch.setattr(appcore, "_allowed_hosts", {"*"})
    assert c.get("/api/config", headers={"Host": "evil.example:8420"}).status_code == 200


# ── minor 2: the retention floor holds on every way in ───────────────────────

@pytest.mark.parametrize("ttl,expect", [(1, 3600), (59, 3600), (3599, 3600), (3600, 3600), (0, 0),
                                        (86401, 86401), (-5, 0), ("x", 0)])
def test_retention_ttl_never_gives_less_than_an_hour(app_module, cfg, ttl, expect):
    """Old: `sessions.ttl: 1` in config.json (or an import) gave every conversation a
    one-second expiry on the next apply_retention."""
    from visualweaver import sessions
    cfg["sessions"] = {**(cfg.get("sessions") or {}), "ttl": ttl}
    assert sessions.retention_ttl() == expect


def test_a_config_import_with_a_retention_of_seconds_is_refused(app_module, cfg, no_auth_env, monkeypatch):
    import json
    from visualweaver import config, routes_settings, state
    cfg.pop("auth", None)
    saved = []
    monkeypatch.setattr(config, "save_config", lambda c: saved.append(c))
    monkeypatch.setattr(routes_settings.sessions, "apply_retention", lambda *a, **k: 0)
    before = dict(state._config.get("sessions") or {})
    c = _from("127.0.0.1")
    for bad in (1, 3599, -1, "1", True):
        body = json.dumps({"sessions": {"ttl": bad, "retention_chosen": True}}).encode()
        r = c.post("/api/config/import", files={"file": ("c.json", body, "application/json")})
        assert r.status_code == 400, (bad, r.status_code, r.text)
    assert saved == [] and dict(state._config.get("sessions") or {}) == before
    body = json.dumps({"sessions": {"ttl": 7200, "retention_chosen": True}}).encode()
    r = c.post("/api/config/import", files={"file": ("c.json", body, "application/json")})
    assert r.status_code == 200, r.text
    assert state._config["sessions"]["ttl"] == 7200


def test_a_config_json_with_a_retention_of_seconds_loads_as_an_hour(app_module, cfg, data_dir, monkeypatch):
    import json
    from test_security_review_1_16 import _load_into_state
    path = data_dir / "config.json"
    path.write_text(json.dumps({"sessions": {"ttl": 1, "retention_chosen": True}}), encoding="utf-8")
    loaded = _load_into_state(app_module, path, monkeypatch)
    assert loaded["sessions"]["ttl"] == 3600


# ── minor 3: which damaged config.json keeps sign-in closed ──────────────────

def _write_cfg(path, obj_or_text, encoding="utf-8", bom=b""):
    import json
    text = obj_or_text if isinstance(obj_or_text, str) else json.dumps(obj_or_text)
    path.write_bytes(bom + text.encode(encoding))


@pytest.fixture
def load_cfg(app_module, cfg, no_auth_env, data_dir, monkeypatch):
    from test_security_review_1_16 import _load_into_state
    from visualweaver import state
    path = data_dir / "config.json"

    def load():
        return _load_into_state(app_module, path, monkeypatch)
    load.path = path
    load.dir = data_dir
    yield load
    state._config_load_error = ""
    for p in data_dir.glob("config.json.corrupt-*"):
        p.unlink()


@pytest.mark.parametrize("encoding,bom", [("utf-8", b"\xef\xbb\xbf"), ("utf-16-le", b"\xff\xfe"),
                                          ("utf-16-be", b"\xfe\xff")])
def test_a_valid_config_json_with_a_byte_order_mark_loads(load_cfg, encoding, bom):
    """Old: a UTF-8 file with a BOM (as some Windows editors save it) failed to parse, was
    quarantined as corrupt, and — naming "auth" — kept the app at 503 for good."""
    from visualweaver import state
    _write_cfg(load_cfg.path, {"provider": "claude", "auth": {"mode": "off"}}, encoding, bom)
    loaded = load_cfg()
    assert state._config_load_error == "" and loaded["provider"] == "claude"
    assert loaded["auth"] == {"mode": "off"} and load_cfg.path.exists()
    assert not list(load_cfg.dir.glob("config.json.corrupt-*"))


@pytest.mark.parametrize("text,encoding,bom,closed", [
    ('{"auth": {"mode": "header"}, "redis": {', "utf-16-le", b"\xff\xfe", True),   # UTF-16 with a BOM
    ('{"auth": {"mode": "header"}, "redis": {', "utf-16-le", b"", True),           # UTF-16 without
    ('{"AUTH": {"mode": "header"}, "redis": {', "utf-8", b"", True),               # another case
    ('{"auth" : {"mode": "header"}, "redis": {', "utf-8", b"\xef\xbb\xbf", True),
    ('', "utf-8", b"", False),                                                      # empty
    ('{"redis": {"host": "127.0.0.1", "po', "utf-8", b"", False),                  # cut short
    ('{"provider": "ollama", "templates": [{"name": "author notes"}], "x": ', "utf-8", b"", False),
])
def test_a_damaged_config_json_is_closed_only_when_it_may_have_set_up_sign_in(load_cfg, text, encoding,
                                                                               bom, closed):
    from visualweaver import auth
    _write_cfg(load_cfg.path, text, encoding, bom)
    loaded = load_cfg()
    assert (loaded.get("auth", {}).get("mode") == auth.UNREADABLE_MODE) is closed


def test_any_kept_copy_naming_auth_keeps_sign_in_closed(load_cfg):
    """Several damaged copies: the newest not naming "auth" used to decide alone."""
    from visualweaver import auth
    (load_cfg.dir / "config.json.corrupt-20260101T000000Z").write_text('{"auth": {"mode": "header"}, "x": ')
    (load_cfg.dir / "config.json.corrupt-20260201T000000Z").write_text('{"redis": ')
    # config.json missing, only the copies left
    assert load_cfg()["auth"]["mode"] == auth.UNREADABLE_MODE
    # config.json damaged without "auth", but an older copy had it
    _write_cfg(load_cfg.path, '{"redis": {"host": ')
    assert load_cfg()["auth"]["mode"] == auth.UNREADABLE_MODE
    # none of them names it: the defaults, as before
    for p in load_cfg.dir.glob("config.json.corrupt-*"):
        p.unlink()
    (load_cfg.dir / "config.json.corrupt-20260301T000000Z").write_text('{"redis": ')
    assert "auth" not in load_cfg() or load_cfg()["auth"].get("mode") != auth.UNREADABLE_MODE


# ── minor 4: cache analytics with sign-in on ─────────────────────────────────

def test_cache_stats_show_everyone_only_their_own_questions(app_module, cfg, no_auth_env, monkeypatch):
    """Old: an administrator's Analytics listed every user's cached questions."""
    from test_security_review_1_16 import PROXY
    from visualweaver import auth, routes_redis
    cfg["auth"] = {"mode": "header", "trusted_proxies": [PROXY], "admin_users": ["root"]}
    root, bob = auth.partition("root"), auth.partition("bob")
    monkeypatch.setattr(routes_redis, "_cache_stats", lambda: {"count": 4, "entries": [
        {"query": "bob: my salary is 90k", "ttl": -1, "owner": bob},
        {"query": "bob: my diagnosis", "ttl": 50, "owner": bob},
        {"query": "root: rotate keys", "ttl": -1, "owner": root},
        {"query": "from before sign-in", "ttl": -1, "owner": ""}]})
    c = _from(PROXY)
    r = c.get("/api/cache/stats", headers={"Remote-User": "root"}).json()
    assert [e["query"] for e in r["entries"]] == ["root: rotate keys"]
    assert r["count"] == 1 and r["total"] == 4
    assert sorted((u["user"], u["count"]) for u in r["by_user"]) == sorted(
        [(bob, 2), (root, 1), ("", 1)])
    assert "salary" not in json.dumps(r) and all("owner" not in e for e in r["entries"])
    b = c.get("/api/cache/stats", headers={"Remote-User": "bob"}).json()
    assert [e["query"] for e in b["entries"]] == ["bob: my salary is 90k", "bob: my diagnosis"]
    assert b["count"] == 2 and "by_user" not in b and "total" not in b
    # sign-in off: the single user sees every entry, as before
    cfg.pop("auth")
    off = _from("127.0.0.1").get("/api/cache/stats").json()
    assert off["count"] == 4 and len(off["entries"]) == 4 and all("owner" not in e for e in off["entries"])


import json  # noqa: E402


# ── minor 10: a project deleted while a conversation's first turn is running ──

from test_projects import _P1, fake_projects                                   # noqa: E402,F401


@pytest.fixture
def no_stored_unfile(monkeypatch):
    """The chat fixture stores nothing: unfiling stored copies has nothing to do."""
    from visualweaver import sessions
    monkeypatch.setattr(sessions, "unbind_project", lambda pid, store=None: [])


def test_a_project_deleted_during_the_first_turn_is_not_saved_with_it(chat, kb, fake_projects,
                                                                       no_stored_unfile):
    """The turn resolved its project when it started; deleted before the answer was stored
    (unfiling every conversation open in memory: this one has no turn in memory yet), the
    store put the deleted project's id back on it."""
    from visualweaver import projects, state
    sid = f"{KEY_PREFIX}proj_gone"
    with chat.websocket_connect(f"/ws/chat/{sid}") as ws:
        ws.send_json({"provider": "ollama", "model": "fake", "content": "q1", "rag_instances": ["kb"],
                      "project": _P1["id"]})
        _frames_until(ws, "stream_start")
        fake_projects.pop(_P1["id"])                       # DELETE /api/projects/{id}
        projects.unbind_all(_P1["id"])
        _frames_until(ws, "stream_end")
    assert "project" not in state._sessions[sid][0]["meta"], state._sessions[sid][0]["meta"]


def test_an_open_conversation_keeps_no_deleted_project_on_its_next_turn(chat, kb, fake_projects,
                                                                        no_stored_unfile):
    from visualweaver import projects, state
    sid = f"{KEY_PREFIX}proj_gone2"
    with chat.websocket_connect(f"/ws/chat/{sid}") as ws:
        _turn(ws, content="q1", rag_instances=["kb"], project=_P1["id"])
        assert state._sessions[sid][0]["meta"]["project"] == _P1["id"]
        fake_projects.pop(_P1["id"])
        projects.unbind_all(_P1["id"])
        _turn(ws, content="q2", rag_instances=["kb"], project=_P1["id"])
    assert "project" not in state._sessions[sid][0]["meta"]
