# SPDX-License-Identifier: AGPL-3.0-or-later
"""The usability-review fixes, measured in Chromium, Firefox and WebKit (ux_fixes_probe.py:
canned API answers, a fake WebSocket, no server)."""
import json
import pathlib
import subprocess

import pytest

from test_science_lanes_browser import _no_browser, _playwright_python

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PROBE = pathlib.Path(__file__).with_name("ux_fixes_probe.py")
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


# ── B1 / B2 / B21: Reset Defaults keeps the installed storage, and says what it does ──

def test_reset_keeps_the_builtin_redis_and_save_never_posts_one(ux):
    a = ux["after_reset"]
    assert a["form"] == ["127.0.0.1", "6389"]          # not the localhost:6379 fallback
    assert a["collected"] is None                       # _collectSettings() has no redis section
    assert a["embed"] == "intfloat/multilingual-e5-base"   # resetting it would mean a re-index
    post = ux["reset_save_post"]
    assert "redis" not in post and "storage" not in post
    assert post["web_sources"] == [] and post["watch_folders"]["folders"] == []   # what it said it clears


def test_reset_dialog_names_what_is_cleared_and_what_is_kept(ux):
    body = ux["reset_modal"]["body"]
    assert "2 saved web sources" in body and "1 watched folder" in body
    assert "every stored API key (keys from environment variables are kept)" in body
    assert "access password" in body
    assert "storage connection" not in body.split("Kept:")[0]   # not listed as cleared
    assert "Redis connection settings" not in body
    assert "Kept: the built-in storage" in body
    assert ux["reset_modal"]["buttons"] == ["Cancel", "Reset to defaults"]


def test_closing_after_a_reset_asks_and_discard_puts_everything_back(ux):
    assert ux["after_reset"]["dirty"] is True
    assert ux["close_after_reset_modal"]["title"] == "Discard unsaved settings?"
    d = ux["after_discard"]
    assert (d["redis"]["host"], d["redis"]["port"]) == ("127.0.0.1", 6389)
    assert d["web"] == ["https://a.example/docs", "https://b.example/wiki"]
    assert d["folders"] == 1 and d["key"] == "__VISUALWEAVER_SECRET_KEPT__"
    assert ux["reopen_form"] == ["127.0.0.1", "6389"]


def test_the_builtin_redis_is_shown_read_only(ux):
    f = ux["builtin_redis_form"]
    assert f["readonly"] == [True, True, True, True] and f["ssl"] is True
    assert "installer" in f["note"] and f["values"] == ["127.0.0.1", "6389"]


def test_no_redis_section_is_posted_or_invented(ux):
    # blank rather than a made-up localhost:6379, and never posted either way
    assert ux["no_redis_section"] == {"form": "", "posts": False, "postsWithIt": False}


def test_the_discard_dialog_no_longer_claims_the_provider_waits_for_save(ux):
    body = ux["close_after_reset_modal"]["body"]
    assert "only remembered once you save" not in body
    assert "stays selected in this browser" in body


# ── B3: list edits are unsaved changes; Discard restores them ─────────────────────

def test_list_edits_raise_the_unsaved_badge_and_discard_restores_them(ux):
    assert ux["dirty_after_add_source"] is True
    assert ux["during"] == {"web": 3, "tpl": ["Default"], "folders": 0}
    assert ux["discard_modal"]["title"] == "Discard unsaved settings?"
    d = ux["after_list_discard"]
    assert d["web"] == ["https://a.example/docs", "https://b.example/wiki"]
    assert d["tpl"] == ["Default", "Redis Expert"] and d["folders"] == 1
    assert d["composer"] == ["💬 Default", "Redis Expert"] and d["open"] is False


# ── B4: a refused streamed request says why and stops ───────────────────────────

def test_a_refused_upload_shows_the_server_reason_and_stops(ux):
    s = ux["ingest_refused"]
    assert s["status"].startswith("✗") and "the limit is 100 MB" in s["status"] and "413" in s["status"]
    assert s["bar"] == "0%"
    assert any(t.startswith("error: Ingestion into “default” failed") and "100 MB" in t
               for t in ux["ingest_refused_toasts"])


def test_a_refused_crawl_shows_the_reason_and_frees_the_start_button(ux):
    c = ux["crawl_refused"]
    assert c["url"] == "ftp://nope"
    assert any("Crawl failed: URL must start with http" in t for t in c["toasts"])
    assert c["start_enabled"] is True


# ── B5: a message sent while the socket is down ─────────────────────────────────

