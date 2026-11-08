# SPDX-License-Identifier: AGPL-3.0-or-later
"""Projects, each conversation's own knowledge-base selection and `#` mentions, measured in
Chromium, Firefox and WebKit (projects_probe.py: canned API answers, a fake WebSocket, no
server; real pointer, touch and keyboard input)."""
import json
import pathlib
import subprocess

import pytest

from test_science_lanes_browser import _no_browser, _playwright_python

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PROBE = pathlib.Path(__file__).with_name("projects_probe.py")
_RUNS: dict = {}
P1, P2, P3 = "p_aaaaaaaaaaaa", "p_bbbbbbbbbbbb", "p_cccccccccccc"
EVIL_KB = "My <b>Docs</b>"


@pytest.fixture(scope="module", params=["chromium", "firefox", "webkit"])
def proj(request):
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


def test_the_probe_ran_every_step_without_page_errors(proj):
    assert not proj.get("step_errors"), proj.get("step_errors")
    assert proj["errors"] == []


def _shown(b, min_w=24, min_h=24):
    return b and b["display"] != "none" and b["w"] >= min_w and b["h"] >= min_h


def test_the_project_switcher_sits_under_new_chat_inside_the_sidebar(proj):
    b = proj["bar"]
    assert _shown(b["filter"], 150, 28) and _shown(b["new"], 24, 24)
    assert b["edit"]["display"] == "none"                      # no project picked yet
    assert b["newchat"]["bottom"] <= b["bar"]["y"] < b["bar"]["bottom"] <= b["new"]["y"] < b["list"]["y"]
    for k in ("filter", "new"):
        assert b["sidebar"]["x"] <= b[k]["x"] and b[k]["right"] <= b["sidebar"]["right"]
    lab = proj["label"]                                          # ＋ Project in the Sessions label
    assert lab["btn"]["right"] <= lab["label_right"] and lab["hit"] is True
    assert proj["filter_options"][:2] == [["", "All conversations"], ["__none__", "No project"]]
    # a project name with markup is shown as text and runs nothing
    assert [P2, "<img src=x onerror=window.__xss=1>"] in proj["filter_options"]
    assert proj["xss"] == {"fired": 0, "imgs": 0}


def test_without_projects_the_sidebar_keeps_its_room(proj):
    """No switcher until a project exists: the conversation list starts where it did."""
    assert proj["empty_bar"]["display"] == "none" and proj["empty_bar"]["h"] == 0
    lab = proj["empty_label"]
    assert lab["btn"]["display"] != "none" and lab["btn"]["w"] >= 24 and lab["btn"]["h"] >= 24 and lab["hit"] is True
    assert lab["label_h"] <= 26                                  # the label row did not grow
    assert proj["empty_list_y"] == proj["bar"]["list"]["y"] - proj["bar"]["bar"]["h"] - 2


def test_rows_carry_their_project_and_the_switcher_filters_them(proj):
    assert proj["rows_all"][2:] == ["s_a", "s_b", "s_c"]
    marks = {m[0]: m for m in proj["row_marks"]}
    assert marks["s_a"][1] == "📘" and marks["s_a"][2] == "rgb(29, 78, 216)" and marks["s_a"][3] >= 12
    assert marks["s_a"][4].endswith("(project Manuals)")
    assert marks["s_b"][1] == "💬" and marks["s_b"][2] is None
    assert proj["rows_p1"] == ["G:Today", "s_a"]          # date groups kept; the empty new chat moved too
    assert proj["rows_none"] == ["G:Today", "s_b"]
    assert _shown(proj["edit_shown"], 28, 28)
    assert proj["filter_border"].startswith(("rgb(29, 78, 216", "rgba(29, 78, 216"))


def test_a_new_chat_in_a_project_starts_with_its_settings_and_sends_them(proj):
    st = proj["new_in_project"]
    assert st["project"] == P1 and st["sel"] == ["kb1", "kb2"]
    assert st["ragSelect"] == "__set__" and st["setOpt"] == "⧉ kb1, kb2"
    assert st["tpl"] == "Redis Expert" and st["docsOnly"] is True
    assert st["prefs"]["ragScope"] == "default§default"          # the browser's preference untouched
    chip = proj["project_chip"]
    assert chip["display"] == "flex" and chip["w"] > 40 and chip["h"] >= 18 and "Manuals" in chip["text"]
    assert chip["border"] == "rgb(29, 78, 216)"
    sent = proj["sent_in_project"][0]
    assert (sent["project"], sent["kb_select"], sent["rag_instances"], sent["template"], sent["documents_only"],
            sent["system_prompt"]) == (P1, ["kb1", "kb2"], ["kb1", "kb2"], "Redis Expert", True,
                                       "You are a Redis expert.")


def test_reopening_a_conversation_restores_its_own_selection(proj):
    """With or without a project: the stored selection, not this browser's preference."""
    loose = proj["open_loose"]
    assert loose["project"] is None and loose["sel"] == ["kb2"] and loose["ragSelect"] == "kb2§default"
    assert loose["tpl"] == ""                        # the project's template did not follow
    p = proj["open_project"]
    assert p["project"] == P1 and p["sel"] == ["kb1", "kb2"] and p["ragSelect"] == "__set__"
    assert p["tpl"] == "Redis Expert"


