# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign-in in the page, measured in Chromium, Firefox and WebKit (signin_probe.py: canned
API answers, no server): the signed-in name is shown as text, and someone who is not an
administrator is offered no control that only an administrator may use."""
import json
import pathlib
import subprocess

import pytest

from test_science_lanes_browser import _no_browser, _playwright_python

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PROBE = pathlib.Path(__file__).with_name("signin_probe.py")
_RUNS: dict = {}
_ALL_TABS = ["status", "redis", "rag", "websources", "cache", "providers", "general", "templates",
             "security", "analytics", "logs", "about"]


@pytest.fixture(scope="module", params=["chromium", "firefox", "webkit"])
def probe(request):
    engine = request.param
    if engine not in _RUNS:
        py = _playwright_python()
        if not py:
            _no_browser()
        r = subprocess.run([py, str(_PROBE), str(_ROOT / "visualweaver" / "index.html"), engine],
                           capture_output=True, text=True, timeout=600, cwd=str(_PROBE.parent))
        lines = (r.stdout or "").strip().splitlines()
        _RUNS[engine] = json.loads(lines[-1]) if lines else {"error": r.stderr[-600:]}
    out = _RUNS[engine]
    if out.get("skip"):
        if engine == "chromium":
            _no_browser()
        pytest.skip(out["skip"])
    assert out.get("ok"), out.get("error")
    return out


def _visible_tabs(view):
    return [t["tab"] for t in view["tabs"] if t["display"] != "none" and t["tab"] != "sep"]


def test_the_probe_ran_every_step_without_page_errors(probe):
    assert not probe.get("step_errors"), probe.get("step_errors")
    assert probe["errors"] == []


def test_the_signed_in_name_is_shown_as_text_inside_the_sidebar(probe):
    """A name the proxy passes is the user's own text: it is never markup."""
    s = probe["user"]["signed"]
    assert s["display"] == "flex" and s["h"] >= 24 and s["w"] > 150
    assert s["name"].startswith("<img src=x") and s["imgs"] == 0 and not s["xss"]
    assert s["sbLeft"] <= s["x"] and s["right"] <= s["sbRight"] and s["nameRight"] <= s["right"]
    assert s["nameClipped"] and s["nameOverflow"] == "ellipsis"          # a long name is cut, not spilled
    assert s["roleDisplay"] == "none"                                   # not an administrator
    a = probe["admin"]["signed"]
    assert a["name"] == "Root" and a["role"] == "admin" and a["roleDisplay"] != "none" and a["roleW"] > 10
    assert probe["off"]["signed"]["display"] == "none"                  # sign-in off: nothing shown


def test_a_non_admin_gets_only_the_read_only_settings(probe):
    u = probe["user"]
    assert _visible_tabs(u) == ["status", "analytics", "about"]
    assert all(t["display"] == "none" for t in u["tabs"] if t["tab"] == "sep")
    assert [t["tab"] for t in u["tabs"] if t["active"]] == ["status"]   # Settings opened on a tab they have
    assert u["footer"]["display"] == "none"                             # no Save button
    assert u["arrow_walk"] == ["analytics", "about", "status", "analytics"]
    assert u["reset_btns"] == ["none", "none"]
    assert u["ctrl_k_tab"] == "status"
    # and the page itself sent nothing an administrator would (no default instance created)
    assert u["posts"] == []


def test_admins_and_single_user_installs_keep_every_control(probe):
    for key in ("admin", "off"):
        v = probe[key]
        assert _visible_tabs(v) == _ALL_TABS, key
        assert v["footer"]["display"] == "flex" and v["footer"]["h"] > 30
        assert v["reset_btns"] == ["flex", "flex"] and v["ctrl_k_tab"] == "general"
        assert "POST /api/rag/instances" in v["posts"]                  # the missing default is created


def test_with_sign_in_on_every_own_conversation_is_listed(probe):
    """The server lists only the user's own conversations, so the sidebar shows all of them,
    not only the ones this browser started. Off, the per-browser list still applies."""
    assert probe["user"]["listed"][1:] == ["s_one", "s_two"]
    assert probe["off"]["listed"][1:] == []


def test_the_signed_in_row_fits_a_phone(probe):
    m = probe["m_signed"]
    assert m["display"] == "flex" and m["h"] >= 24
    assert m["sbLeft"] <= m["x"] and m["right"] <= m["sbRight"] <= m["vw"]
    assert 0 <= m["top"] and m["bottom"] <= m["vh"] and m["docOverflow"] == 0
    assert m["nameClipped"] and m["imgs"] == 0
