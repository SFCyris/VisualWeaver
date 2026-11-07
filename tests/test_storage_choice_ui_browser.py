# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings → Storage and Settings → About for the three installs (Redis, MongoDB, both)
and for an older server without /api/storage or /api/components — in a real browser with
canned API answers. Chromium always; Firefox and WebKit when their engines are installed."""
import json
import pathlib
import re
import subprocess

import pytest

from test_science_lanes_browser import _no_browser, _playwright_python

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PROBE = pathlib.Path(__file__).with_name("storage_choice_ui_probe.py")
_RUNS: dict = {}


@pytest.fixture(scope="module", params=["chromium", "firefox", "webkit"])
def probe(request):
    engine = request.param
    if engine not in _RUNS:
        py = _playwright_python()
        if not py:
            _no_browser()
        r = subprocess.run([py, str(_PROBE), str(_ROOT / "visualweaver" / "index.html"), engine],
                           capture_output=True, text=True, timeout=300, cwd=str(_PROBE.parent))
        _RUNS[engine] = json.loads(r.stdout.strip().splitlines()[-1])
    out = _RUNS[engine]
    if out.get("skip"):
        if engine == "chromium":
            _no_browser()
        pytest.skip(out["skip"])
    assert out.get("ok"), out
    return out


def _no_errors(out):
    for k in ("redis", "mongodb", "both", "degraded", "fail_500", "fail_net", "fail_slow"):
        assert out[k]["errors"] == [], (k, out[k]["errors"])


def test_no_page_errors_in_any_install(probe):
    _no_errors(probe)


def test_redis_install_shows_the_builtin_redis_and_its_form(probe):
    v = probe["redis"]
    s = v["snap"]
    assert v["boot_storage"] == "redis"                      # known before Settings is opened
    assert (s["banner"], s["bannerNeutral"], s["mongoNotice"], s["redisNotice"]) == ("Redis", False, False, True)
    assert s["title"] == "Built-in storage"
    assert s["builtins"] == ["Built-in Redis ✓ Vector search App data & cache 127.0.0.1:6389 · 1 instance "
                             "Redis 8.8.1 · Query Engine 8.8.1"]
    assert (s["form"], s["mem"], s["app"], s["formTitle"]) == ("block", "block", "block", "none")
    assert "default" not in s["removable"]


def test_mongodb_install_never_calls_its_builtin_redis(probe):
    v = probe["mongodb"]
    s = v["snap"]
    assert v["boot_storage"] == "mongodb"
    assert (s["banner"], s["bannerNeutral"], s["mongoNotice"]) == ("MongoDB", True, True)
    assert s["builtins"] == ["Built-in MongoDB ✓ Vector search App data & cache 127.0.0.1:27019 · 1 instance "
                             "MongoDB 9.0.4 · Search 1.78.0"]
    # the Redis host/port form and memory monitor edit a store this install does not have
    assert (s["form"], s["mem"]) == ("none", "none")
    assert s["appOptions"][0] == ["default", "Built-in MongoDB · 127.0.0.1:27019", False]
    assert s["appHint"] == "In use: Built-in MongoDB."
    assert "Built-in Redis" not in s["tabText"]
    assert "default" not in s["removable"]
    assert v["new_instance"]["options"][0] == ["default", "Built-in MongoDB", False]
    assert v["new_instance"]["value"] == "default"
    # Status, Analytics and Cache: the built-in is MongoDB everywhere
    assert v["status_card"].startswith("MongoDB ● Online Built-in MongoDB · 127.0.0.1:27019")
    assert "Redis" not in v["status_card"]
    assert "Built-in MongoDB MongoDB 127.0.0.1:27019 · database visualweaver" in v["analytics"]
    assert "Version 9.0.4 Data size 2.0 MB On disk 4.0 MB Documents 1,234" in v["analytics"]
    assert "Redis" not in v["analytics"]
    assert v["cache_hint"] == "Stored in Built-in MongoDB — change it under Settings → Storage."
    display, warn = v["cache_warn"]
    assert display == "block" and "Built-in MongoDB" in warn and "Redis" not in warn


def test_both_installed_lists_two_builtins_neither_removable(probe):
    v = probe["both"]
    s = v["snap"]
    assert (s["banner"], s["bannerNeutral"], s["mongoNotice"]) == ("Redis + MongoDB", False, True)
    assert len(s["builtins"]) == 2
    assert s["builtins"][1].startswith("Built-in MongoDB ✓ Vector search 127.0.0.1:27019 · 0 instances")
    assert "App data & cache" in s["builtins"][0] and "App data & cache" not in s["builtins"][1]
    assert s["formTitle"] == "block"                         # "Built-in Redis connection" tells the two apart
    assert not any(c.startswith("local-mongodb") for c in s["cards"])    # listed once, in Built-in
    assert "local-mongodb" not in s["removable"] and "default" not in s["removable"]
    assert ["local-mongodb", "Built-in MongoDB", False] in v["new_instance"]["options"]
    # move app data to the built-in MongoDB: the badge, New Instance and the Cache tab follow
    a = v["both_after"]
    assert a["appValue"] == "local-mongodb" and a["appHint"] == "In use: Built-in MongoDB."
    assert "App data & cache" in a["builtins"][1] and "App data & cache" not in a["builtins"][0]
    assert v["both_new_instance"] == "local-mongodb"
    assert v["both_cache_hint"].startswith("Stored in Built-in MongoDB")
    assert any("App data moved to Built-in MongoDB" in t for t in v["both_toasts"])


def test_storage_that_cannot_hold_app_data_is_listed_disabled_with_the_reason(probe):
    opts = {o[0]: (o[1], o[2]) for o in probe["redis"]["snap"]["appOptions"]}
    assert opts["default"] == ("Built-in Redis · 127.0.0.1:6389", False)
    assert opts["team-redis"] == ("team-redis — Redis · 10.0.0.5:6380", False)
    assert opts["atlas"][1] is False                         # unchecked by the server, passed the page's probe
    assert opts["plain"] == ("plain — MongoDB · 10.0.0.9:27017 (no vector search)", True)
    assert opts["down"] == ("down — Redis · 10.0.0.7:6379 (can't connect)", True)
    assert probe["redis"]["snap"]["appBtn"] == ["Move app data", True]     # nothing to move yet


def test_switching_asks_first_then_copies_and_shows_the_counts(probe):
    v = probe["redis"]
    assert v["before_confirm"]["appBtn"] == ["Move app data", False]
    is_open, title, body, buttons = v["modal"]
    assert is_open and title == "Move app data to team-redis?"
    assert "copied from Built-in Redis to team-redis" in body
    assert "answer cache starts empty on team-redis" in body
    assert "Built-in Redis keeps its copy" in body
    assert buttons == ["Cancel", "Move app data"]
    assert v["posts_before_confirm"] == []                  # nothing sent before the confirmation
    assert v["busy"] == ["⏳ Copying app data…", True, True]
    assert v["switch_post"] == {"connection": "team-redis"}
    toast = v["switch_toasts"][-1]
    assert "App data moved to team-redis" in toast
    assert "12 conversations" in toast and "the usage tally" in toast and "retrieval stats" in toast
    assert "40 watched-folder file records" in toast and "answer cache starts empty" in toast
    a = v["after_switch"]
    assert a["appValue"] == "team-redis" and a["appHint"] == "In use: team-redis."
    assert a["appBtn"] == ["Move app data", True]
    assert "App data & cache" not in a["builtins"][0]
    assert v["new_instance_after"] == "team-redis"         # New Instance follows the app-data store


def test_a_refused_switch_shows_the_servers_reason_and_changes_nothing(probe):
    v = probe["redis"]
    assert any("MongoDB 'atlas' cannot be reached: connection timed out" in t for t in v["refused_toasts"])
    assert v["app_after_refusal"] == "team-redis"
    assert v["after_refusal"]["appHint"].startswith("Moves app data from team-redis to atlas")
    assert v["after_refusal"]["appBtn"] == ["Move app data", False]      # can be retried


def test_cancel_sends_nothing_and_puts_the_selection_back(probe):
    assert probe["redis"]["cancel"] == {"posts_added": 0, "value": "team-redis", "btn_disabled": True}


def test_escape_or_a_click_outside_undoes_the_choice_like_cancel(probe):
    for way in ("escape", "outside"):
        assert probe["redis"][f"dismiss_{way}"] == {"posts_added": 0, "value": "team-redis", "btn_disabled": True}, way


def test_cards_show_server_and_search_versions(probe):
    assert probe["redis"]["cards_versions"] == [["default", "Redis 8.8.1 · Query Engine 8.8.1"],
                                                ["team-redis", "Redis 7.4.2 · RediSearch 2.10.5"]]
    assert ["local-mongodb", "MongoDB 9.0.4 · Search 1.78.0"] in probe["both"]["cards_versions"]
    assert probe["redis"]["storage_version_only"] == "Redis 8.8.1"   # /api/storage's version alone


def test_about_lists_components_and_an_unreachable_connections_error(probe):
    for kind, first in (("redis", "Built-in Redis App data & cache Redis 8.8.1 Redis Query Engine 8.8.1 ✓ Connected"),
                        ("mongodb", "Built-in MongoDB App data & cache MongoDB Community 9.0.4 MongoDB Search 1.78.0 ✓ Connected")):
        a = probe[kind]["about"]
        assert a["rows"][0] == first
        assert a["rows"][-1].startswith("atlas MongoDB — — ✗ Can't connect")
        assert a["err"] == ["Login failed: bad auth : authentication failed"]
        assert a["app"].startswith("Application VisualWeaver 1.14.0 Python 3.11.9")
        assert a["libs"] == "Libraries redis-py 6.4.0 redisvl 0.8.2 pymongo 4.15.1"
        assert a["hint"] == ""
    assert "Container runtime docker (colima)" in probe["mongodb"]["about"]["app"]
    assert len(probe["both"]["about"]["rows"]) == 4


def test_copy_gives_a_plain_text_summary_without_hosts(probe):
    text = probe["mongodb"]["copied"]
    lines = text.split("\n")
    assert lines[0] == "VisualWeaver 1.14.0 · Python 3.11.9"
    assert lines[1] == "Storage:"
    assert lines[2] == ("  Built-in MongoDB: MongoDB Community 9.0.4 · MongoDB Search 1.78.0 · connected"
                        " · app data & answer cache")
    assert lines[4] == "  atlas: MongoDB · cannot connect (Login failed: bad auth : authentication failed)"
    assert "Libraries: redis-py 6.4.0, redisvl 0.8.2, pymongo 4.15.1" in lines
    assert "Container runtime: docker (colima)" in lines
    assert lines[-1].startswith("Browser: ")
    assert not re.search(r"127\.0\.0\.1|mongodb\.net|10\.0\.0\.", text)
    assert probe["mongodb"]["copy_btn"] == "✓ Copied"


def test_the_welcome_line_leads_to_about(probe):
    text, display, width = probe["redis"]["welcome_chip"]
    assert text == "🧩 Components" and display != "none" and width > 0
    assert probe["redis"]["welcome_chip_opens"] is True


def test_an_older_server_renders_storage_as_before_and_about_from_what_it_has(probe):
    d = probe["degraded"]
    assert d["storage"] is None and d["components"] is None
    assert (d["title"], d["banner"], d["form"], d["mem"], d["app"]) == ("Built-in Redis", "Redis", "block", "block", "none")
    # an endpoint flagged built-in by the server: marked, and no ✕
    assert d["cards"][0].startswith("local-mongodb MongoDB Built-in")
    assert "local-mongodb" not in d["removable"] and "team-redis" in d["removable"]
    assert d["about"]["hint"].startswith("This server does not list its components")
    assert d["about"]["rows"] == ["Built-in Redis Redis — — ✓ Connected"]      # from /api/status/redis


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_pills_and_the_selector_card_are_painted(probe, theme):
    for kind in ("redis", "mongodb", "both"):
        m = probe[kind][f"measure_{theme}"]
        card, sel = m["appCard"], m["appSelect"]
        assert card["display"] == "block" and card["w"] > 300 and card["h"] > 80, card
        assert sel["display"] != "none" and sel["w"] > 150 and sel["h"] >= 18, sel   # WebKit draws a native 21 px select
        pills = {p["text"]: p for p in m["pills"]}
        for p in pills.values():
            assert p["display"] != "none" and p["w"] > 40 and p["h"] >= 14, p
        assert pills["✓ Vector search"]["color"] != pills["✗ Can't connect"]["color"]
        about = probe[kind][f"about_measure_{theme}"]
        assert all(x["w"] > 0 and x["h"] > 0 and x["display"] != "none" for x in about)
    # the pill colours follow the theme
    light = {p["text"]: p["color"] for p in probe["redis"]["measure_light"]["pills"]}
    dark = {p["text"]: p["color"] for p in probe["redis"]["measure_dark"]["pills"]}
    assert light["✓ Vector search"] != dark["✓ Vector search"]


def test_copied_errors_leave_out_addresses(probe):
    line = [l for l in probe["redis"]["copy_redacted"].split("\n") if l.startswith("  atlas:")][0]
    assert line == "  atlas: MongoDB · cannot connect (<host>, <host>, <host>, <host> and <host> timed out for user <user>)"


def test_a_mongodb_install_does_not_save_a_builtin_redis_section(probe):
    # No install does: the built-in storage is the installer's, shown read-only, and the
    # server keeps it on every save — a posted section once moved the app onto an empty store.
    assert probe["redis"]["saves_redis_section"] is False
    assert probe["both"]["saves_redis_section"] is False
    assert probe["mongodb"]["saves_redis_section"] is False


@pytest.mark.parametrize("mode", ["500", "net"])
def test_a_failing_storage_route_never_turns_a_mongodb_install_into_redis(probe, mode):
    f = probe["fail_" + mode]
    s = f["snap"]
    assert (s["banner"], s["bannerNeutral"], s["title"]) == ("MongoDB", True, "Built-in MongoDB")
    assert s["builtins"][0].startswith("Built-in MongoDB ✓ Vector search 127.0.0.1:27019")
    assert (s["form"], s["mem"], s["app"]) == ("none", "none", "none")
    assert "default" not in s["removable"]
    assert f["saves_redis_section"] is False
    assert f["new_instance"][0] == "Built-in MongoDB"
    assert f["status_card"].startswith("MongoDB ● Online Built-in MongoDB · v9.0.4")
    assert "Redis" not in f["status_card"]
    assert f["retried"] is True                               # the status poll asks again


def test_start_up_does_not_wait_for_the_storage_route(probe):
    f = probe["fail_slow"]
    assert f["ready_before_storage"] is True, f              # /api/storage answers at 3 s
    assert f["init_ms"] < 2800, f
    assert f["baseline_has_redis"] is False                  # no install posts a redis section
    assert f["dirty_after_storage"] is False   # the late answer is not an unsaved change


def test_an_unreachable_app_data_store_is_called_out(probe):
    display, text = probe["both"]["app_down_cache"]
    assert display == "block" and text.startswith("⚠ Built-in MongoDB cannot be reached")
    assert "⚠ App data & answer cache: Built-in MongoDB — it cannot be reached" in probe["both"]["app_down_status"]
