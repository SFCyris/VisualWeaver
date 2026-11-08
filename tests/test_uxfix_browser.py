# SPDX-License-Identifier: AGPL-3.0-or-later
"""The 1.16.0 review fixes in Chromium, Firefox and WebKit (uxfix_probe.py: canned API
answers, a fake WebSocket, no server; real pointer and keyboard input).

Before these fixes: the "· Documents only" suffix of the grounding badge was drawn at
opacity .85 (3.7:1 in light mode, 4.3:1 in dark); stepping to an earlier version of a
regenerated answer showed it with the LATEST version's sources, citations and badge; a
conversation started in another browser could not be reached or deleted from this one;
someone who is not an administrator was offered buttons into settings they cannot open;
a signed-out page reconnected every 2 s for ever and called the storage unreachable; a
send while reconnecting was announced twice; and each conversation row was a
role="button" holding three other buttons, under date labels that were not headings.
"""
import json
import pathlib
import subprocess

import pytest

from test_science_lanes_browser import _no_browser, _playwright_python

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PROBE = pathlib.Path(__file__).with_name("uxfix_probe.py")
_RUNS: dict = {}


@pytest.fixture(scope="module", params=["chromium", "firefox", "webkit"])
def ux(request):
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


def test_the_probe_ran_every_step_without_page_errors(ux):
    assert not ux.get("step_errors"), ux.get("step_errors")
    assert ux["errors"] == []


# ── 8. badge contrast, every state, both themes ──────────────────────────────

_LABELS = {"grounded_docs": "● Grounded · Documents only", "partly": "◐ Partly grounded",
           "uncited_docs": "○ Uncited · Documents only", "norag": "– No documents used",
           "noretrieval": "– Nothing retrieved", "searchfailed": "⚠ Search failed · Documents only",
           "notfound": "∅ Not in your documents · Documents only"}


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_every_badge_state_and_its_mode_suffix_reach_4_5_to_1(ux, scheme):
    """Measured with the element's opacity applied to its colour."""
    got = ux[f"badges_{scheme}"]
    assert ux[f"badges_{scheme}_theme"] == (None if scheme == "light" else "dark")
    for state, label in _LABELS.items():
        g = got[state]
        assert g and g["text"] == label and g["display"] in ("flex", "inline-flex") and g["w"] > 60 and g["h"] >= 16, (state, g)
        assert g["badge"]["ratio"] >= 4.5, (state, g["badge"])
        if "Documents only" in label:
            assert g["mode"]["ratio"] >= 4.5, (state, g["mode"])


def test_at_phone_width_every_badge_stays_inside_the_window(ux):
    b = ux["badges_375"]
    assert b["overflow"] == 0 and len(b["badges"]) == len(_LABELS)
    assert all(x["left"] >= 0 and x["right"] <= b["vw"] and x["w"] > 60 for x in b["badges"]), b


def test_a_failed_search_and_nothing_retrieved_have_their_own_neutral_badges(ux):
    light = ux["badges_light"]
    neutral = light["norag"]["badge"]["color"]
    assert light["noretrieval"]["badge"]["color"] == neutral != light["uncited_docs"]["badge"]["color"]
    assert light["searchfailed"]["badge"]["color"] == neutral
    assert light["searchfailed"]["nomatch"] is False        # not "No KB match": the search never ran
    assert "could not be searched" in light["searchfailed"]["aria"]


# ── 5. versions keep their own sources ───────────────────────────────────────

def test_each_answer_version_shows_its_own_sources_citations_and_badge(ux):
    assert ux["ver_regen_sent"] == 1
    latest, first, again = ux["ver_latest"], ux["ver_first"], ux["ver_second_again"]
    assert latest == {**latest, "sources": ["B", "C"], "refs": ["[1]", "[2]"], "broken": 0,
                      "badge": "● Grounded · Documents only", "ver": "2/2"}
    # Old: version 1 was shown with B and C, under "Documents only".
    assert first["sources"] == ["A"] and first["refs"] == ["[1]"] and first["broken"] == 0
    assert first["badge"] == "● Grounded" and first["ver"] == "1/2"
    assert first["inspDisplay"] == "block" and first["inspH"] > 20
    assert ux["ver_first_card"].startswith("https://docs.example.org/a.html")
    assert again["sources"] == ["B", "C"] and again["badge"] == latest["badge"]


# ── 1. conversations this browser does not list ──────────────────────────────

