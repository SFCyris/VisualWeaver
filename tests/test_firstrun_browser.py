# SPDX-License-Identifier: AGPL-3.0-or-later
"""The first-run checklist and the chat status strip, measured in Chromium, Firefox and
WebKit (firstrun_probe.py: canned API answers, a fake WebSocket, no server).

Before 1.16.0 the welcome screen said nothing about whether the storage, a model and any
documents were in place, and the chat view said nothing about the storage, the provider
or the server connection: a dropped socket showed only as a "WebSocket error" toast that
repeated on every 2-s reconnect and never expired.
"""
import json
import pathlib
import subprocess

import pytest

from test_science_lanes_browser import _no_browser, _playwright_python

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PROBE = pathlib.Path(__file__).with_name("firstrun_probe.py")
_RUNS: dict = {}


@pytest.fixture(scope="module", params=["chromium", "firefox", "webkit"])
def fr(request):
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


def _texts(strip):
    return [i["text"] for i in strip]


def _nows(text):
    return "".join(text.split())


def test_the_probe_ran_every_step_without_page_errors(fr):
    assert not fr.get("step_errors"), fr.get("step_errors")
    assert fr["errors"] == []


# ── the checklist ────────────────────────────────────────────────────────────

def test_a_fresh_install_shows_the_checklist_with_the_knowledge_step_to_do(fr):
    c = fr["fresh_checklist"]
    assert c["hidden"] is False and c["display"] == "block" and c["h"] > 60 and c["w"] > 200
    rows = c["rows"]
    assert [r["cls"].split()[-1] for r in rows] == ["fr-ok", "fr-ok", "fr-todo"]
    # innerText of the visually hidden "Done: " differs by engine (WebKit 27 drops its
    # trailing space: "Done:Storage"), so the visible text is compared without spaces and
    # the words themselves through textContent...
    assert _nows(rows[0]["text"]) == _nows("Done: Storage Built-in Redis is reachable")
    assert rows[0]["tc"] == "Done: Storage Built-in Redis is reachable"
    assert rows[1]["tc"] == "Done: Model Ollama · llama3:latest"
    assert rows[2]["tc"].startswith("To do: Knowledge Add your first documents to “default”")
    # ...and what a screen reader gets still says "Done: Storage", spaced.
    aria = fr["fresh_aria"]
    assert "Done: Storage Built-in Redis is reachable" in aria[0], aria[0]
    assert "Done: Model" in aria[1] and "To do: Knowledge" in aria[2], aria
    assert rows[2]["btn"] == "Add documents" and rows[2]["btnRight"] <= c["right"]
    assert rows[0]["icoColor"] != rows[2]["icoColor"]          # done and to-do look different too


def test_add_documents_opens_the_upload_with_the_target_chosen_and_focused(fr):
    a = fr["add_docs"]
    assert a["open"] and a["tab"] == "rag" and a["target"] == "default§default"
    assert a["focus"] == "ingest-drop" and 0 <= a["dropTop"] and a["dropBottom"] <= a["vh"]
    after = fr["after_ingest"]
    assert after["posts"] == ["POST /api/rag/default/ingest/files/stream?endpoint=default"]
    assert after["checklist"]["hidden"] is True and after["checklist"]["h"] == 0   # all done: gone


def test_the_knowledge_step_targets_the_instance_the_chat_searches(fr):
    assert "to “handbook”" in fr["handbook_row"]["text"]
    assert fr["handbook_target"] == "handbook§default"


def test_a_missing_model_has_a_one_click_fix(fr):
    rows = fr["nomodel_checklist"]["rows"]
    assert rows[1]["cls"].endswith("fr-bad") and "Ollama has no chat model" in rows[1]["text"]
    assert rows[1]["sr"] == "Needs attention: " and rows[1]["btn"] == "Set up Ollama"
    assert fr["nomodel_fix"] == {"open": True, "tab": "providers", "card": True}
    assert _texts(fr["nomodel_strip"])[1] == "✗ Model Ollama has no chat model"


def test_the_checklist_can_be_hidden_and_stays_hidden_in_this_browser(fr):
    d = fr["dismissed"]
    assert d["checklist"]["hidden"] is True and d["checklist"]["h"] == 0 and d["stored"] == "1"
    assert d["focus"] == "msg-input" and d["setupStillShown"] is True
    assert fr["dismissed_reload"]["hidden"] is True and fr["dismissed_reload"]["rows"] == []


def test_the_checklist_is_not_shown_when_everything_is_done(fr):
    assert fr["alldone_checklist"]["hidden"] is True and fr["alldone_checklist"]["h"] == 0


def test_storage_down_is_the_first_fix_and_blocks_the_knowledge_step(fr):
    rows = fr["down_checklist"]["rows"]
    assert rows[0]["cls"].endswith("fr-bad") and rows[0]["btn"] == "Open Status"
    assert rows[2]["cls"].endswith("fr-blocked") and rows[2]["btn"] is None
    assert _texts(fr["down_strip"])[0] == "✗ Storage Built-in Redis unreachable"
    assert fr["down_strip_click"] == "status"


