# SPDX-License-Identifier: AGPL-3.0-or-later
"""Second review of 1.16.0 in Chromium, Firefox and WebKit (rereview_probe.py: canned API
answers, a fake WebSocket, no server; real pointer input).

Before these fixes: a Documents only follow-up answered from the previous answer's sources
looked exactly like one answered from its own search ("1 matched chunk"); an answer citing
[1] and [2] when nothing had been retrieved was badged a neutral "– Nothing retrieved"; a
made-up "[9]" with 3 sources was left as plain text and the answer badged "● Grounded";
and while /api/me failed, hung or answered 503 every administrator control was shown.
"""
import json
import pathlib
import subprocess

import pytest

from test_science_lanes_browser import _no_browser, _playwright_python

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PROBE = pathlib.Path(__file__).with_name("rereview_probe.py")
_RUNS: dict = {}


@pytest.fixture(scope="module", params=["chromium", "firefox", "webkit"])
def rr(request):
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


def test_the_probe_ran_every_step_without_page_errors(rr):
    assert not rr.get("step_errors"), rr.get("step_errors")
    assert rr["errors"] == []


# ── M1: a follow-up answered from the previous answer's sources says so ──────

@pytest.mark.parametrize("width", [1400, 375])
def test_reused_sources_are_labelled_in_the_source_list_and_the_badge(rr, width):
    own, reused = rr[f"reused_{width}"]
    assert own["header"] == "1 matched chunk" and own["icon"] == "📚" and own["title"] is None
    assert "previous answer" not in (own["badgeAria"] or "")
    assert reused["header"] == "1 source from the previous answer" and reused["icon"] == "↩"
    assert "previous answer cited" in reused["title"]
    assert "previous answer's" in reused["badgeAria"]
    # drawn, and inside its header and the window
    assert reused["display"] == "flex" and reused["sw"] > 100 and reused["sright"] <= reused["hright"]
    assert reused["hright"] <= rr[f"reused_vw_{width}"] and rr[f"reused_overflow_{width}"] == 0


def test_the_reused_source_list_opens_with_a_click(rr):
    o = rr["reused_open"]
    assert o["open"] and o["display"] == "block" and o["bodyH"] > 20 and o["src"]


# ── minor 7: citing with nothing retrieved; a made-up number ─────────────────

@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_citations_with_nothing_retrieved_are_badged_as_missing_sources(rr, scheme):
    miss, none, partly = rr[f"missing_badges_{scheme}"]
    assert miss["cls"] == "ground-badge g-missing" and miss["text"] == "○ Cites missing sources"
    assert "cites 2 source numbers" in miss["aria"]
    assert miss["display"] == "flex" and miss["w"] > 100 and miss["h"] >= 16
    assert miss["badge"]["ratio"] >= 4.5, miss["badge"]
    assert none["cls"] == "ground-badge g-noretrieval" and none["text"] == "– Nothing retrieved"
    # [9] with 3 sources counts against the answer
    assert partly["text"] == "◐ Partly grounded" and "1 citation points to no source" in partly["aria"]


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_a_made_up_number_is_drawn_as_a_broken_citation(rr, scheme):
    first, second, third = rr[f"missing_broken_{scheme}"]
    assert [b["text"][:3] for b in first] == ["[1]", "[2]"] and second == []
    assert [b["text"][:3] for b in third] == ["[9]"]
    for b in first + third:
        assert b["w"] > 10 and b["h"] > 10 and "line-through" in b["deco"]


def test_the_missing_sources_badge_fits_a_phone(rr):
    m = rr["missing_375"]
    assert m["overflow"] == 0 and all(b["right"] <= m["vw"] for b in m["badges"])
    assert m["badges"][0]["text"] == "○ Cites missing sources"


# ── minor 9: no administrator control while sign-in is unknown ──────────────

@pytest.mark.parametrize("case", ["me_hang", "me_503", "me_html", "me_retry_before", "me_user"])
def test_administrator_controls_are_hidden_until_sign_in_is_known(rr, case):
    btn = rr[case]["btn"]
    assert btn["display"] == "none" and btn["w"] == 0, (case, btn)


def test_a_failed_sign_in_check_is_asked_again(rr):
    assert rr["me_503"]["me"] is None and rr["me_503"]["calls"] >= 2
    assert rr["me_hang"]["cls"] == "vw-user"


def test_a_single_user_install_gets_its_controls_back_after_a_retry(rr):
    after = rr["me_retry_after"]
    assert after["me"] == {"auth": False, "user": None, "admin": True} and after["calls"] == 3
    assert after["btn"]["display"] != "none" and after["btn"]["w"] > 30 and after["btn"]["h"] > 15
    assert rr["me_retry_click"] >= 1, "the shown control did not respond to a click"


def test_sign_in_off_shows_the_controls_at_once(rr):
    off = rr["me_off"]
    assert off["calls"] == 1 and off["btn"]["display"] != "none" and off["btn"]["w"] > 30
