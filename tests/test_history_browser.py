# SPDX-License-Identifier: AGPL-3.0-or-later
"""The organised conversation sidebar, measured in Chromium, Firefox and WebKit
(history_probe.py: canned API answers, a fake WebSocket, no server)."""
import json
import pathlib
import subprocess

import pytest

from test_science_lanes_browser import _no_browser, _playwright_python

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PROBE = pathlib.Path(__file__).with_name("history_probe.py")
_RUNS: dict = {}


@pytest.fixture(scope="module", params=["chromium", "firefox", "webkit"])
def hist(request):
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


def _acts(m):
    return {a["label"]: a for a in m["acts"]}


def test_the_probe_ran_every_step_without_page_errors(hist):
    assert not hist.get("step_errors"), hist.get("step_errors")
    assert hist["errors"] == []


def test_conversations_are_grouped_pinned_first_then_by_day(hist):
    rows = [r.split("|")[0] if not r.startswith("G:") else r for r in hist["rows"]]
    assert rows[0] == "G:Pinned" and rows[1] == "s_pin"
    assert rows[2] == "G:Today" and rows[3].startswith("sess_")      # the new conversation
    assert rows[4:] == ["s_today", "s_xss", "G:Yesterday", "s_yday", "G:Previous 7 days", "s_week",
                        "G:Previous 30 days", "s_month", "G:Older", "s_old"]
    g = hist["group_box"]
    assert g["display"] == "block" and g["h"] > 10


def test_rows_say_when_they_expire_only_while_a_limit_applies(hist):
    exp = {r.split("|")[0]: r.split("|")[2] for r in hist["rows"] if not r.startswith("G:")}
    assert exp["s_today"] == "expires in 20 h" and exp["s_yday"] == "expires in 22 h"
    assert exp["s_week"] == "expires in 40 min" and exp["s_xss"] == "expires in 3 days"
    assert exp["s_pin"] == exp["s_month"] == exp["s_old"] == ""    # pinned / kept: no line
    b = hist["exp_box"]
    assert b["display"] == "block" and b["h"] >= 10 and b["w"] > 60 and b["title"]


def test_rename_and_pin_appear_on_hover_and_fit_in_the_row(hist):
    idle, hover = _acts(hist["acts_idle"]), _acts(hist["acts_hover"])
    assert idle["Rename conversation"]["display"] == "none" and idle["Pin conversation"]["display"] == "none"
    for label in ("Rename conversation", "Pin conversation", "Delete conversation"):
        a = hover[label]
        assert a["display"] == "flex" and a["w"] >= 24 and a["h"] >= 24, (label, a)
        assert a["right"] <= hist["acts_hover"]["row"]["right"] <= hist["acts_hover"]["sidebarRight"]
    assert hover["Rename conversation"]["right"] <= hover["Pin conversation"]["x"]
    assert hist["acts_hover"]["titleW"] >= 80                        # the name still has room
    # a pinned row keeps its lit pin without hover; a new, empty conversation has neither
    assert _acts(hist["acts_pinned_idle"])["Unpin conversation"]["display"] == "flex"
    assert hist["new_conv_acts"] == 0


def test_rename_in_place_sends_the_typed_name_and_keeps_the_conversation(hist):
    f = hist["rename_focus"]
    assert f["cls"] == "session-rename" and f["value"] == "Week chat" and f["selected"] == 9 and f["w"] > 60
    assert hist["rename_typed"] == "Boiler  notes and more"                 # spaces reached the box
    assert hist["rename_patch"] == [{"id": "s_week", "body": {"title": "Boiler notes and more"}}]
    assert "s_week|Boiler notes and more|expires in 40 min" in hist["rename_rows"]
    assert hist["rename_session_after"] == hist["rename_session_before_enter"]   # Enter did not open it
    assert hist["rename_cancel"] == {"patches": 1, "title": "Boiler notes and more", "input_left": False}
    assert hist["rename_survives_render"] == "Kept"


def test_a_title_is_text_everywhere_never_markup(hist):
    assert hist["xss"] == {"value": "<img src=x onerror=window.__xss=1>", "fired": 0, "imgs": 0}
    assert "s_xss|<img src=x onerror=window.__xss=1>|expires in 3 days" in hist["rows"]


def test_pinning_moves_the_conversation_to_pinned_and_lifts_its_expiry(hist):
    assert hist["pin_patch"] == [{"id": "s_today", "body": {"pinned": True}}]
    assert hist["pin_rows"][:3] == ["G:Pinned", "s_today|Today chat|", "s_pin|Pinned chat|"]
    b = hist["pin_btn"]
    assert b["pressed"] == "true" and b["on"] is True and b["display"] == "flex"
    assert "s_today|Today chat|expires in 24 h" in hist["unpin_rows"]          # back on the clock
    assert hist["unpin_rows"][:3] == ["G:Pinned", "s_pin|Pinned chat|", "G:Today"]


def test_a_refused_pin_says_so_and_changes_nothing(hist):
    f = hist["pin_fail"]
    assert "error: Could not update the conversation: store down" in f["toasts"]
    assert f["rows"] == ["G:Pinned", "s_pin|Pinned chat|", "G:Today"]


def test_the_retention_setting_shows_the_choice_and_saves_it(hist):
    s = hist["retention_select"]
    assert s["value"] == "86400" and s["options"] == ["Forever", "30 days", "1 day"]
    assert s["display"] != "none" and s["h"] >= 18 and s["w"] > 100
    assert hist["retention_post"] == {"persist": True, "ttl": 0}
    assert hist["sessions_reloaded"] >= 2                 # the rows' expiry is read again after a save
    c = hist["retention_custom"]
    assert c == {"value": "604800", "label": "7 days (set in config.json)", "dirty": False}
    assert hist["retention_custom_post"] == {"persist": True, "ttl": 604800}


def test_on_a_phone_the_open_conversation_offers_rename_and_pin(hist):
    s = hist["m_sidebar"]
    assert s["display"] == "flex" and s["right"] <= 375
    assert hist["m_doc_overflow"] == 0 and hist["m_list_overflow"] == 0
    other = _acts(hist["m_acts_other"])
    if hist["m_hover_none"]:                                # touch: only the open conversation
        assert other["Rename conversation"]["display"] == "none"
    assert other["Delete conversation"]["display"] == "flex"
    assert hist["m_after_open"] == ["s_today", "none"]
    act = _acts(hist["m_acts_active"])
    for label in ("Rename conversation", "Pin conversation", "Delete conversation"):
        assert act[label]["display"] == "flex" and act[label]["right"] <= hist["m_acts_active"]["row"]["right"] <= 375
    assert hist["m_acts_active"]["titleW"] >= 80
    m = hist["m_rename"]
    assert m and m["focused"] is True and m["w"] >= 80 and m["right"] <= 375
    assert hist["m_sidebar_after"] == "flex"                 # renaming does not close the overlay
