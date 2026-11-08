# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings → Storage and the New Instance form, in a real browser with canned API
answers: Redis and MongoDB connections, one without vector search."""
import json
import pathlib
import subprocess

import pytest

from test_science_lanes_browser import _no_browser, _playwright_python

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PROBE = pathlib.Path(__file__).with_name("storage_ui_probe.py")


@pytest.fixture(scope="module")
def probe():
    py = _playwright_python()
    if py is None:
        _no_browser()
    r = subprocess.run([py, str(_PROBE), str(_ROOT / "visualweaver" / "index.html")],
                       capture_output=True, text=True, timeout=240)
    out = json.loads(r.stdout.strip().splitlines()[-1])
    if out.get("skip"):
        pytest.skip(out["skip"])
    assert out.get("ok"), out
    return out


def test_the_tab_is_storage_and_lists_every_kind_of_connection(probe):
    assert probe["tab"].endswith("Storage")
    cards = probe["cards"]
    assert len(cards) == 3, cards                              # the built-in Redis is not a card here
    assert cards[0].startswith("team-redis Redis ✓ Vector search 10.0.0.5:6380 · db2 · TLS · 0 instances")
    assert cards[1].startswith("atlas MongoDB ✓ Vector search cluster0.ab.mongodb.net · user alice · database kb · 1 instance")
    assert "⚠ No vector search" in cards[2] and cards[2].startswith("plain MongoDB")
    assert not any("__VISUALWEAVER_SECRET_KEPT__" in c for c in cards)
    assert probe["errors"] == [], probe["errors"]


def test_the_add_form_shows_only_the_chosen_kinds_fields(probe):
    assert probe["fields_redis"] == ["block", "none"]
    assert probe["fields_mongo"] == ["none", "block"]
    assert probe["test_post"] == {"name": "plain2", "type": "mongodb", "uri": "mongodb://plain:27017",
                                  "username": "bob", "password": "pw", "database": "visualweaver"}
    assert "no vector search" in probe["test_verdict"] and "cannot hold RAG instances" in probe["test_verdict"]


def test_new_instance_offers_storage_and_disables_what_cannot_hold_one(probe):
    assert probe["label"].lower() == "storage"
    opts = {v: (t, d) for v, t, d in probe["options"]}
    assert list(opts) == ["default", "team-redis", "atlas", "plain"]   # default listed once
    assert opts["default"] == ("Built-in Redis", False)
    assert opts["atlas"] == ("atlas — MongoDB · cluster0.ab.mongodb.net", False)
    assert opts["plain"][1] is True and "(no vector search)" in opts["plain"][0]
    assert probe["create_post"] == {"name": "kb2", "color": "#7c6ff7", "endpoint": "atlas"}
    assert probe["instance_badge"] == ["🗄 atlas · MongoDB"]


def test_a_server_username_and_password_build_the_string(probe):
    assert probe["preview_atlas"] == "Connects to mongodb+srv://cluster0.clmrcbn.mongodb.net/"
    assert probe["preview_plain"] == "Connects to mongodb://10.0.0.9:27017/"
    # the server goes to the API as typed; the server side builds the same string
    assert probe["server_post"] == {"name": "plain2", "type": "mongodb", "uri": "cluster0.clmrcbn.mongodb.net",
                                    "username": "scyris_db_user", "password": "pw", "database": "visualweaver"}
    assert probe["preview_pasted"] == "Connects to mongodb+srv://c0.ab.mongodb.net/?retryWrites=true"


def test_an_atlas_string_is_split_into_its_fields(probe):
    uri, user, pw, focus, hint = probe["atlas_placeholder"]
    assert uri == "mongodb+srv://cluster0.clmrcbn.mongodb.net/?compressors=zlib"
    assert (user, pw, focus) == ("scyris_db_user", "", "new-ep-mpass")
    assert "scyris_db_user" in hint and "<db_password>" in hint
    assert probe["atlas_password"] == ["mongodb+srv://c0.ab.mongodb.net/?retryWrites=true", "bob", "p@ss"]
    assert probe["down_card"].startswith("atlas MongoDB ✗ Can't connect")


def test_a_refused_creation_is_shown_to_the_user(probe):
    assert "has no vector search" in probe["refusal_toast"]


def test_an_older_server_without_the_storage_route_renders_the_tab_as_before(probe):
    assert probe["legacy"] == {"storage": None, "title": "Built-in Redis", "banner": "Redis",
                               "form": "block", "mem": "block", "app": "none",
                               "mongoNotice": "none", "builtinCards": 0}