def test_a_change_is_stored_with_the_conversation(proj):
    c = proj["change_in_project"]
    assert c["prefs_same"] is True                    # inside a project: not the browser's preference
    assert c["patches"] == [{"m": "PATCH", "u": "/api/sessions/s_a", "body": {"kb_select": ["kb1"]}}]
    assert c["state"]["setOpt"] is None and c["state"]["ragSelect"] == "kb1§default"
    # without a project the preference still follows (as before), and the conversation stores it
    assert proj["change_loose"] == {"prefs": "__all__", "patch": [{"kb_select": "all"}]}
    n = proj["new_loose"]
    assert n["project"] is None and n["sel"] == "all" and n["tpl"] == ""


def test_projects_are_created_edited_and_deleted_from_the_sidebar(proj):
    e = proj["editor"]
    assert e["modal"]["display"] == "block" and e["focus"] == "pj-name"
    assert e["kbs"] == ["default", "kb1", "kb2", EVIL_KB] and e["imgs"] == 0
    assert _shown(e["kb_box"], 100, 18)
    c = proj["created"]
    assert c["post"] == [{"name": "Support", "icon": "", "color": "#7c6ff7", "kbs": ["kb2"], "template": "",
                          "provider": "", "model": "", "documents_only": False}]
    assert c["filter"] == P3 and _shown(c["edit"], 28, 28) and c["modal_open"] is False
    assert c["current"]["project"] == P3 and c["current"]["sel"] == ["kb2"]      # the empty chat moved in
    assert proj["edit_values"] == {"name": "Manuals", "kbs": ["kb1", "kb2"], "tpl": "Redis Expert", "docs": "on"}
    d = proj["delete_confirm"]
    assert d["title"] == "Delete project?" and "are kept" in d["body"] and d["focus"] == "Cancel"
    gone = proj["deleted"]
    assert gone["calls"] == [f"/api/projects/{P1}"] and gone["filter"] == ""
    assert "s_a" in gone["rows"] and gone["s_a_project"] is None and P1 not in gone["options"]
    assert any("1 conversation kept without a project" in t for t in gone["toasts"])


def test_advice_can_be_read_for_one_project(proj):
    assert proj["adv_row"]["display"] == "flex" and proj["adv_row"]["h"] >= 18
    assert proj["adv_calls"][-1] == f"/api/rag/advice?project={P2}"


def test_someone_who_is_not_an_admin_gets_the_switcher_only(proj):
    assert proj["user_bar"]["new"]["display"] == "none"
    assert _shown(proj["user_bar"]["filter"], 150, 28)


def test_typing_a_hash_offers_the_knowledge_bases_above_the_box(proj):
    m = proj["menu_all"]
    assert not m["hidden"] and m["display"] == "block"
    assert m["opts"] == ["default", "kb1", "kb2", EVIL_KB] and m["markup"] == 0
    assert m["bottom"] <= m["taTop"] and m["y"] >= 0 and m["right"] <= m["vw"]
    assert m["optH"] >= 24 and m["selected"] == 0 and m["active"] == "mention-opt-0"
    assert m["selBg"] in ("rgb(83, 72, 224)", "rgb(101, 90, 216)")
    assert proj["menu_hit"] is True                    # nothing covers the options
    assert proj["menu_k"]["opts"] == ["kb1", "kb2"]
    d = proj["menu_down"]
    assert d["selected"] == 1 and d["active"] == "mention-opt-1" and d["focus"] is True


def test_picking_a_mention_makes_a_chip_and_keeps_the_question(proj):
    a = proj["after_enter"]
    assert a["menu"]["hidden"] and a["menu"]["value"] == "compare " and a["mentions"] == ["kb2"]
    assert a["chips"][0]["text"] == "kb2" and a["chips"][0]["display"] == "flex" and a["chips"][0]["h"] >= 18
    esc = proj["after_escape"]
    assert esc["hidden"] and esc["value"] == "compare #" and esc["focus"]
    assert proj["menu_my"]["opts"] == [EVIL_KB]
    c = proj["after_click"]
    assert [x["text"] for x in c["chips"]] == ["kb2", EVIL_KB] and c["markup"] == 0 and c["focus"] == "msg-input"
    assert c["chips"][0]["right"] <= c["chips"][1]["x"]


def test_mentions_restrict_this_question_only_also_under_documents_only(proj):
    s = proj["sent_mention"][0]
    assert s["rag_instances"] == ["kb2", EVIL_KB] and "rag_instance" not in s
    assert s["kb_select"] == ["default"] and s["documents_only"] is True
    a = proj["after_send"]
    assert a["chips"] == [] and a["sel"] == ["default"]
    assert a["bubble_tags"] == ["# kb2", f"# {EVIL_KB}"]
    assert proj["after_remove"] == []


def test_at_phone_width_the_switcher_menu_and_chips_fit(proj):
    b = proj["m_bar"]
    assert _shown(b["filter"], 150, 28) and _shown(b["new"], 24, 24)
    assert b["filter"]["right"] <= b["sidebar"]["right"] <= 375 and b["new"]["right"] <= b["sidebar"]["right"]
    assert proj["m_overflow"] == [0, 0] and proj["m_overflow_after"] == 0
    assert _shown(proj["m_edit"], 28, 28) and proj["m_rows"] == ["G:Today", "s_a"]
    assert proj["m_new"]["project"] == P1 and proj["m_sidebar_after"] is False
    m = proj["m_menu"]
    assert not m["hidden"] and m["x"] >= 0 and m["right"] <= 375 and m["bottom"] <= m["taTop"]
    assert m["optH"] >= 24 and proj["m_hit"] is True
    assert [c["text"] for c in proj["m_chips"]] == ["kb1"]
    assert proj["m_chips"][0]["right"] <= 375 and proj["m_scope"]["right"] <= 375