@pytest.mark.parametrize("w", [1400, 375])
def test_settings_counts_the_conversations_this_browser_does_not_list(ux, w):
    assert ux[f"unl_{w}_rows"][-1] == "s_mine" and "s_other1" not in ux[f"unl_{w}_rows"]
    u = ux[f"unl_{w}"]
    assert u["display"] == "block" and u["h"] > 40 and u["text"].startswith("2 stored conversations are not listed")
    assert [b["text"] for b in u["buttons"]] == ["Show them in the sidebar", "Delete them…"]
    assert all(b["h"] >= 24 and b["right"] <= u["panelRight"] for b in u["buttons"]), u
    assert ux[f"unl_{w}_overflow"] == 0


def test_show_adds_them_to_this_browsers_sidebar(ux):
    s = ux["unl_after_show"]
    assert set(s["rows"]) >= {"s_mine", "s_other1", "s_other2"} and "s_empty" not in s["rows"]
    assert s["stored"][-2:] == ["s_other2", "s_other1"]       # newest last: last to be trimmed
    assert s["panel"]["text"].startswith("Every stored conversation is listed")
    assert s["panel"]["actsDisplay"] == "none"


def test_delete_asks_first_then_removes_each_one_on_the_server(ux):
    m = ux["unl_modal"]
    assert m["title"] == "Delete 2 conversations?" and "cannot be undone" in m["body"] and "pinned" in m["body"]
    assert ux["unl_deletes_before_confirm"] == []
    d = ux["unl_deleted"]
    assert d["deletes"] == ["s_other1", "s_other2"]
    assert d["panel"]["text"].startswith("Every stored conversation is listed") and "s_other1" not in d["rows"]


def test_with_sign_in_on_there_is_no_unlisted_control(ux):
    assert ux["unl_signin"]["display"] == "none" and ux["unl_signin"]["h"] == 0


# ── 3. someone who is not an administrator ───────────────────────────────────

@pytest.mark.parametrize("w", [1400, 375])
def test_the_checklist_tells_a_user_to_ask_instead_of_offering_admin_buttons(ux, w):
    c = ux[f"na_checklist_{w}"]
    assert c["hidden"] is False and c["h"] > 60
    storage, model, knowledge = c["rows"]
    assert model["cls"].endswith("fr-bad") and model["buttons"] == 0
    assert model["ask"]["text"] == "Ask your administrator to set up a model." and model["ask"]["display"] != "none"
    assert model["ask"]["w"] > 100 and model["ask"]["right"] <= c["right"]
    # no documents yet is not a to-do of theirs
    assert knowledge["cls"].endswith("fr-info") and knowledge["buttons"] == 0
    assert knowledge["ask"]["text"] == "Ask your administrator to add documents."
    assert ux[f"na_overflow_{w}"] == 0


def test_the_ask_your_administrator_text_is_readable(ux):
    for k in ("na_ask_contrast_1400", "na_ask_contrast_375", "na_ask_contrast_dark"):
        assert ux[k]["ratio"] >= 4.5, (k, ux[k])


def test_a_user_gets_no_checklist_when_only_the_documents_are_missing(ux):
    assert ux["na_only_docs"]["hidden"] is True and ux["na_only_docs"]["h"] == 0


def test_the_strip_model_item_is_not_a_button_for_a_user(ux):
    strip = ux["na_strip_1400"]
    assert strip[0]["tag"] == "BUTTON"                       # Status is open to everyone
    assert strip[1]["tag"] == "SPAN" and "Ask your administrator to set up a model" in strip[1]["label"]
    assert ux["na_strip_click"] is False
    assert ux["na_add_docs"] == {"open": False} and any("administrators" in t for t in ux["na_add_docs_toasts"])


def test_save_to_knowledge_base_is_not_offered_to_a_user(ux):
    acts = {a["text"]: a for a in ux["na_actions"]}
    assert acts["💾"]["display"] == "none" and acts["💾"]["w"] == 0
    assert acts["🔖"]["display"] != "none" and acts["🔖"]["label"] == "Bookmark this message"
    assert {a["text"]: a["display"] for a in ux["admin_actions"]}["💾"] != "none"


def test_message_bookmarks_are_named_apart_from_pinned_conversations(ux):
    assert "Bookmarks" in ux["bookmarks_label"] and "Pinned" not in ux["bookmarks_label"]
    assert any(t.startswith("🔖Bookmarked messages") for t in ux["bookmarks_label"])


