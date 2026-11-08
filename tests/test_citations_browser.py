# SPDX-License-Identifier: AGPL-3.0-or-later
"""Citation source cards, grounding badges and the Documents only toggle, measured in
Chromium, Firefox and WebKit (citations_probe.py: canned API answers, a fake WebSocket, no
server; real pointer, keyboard and touch input).

Before 1.16.0 a [n] marker could only be clicked to scroll down to the source list — there
was no way to see what it cited where it stood — an answer said nothing about how well it
was backed, a citation number with no source behind it read as plain text, and there was no
Documents only mode to ask for.
"""
import json
import pathlib
import subprocess

import pytest

from test_science_lanes_browser import _no_browser, _playwright_python

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PROBE = pathlib.Path(__file__).with_name("citations_probe.py")
_RUNS: dict = {}


@pytest.fixture(scope="module", params=["chromium", "firefox", "webkit"])
def cit(request):
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


def _inside(card, gutter=16):
    """Displayed, sized, inside the window with the gutter, on top, not clipped."""
    assert card["display"] == "block" and card["position"] == "fixed" and card["parent"] == "BODY", card
    assert card["w"] > 200 and card["h"] > 40, card
    assert card["x"] >= gutter and card["right"] <= card["vw"] - gutter, card
    assert card["y"] >= gutter and card["bottom"] <= card["vh"] - gutter, card
    assert card["onTop"] is True, "something covers the card (or it is clipped)"
    assert card["hOverflow"] is False


def test_the_probe_ran_every_step_without_page_errors(cit):
    assert not cit.get("step_errors"), cit.get("step_errors")
    assert cit["errors"] == []


def test_each_answer_carries_its_grounding_badge(cit):
    b = cit["badges"]
    assert [x["cls"].split()[-1] for x in b] == ["g-partly", "g-grounded", "g-norag", "g-uncited", "g-notfound"]
    assert b[0]["text"] == "◐ Partly grounded" and "1 citation points to no source" in b[0]["title"]
    assert b[1]["text"] == "● Grounded"
    assert b[4]["text"] == "∅ Not in your documents · Documents only"
    for x in b:
        assert x["display"] in ("flex", "inline-flex") and x["w"] > 40 and x["h"] >= 14, x
    # the states are told apart by colour, not only by text
    assert len({b[0]["color"], b[1]["color"], b[3]["color"]}) == 3


def test_a_citation_with_no_source_renders_broken_not_as_a_card(cit):
    """[7] when three sources were retrieved: struck through, dashed, not a button."""
    (x,) = cit["broken"]
    assert x["text"] == "[7]" and x["isButton"] is False
    assert "line-through" in x["deco"] and x["border"] == "dashed"
    assert x["w"] > 10 and x["h"] > 10
    assert "no source 7" in x["title"]
    assert cit["refs"] == ["[1]", "[2]", "[3]", "[1]"]


def test_hovering_a_citation_shows_its_source_card_inside_the_window(cit):
    c = cit["hover_card"]
    _inside(c)
    assert c["n"] == "[1]" and c["title"] == "docs.example.org/streams/intro.html"
    assert c["link"] == "https://docs.example.org/streams/intro.html"
    assert c["score"] == "Match 81.0% · rerank 0.931"
    assert cit["hover_ref"]["describedby"] == "cite-card" and cit["hover_ref"]["expanded"] == "true"
    # placed just below its marker
    assert 0 <= c["y"] - cit["hover_ref"]["bottom"] <= 12
    assert cit["hover_into_card"] == "block", "the card closed when the pointer moved onto it"
    assert cit["hover_left"] == "none"


def test_document_text_in_the_card_is_shown_never_parsed(cit):
    c = cit["hover_card"]
    assert c["text"].startswith('Streams are append-only. <img src=x onerror="window.__xss=1"> <b>bold</b>')
    assert c["markup"] is False and c["xss"] is False


def test_a_card_near_the_bottom_opens_above_its_marker(cit):
    c, ref = cit["low_card"], cit["low_ref"]
    _inside(c)
    assert c["bottom"] <= ref["top"], (c, ref)


def test_keyboard_focus_shows_the_card_and_escape_closes_it(cit):
    assert cit["kb_focus"] == "cite-ref|[1]"
    _inside(cit["kb_card"])
    assert cit["kb_ref"] == {"describedby": "cite-card", "expanded": "true"}
    assert cit["kb_escape"] == {"card": "none", "focus": "cite-ref|[1]", "expanded": "false"}
    assert cit["kb_next"]["focus"] == "[2]"
    _inside(cit["kb_next"]["card"])
    assert cit["kb_next"]["card"]["n"] == "[2]" and cit["kb_next"]["card"]["score"] == "Match 62.0%"