def test_a_message_sent_during_a_reconnect_waits_and_is_delivered(ux):
    q = ux["ws_queued"]
    assert q["lost"] == 0                                 # never thrown at the closed socket
    assert q["phase"] == "Reconnecting to the server…"
    assert ux["ws_delivered"]["sent"] == ["Thanks!"] and ux["ws_delivered"]["sockets"] >= 2
    assert ux["ws_after_answer"] == {"streaming": False, "typing": False}


def test_a_send_that_cannot_reconnect_fails_visibly_and_gives_the_text_back(ux):
    f = ux["ws_failed"]
    assert f["streaming"] is False and f["typing"] is False
    assert f["input"] == "Second question" and f["stored"] == 0
    assert f["sendDisabled"] is False
    assert any("your message was not sent" in t for t in ux["ws_failed_toasts"])


def test_a_drop_mid_answer_unlocks_the_composer_and_says_so(ux):
    assert ux["ws_mid_stream"] == {"streaming": False, "cursor": False}
    assert any("the answer was interrupted" in t for t in ux["ws_mid_toasts"])
    # the reconnect that followed a failed send cleared that failure's message
    assert not any("your message was not sent" in t for t in ux["ws_mid_toasts"])


# ── B6 / C2: the composer and the top bar on narrow screens ─────────────────────

@pytest.mark.parametrize("width,floor", [("320", 140), ("375", 190), ("390", 200), ("768", 300)])
def test_the_message_box_is_usable_from_320_to_768px(ux, width, floor):
    L = ux["layout"][width]
    assert L["msg"]["w"] >= floor, L["msg"]
    assert L["docOverflow"] == 0 and L["topbarOverflow"] == 0
    assert L["gear"]["right"] <= int(width), L["gear"]           # ⚙️ fully on screen
    assert L["send"]["right"] <= int(width)
    assert L["tpl"]["right"] <= int(width)


@pytest.mark.parametrize("width", ["320", "375", "390"])
def test_the_session_title_has_room_on_a_phone(ux, width):
    assert ux["layout"][width]["title"]["w"] >= 120, ux["layout"][width]["title"]


def test_the_wide_layout_is_unchanged(ux):
    L = ux["layout"]["1024"]
    assert L["tpl"]["w"] == 140 and L["msg"]["w"] >= 400
    # one row, side by side: the picker sits beside the message box, not above it
    assert L["tpl"]["right"] <= L["wrap"]["x"] and L["tpl"]["bottom"] > L["wrap"]["y"]


def test_the_phone_sidebar_closes_once_a_conversation_is_picked(ux):
    L = ux["layout"]["390"]
    assert L["sidebar_open"] == "flex" and L["sidebar_after_pick"] == "none"
    assert L["session_after_pick"] == "s_other"


# ── B7: a new instance becomes the target; empty is not "no match" ─────────────

def test_a_new_instance_becomes_the_target_and_the_form_clears(ux):
    a = ux["after_create"]
    assert a["ingest"] == a["crawl"] == a["watch"] == a["topbar"] == "handbook§default"
    assert a["chat"] == {"name": "handbook", "ep": "default"}
    assert a["name"] == "" and ux["second_create_name"] == ""
    assert a["dirty"] is False          # applied at once: closing Settings does not ask about it
    assert any("Created \"handbook\"" in t and "now use it" in t for t in ux["create_toasts"])


def test_ingest_progress_and_toast_name_the_target(ux):
    i = ux["ingest_into_new"]
    assert i["status"] == "✓ Indexed 1 file(s) into “handbook”"
    assert "success: Ingested 1 file into “handbook”" in i["toasts"]
    assert i["url"].startswith("/api/rag/handbook/")


def test_an_empty_instance_is_called_empty_not_no_match(ux):
    assert ux["empty_badge"] == "📭 “handbook” is empty"
    assert ux["nomatch_badge"] == "📭 No KB match"            # once it holds documents


# ── B8 / B9: settings tabs ──────────────────────────────────────────────────────

def test_every_settings_tab_is_visible_on_a_desktop_panel(ux):
    t = ux["tabs_desktop"]
    assert t["overflow"] <= 0 and t["allInside"] is True and t["rows"] >= 2


def test_the_tabs_are_an_aria_tablist(ux):
    t = ux["tabs_desktop"]
    assert t["role"] == "tablist" and t["n"] == 12
    assert t["selected"] == ["about"] and t["tabStops"] == ["about"]
    assert t["panel"] == ["tabpanel", "stab-about"]
    assert ux["tabs_arrow"] == ["logs", "logs", True] and ux["tabs_home"] == "status"