# ── 6. signed out ────────────────────────────────────────────────────────────

def test_a_refused_reconnect_with_the_sign_in_gone_says_signed_out_and_stops(ux):
    s = ux["so_strip"]
    assert s["state"] == "signedout" and s["signedOut"] is True
    assert s["items"][2]["text"].endswith("signed out — reload to sign in") and "hs-bad" in s["items"][2]["cls"]
    assert [b["text"] for b in s["buttons"]] == ["Reload"] and s["buttons"][0]["w"] > 20
    assert s["items"][0]["cls"].endswith("hs-wait")          # the storage is not claimed to be down
    # the typed message is back in the box, and said so
    assert ux["so_input"] == "Is anyone there?" and ux["so_streaming"] is False
    assert any("signed out" in t and "back in the message box" in t for t in ux["so_toasts"])
    assert ux["so_said"][-1] == "Signed out — reload the page to sign in again"
    # Old: a new socket every 2 s, for ever.
    assert ux["so_new_sockets"] == 0 and ux["so_new_sockets_retry"] == 0


def test_a_status_poll_refused_401_shows_signed_out_not_storage_down(ux):
    s = ux["so_poll_strip"]
    assert s["state"] == "signedout" and s["items"][0]["cls"].endswith("hs-wait")
    m = ux["so_poll_375"]                                       # measured at 375 px
    assert m["docOverflow"] == 0 and m["stripOverflow"] <= 0
    assert m["reload"]["w"] > 20 and m["reload"]["h"] >= 14 and m["reload"]["right"] <= m["vw"]


def test_with_sign_in_off_a_server_that_is_away_is_still_just_reconnecting(ux):
    assert ux["away_strip"]["state"] == "reconnecting" and ux["away_strip"]["signedOut"] is False


# ── 12. one announcement per queued send ─────────────────────────────────────

def test_a_send_on_a_dropped_socket_is_announced_once(ux):
    """Old: "Reconnecting to the server" from the send, then "Connection to the server lost
    — reconnecting" from the strip when the close event arrived."""
    assert ux["said_unclosed"] == ["Connection to the server lost — reconnecting. "
                                   "Your message will be sent when the connection is back"]
    assert ux["said_after_drop"] == ["Your message will be sent when the connection is back"]


# ── 11. the conversation list's structure ────────────────────────────────────

@pytest.mark.parametrize("w", [1400, 375])
def test_conversation_rows_are_buttons_side_by_side_under_headings(ux, w):
    s = ux[f"sl_{w}"]
    assert [g["text"] for g in s["groups"]] == ["Pinned", "Today", "Previous 7 days"]
    assert all(g["role"] == "heading" and g["level"] == "3" for g in s["groups"])
    assert s["roleButtons"] == 0 and s["nested"] == 0
    for row in s["rows"]:
        assert row["open"] == "BUTTON" and row["name"].startswith("Open conversation: ")
        assert row["titleW"] > 100 and row["titleH"] >= 14 and row["openRight"] <= row["rowRight"], row
    assert sum(1 for r in s["rows"] if r["current"] == "true") == 1
    aria = ux[f"sl_aria_{w}"]
    assert 'heading "Pinned" [level=3]' in aria and 'button "Open conversation: Warranty terms"' in aria


def test_the_keyboard_reaches_a_rows_actions_and_enter_opens_it(ux):
    assert ux["sl_tab"][0] == "Open conversation: Warranty terms"
    labels = [t.split("|")[0] for t in ux["sl_tab"][1:]]
    assert labels == ["Rename conversation: Warranty terms", "Pin conversation: Warranty terms",
                      "Delete conversation: Warranty terms"]
    assert all(t.split("|")[1] != "none" and int(t.split("|")[2]) >= 24 for t in ux["sl_tab"][1:])
    assert ux["sl_back_on_open"] == "session-open" and ux["sl_enter_opened"] == "s_week"
    # opening it made it this browser's most recent: the last id to be dropped
    assert ux["sl_stored_after_open"][-1] == "s_week" and ux["sl_stored_after_open"].count("s_week") == 1


def test_rename_types_into_a_box_outside_the_rows_button(ux):
    r = ux["sl_rename"]
    assert r["cls"] == "session-rename" and r["value"] == " X" and r["inButton"] is False
    assert r["openHidden"] is True and r["w"] > 80