def test_enter_moves_focus_into_the_card_and_its_controls_are_reachable(cit):
    """Old: the card was role="tooltip" and Enter jumped straight to the source list, so its
    link and "Show in sources" button could not be reached from the keyboard at all."""
    e = cit["kb_enter"]
    assert e["card"] == "block" and e["role"] == "dialog" and e["label"].startswith("Source 2")
    assert e["inCard"] and e["focus"].startswith("cite-card-jump|Show in sources")
    assert cit["kb_enter_tab"]["inCard"] is True                 # Tab stays inside
    esc = cit["kb_enter_escape"]
    assert esc["card"] == "none" and esc["focus"] == "cite-ref|[2]"   # Escape: back to the marker
    j = cit["kb_jump"]
    assert j["open"] is True and j["focusId"].startswith("cite-") and j["card"] == "none"
    # a source with a URL: Space opens the card on its link, Tab reaches the button, then wraps
    assert cit["kb_space_link"]["inCard"] and cit["kb_space_link"]["focus"].startswith("|https://docs.example.org")
    assert cit["kb_space_tab"]["focus"].startswith("cite-card-jump|")
    assert cit["kb_space_wrap"]["focus"].startswith("|https://docs.example.org")


def test_the_card_follows_a_scroll_and_closes_when_its_marker_leaves(cit):
    s = cit["kb_scroll_small"]
    assert s["display"] == "block" and s["before"] - s["after"] == 30, s
    assert cit["kb_scroll_away"] == "none"


def test_a_click_jumps_to_the_source_as_before(cit):
    assert cit["click_jump"] == {"inspectorOpen": True, "contentOpen": True, "card": "none"}


def test_on_a_phone_a_tap_toggles_the_card_inside_the_375px_window(cit):
    c = cit["m_card"]
    _inside(c)
    assert c["vw"] == 375 and c["w"] == 375 - 32
    assert cit["m_scrolled"] is False, "the tap jumped to the source list instead of showing the card"
    assert cit["m_second_tap"] == "none"
    assert cit["m_reopen"] == "block" and cit["m_outside"] == "none"



def test_the_toggle_has_room_and_does_not_squeeze_the_message_box(cit):
    """At 375 px it sits on the template picker's row, above Send; inside the message box it
    took 30 px from the textarea and the placeholder wrapped."""
    for t in (cit["m_toggle"], cit["d_toggle"]):
        b = t["btn"]
        assert t["display"] != "none" and b["w"] >= 24 and b["h"] >= 24, t
        assert 0 <= b["x"] and b["right"] <= t["vw"], t
        assert t["placeholderWraps"] is False, t
        for other in ("tpl", "wrap", "send"):             # overlaps nothing
            o = t[other]
            assert b["right"] <= o["x"] or o["right"] <= b["x"] or b["bottom"] <= o["y"] or o["bottom"] <= b["y"], (other, t)
    m = cit["m_toggle"]
    assert m["btn"]["bottom"] <= m["wrap"]["y"] and m["inputW"] >= 200


def test_the_toggle_restores_with_the_conversation_and_reaches_the_server(cit):
    assert cit["toggle_restored"]["pressed"] == "true"            # its latest answer ran in the mode
    assert cit["toggle_restored"]["chip"].replace("\n", " ") == "📚 Documents only ✕"
    assert cit["new_conv_pressed"] == "false"
    a = cit["after_click"]
    assert a["pressed"] == "true" and a["rowDisplay"] == "flex" and a["bg"] != "rgba(0, 0, 0, 0)"
    assert cit["sent"]["documents_only"] is True and cit["sent"]["content"] == "what is the warranty?"
    assert cit["live_notfound"]["cls"] == "ground-badge g-notfound"
    assert cit["chip_off"] == ["false", "none"]
    assert cit["sent2"] is False


def test_a_live_streamed_answer_is_graded_when_it_is_final(cit):
    assert cit["live_grounded"] == {"badge": "● Grounded", "refs": ["[1]", "[2]"], "stored": True}


def test_the_settings_default_applies_to_new_conversations_and_is_saved(cit):
    assert cit["default_on"] == ["true", True]
    assert cit["settings_checkbox"] is True
    assert cit["settings_after_click"] is False and cit["saved"] is False
    assert cit["settings_box"]["display"] != "none" and cit["settings_box"]["h"] > 10