@pytest.mark.parametrize("tab", ["about", "analytics", "status"])
def test_a_deep_linked_tab_is_scrolled_into_view_on_a_phone(ux, tab):
    p = ux["tabs_phone"][tab]
    assert p["scrollable"] and p["inView"], p
    assert any(p["fades"]) and "linear-gradient" in (p["mask"] or "")


# ── B10 / B11: keyboard and labels ──────────────────────────────────────────────

def test_drop_zone_import_and_instance_cards_work_from_the_keyboard(ux):
    assert ux["kb_drop_role"] == ["button", 0]
    assert ux["kb_clicks"] == ["file-input", "rag-import-1"]
    assert ux["kb_card"] == {"active": "kb", "focus": "rag-pick-1", "pressed": "true", "cardIsPlain": True}


def test_welcome_chips_are_buttons(ux):
    assert ux["chips"] and all(c == ["BUTTON", 0] for c in ux["chips"])


def test_template_fields_have_labels(ux):
    assert ux["template_labels"] == ["NAME", "SYSTEM PROMPT", "NAME", "SYSTEM PROMPT"]


# ── B12 / B17: models ───────────────────────────────────────────────────────────

def test_while_models_load_the_welcome_card_says_it_is_looking(ux):
    w = ux["loading"]
    assert w["title"] == "Looking for models…" and "no local models" not in w["why"]
    assert w["setupVisible"] and not w["actionsVisible"] and not w["chipsVisible"]
    assert ux["loaded"]["chipsVisible"] and not ux["loaded"]["setupVisible"]


def test_embedding_models_are_not_offered_or_picked_for_chat(ux):
    assert ux["models"] == {"current": "llama3:latest", "list": ["llama3:latest"]}
    assert ux["models_table"][0] == ["nomic-embed-text:latest", "Embedding 🗑"]
    assert ux["models_table"][2] == ["mxbai-embed-large:latest", "Embedding 🗑"]
    e = ux["embed_only"]
    assert e["model"] == "" and "only has embedding models" in e["why"]


# ── B13: the connection holding the app data ────────────────────────────────────

def test_the_app_data_connection_offers_no_remove_and_says_why(ux):
    r = ux["ep_remove"]
    assert r["removable"] == ["spare"]
    assert r["locked"][0][:2] == ["team", "true"] and "app data" in r["locked"][0][2]
    assert ux["ep_locked_click"]["modal"] is None
    assert any(t.startswith("info: Holds the app data") for t in ux["ep_locked_click"]["toasts"])


def test_an_unreachable_connection_does_not_claim_to_hold_nothing(ux):
    body = ux["remove_unreachable"]["body"]
    assert "No RAG instances are stored" not in body and "cannot be reached" in body
    assert "No RAG instances are stored on this connection." in ux["ep_spare_modal"]["body"]


# ── B14: contrast, both themes ──────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["contrast_js_warn_light", "contrast_js_warn_dark", "contrast_pill_light",
                                 "contrast_pill_dark", "contrast_pinned_light", "contrast_banner_dark",
                                 "contrast_banner_light"])
def test_flagged_text_reaches_wcag_aa(ux, key):
    assert ux[key]["ratio"] >= 4.5, ux[key]


@pytest.mark.parametrize("key", ["contrast_status_light", "contrast_status_dark"])
def test_status_tiles_reach_wcag_aa(ux, key):
    assert len(ux[key]) == 3 and all(t["ratio"] >= 4.5 for t in ux[key]), ux[key]


def test_the_playwright_notice_follows_what_the_server_reports(ux):
    assert ux["js_warn_available"] == "none" and ux["js_warn_missing"] == "block"


# ── B15 / B16 / B18 / B19 / F1 ──────────────────────────────────────────────────

def test_the_composer_lists_default_once(ux):
    assert ux["template_options"] == ["💬 Default", "Redis Expert"]


def test_the_drop_zone_names_every_accepted_type(ux):
    assert ux["drop_hint"] == "Drop PDF, TXT, Markdown, CSV, DOCX or XLSX files, or click to browse"


def test_per_page_counters_are_labelled_as_this_session(ux):
    lab = ux["analytics_labels"]
    assert lab[:4] == ["Queries this session", "Cache hits this session", "Hit rate this session",
                       "Avg latency this session"]


@pytest.mark.parametrize("key", ["session_del", "doc_attach", "img_attach", "mic"])
def test_small_controls_are_at_least_24px(ux, key):
    t = ux["targets"][key]
    assert t["w"] >= 24 and t["h"] >= 24, t


def test_read_only_tabs_hide_save_unless_something_is_unsaved(ux):
    assert ux["footer"] == {"analytics": "none", "status": "none", "about": "none", "general": "flex",
                            "status_dirty": "flex"}