# ── the strip ────────────────────────────────────────────────────────────────

def test_the_strip_names_storage_model_and_server_in_words(fr):
    s = fr["fresh_strip"]
    assert _texts(s) == ["✓ Storage Built-in Redis", "✓ Model Ollama · llama3:latest", "✓ Server connected"]
    assert all(i["display"] == "flex" and i["h"] >= 14 and i["w"] > 30 for i in s)
    assert s[0]["aria"] == "Storage: Built-in Redis is reachable. Open Settings, Status"
    box, area = fr["fresh_strip_box"], fr["fresh_input_box"]
    assert area["y"] <= box["y"] and box["bottom"] <= area["bottom"]     # inside the composer


def test_the_strip_follows_the_storage_poll_and_announces_the_change(fr):
    down, back = fr["live_storage_down"], fr["live_storage_back"]
    assert _texts(down["strip"])[0] == "✗ Storage Built-in Redis unreachable"
    assert down["strip"][0]["icoColor"] != fr["live_initial"][0]["icoColor"]
    assert down["said"] == "Storage cannot be reached: Built-in Redis"
    assert _texts(back["strip"])[0] == "✓ Storage Built-in Redis"
    assert back["said"] == "Storage reachable again: Built-in Redis"


def test_the_strip_follows_ollama_going_away_and_coming_back(fr):
    assert fr["live_ollama_down"]["text"] == "✗ Model Ollama is not reachable"
    assert fr["live_ollama_back"]["text"] == "✓ Model Ollama · llama3:latest"


def test_a_dropped_socket_says_reconnecting_and_a_waiting_message_says_so(fr):
    d = fr["ws_drop"]
    assert d["strip"]["text"] == "⟳ Server reconnecting…"
    assert d["said"] == "Connection to the server lost — reconnecting"
    assert d["retry"]["display"] != "none" and d["retry"]["w"] > 20
    q = fr["ws_queued"]
    assert q["strip"]["text"] == "⟳ Server reconnecting… · your message will be sent when it is back"


def test_a_server_that_stays_away_reads_unreachable_and_the_message_comes_back(fr):
    o = fr["ws_offline"]
    texts = _texts(o["strip"])
    assert texts[2] == "✗ Server unreachable" and o["said"] == "The server cannot be reached"
    assert texts[0].startswith("… Storage") and texts[1].startswith("… Model")   # not claimed while away
    assert o["input"] == "Is it back?" and o["streaming"] is False             # nothing lost, no endless Thinking…
    assert not [t for t in o["toasts"] if "WebSocket error" in t]


def test_retry_now_reconnects_and_the_strip_says_connected(fr):
    b = fr["ws_back"]
    assert _texts(b["strip"])[2] == "✓ Server connected" and b["said"] == "Connected to the server again"
    assert b["newSockets"] >= 1 and b["wsErrorToasts"] == []
    assert fr["ws_delivered"]["sent"] == ["Second"]
    assert fr["ws_delivered"]["strip"]["text"] == "✓ Server connected"


def test_a_browser_without_network_reads_offline(fr):
    assert fr["net_offline"]["text"] == "✗ Server offline — no network"


# ── layout and theme ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["1400_light", "375_light", "375_dark", "320_light"])
def test_strip_and_checklist_fit_without_squeezing_the_composer(fr, key):
    v = fr["layout"][key]
    width = int(key.split("_")[0])
    assert v["docOverflow"] == 0 and v["stripOverflow"] == 0
    s = v["strip"]
    assert s["display"] == "flex" and s["h"] >= 14 and s["right"] <= width
    assert all(i["right"] <= width and i["w"] > 20 for i in v["items"])
    assert s["y"] >= v["wrap"]["bottom"]                      # its own row, under the composer
    assert v["msg"]["w"] >= (700 if width > 1000 else 150)
    c = v["checklist"]
    assert c["hidden"] is False and c["right"] <= width and all(
        r["btnRight"] is None or r["btnRight"] <= width for r in c["rows"])
    for name, m in v["contrast"].items():
        if m:
            assert m["ratio"] >= 4.5, (name, m)


@pytest.mark.parametrize("key", ["1400_light", "375_light", "375_dark", "320_light"])
def test_the_top_of_a_tall_welcome_screen_can_be_scrolled_to(fr, key):
    """Old: centred in the chat area, a welcome screen made taller than it by the checklist
    overflowed above the top on a phone — the logo and title sat at y < 0, out of reach."""
    top = fr["layout"][key]["welcomeTop"]
    assert top["logo"] >= top["area"], top


def test_a_phone_keeps_the_marks_and_names_and_shows_a_failure_in_full(fr):
    assert _texts(fr["layout"]["375_light"]["items"]) == ["✓ Storage", "✓ Model", "✓ Server"]
    assert _texts(fr["layout"]["320_light"]["items"])[0] == "✗ Storage Built-in Redis unreachable"