# ── B20 / B22 / F3 ──────────────────────────────────────────────────────────────

def test_cancel_comes_first_in_every_confirmation(ux):
    assert all(b[0] == "Cancel" for b in ux["dialog_orders"].values()), ux["dialog_orders"]


def test_a_form_error_goes_once_the_form_succeeds(ux):
    assert "error: Enter a name for the connection" in ux["ep_error_first"]
    assert not any(t.startswith("error:") for t in ux["ep_error_after_success"])
    assert 'success: Connection "third" added' in ux["ep_error_after_success"]


def test_default_is_created_once_and_never_when_it_exists(ux):
    assert ux["default_posts_existing"] == 0
    assert ux["fresh_default_posts"] == ["default"]
    assert ux["concurrent_default_posts"] == ["default"]    # coalesced, not one per caller


def test_the_chat_view_says_when_the_app_data_store_is_down(ux):
    d = ux["storage_down"]
    assert d["display"] == "flex" and d["h"] > 20 and "cannot be reached" in d["text"]
    assert ux["storage_down_link"] == "status" and ux["storage_up"] == "none"


# ── app data off a store that is gone ───────────────────────────────────────────

def test_a_failed_copy_offers_to_switch_without_copying(ux):
    m = ux["switch_copy_failed"]
    assert m["buttons"] == ["Cancel", "Switch without copying"]
    assert 'send "copy": false' not in m["body"] and "Nothing is copied" in m["body"]
    assert "already stored there, if any" in m["body"] and "starting empty" not in m["body"]
    assert ux["switch_posts"] == [{"connection": "team"}, {"connection": "team", "copy": False}]
    assert any(t.startswith("success: App data is now kept in team") for t in ux["switch_toasts"])


# ── stored values are text, never markup ─────────────────────────────────────────

def test_no_stored_value_can_inject_markup(ux):
    x = ux["xss"]
    assert x["fired"] == [] and x["injected"] == 0
    assert x["nameShown"] and x["titleShown"]          # shown as text
    assert x["dot"] == "rgb(99, 102, 241)"             # a bad colour falls back to the default


# ── second review: conversations, attachments, pins, ratings, voice ─────────────

def test_the_sidebar_uses_titles_and_the_server_order(ux):
    rows = ux["session_rows"]
    assert [r.split(":", 1)[1] for r in rows] == ["New Conversation", "Travel policy", "The limit is 150 EUR."]
    old = ux["session_rows_old_server"]
    assert [r.split(":", 1)[0] for r in old][1:] == ["s_new", "s_old"]


def test_a_reopened_conversation_shows_no_welcome_hero(ux):
    r = ux["reopen"]
    assert r["hero"] is False and r["messages"] == 2 and r["firstTop"] < 60


def test_reopening_scrolls_once_not_once_per_message(ux):
    assert ux["reopen_scrolls"] == 1


def test_clicking_the_open_conversation_does_not_reconnect(ux):
    # the old onclose scheduled a reconnect that replaced the live socket every 2 s
    assert ux["active_click_sockets"] == [0, 1]
    assert ux["replaced_socket_reconnects"] == 1         # the replacement only, no reconnect after it
    assert ux["replaced_socket_frame"] == {"added": 0, "streaming": False}   # detached, not just closed


def test_removing_attachments_removes_exactly_those(ux):
    assert ux["images_left"] == {"queued": ["B"], "shown": 1}
    assert ux["files_left"] == {"queued": ["slow.txt=T:slow.txt", "three.txt=T:three.txt"],
                                "shown": ["slow.txt", "three.txt"]}
    assert ux["files_after_remove_last"] == ["slow.txt"]


def test_dropped_images_need_a_vision_model_too(ux):
    d = ux["drop_no_vision"]
    assert d["queued"] == 0 and "error: Switch to a vision model to use images" in d["toasts"]


def test_a_pin_from_another_conversation_opens_it(ux):
    assert ux["pin"] == {"session": "s_old", "present": True}


def test_a_rating_credits_the_model_that_wrote_the_answer(ux):
    f = ux["feedback"]
    assert (f["provider"], f["model"]) == ("ollama", "llama3:latest")
    assert f["rag_instances"] is None and f["query"] == "What is the hotel limit?"


def test_voice_input_sets_the_language_spaces_phrases_and_says_where_audio_goes(ux):
    v = ux["voice"]
    assert v["lang"] and v["lang"] != "undefined"
    assert v["value"] == "hello world again"
    assert "vendor" in v["title"]
    # on-device recognition is not forced on, nor probed with available()
    assert v["local"] is False and v["asked"] is None
